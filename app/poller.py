import asyncio
import os
import time
import httpx
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
from app.config import MAIN_BACKEND_URL, API_KEY, POLL_INTERVAL_MS, LOOKAHEAD_MINUTES, GRACE_MINUTES
from app.logger import logger
from app.script_runner import script_runner
from app.camera_distributor import should_process_camera

WORKER_ID = os.environ.get("WORKER_ID", "worker-0")
# Only the main worker writes RUNNING/COMPLETED status back (feeds the Executions tab).
# A single-worker stack (uat/dev) has a differently named worker, so allow an env override.
IS_MAIN_WORKER = (
    WORKER_ID in ("worker-0", "main")
    or os.environ.get("IS_MAIN_WORKER", "").strip().lower() in ("1", "true", "yes")
)

# Capture-once scaling: collapse all of a shift's detection analytics into ONE
# master_pipeline process per worker (shared YOLO, fan-out to every analytic)
# instead of one process per analytic. Set GROUP_DETECTION_ANALYTICS=0 to revert.
GROUP_DETECTION_ANALYTICS = os.environ.get("GROUP_DETECTION_ANALYTICS", "1") == "1"
# Shift session (branch new_architectured_stream, docs/SHARED_STREAM_SESSION_PLAN.md): RTSP
# detection analytics of one (exam, shift, day) run as ONE session that keeps every camera's
# stream open; each analytic keeps its own window (written to a session file the running pipeline
# re-reads), analytics that appear later JOIN the session (no relaunch), and each analytic's
# status goes RUNNING / COMPLETED at its own start / end. SHIFT_SESSION=0 = window-key grouping.
SHIFT_SESSION = os.environ.get("SHIFT_SESSION", "0") == "1"
SESSION_DIR = os.environ.get("SESSION_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                         "scripts", "logs", "sessions"))


def _to_epoch(value):
    """Backend datetime (naive = IST, or ISO with zone / Z) -> epoch seconds, or None."""
    if not value:
        return None
    try:
        v = str(value)
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.timestamp()
    except Exception:
        return None


def _iso_ist(epoch):
    return datetime.fromtimestamp(epoch, IST).replace(tzinfo=None).isoformat(timespec="seconds")


# 0 = never run vision classification here (no local vLLM); see queue_assignment.
ENABLE_VISION = os.getenv("ENABLE_VISION", "1").strip().lower() in ("1", "true", "yes")

class Poller:
    def __init__(self):
        self.is_running = False
        self.backend_url = MAIN_BACKEND_URL
        self.api_key = API_KEY
        self.poll_interval_s = POLL_INTERVAL_MS / 1000.0
        self.lookahead_mins = LOOKAHEAD_MINUTES
        self.grace_mins = GRACE_MINUTES

        self.running_scripts = set()
        self.scheduled_scripts = {}  # id -> Task
        # assignment_id -> the scheduled_at we actually completed. A dict, not a
        # set: an operator re-running a script from the dashboard puts it back to
        # PENDING with a NEW start time, which is a genuine new run rather than
        # the restart loop this guard exists to stop. Keeping the completed
        # start time lets queue_assignment tell those two cases apart; as a bare
        # set it swallowed every re-run until the poller was restarted.
        self.completed_scripts = {}
        # Vision assignments already swept to COMPLETED. Kept separate from
        # completed_scripts: that one means "do not relaunch locally" and a
        # vision run joins it the moment it finishes, which is BEFORE its
        # window closes. Gating the sweep on it therefore skipped exactly the
        # assignments the sweep exists to close, leaving them RUNNING forever
        # until the poller restarted and cleared it.
        self.swept_scripts = set()
        self.group_members = {}      # group_key -> [member execution_ids] (grouped detection)
        self.group_analytics = {}    # group_key -> set(analytics) currently launched
        self.group_relaunching = set()  # group_keys mid-relaunch (membership grew)
        # Shift sessions: key -> {"members": {id: {...}}, "file": path, "status": {id: "RUNNING"|"COMPLETED"}}
        self.sessions = {}
        # lease mode: backend assignment id -> (poller key, assignment) for rebalance relaunches
        self._lease_keys = {}
        self._lease_waiting = set()     # poller keys waiting for shards (not running)
        self._lease_relaunching = set() # poller keys restarting after a rebalance (keep shards)
        self._poll_task = None

        # --- Crash backoff tracking (merged from worker poller) ---
        # Prevents OOM-killed / crash-looping workers from reloading GPU models
        # in a tight loop and thrashing GPU memory. Applies to NON-vision scripts
        # (vision is one-shot via completed_scripts, so it can't loop).
        # Format: { assignment_id: {"failures": int, "last_fail_ts": float, "quarantine_until": float} }
        self._crash_tracker: dict = {}
        self._MAX_CRASHES_IN_WINDOW = 3   # max failures within WINDOW before quarantine
        self._CRASH_WINDOW_S = 600        # 10-minute window
        self._QUARANTINE_S   = 900        # quarantine for 15 minutes

    # ── Vision-script detection ─────────────────────────────────────────────
    # Single source of truth: script_runner.VISION_CODES (incl. vision_neet).
    # Vision/classification scripts run ONCE per worker over their camera shard;
    # they must not auto-restart and are held RUNNING until the shift window ends.
    def _normalized_code(self, assignment) -> str:
        meta = assignment.get("execution_metadata", {}) or {}
        raw = meta.get("script_code") or assignment.get("script_code")
        return script_runner._normalize_script_code(raw)

    def _is_vision(self, assignment) -> bool:
        return self._normalized_code(assignment) in script_runner.VISION_CODES

    def _group_detection_assignments(self, all_data):
        """Collapse all of a shift's detection analytics into ONE job per
        (exam_id, shift_id, link_type) so each worker runs a single master_pipeline
        that captures every camera ONCE and the unified detector fans the shared
        YOLO pass out to all analytics. Vision/neet and non-pipeline assignments
        pass through unchanged. Single-analytic shifts also pass through as-is
        (nothing to merge). Gated by GROUP_DETECTION_ANALYTICS.

        IMPORTANT — scheduling flexibility: the group key includes the scheduled
        window (start + end), so ONLY analytics scheduled for the SAME time window
        are merged. Analytics the user scheduled at different times run as separate
        processes, exactly as before. Same-window members also start AND end
        together, so no analytic over-runs its window.
        """
        if not GROUP_DETECTION_ANALYTICS:
            return all_data
        passthrough = []
        groups = {}
        for a in all_data:
            code = self._normalized_code(a)
            if self._is_vision(a) or code not in script_runner.PIPELINE_CODES:
                passthrough.append(a)
                continue
            meta  = a.get("execution_metadata", {}) or {}
            exam  = meta.get("exam_id")  or a.get("exam_id")
            shift = meta.get("shift_id") or a.get("shift_id")
            link  = str(meta.get("link_type") or a.get("link_type") or "HLS").upper()
            # Window so only same-time analytics merge (preserves per-script scheduling).
            sched = a.get("scheduled_at") or meta.get("scheduled_at") or ""
            end   = (a.get("end_at") or meta.get("end_at")
                     or a.get("custom_end_time") or a.get("shift_end_time") or "")
            session = SHIFT_SESSION and link == "RTSP"
            if session:
                day = str(meta.get("assignment_date") or a.get("assignment_date") or sched)[:10]
                key = f"ses-{exam}-{shift}-{day}"
            else:
                key = f"grp-{exam}-{shift}-{link}-{sched}-{end}"
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"rep": a, "members": [], "analytics": [], "freq_by_script": {},
                                   "cls_by_script": {}, "session": session, "windows": {}}
            g["members"].append(str(a["id"]))
            if session:
                # The session must outlive the shift's later analytics (they are fetched only a
                # few minutes before they start), so it runs at least to the shift's end.
                sh_end = a.get("shift_end_time") or meta.get("shift_end_time")
                if sh_end and len(str(day)) == 10:
                    g["shift_end"] = max(g.get("shift_end") or 0, _to_epoch(f"{day}T{sh_end}") or 0)
                g["windows"][str(a["id"])] = {
                    "code": code, "start": _to_epoch(sched), "end": _to_epoch(end),
                    "freq": meta.get("custom_frequency_minutes") or a.get("custom_frequency_minutes"),
                    "cls": meta.get("camera_classifications") or a.get("camera_classifications"),
                }
            if code not in g["analytics"]:
                g["analytics"].append(code)
            # Each merged analytic keeps its OWN DB-configured frequency (the
            # UI's per-shift-script Frequency field) — a merged group used to
            # collapse onto whichever assignment happened to be first in the
            # poll batch, so every other analytic silently inherited a
            # sibling's cooldown instead of its own. Recorded per script code
            # here and consumed as COOLDOWN_TIME_<SCRIPT> by script_runner.py.
            freq = meta.get("custom_frequency_minutes") or a.get("custom_frequency_minutes")
            if freq and code not in g["freq_by_script"]:
                g["freq_by_script"][code] = freq
            # Same for the assignment's "Camera Type" selection: each merged analytic
            # runs on ITS OWN camera types (consumed as CAMERA_CLASSIFICATIONS_<SCRIPT>
            # by script_runner.py), not whichever sibling happened to be first.
            cls = meta.get("camera_classifications") or a.get("camera_classifications")
            if cls and code not in g["cls_by_script"]:
                g["cls_by_script"][code] = cls
        combined = []
        for key, g in groups.items():
            if len(g["members"]) == 1 and not g["session"]:
                combined.append(g["rep"])          # nothing to merge
                continue
            rep = dict(g["rep"])                    # shallow copy — keeps exam/shift/link/scheduled_at/metadata
            rep["id"] = key
            rep["_member_ids"] = g["members"]
            rep["_analytics"]  = g["analytics"]
            rep["_freq_by_script"] = g["freq_by_script"]
            rep["_cls_by_script"]  = g["cls_by_script"]
            if g["session"]:
                rep["_session"] = g["windows"]
                starts = [w["start"] for w in g["windows"].values() if w["start"]]
                ends = [w["end"] for w in g["windows"].values() if w["end"]]
                if starts:
                    rep["scheduled_at"] = _iso_ist(min(starts))
                if g.get("shift_end"):
                    ends.append(g["shift_end"])
                if ends:
                    rep["end_at"] = _iso_ist(max(ends))
                rep["_shift_end"] = g.get("shift_end")
            combined.append(rep)
            logger.info(f"[group] {key}: {len(g['members'])} analytics → one master_pipeline ({g['analytics']})")
        return passthrough + combined

    # ── shift sessions ────────────────────────────────────────────────────
    def _session_write(self, key, rec):
        """Rewrite the session file from rec["members"] (the pipeline re-reads it every few seconds)."""
        os.makedirs(SESSION_DIR, exist_ok=True)
        rec["file"] = os.path.join(SESSION_DIR, f"{key}.json")
        analytics = {}
        import json as _json
        for m in rec["members"].values():
            name = script_runner.ANALYTICS_ARG_OVERRIDES.get(m["code"], m["code"])
            if not name or not m.get("start") or not m.get("end"):
                continue
            cls = m.get("cls")
            if isinstance(cls, str):
                try:
                    cls = _json.loads(cls)
                except Exception:
                    cls = [cls]
            cls = [str(c).strip() for c in (cls or []) if str(c).strip()] or None
            # one entry per member window: the same analytic can run with different camera
            # types / frequencies in different windows (e.g. Crowd on Lab AND on Outside)
            a = analytics.setdefault(name, {"windows": []})
            a["windows"].append({"start": m["start"], "end": m["end"], "freq": m.get("freq"), "cls": cls})
        tmp = rec["file"] + ".tmp"
        with open(tmp, "w") as f:
            _json.dump({"session": key, "updated": time.time(), "analytics": analytics}, f, indent=1)
        os.replace(tmp, rec["file"])

    async def _session_prune(self, client):
        """Drop session windows whose assignment was deleted, cancelled or deactivated.

        15 Oct 2026: assignments of a live shift were deleted and re-created; their old windows stayed
        in rec["members"] for the life of the poller, one of them (05:24 -> next day 00:00) kept the
        intruder analytic running for 5 hours and made the poller relaunch the session after it was
        stopped. A member is dropped only on an explicit answer from the backend (404, CANCELLED /
        STOPPED, or is_active = 0); a failed or timed-out request never drops anything.
        """
        now = time.time()
        if now - getattr(self, "_last_session_prune", 0) < 60:
            return
        self._last_session_prune = now
        for key, rec in list(self.sessions.items()):
            dropped = []
            for mid in list(rec["members"].keys()):
                if not str(mid).isdigit():
                    continue
                try:
                    r = await client.get(f"{self.backend_url}/executions/{mid}",
                                         headers={"X-API-Key": self.api_key}, timeout=10)
                except Exception:
                    continue
                if r.status_code == 404:
                    dropped.append((mid, "deleted"))
                elif r.status_code == 200:
                    data = (r.json() or {}).get("data") or {}
                    status = str(data.get("status", "")).upper()
                    if status in ("CANCELLED", "CANCELED", "STOPPED"):
                        dropped.append((mid, status.lower()))
                    elif data.get("is_active") in (0, False, "0"):
                        dropped.append((mid, "inactive"))
            if dropped:
                for mid, why in dropped:
                    m = rec["members"].pop(mid, None) or {}
                    logger.warning(f"[session] {key}: dropped window of assignment {mid} ({why}) "
                                   f"{m.get('code')} start={m.get('start')} end={m.get('end')}")
                try:
                    self._session_write(key, rec)
                except Exception as e:
                    logger.error(f"[session] {key}: could not rewrite session file after pruning: {e}")

    def _session_merge(self, assignment):
        """Merge this poll's members into the session record and rewrite its file. Returns the record."""
        key = str(assignment["id"])
        rec = self.sessions.setdefault(key, {"members": {}, "status": {}, "file": None})
        rec["members"].update(assignment.get("_session") or {})
        self._session_write(key, rec)
        assignment["_session_file"] = rec["file"]
        assignment["_member_ids"] = list(rec["members"].keys())
        # the run lasts from the first window's start to the last window's end
        rec["shift_end"] = max(rec.get("shift_end") or 0, assignment.get("_shift_end") or 0)
        ends = [m["end"] for m in rec["members"].values() if m.get("end")] + ([rec["shift_end"]] if rec["shift_end"] else [])
        if ends:
            assignment["end_at"] = _iso_ist(max(ends))
        return rec

    async def _session_status_tick(self):
        """Each member of a running session: RUNNING at its own start, COMPLETED at its own end."""
        if not (SHIFT_SESSION and IS_MAIN_WORKER):
            return
        now = time.time()
        for key, rec in list(self.sessions.items()):
            running = key in self.running_scripts
            for mid, m in rec["members"].items():
                st = rec["status"].get(mid)
                if st != "COMPLETED" and m.get("end") and now >= m["end"] and (running or st == "RUNNING"):
                    await self.update_assignment_status(mid, "COMPLETED", {"result_data": {"note": "analytic window ended (shift session)"}})
                    rec["status"][mid] = "COMPLETED"
                elif st is None and running and m.get("start") and m["start"] <= now < (m.get("end") or now + 1):
                    await self.update_assignment_status(mid, "RUNNING")
                    rec["status"][mid] = "RUNNING"

    def _status_targets(self, assignment):
        """The real backend execution_ids whose status this job represents — the
        member ids for a grouped detection job, else just the assignment id."""
        return assignment.get("_member_ids") or [str(assignment.get("id"))]

    async def _update_status_all(self, assignment, status, data=None):
        if assignment.get("_session") is not None and status == "RUNNING":
            await self._session_status_tick()       # members go RUNNING at their own start
            return
        if assignment.get("_session") is not None and status == "COMPLETED":
            rec = self.sessions.get(str(assignment["id"])) or {}
            for mid in (rec.get("members") or {}):
                if (rec.get("status") or {}).get(mid) != "COMPLETED":
                    await self.update_assignment_status(mid, status, data)
                    rec.setdefault("status", {})[mid] = status
            return
        for tid in self._status_targets(assignment):
            await self.update_assignment_status(tid, status, data)

    async def _relaunch_group(self, assignment):
        """Stop a grouped detection job and relaunch it with the full (grown)
        analytics set. Kept guarded by group_relaunching so polls don't double-run."""
        key = str(assignment["id"])
        already_started = key in self.running_scripts
        try:
            task = self.scheduled_scripts.pop(key, None)
            if task:
                task.cancel()
            if key in self.running_scripts:
                await script_runner.cancel(key)
                for _ in range(40):                 # wait for the old run to release the key
                    if key not in self.running_scripts:
                        break
                    await asyncio.sleep(0.5)
            self.running_scripts.discard(key)
        except Exception as e:
            logger.warning(f"[group] relaunch cleanup for {key} failed: {e}")
        # Not started yet (only queued for its window): re-queue so it still waits for
        # its scheduled start instead of launching the moment membership changes.
        if key not in self.running_scripts and not already_started:
            self.group_relaunching.discard(key)
            self.queue_assignment(assignment)
            return
        # run_assignment re-adds the key and clears group_relaunching once committed.
        await self.run_assignment(assignment)

    async def start(self):
        if self.is_running:
            logger.warning("Poller is already running")
            return

        logger.info("Starting poller service...")
        from app.leases import lease_client
        lease_client.on_change = self._on_lease_change
        self.is_running = True

        # Poll immediately then loop
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(f"Poller started | interval: {self.poll_interval_s}s | lookahead: {self.lookahead_mins}min")

    async def stop(self):
        self.is_running = False
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None

        for handle in self.scheduled_scripts.values():
            handle.cancel()

        self.scheduled_scripts.clear()
        logger.info("Poller stopped")

    async def _poll_loop(self):
        while self.is_running:
            await self.poll()
            await asyncio.sleep(self.poll_interval_s)

    async def _enforce_cancellations(self, client):
        """Stop locally-running scripts that have been CANCELLED in the backend.

        The HTTP cancel fan-out from the main server can't reach workers in this
        environment (their FastAPI ports are firewalled / not routable from main),
        so each worker self-cancels: it polls the status of its own running
        executions and kills any that the backend now marks CANCELLED/STOPPED.
        Workers CAN always reach the backend (that's how they poll), so this path
        is reliable regardless of the cross-host network topology.
        """
        for execution_id in list(self.running_scripts):
            try:
                # A grouped detection job's key ("grp-…") is not a real backend
                # execution — check its member executions instead.
                member_ids = self.group_members.get(execution_id, [execution_id])
                cancelled = False
                for mid in member_ids:
                    r = await client.get(
                        f"{self.backend_url}/executions/{mid}",
                        headers={"X-API-Key": self.api_key},
                    )
                    if r.status_code != 200:
                        continue
                    data = r.json().get("data") or {}
                    status = str(data.get("status", "")).upper()
                    if status in ("CANCELLED", "CANCELED", "STOPPED"):
                        cancelled = True
                        break
                if cancelled:
                    logger.info(
                        f"⏹ Execution {execution_id} is {status} in backend — "
                        f"stopping local script"
                    )
                    await script_runner.cancel(execution_id)
                    self.running_scripts.discard(execution_id)
                    self.scheduled_scripts.pop(execution_id, None)
                    self.group_members.pop(execution_id, None)
            except Exception as e:
                logger.debug(f"Cancellation check for {execution_id} failed: {e}")

    async def poll(self):
        try:
            logger.debug("Polling for upcoming assignments...")

            async with httpx.AsyncClient() as client:
                # 0. Enforce cancellations on anything we're running locally.
                await self._enforce_cancellations(client)
                await self._session_prune(client)

                # 1. Fetch PENDING tasks
                pending_response = await client.get(
                    f"{self.backend_url}/executions/pending",
                    headers={"X-API-Key": self.api_key},
                    params={
                        "limit": 50,
                        "lookahead": self.lookahead_mins,
                        "grace": self.grace_mins
                    }
                )
                pending_response.raise_for_status()
                pending_data = pending_response.json().get("data", [])

                # 2. Fetch RUNNING tasks (so workers that restart mid-shift can catch up)
                running_data = []
                try:
                    running_response = await client.get(
                        f"{self.backend_url}/executions/running",
                        headers={"X-API-Key": self.api_key},
                        params={"limit": 50}
                    )
                    if running_response.status_code == 200:
                        running_data = running_response.json().get("data", [])
                except Exception as e:
                    logger.debug(f"Could not fetch running assignments: {e}")

                # Combine tasks, removing duplicates by ID (using dict for deduplication)
                seen = {}
                for item in (pending_data + running_data):
                    seen[str(item["id"])] = item
                all_data = list(seen.values())

                # Main worker sweeps vision assignments whose shift window has
                # ended → COMPLETED. Vision is kept RUNNING during its window so
                # late/restarted workers can finish their camera shard (see
                # run_assignment); this closes it once the window is over so it
                # doesn't linger as RUNNING forever.
                if IS_MAIN_WORKER:
                    for a in running_data:
                        aid = str(a["id"])
                        if (self._is_vision(a)
                                and aid not in self.swept_scripts
                                and self._shift_window_closed(a)):
                            await self.update_assignment_status(
                                aid, "COMPLETED",
                                {"result_data": {"note": "shift window closed — sweep"}})
                            self.swept_scripts.add(aid)
                            self.completed_scripts[aid] = a.get("scheduled_at")

                if all_data:
                    logger.info(f"Fetched {len(all_data)} assignment(s) (Pending: {len(pending_data)}, Running: {len(running_data)})")
                    # Capture-once: merge a shift's detection analytics into one job/worker.
                    await self._session_status_tick()
                    for assignment in self._group_detection_assignments(all_data):
                        # Filter assignments based on camera distribution
                        if should_process_camera(assignment):
                            self.queue_assignment(assignment)
                        else:
                            logger.debug(f"Assignment {assignment.get('id')} skipped: not assigned to this worker")
                else:
                    logger.debug("No upcoming assignments")

        except Exception as e:
            logger.error(f"Error polling backend: {e}")

    def queue_assignment(self, assignment):
        assignment_id = str(assignment["id"])
        already_running = str(assignment.get("status", "")).upper() == "RUNNING"

        # A rebalance restart is in flight for this assignment — don't double-launch.
        if assignment_id in self._lease_relaunching:
            return
        # Retired from the Fleet page: this worker takes no work at all.
        from app.fleet_agent import fleet_agent
        if fleet_agent.retired:
            return
        # Detection-only servers (ENABLE_VISION=0, e.g. a server added from the
        # Fleet page without a local vLLM) leave vision classification to the
        # workers that have it.
        if not ENABLE_VISION and self._is_vision(assignment):
            return

        grouped = assignment.get("_analytics")
        # A relaunch (membership grew) is in flight — don't double-launch.
        if grouped and assignment_id in self.group_relaunching:
            return
        # Grouped detection: if more analytics for this (exam,shift,link) appeared
        # after the group was already queued/running, relaunch with the FULL set —
        # otherwise the late analytics never run (each later poll just "skips").
        if assignment.get("_session") is not None:
            before = set((self.sessions.get(assignment_id) or {}).get("members", {}).keys())
            self._session_merge(assignment)
            if assignment_id in self.running_scripts or assignment_id in self.scheduled_scripts:
                added = set(assignment["_session"]) - before
                if added:
                    logger.info(f"[session] {assignment_id}: {len(added)} analytic(s) joined the running session "
                                f"({sorted({assignment['_session'][i]['code'] for i in added})}) — no restart")
                return
        if grouped and (assignment_id in self.running_scripts or assignment_id in self.scheduled_scripts):
            current   = set(grouped)
            committed = self.group_analytics.get(assignment_id, set())
            if current - committed:
                logger.info(
                    f"[group] {assignment_id} grew {sorted(committed)} → {sorted(current)} "
                    f"— relaunching with all analytics"
                )
                self.group_relaunching.add(assignment_id)
                self.group_analytics[assignment_id] = current
                asyncio.create_task(self._relaunch_group(assignment))
            return

        # If queued for future but backend says RUNNING now — cancel and run immediately
        if assignment_id in self.scheduled_scripts and already_running:
            logger.info(f"Assignment {assignment_id} is now RUNNING on backend — triggering immediate run")
            self.scheduled_scripts[assignment_id].cancel()
            self.scheduled_scripts.pop(assignment_id, None)
            asyncio.create_task(self.run_assignment(assignment))
            return

        if assignment_id in self.running_scripts or assignment_id in self.scheduled_scripts:
            logger.debug(f"Assignment {assignment_id} already queued/running — skipping")
            return

        if assignment_id in self.completed_scripts:
            completed_sched = self.completed_scripts[assignment_id]
            if completed_sched == assignment.get("scheduled_at"):
                logger.debug(f"Assignment {assignment_id} already completed locally — skipping to prevent loop")
                return
            # Re-queued for a different start time — an operator re-run. Drop the
            # guard so it launches instead of being silently ignored.
            logger.info(
                f"Assignment {assignment_id} re-queued for {assignment.get('scheduled_at')} "
                f"(last completed {completed_sched}) — clearing completed guard"
            )
            self.completed_scripts.pop(assignment_id, None)
            self.swept_scripts.discard(assignment_id)

        scheduled_at_str = assignment.get("scheduled_at")

        # RUNNING status or missing scheduled_at = run immediately
        if already_running or not scheduled_at_str:
            if not scheduled_at_str and not already_running:
                logger.warning(f"Assignment {assignment_id} has no scheduled_at time — running immediately")

            asyncio.create_task(self.run_assignment(assignment))
            return

        try:
            # Handle ISO timestamp parsing — always compare in IST
            if scheduled_at_str.endswith("Z"):
                scheduled_at_str = scheduled_at_str[:-1] + "+00:00"
            scheduled_at = datetime.fromisoformat(scheduled_at_str)
            if scheduled_at.tzinfo is not None:
                scheduled_at = scheduled_at.astimezone(IST)
            else:
                # Treat naive datetime as IST (backend always sends IST)
                scheduled_at = scheduled_at.replace(tzinfo=IST)
            now = datetime.now(IST)
            delay_s = (scheduled_at - now).total_seconds()
        except Exception as e:
            logger.error(f"Failed to parse time for assignment {assignment_id}: {e}")
            return

        if delay_s <= 0:
            logger.info(f"Assignment {assignment_id} is overdue by {abs(round(delay_s))}s — running immediately")
            # Fire and forget
            asyncio.create_task(self.run_assignment(assignment))
        else:
            logger.info(f"Assignment {assignment_id} queued — fires in {round(delay_s)}s")
            if grouped:
                self.group_analytics[assignment_id] = set(grouped)   # so a later poll doesn't see "grew []"
            task = asyncio.create_task(self._delayed_run(assignment, delay_s))
            self.scheduled_scripts[assignment_id] = task

    async def _delayed_run(self, assignment, delay_s):
        assignment_id = str(assignment["id"])
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            return

        self.scheduled_scripts.pop(assignment_id, None)
        await self.run_assignment(assignment)

    async def run_assignment(self, assignment):
        assignment_id = str(assignment["id"])

        # --- Quarantine check: prevent crash-looping GPU workers ---
        tracker = self._crash_tracker.get(assignment_id, {})
        quarantine_until = tracker.get("quarantine_until", 0)
        if time.time() < quarantine_until:
            remaining = round(quarantine_until - time.time())
            logger.warning(
                f"Assignment {assignment_id} is quarantined for {remaining}s "
                f"(crashed too many times). Skipping to avoid GPU memory thrashing."
            )
            return

        self.running_scripts.add(assignment_id)
        if assignment.get("_member_ids"):
            self.group_members[assignment_id] = assignment["_member_ids"]
        if assignment.get("_analytics"):
            self.group_analytics[assignment_id] = set(assignment["_analytics"])
        # Launch committed (key now in running_scripts) — allow future growth detection.
        self.group_relaunching.discard(assignment_id)
        is_vision = self._is_vision(assignment)

        try:
            logger.info(f"▶ Running assignment {assignment_id}: {assignment.get('task_name', 'Unknown task')}")

            # Only main worker updates global status to RUNNING
            if IS_MAIN_WORKER:
                await self._update_status_all(assignment, "RUNNING")

            # Claim the camera shards this worker will own (plan §4.2). No-op
            # unless COORDINATION_MODE=lease, so hash mode is unaffected.
            lease = await self._claim_lease(assignment, assignment_id)
            if lease and lease.get("refused"):
                # Drained from the Fleet page: start nothing new here. The other
                # workers claim these cameras; the finally block cleans up.
                logger.info(f"⏸ Assignment {assignment_id} not started: worker is {lease['refused']}")
                return
            if lease is not None and not lease.get("shards"):
                # Lease mode, no shards free yet (the other workers hold them all).
                # Do not run — without shards the pipeline would fall back to the
                # static hash split and duplicate cameras. The balance loop relaunches
                # this assignment as soon as a fair share frees up.
                self._lease_waiting.add(assignment_id)
                logger.info(f"⏳ Assignment {assignment_id}: no camera shards free yet — waiting for a rebalance")
                return
            self._lease_waiting.discard(assignment_id)

            result = await script_runner.execute_script(assignment, lease=lease)

            logger.info(f"✔ Assignment {assignment_id} completed")

            # Reset crash tracker on success
            self._crash_tracker.pop(assignment_id, None)

            # Remember that this completed successfully so we never restart it —
            # ONLY for vision/classification scripts (vision, vision_neet). Other
            # detectors must restart if they exit early.
            if is_vision:
                self.completed_scripts[assignment_id] = assignment.get("scheduled_at")

            # Only main worker updates global status to COMPLETED.
            # BUGFIX (camera coverage): a vision assignment runs ONCE per worker
            # over that worker's own camera shard. If the main worker flips the
            # whole assignment to COMPLETED the moment ITS 1/N shard finishes, the
            # assignment immediately vanishes from /executions/{pending,running},
            # so any worker that has not started yet — a restarted container, a
            # late-joining VM, or one that simply polled in a gap — can never fetch
            # it again and its share of cameras is never classified. So for vision
            # we keep the assignment RUNNING until the shift window closes, letting
            # stragglers catch up via /executions/running; a sweep (see poll())
            # marks it COMPLETED once the window ends. Non-vision detectors re-run
            # every frequency cycle, so they still complete immediately.
            if IS_MAIN_WORKER and (not is_vision or self._shift_window_closed(assignment)):
                await self._update_status_all(assignment, "COMPLETED", {"result_data": result})

        except Exception as e:
            if "__TIMEOUT_COMPLETED__" in str(e):
                logger.info(f"✔ Assignment {assignment_id} timed out after duration — marking COMPLETED")
                if IS_MAIN_WORKER:
                    await self._update_status_all(assignment, "COMPLETED", {"result_data": {"note": "Script ran for full duration"}})
                # A clean duration-timeout is a successful run.
                self._crash_tracker.pop(assignment_id, None)
                if is_vision:
                    self.completed_scripts[assignment_id] = assignment.get("scheduled_at")
            else:
                logger.error(f"✘ Assignment {assignment_id} failed: {e}")

                if is_vision:
                    # Vision is one-shot (completed_scripts) — a crash here is
                    # terminal, same as before.
                    if IS_MAIN_WORKER:
                        await self._update_status_all(assignment, "FAILED", {"error_log": str(e)})
                else:
                    # --- Crash backoff: track failures for restartable detectors ---
                    now = time.time()
                    t = self._crash_tracker.setdefault(
                        assignment_id, {"failures": 0, "last_fail_ts": now}
                    )
                    # Reset the failure count if the last failure was long ago.
                    if now - t.get("last_fail_ts", 0) > self._CRASH_WINDOW_S:
                        t["failures"] = 0
                    t["failures"] += 1
                    t["last_fail_ts"] = now
                    quarantined = t["failures"] >= self._MAX_CRASHES_IN_WINDOW
                    if quarantined:
                        t["quarantine_until"] = now + self._QUARANTINE_S
                        logger.error(
                            f"Assignment {assignment_id} quarantined for {self._QUARANTINE_S}s "
                            f"after {t['failures']} crashes in {self._CRASH_WINDOW_S}s window — "
                            f"prevents GPU memory thrashing from crash-restarting workers."
                        )

                    # A crash before the scheduled window has closed is recoverable:
                    # report RUNNING (not FAILED) so the assignment stays visible to
                    # /executions/running and gets relaunched on the next poll cycle
                    # (queue_assignment → run_assignment, gated by the quarantine
                    # check above). Only give up for real once quarantined or the
                    # window has actually ended — otherwise a mid-window crash
                    # (e.g. 1pm–3pm script dying at 1:30pm) previously got marked
                    # FAILED, which getPending/getRunning both exclude, so it was
                    # never picked up again for the rest of its window.
                    window_closed = self._shift_window_closed(assignment)
                    if IS_MAIN_WORKER:
                        if quarantined or window_closed:
                            await self._update_status_all(assignment, "FAILED", {"error_log": str(e)})
                        else:
                            logger.warning(
                                f"Assignment {assignment_id} crashed mid-window "
                                f"(failure {t['failures']}/{self._MAX_CRASHES_IN_WINDOW}) — "
                                f"will retry on next poll"
                            )
                            await self._update_status_all(assignment, "RUNNING", {"error_log": str(e)})

        finally:
            # Hand the shards back so another worker can take them straight away
            # instead of waiting for the lease to time out.
            await self._release_lease(assignment_id)
            self.running_scripts.discard(assignment_id)
            self.group_members.pop(assignment_id, None)
            self.group_analytics.pop(assignment_id, None)
            self.group_relaunching.discard(assignment_id)

    async def _claim_lease(self, assignment, assignment_id):
        """Claim shard ownership for this assignment; None in hash mode."""
        from app.leases import lease_client
        if not lease_client.is_enabled():
            return None
        meta = assignment.get("execution_metadata", {}) or {}
        real_ids = self.group_members.get(assignment_id, [assignment_id])
        try:
            # A grouped job covers several assignments; lease against the first
            # real backend id, since the synthetic "grp-..." key does not exist
            # server-side.
            target = int(real_ids[0])
        except (TypeError, ValueError, IndexError):
            logger.warning(f"leases: cannot resolve a backend id for {assignment_id}; running unleased")
            return None
        self._lease_keys[target] = (assignment_id, assignment)
        try:
            return await lease_client.claim(
                target,
                exam_id=meta.get("exam_id"),
                shift_id=meta.get("shift_id"),
                script_code=self._normalized_code(assignment),
            )
        except Exception as exc:
            # Never let a lease failure block detection.
            logger.warning(f"leases: claim failed for {assignment_id}: {exc}; running unleased")
            return None

    async def _release_lease(self, assignment_id):
        from app.leases import lease_client
        if not lease_client.is_enabled():
            return
        # Restarting after a rebalance, or waiting for shards: keep what we hold
        # (and keep watching) — the next run uses exactly these shards.
        if assignment_id in self._lease_relaunching or assignment_id in self._lease_waiting:
            return
        for real_id in self.group_members.get(assignment_id, [assignment_id]):
            try:
                await lease_client.release(int(real_id))
            except (TypeError, ValueError):
                continue
            except Exception as exc:
                logger.warning(f"leases: release failed for {real_id}: {exc}")

    async def _on_lease_change(self, backend_id):
        """The balance loop changed this worker's shards for an assignment: restart
        it with the new set (or start it, if it was waiting for shards)."""
        entry = self._lease_keys.get(int(backend_id))
        if not entry:
            return
        key, assignment = entry
        from app.leases import lease_client
        held = lease_client.owned(int(backend_id))
        if key in self.running_scripts:
            logger.info(f"[lease] {key}: shards changed — restarting with {len((held or {}).get('shards') or ())} shard(s)")
            self._lease_relaunching.add(key)
            try:
                await script_runner.cancel(key)
                for _ in range(60):                  # wait for the old run's finally
                    if key not in self.running_scripts:
                        break
                    await asyncio.sleep(0.5)
            finally:
                self._lease_relaunching.discard(key)
            if not held:
                return                               # shed everything (drained): stay stopped
        elif not held or key not in self._lease_waiting:
            return
        self._lease_waiting.discard(key)
        asyncio.create_task(self.run_assignment(assignment))

    async def update_assignment_status(self, assignment_id, status, data=None):
        if data is None:
            data = {}
        try:
            payload = {"status": status}
            payload.update(data)

            async with httpx.AsyncClient() as client:
                res = await client.put(
                    f"{self.backend_url}/executions/{assignment_id}/status",
                    json=payload,
                    headers={
                        "X-API-Key": self.api_key,
                        "Content-Type": "application/json"
                    }
                )
                res.raise_for_status()
                logger.debug(f"Assignment {assignment_id} → {status}")
        except Exception as e:
            logger.error(f"Failed to update assignment {assignment_id} status: {e}")

    def _shift_window_closed(self, assignment) -> bool:
        """True once the assignment's shift window has ended (compared in IST).

        Used to decide when it is safe to mark a vision assignment globally
        COMPLETED. Before the window closes we keep it RUNNING so late/restarted
        workers can still run their own camera shard (see run_assignment / poll).
        Falls back to True (immediate completion) when no window info is present."""
        # /executions/pending and /executions/running both send the window as
        # end_at ("YYYY-MM-DDTHH:MM:SS", naive IST) — custom_end_time /
        # shift_end_time are NOT in the payload, so read end_at first or every
        # vision assignment gets swept to COMPLETED on the poll after it starts.
        meta = assignment.get("execution_metadata", {}) or {}
        end_at_str = assignment.get("end_at") or meta.get("end_at")
        if end_at_str:
            try:
                end_at = datetime.fromisoformat(str(end_at_str))
                if end_at.tzinfo is None:
                    end_at = end_at.replace(tzinfo=IST)
                return datetime.now(IST) >= end_at
            except Exception:
                pass
        end_str = assignment.get("custom_end_time") or assignment.get("shift_end_time")
        if not end_str:
            return True
        try:
            parts = [int(x) for x in str(end_str).split(":")[:3]]
            while len(parts) < 3:
                parts.append(0)
            now = datetime.now(IST)
            end_dt = now.replace(hour=parts[0], minute=parts[1],
                                 second=parts[2], microsecond=0)
            return now >= end_dt
        except Exception:
            return True

    def get_status(self):
        return {
            "isPolling": self.is_running,
            "pollIntervalMs": int(self.poll_interval_s * 1000),
            "lookaheadMins": self.lookahead_mins,
            "graceMins": self.grace_mins,
            "runningCount": len(self.running_scripts),
            "scheduledCount": len(self.scheduled_scripts),
            "runningIds": list(self.running_scripts),
            "scheduledIds": list(self.scheduled_scripts.keys()),
            "quarantinedIds": [aid for aid, t in self._crash_tracker.items()
                               if time.time() < t.get("quarantine_until", 0)],
        }

poller_service = Poller()
