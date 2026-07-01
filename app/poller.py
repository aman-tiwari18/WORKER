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
IS_MAIN_WORKER = WORKER_ID in ("worker-0", "main")

# Capture-once scaling: collapse all of a shift's detection analytics into ONE
# master_pipeline process per worker (shared YOLO, fan-out to every analytic)
# instead of one process per analytic. Set GROUP_DETECTION_ANALYTICS=0 to revert.
GROUP_DETECTION_ANALYTICS = os.environ.get("GROUP_DETECTION_ANALYTICS", "1") == "1"

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
        self.completed_scripts = set()
        self.group_members = {}      # group_key -> [member execution_ids] (grouped detection)
        self.group_analytics = {}    # group_key -> set(analytics) currently launched
        self.group_relaunching = set()  # group_keys mid-relaunch (membership grew)
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
            key   = f"grp-{exam}-{shift}-{link}-{sched}-{end}"
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"rep": a, "members": [], "analytics": []}
            g["members"].append(str(a["id"]))
            if code not in g["analytics"]:
                g["analytics"].append(code)
        combined = []
        for key, g in groups.items():
            if len(g["members"]) == 1:
                combined.append(g["rep"])          # nothing to merge
                continue
            rep = dict(g["rep"])                    # shallow copy — keeps exam/shift/link/scheduled_at/metadata
            rep["id"] = key
            rep["_member_ids"] = g["members"]
            rep["_analytics"]  = g["analytics"]
            combined.append(rep)
            logger.info(f"[group] {key}: {len(g['members'])} analytics → one master_pipeline ({g['analytics']})")
        return passthrough + combined

    def _status_targets(self, assignment):
        """The real backend execution_ids whose status this job represents — the
        member ids for a grouped detection job, else just the assignment id."""
        return assignment.get("_member_ids") or [str(assignment.get("id"))]

    async def _update_status_all(self, assignment, status, data=None):
        for tid in self._status_targets(assignment):
            await self.update_assignment_status(tid, status, data)

    async def _relaunch_group(self, assignment):
        """Stop a grouped detection job and relaunch it with the full (grown)
        analytics set. Kept guarded by group_relaunching so polls don't double-run."""
        key = str(assignment["id"])
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
        # run_assignment re-adds the key and clears group_relaunching once committed.
        await self.run_assignment(assignment)

    async def start(self):
        if self.is_running:
            logger.warning("Poller is already running")
            return

        logger.info("Starting poller service...")
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
                                and aid not in self.completed_scripts
                                and self._shift_window_closed(a)):
                            await self.update_assignment_status(
                                aid, "COMPLETED",
                                {"result_data": {"note": "shift window closed — sweep"}})
                            self.completed_scripts.add(aid)

                if all_data:
                    logger.info(f"Fetched {len(all_data)} assignment(s) (Pending: {len(pending_data)}, Running: {len(running_data)})")
                    # Capture-once: merge a shift's detection analytics into one job/worker.
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

        grouped = assignment.get("_analytics")
        # A relaunch (membership grew) is in flight — don't double-launch.
        if grouped and assignment_id in self.group_relaunching:
            return
        # Grouped detection: if more analytics for this (exam,shift,link) appeared
        # after the group was already queued/running, relaunch with the FULL set —
        # otherwise the late analytics never run (each later poll just "skips").
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
            logger.debug(f"Assignment {assignment_id} already completed locally — skipping to prevent loop")
            return

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

            result = await script_runner.execute_script(assignment)

            logger.info(f"✔ Assignment {assignment_id} completed")

            # Reset crash tracker on success
            self._crash_tracker.pop(assignment_id, None)

            # Remember that this completed successfully so we never restart it —
            # ONLY for vision/classification scripts (vision, vision_neet). Other
            # detectors must restart if they exit early.
            if is_vision:
                self.completed_scripts.add(assignment_id)

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
                    self.completed_scripts.add(assignment_id)
            else:
                logger.error(f"✘ Assignment {assignment_id} failed: {e}")
                if IS_MAIN_WORKER:
                    await self._update_status_all(assignment, "FAILED", {"error_log": str(e)})

                # --- Crash backoff: track failures for non-vision scripts ---
                # Vision is one-shot (completed_scripts), so it can't crash-loop;
                # only restartable detectors need quarantine protection.
                if not is_vision:
                    now = time.time()
                    t = self._crash_tracker.setdefault(
                        assignment_id, {"failures": 0, "last_fail_ts": now}
                    )
                    # Reset the failure count if the last failure was long ago.
                    if now - t.get("last_fail_ts", 0) > self._CRASH_WINDOW_S:
                        t["failures"] = 0
                    t["failures"] += 1
                    t["last_fail_ts"] = now
                    if t["failures"] >= self._MAX_CRASHES_IN_WINDOW:
                        t["quarantine_until"] = now + self._QUARANTINE_S
                        logger.error(
                            f"Assignment {assignment_id} quarantined for {self._QUARANTINE_S}s "
                            f"after {t['failures']} crashes in {self._CRASH_WINDOW_S}s window — "
                            f"prevents GPU memory thrashing from crash-restarting workers."
                        )

        finally:
            self.running_scripts.discard(assignment_id)
            self.group_members.pop(assignment_id, None)
            self.group_analytics.pop(assignment_id, None)
            self.group_relaunching.discard(assignment_id)

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
