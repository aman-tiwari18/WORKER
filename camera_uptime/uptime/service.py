"""Wires the pieces together: control loop (schedule, rosters, safety), worker threads, writer, API."""
from __future__ import annotations

import collections
import fcntl
import logging
import os
import signal
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from . import promote, runs
from .api import start_api
from .camview import Camview, TokenClient
from .capture import Ffmpeg, frame_rel_path, save_frame
from .checker import Checker, CheckResult
from .db import Database
from .hls import HlsClient, TokenBucket
from .outcomes import UNKNOWN, UP
from .roster import Camera, RosterGuard, Shard, in_scope, load_run_roster, merge_rosters, owns_dvr
from .scheduler import Scheduler
from .schedule import Window, load_live_windows

log = logging.getLogger("uptime.service")

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def mem_available_gb() -> float | None:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return None


class RunState:
    def __init__(self, w: Window, run_id: int):
        self.w = w
        self.run_id = run_id
        self.guard = RosterGuard()
        self.next_roster = 0.0


class Service:
    def __init__(self, settings):
        s = self.s = settings
        self.db = Database(s)  # thread-local connections: control loop + writer only
        self.tokens = TokenClient(s.token_server)
        self.camview = Camview(self.tokens, s.rtsp_stream_type, s.camview_api_timeout_s, s.stop_video)
        self.bucket = TokenBucket(s.hls_req_per_s)
        self.hls = HlsClient(self.bucket, s.hls_portal_timeout_s)
        self.ffmpeg = Ffmpeg(width=s.frame_width)
        self.checker = Checker(s, self.camview, self.hls, self.ffmpeg, self.bucket)
        self.sched = Scheduler(s)
        self.sched.on_event = self._event
        from .writer import Writer
        self.writer = Writer(self.db, s.spool_dir)
        self.runs: dict[tuple, RunState] = {}
        self._runs_lock = threading.Lock()
        self.stop = threading.Event()
        self.workers: list[threading.Thread] = []
        # Frame save + result record run here, so a slow NFS write never holds a check thread.
        self.recorder = ThreadPoolExecutor(max_workers=s.frame_save_threads, thread_name_prefix="uptime-record")
        self._record_backlog = 0
        self._backlog_lock = threading.Lock()
        self.started_at = now_ist()
        self.recent: collections.deque = collections.deque(maxlen=30)
        self._last_checked_seed: dict[str, float] = {}
        self._token_ok: bool | None = None
        self._next = {"schedule": 0.0, "safety": 0.0, "counters": 0.0}
        self.shard = Shard(s.shard_index, s.shard_count)
        self.worker_id = s.worker_id or socket.gethostname()
        self._idle_logged = False
        self._lock_fh = None
        self.api = None

    # ------------------------------------------------------------------ lifecycle
    def _acquire_lock(self) -> bool:
        self._lock_fh = open(self.s.lock_file, "w")
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.error("Another camera-uptime instance holds %s — exiting", self.s.lock_file)
            return False
        self._lock_fh.write(str(os.getpid()))
        self._lock_fh.flush()
        return True

    def run_forever(self) -> int:
        if not self._acquire_lock():
            return 1
        if self.s.enabled:  # off: no database at all, so a server without DB access just idles
            missing = self.db.missing_tables()
            if self.s.shard_auto and not self.db.has_table(runs.INSTANCES_TABLE):
                missing.append(runs.INSTANCES_TABLE)
            if missing:
                log.error("Missing tables %s — apply camera_uptime/schema.sql first", missing)
                return 2
            n = runs.close_stale(self.db, now_ist())
            if n:
                log.info("Marked %d run(s) INTERRUPTED (service was down at their window end)", n)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())
        if self.s.enabled:
            self.writer.start()
        self.api = start_api(self, self.s.http_host, self.s.http_port)
        log.info("camera-uptime started: %s", {k: v for k, v in self.s.public().items() if k not in ("db_user",)})
        try:
            while not self.stop.is_set():
                try:
                    self._tick()
                except Exception:
                    log.exception("control loop error")
                self.stop.wait(1.0)
        finally:
            self._shutdown()
        return 0

    def _shutdown(self) -> None:
        log.info("Stopping: waiting for in-flight checks")
        self.stop.set()
        deadline = time.monotonic() + 15
        for t in self.workers:
            t.join(max(0.1, deadline - time.monotonic()))
        self.recorder.shutdown(wait=True)  # finish saving and recording the checks already done
        killed = self.ffmpeg.kill_all()
        if killed:
            log.info("Killed %d ffmpeg process(es)", killed)
        if not self.s.enabled:
            if self.api:
                self.api.shutdown()
            log.info("Stopped (was idle)")
            return
        try:
            self._update_counters()
        except Exception:
            pass
        self.writer.stop()
        if self.s.shard_auto:
            try:
                runs.leave(self.db, self.worker_id)
            except Exception:
                pass
        if self.api:
            self.api.shutdown()
        log.info("Stopped (runs left RUNNING resume on restart)")

    # ------------------------------------------------------------------ control loop
    def _tick(self) -> None:
        mono, now = time.monotonic(), now_ist()
        if not self.s.enabled:  # UPTIME_ENABLED=0 (Fleet page): stay up, check nothing, take no share
            if not self._idle_logged:
                log.info("UPTIME_ENABLED=0 — idle (no runs, no checks). Switch it on from the Fleet page.")
                self._idle_logged = True
            return
        with self._runs_lock:
            ended = [rs for rs in self.runs.values() if now >= rs.w.window_end]
        for rs in ended:
            self._close(rs, "COMPLETED")
        if ended:
            self._apply_union()
        if mono >= self._next["schedule"]:
            self._next["schedule"] = mono + self.s.schedule_refresh_s
            if self.s.shard_auto:
                self._refresh_shard()
            self._refresh_schedule(now)
        changed = False
        with self._runs_lock:
            due = [rs for rs in self.runs.values() if mono >= rs.next_roster]
        for rs in due:
            rs.next_roster = mono + self.s.roster_refresh_s
            changed |= self._refresh_roster(rs)
        if changed:
            self._apply_union()
        if mono >= self._next["safety"]:
            self._next["safety"] = mono + 30
            self._safety(now)
        if mono >= self._next["counters"]:
            self._next["counters"] = mono + 60
            self._update_counters()
        self._ensure_workers()

    def _refresh_schedule(self, now: datetime) -> None:
        try:
            wins = load_live_windows(self.db, now, self.s.start_hold_min, self.s.max_window_hours)
        except Exception as e:
            log.error("Schedule read failed (keeping current runs): %s", e)
            return
        if not self.s.include_hls:
            wins = [w for w in wins if w.link_type == "RTSP"]
        live = {w.key: w for w in wins}
        changed = False
        with self._runs_lock:
            current = dict(self.runs)
        for key, rs in current.items():
            w = live.get(key)
            if w is None:
                self._close(rs, "COMPLETED" if now >= rs.w.window_end else "STOPPED")
                changed = True
            elif w != rs.w:
                log.info("Run %s window changed: %s-%s -> %s-%s", rs.run_id, rs.w.window_start, rs.w.window_end,
                         w.window_start, w.window_end)
                runs.open_run(self.db, w, self.s.public())
                rs.w = w
        for key, w in live.items():
            if key in current:
                continue
            run_id = runs.open_run(self.db, w, self.s.public())
            self._last_checked_seed.update(runs.last_checked_epochs(self.db, [w.exam_id]))
            with self._runs_lock:
                self.runs[key] = RunState(w, run_id)
            log.info("Run %s opened: exam %s '%s' %s shift %s (%s) window %s - %s", run_id, w.exam_id, w.exam_name,
                     w.exam_date, w.shift_id, w.shift_name, w.window_start, w.window_end)
            self._event("run", str(run_id), "open", None, {"exam_id": w.exam_id, "shift_id": w.shift_id,
                                                          "exam_date": str(w.exam_date)}, run_id)
        if changed:
            self._apply_union()

    def _refresh_shard(self) -> None:
        """Auto sharding: heartbeat, then take this instance's place among the live ones. When the fleet
        changes (a server added, stopped or removed) every run reloads its roster at once."""
        try:
            runs.heartbeat(self.db, self.worker_id, socket.gethostname())
            live = runs.live_instances(self.db, self.s.instance_ttl_s)
        except Exception as e:
            log.warning("Instance heartbeat failed (keeping shard %d of %d): %s",
                        self.shard.shard_index, self.shard.shard_count, e)
            return
        if self.worker_id not in live:
            live = sorted(live + [self.worker_id])
        if not self.shard.set(live.index(self.worker_id), len(live)):
            return
        self.bucket.rate = self.s.hls_req_per_s_fleet / self.shard.shard_count
        log.info("Shard now %d of %d (%s) — reloading rosters", self.shard.shard_index, self.shard.shard_count,
                 ", ".join(live))
        self._event("shard", self.worker_id, "rebalance", None,
                    {"index": self.shard.shard_index, "count": self.shard.shard_count, "instances": live})
        with self._runs_lock:
            for rs in self.runs.values():
                rs.guard = RosterGuard()  # the jump is expected: apply the new share right away
                rs.next_roster = 0.0

    def _refresh_roster(self, rs: RunState) -> bool:
        cams = load_run_roster(self.db, rs.w)
        if cams is None:
            return False
        cams = {k: c for k, c in cams.items() if in_scope(c, self.s, self.shard)}  # run covers what is checked
        if not rs.guard.offer(cams):
            return False
        try:
            seeded, removed = runs.seed(self.db, rs.run_id, rs.w, cams, owns=lambda k: owns_dvr(self.shard, k))
        except Exception as e:
            log.error("Seeding run %s failed: %s", rs.run_id, e)
            return True
        log.info("Run %s roster: %d cameras (%d no longer scheduled)", rs.run_id, seeded, removed)
        return True

    def _apply_union(self) -> None:
        with self._runs_lock:
            rosters = {k: rs.guard.applied for k, rs in self.runs.items() if rs.guard.applied}
        merged = merge_rosters(rosters)
        seed, self._last_checked_seed = self._last_checked_seed, {}
        self.sched.set_cameras(merged, seed)

    def _close(self, rs: RunState, status: str) -> None:
        with self._runs_lock:
            if self.runs.get(rs.w.key) is not rs:
                return
            del self.runs[rs.w.key]
        try:
            runs.close_run(self.db, rs.run_id, status, self.sched.paused_seconds)
        except Exception as e:
            log.error("Closing run %s failed: %s", rs.run_id, e)
        log.info("Run %s %s (exam %s %s shift %s)", rs.run_id, status, rs.w.exam_id, rs.w.exam_date, rs.w.shift_id)
        self._event("run", str(rs.run_id), "close", status, None, rs.run_id)

    def _safety(self, now: datetime) -> None:
        health = self.tokens.health()
        self._token_ok = health is not None
        if health is not None:
            self.sched.token_health(health.get("in_backoff") or {})
        if self.s.pause_file and os.path.exists(self.s.pause_file):
            self.sched.pause("file", f"{self.s.pause_file} exists")
        else:
            self.sched.resume("file")
        avail = mem_available_gb()
        if avail is not None and avail < self.s.min_mem_available_gb:
            self.sched.pause("memory", f"MemAvailable {avail:.1f} GB < {self.s.min_mem_available_gb} GB")
        else:
            self.sched.resume("memory")
        if self.s.pause_during_vision:
            try:
                self.sched.set_blocked_exams(runs.vision_running_exams(self.db, now))
            except Exception as e:
                log.warning("Vision interlock check failed: %s", e)

    def _update_counters(self) -> None:
        with self._runs_lock:
            ids = [rs.run_id for rs in self.runs.values()]
        for run_id in ids:
            try:
                runs.update_counters(self.db, run_id, self.sched.paused_seconds)
            except Exception as e:
                log.warning("Counter update for run %s failed: %s", run_id, e)

    # ------------------------------------------------------------------ workers
    def _ensure_workers(self) -> None:
        lanes = self.sched.status()["lanes"]
        want = min(64, sum(ln["max_inflight"] for ln in lanes))
        while len(self.workers) < want:
            t = threading.Thread(target=self._worker, name=f"uptime-worker-{len(self.workers) + 1}", daemon=True)
            self.workers.append(t)
            t.start()

    def _open_runs_for(self, cam: Camera, t: datetime) -> list[RunState]:
        with self._runs_lock:
            out = []
            for m in cam.members:
                rs = self.runs.get(m.run_key)
                if rs is not None and rs.w.contains(t):
                    out.append(rs)
            return out

    def _worker(self) -> None:
        while not self.stop.is_set():
            job = self.sched.next_job()
            if job is None:
                self.stop.wait(0.5)
                continue
            lane, cam = job
            started = now_ist()
            targets = self._open_runs_for(cam, started)
            if not targets:
                self.sched.release(cam.key)
                continue
            try:
                res = self.checker.check(cam, self.sched.dvr_recent_up(lane, cam.dvr_key), self.stop,
                                         repeat=self.sched.is_retry(lane, cam.key))
            except Exception as e:
                log.exception("check of %s crashed", cam.key)
                res = CheckResult(result=UNKNOWN, reason="internal_error", stage="internal", note=str(e))
            self.sched.done(cam.key, res.result, res.reason, res.total_ms / 1000.0, started.timestamp())
            if res.reason == "stopped":
                continue
            with self._backlog_lock:
                self._record_backlog += 1
            self.recorder.submit(self._record_safe, cam, targets, started, res)

    def _record_safe(self, cam: Camera, targets: list[RunState], started: datetime, res: CheckResult) -> None:
        try:
            self._record(cam, targets, started, res)
        except Exception:
            log.exception("recording the check of %s failed", cam.key)
        finally:
            with self._backlog_lock:
                self._record_backlog -= 1

    def _promote(self, rs: RunState, camera_row_id: int, cam: Camera, jpeg: bytes) -> None:
        rel = promote.frame_name(rs.w.exam_id, rs.w.exam_date, rs.w.shift_id, cam.key)
        if not save_frame(self.s.classification_frames_root, rel, jpeg):
            return
        self.writer.submit("promote", {"camera_row_id": camera_row_id, "shift_id": rs.w.shift_id,
                                       "exam_date": rs.w.exam_date,
                                       "frame_path": f"{self.s.classification_frames_root.rstrip('/')}/{rel}"})

    def _record(self, cam: Camera, targets: list[RunState], started: datetime, res: CheckResult) -> None:
        for rs in targets:
            m = next((m for m in cam.members if m.run_key == rs.w.key), cam.members[0])
            frame_path = None
            if res.result == UP and res.jpeg and self.s.save_frames:
                rel = frame_rel_path(rs.w.exam_id, rs.w.exam_date, rs.w.shift_id, cam.key)
                if save_frame(self.s.frames_root, rel, res.jpeg):
                    frame_path = rel
            if (res.result == UP and res.jpeg and self.s.promote_to_cameras and cam.link_type == "RTSP"
                    and m.camera_row_id):
                self._promote(rs, m.camera_row_id, cam, res.jpeg)
            self.writer.submit("check", {
                "check_uuid": str(uuid.uuid4()), "run_id": rs.run_id, "exam_id": rs.w.exam_id,
                "shift_id": rs.w.shift_id, "exam_date": rs.w.exam_date, "link_type": cam.link_type,
                "camera_row_id": m.camera_row_id, "camera_key": cam.key, "dvr_key": cam.dvr_key,
                "server": cam.server, "tc_code": m.tc_code, "camera_name": m.camera_name,
                "centre_name": m.centre_name, "city": m.city, "state": m.state, "checked_at": started,
                "result": res.result, "reason": res.reason, "stage": res.stage, "http_status": res.http_status,
                "api_code": res.api_code, "frame_blank": res.frame_blank, "startvideo_ms": res.startvideo_ms,
                "ffmpeg_ms": res.ffmpeg_ms, "total_ms": res.total_ms, "frame_path": frame_path,
            })
        name = cam.members[0].camera_name or cam.key
        line = f"{res.result:<7} {cam.lane} {cam.device_id or ''}/{cam.camera_no or ''} {name[:40]} " \
               f"{res.reason} {res.total_ms / 1000:.1f}s" + (" blank" if res.frame_blank else "")
        (log.info if res.result == UP else log.warning if res.result == UNKNOWN else log.info)(line)
        self.recent.appendleft({"at": started.isoformat(sep=" ", timespec="seconds"), "camera": cam.key,
                                "name": name, "result": res.result, "reason": res.reason,
                                "seconds": round(res.total_ms / 1000, 1), "blank": res.frame_blank})

    # ------------------------------------------------------------------ events / API views
    def _event(self, scope, scope_key, event, reason, detail, run_id=None) -> None:
        if self.s.worker_id:  # several instances write the same events table
            detail = {**(detail or {}), "worker": self.s.worker_id}
        self.writer.submit("event", {"at": now_ist(), "scope": scope, "scope_key": scope_key, "event": event,
                                     "reason": reason, "detail": detail, "run_id": run_id})

    def health(self) -> dict:
        return {"status": "ok", "started_at": self.started_at, "token_server_ok": self._token_ok,
                "db_last_error": self.writer.last_error, "paused": self.sched.status()["paused"]}

    def status(self) -> dict:
        with self._runs_lock:
            live = [{"run_id": rs.run_id, "exam_id": rs.w.exam_id, "exam_name": rs.w.exam_name,
                     "shift_id": rs.w.shift_id, "shift_name": rs.w.shift_name, "exam_date": str(rs.w.exam_date),
                     "link_type": rs.w.link_type, "window_start": rs.w.window_start, "window_end": rs.w.window_end,
                     "cameras": len(rs.guard.applied or {})} for rs in self.runs.values()]
        return {"now": now_ist(), "started_at": self.started_at, "enabled": self.s.enabled, "worker_id": self.worker_id,
                "shard": {"index": self.shard.shard_index, "count": self.shard.shard_count, "auto": self.s.shard_auto},
                "runs": live, "scheduler": self.sched.status(),
                "stream_ports": self.camview.ports(), "writer": dict(self.writer.stats),
                "writer_queue": self.writer.q.qsize(), "record_backlog": self._record_backlog,
                "db_last_error": self.writer.last_error,
                "token_server_ok": self._token_ok, "recent": list(self.recent)[:20]}
