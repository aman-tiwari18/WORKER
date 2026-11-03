"""Chooses the next camera to check.

Rules (all must hold for a camera to start):
  * its lane (one per Camview server, plus one "hls" lane) has fewer than MAX_INFLIGHT_PER_SERVER
    checks in flight (the HLS lane always 1)
  * its DVR (host + device) has nothing in flight and nothing started in the last DVR_MIN_GAP_S
  * fewer than MAX_NEW_STREAMS_PER_MIN RTSP streams were started in the last minute
  * neither the lane nor the DVR is paused / backing off, and the whole checker is not paused
Order: round-robin across DVRs inside a lane; inside a DVR, the camera checked longest ago first
(across shifts too, so cameras missed in one shift go first in the next). A pass ends when every
camera of the lane was checked once, or, after CARRY_OVER_AFTER_S, when only lagging DVRs (refusing
sessions / unreachable in this pass) still have cameras left: those carry over into the next pass.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass, field

from .outcomes import RETRYABLE, UP
from .roster import HLS_LANE, Camera, in_scope

log = logging.getLogger("uptime.scheduler")


@dataclass
class DvrState:
    key: str
    cams: list[str] = field(default_factory=list)
    inflight: int = 0
    last_start: float = -1e18
    backoff_until: float = 0.0
    backoff_reason: str = ""
    last_up: float = -1e18
    lag_pass: int = 0  # lane pass in which the DVR last refused a session / was unreachable


@dataclass
class LaneState:
    name: str
    kind: str  # "rtsp" | "hls"
    max_inflight: int
    inflight: int = 0
    paused_until: float = 0.0
    pause_reason: str = ""
    dvrs: dict[str, DvrState] = field(default_factory=dict)
    rr: collections.deque = field(default_factory=collections.deque)
    pending: set[str] = field(default_factory=set)
    requeued: set[str] = field(default_factory=set)
    pass_no: int = 1
    pass_started: float = 0.0
    pass_checks: int = 0
    counts: collections.Counter = field(default_factory=collections.Counter)
    avg_check_s: float = 0.0
    last_pass_s: float | None = None
    carried: int = 0  # cameras carried into this pass from lagging DVRs


class Scheduler:
    def __init__(self, settings, clock=time.monotonic):
        self.s = settings
        self._clock = clock
        self._lock = threading.Lock()
        self.cams: dict[str, Camera] = {}
        self.last_checked: dict[str, float] = {}  # epoch seconds of the last check (LRU order)
        self.inflight: dict[str, tuple[str, str]] = {}  # camera key -> (lane, dvr_key) while checking
        self.lanes: dict[str, LaneState] = {}
        self._lane_rr: collections.deque = collections.deque()
        self._starts: collections.deque = collections.deque()  # RTSP stream starts, last 60 s
        self._pauses: dict[str, str] = {}  # source -> reason (api, file, memory)
        self._pause_since: float | None = None
        self.paused_seconds = 0.0
        self.blocked_exams: set[int] = set()
        self.on_event = None  # callback(scope, scope_key, event, reason, detail)

    # ------------------------------------------------------------------ roster
    def _lane_allowed(self, cam: Camera) -> bool:
        return in_scope(cam, self.s)

    @staticmethod
    def _copy_lane(prev: LaneState, lane: LaneState) -> None:
        for f in ("paused_until", "pause_reason", "pass_no", "pass_started", "pass_checks",
                  "counts", "avg_check_s", "last_pass_s", "carried"):
            setattr(lane, f, getattr(prev, f))

    def set_cameras(self, cams: dict[str, Camera], last_checked: dict[str, float] | None = None) -> None:
        """Replace the camera set (union of all live runs), keeping pass progress and backoffs."""
        now = self._clock()
        with self._lock:
            for k, t in (last_checked or {}).items():
                if t > self.last_checked.get(k, 0.0):
                    self.last_checked[k] = t
            old = self.lanes
            lanes: dict[str, LaneState] = {}
            for key, cam in cams.items():
                if not self._lane_allowed(cam):
                    continue
                lane = lanes.get(cam.lane)
                if lane is None:
                    is_hls = cam.lane == HLS_LANE
                    lane = LaneState(cam.lane, "hls" if is_hls else "rtsp",
                                     1 if is_hls else self.s.max_inflight_per_server, pass_started=now)
                    if cam.lane in old:
                        self._copy_lane(old[cam.lane], lane)
                    lanes[cam.lane] = lane
                dvr = lane.dvrs.get(cam.dvr_key)
                if dvr is None:
                    prev = old.get(cam.lane).dvrs.get(cam.dvr_key) if cam.lane in old else None
                    dvr = DvrState(cam.dvr_key)
                    if prev is not None:
                        dvr.last_start, dvr.last_up = prev.last_start, prev.last_up
                        dvr.backoff_until, dvr.backoff_reason = prev.backoff_until, prev.backoff_reason
                        dvr.lag_pass = prev.lag_pass
                    lane.dvrs[cam.dvr_key] = dvr
                dvr.cams.append(key)
            for name, lane in lanes.items():
                keys = {k for d in lane.dvrs.values() for k in d.cams}
                prev = old.get(name)
                if prev is None:
                    lane.pending = set(keys)
                else:
                    known = {k for d in prev.dvrs.values() for k in d.cams}
                    lane.pending = (prev.pending & keys) | (keys - known)
                    lane.requeued = prev.requeued & keys
                self._order(lane)
            for lane in lanes.values():
                lane.inflight = 0
                for d in lane.dvrs.values():
                    d.inflight = 0
            for lane_name, dvr_key in self.inflight.values():
                lane = lanes.get(lane_name)
                if lane is not None:
                    lane.inflight += 1
                    if dvr_key in lane.dvrs:
                        lane.dvrs[dvr_key].inflight += 1
            self.lanes = lanes
            self.cams = {k: c for k, c in cams.items() if c.lane in lanes}
            self._lane_rr = collections.deque(sorted(lanes))

    def _order(self, lane: LaneState) -> None:
        lc = self.last_checked
        for d in lane.dvrs.values():
            d.cams.sort(key=lambda k: lc.get(k, 0.0))
        lane.rr = collections.deque(sorted(lane.dvrs, key=lambda dk: lc.get(lane.dvrs[dk].cams[0], 0.0)))

    # ------------------------------------------------------------------ picking
    def next_job(self) -> tuple[str, Camera] | None:
        now = self._clock()
        with self._lock:
            if self._pauses:
                return None
            while self._starts and now - self._starts[0] > 60:
                self._starts.popleft()
            for _ in range(len(self._lane_rr)):
                name = self._lane_rr[0]
                self._lane_rr.rotate(-1)
                lane = self.lanes.get(name)
                if lane is None or lane.inflight >= lane.max_inflight or now < lane.paused_until:
                    continue
                if lane.kind == "rtsp" and len(self._starts) >= self.s.max_new_streams_per_min:
                    continue
                if not lane.pending or self._only_laggards_left(lane, now):
                    self._next_pass(lane, now)
                    if not lane.pending:
                        continue
                cam = self._pick(lane, now)
                if cam is not None:
                    return name, cam
            return None

    def _blocked(self, cam: Camera) -> bool:
        return (cam.link_type == "RTSP" and bool(self.blocked_exams)
                and all(m.run_key[0] in self.blocked_exams for m in cam.members))

    def _pick(self, lane: LaneState, now: float) -> Camera | None:
        gap = self.s.dvr_min_gap_s if lane.kind == "rtsp" else 0.0
        for _ in range(len(lane.rr)):
            d = lane.dvrs[lane.rr[0]]
            lane.rr.rotate(-1)
            if d.inflight or now < d.backoff_until or now - d.last_start < gap:
                continue
            for key in d.cams:
                if key in lane.pending and key not in self.inflight:
                    cam = self.cams.get(key)
                    if cam is None or self._blocked(cam):
                        continue
                    lane.pending.discard(key)
                    self.inflight[key] = (lane.name, d.key)
                    d.inflight += 1
                    d.last_start = now
                    lane.inflight += 1
                    if lane.kind == "rtsp":
                        self._starts.append(now)
                    return cam
        return None

    def _lagging(self, lane: LaneState, d: DvrState, now: float) -> bool:
        return now < d.backoff_until or d.lag_pass == lane.pass_no

    def _only_laggards_left(self, lane: LaneState, now: float) -> bool:
        """The pass ran CARRY_OVER_AFTER_S and only lagging DVRs still have cameras pending / in flight,
        while at least one other DVR is done and waiting for the next pass."""
        if not lane.pending or now - lane.pass_started < self.s.carry_over_after_s:
            return False
        waiting = False
        for d in lane.dvrs.values():
            busy = d.inflight or any(k in lane.pending for k in d.cams)
            if not self._lagging(lane, d, now):
                if busy:
                    return False
                waiting = True
        return waiting

    def _next_pass(self, lane: LaneState, now: float) -> None:
        carry = len(lane.pending)
        if lane.inflight and not carry:
            return  # let the last checks of this pass finish first
        lane.last_pass_s = now - lane.pass_started
        log.info("Lane %s pass %d done: %d checks in %.0f s (%s)%s", lane.name, lane.pass_no, lane.pass_checks,
                 lane.last_pass_s, dict(lane.counts),
                 f", {carry} cameras of lagging DVRs carried over" if carry else "")
        lane.carried = carry
        lane.pass_no += 1
        lane.pass_started = now
        lane.pass_checks = 0
        lane.counts = collections.Counter()
        lane.requeued = set()
        self._order(lane)
        lane.pending = {k for d in lane.dvrs.values() for k in d.cams if k not in self.inflight}

    # ------------------------------------------------------------------ results
    def release(self, key: str) -> None:
        """A picked camera that was not checked (its windows closed): undo the bookkeeping."""
        with self._lock:
            self._settle(key)

    def _settle(self, key: str):
        lane_name, dvr_key = self.inflight.pop(key, (None, None))
        lane = self.lanes.get(lane_name) if lane_name else None
        dvr = lane.dvrs.get(dvr_key) if lane is not None else None
        if lane is not None:
            lane.inflight = max(0, lane.inflight - 1)
        if dvr is not None:
            dvr.inflight = max(0, dvr.inflight - 1)
        return lane, dvr

    def done(self, key: str, result: str, reason: str, total_s: float, wall_ts: float) -> None:
        now = self._clock()
        events = []
        with self._lock:
            lane, dvr = self._settle(key)
            if lane is None:
                return
            if reason == "stopped":
                lane.pending.add(key)
                return
            self.last_checked[key] = wall_ts
            lane.avg_check_s = total_s if not lane.avg_check_s else 0.9 * lane.avg_check_s + 0.1 * total_s
            lane.counts[result] += 1
            lane.pass_checks += 1
            if result == UP and dvr is not None:
                dvr.last_up = now
            if dvr is not None and reason in ("dvr_unavailable", "dvr_capacity"):
                secs = self.s.dvr_unavailable_backoff_s if reason == "dvr_unavailable" else self.s.dvr_capacity_backoff_s
                dvr.backoff_until, dvr.backoff_reason = now + secs, reason
                dvr.lag_pass = lane.pass_no
                events.append(("dvr", dvr.key, "backoff", reason, {"seconds": secs}))
            elif reason == "token_unavailable":
                events += self._pause_lane(lane, now, self.s.token_backoff_s, reason)
            elif reason == "token_rejected":
                events += self._pause_lane(lane, now, 60.0, reason)
            elif reason == "tcs_blocked":
                events += self._pause_lane(lane, now, self.s.tcs_block_pause_s, reason)
            if reason in RETRYABLE and key not in lane.requeued and key in self.cams:
                lane.requeued.add(key)
                lane.pending.add(key)  # one more try later in this pass
        self._emit(events)

    def _pause_lane(self, lane: LaneState, now: float, secs: float, reason: str) -> list:
        until = now + secs
        if until <= lane.paused_until:
            return []
        lane.paused_until, lane.pause_reason = until, reason
        log.warning("Lane %s paused %.0f s (%s)", lane.name, secs, reason)
        return [("server" if lane.kind == "rtsp" else "hls", lane.name, "pause", reason, {"seconds": round(secs)})]

    def is_retry(self, lane_name: str, key: str) -> bool:
        """The camera already had a retryable failure earlier in this pass."""
        with self._lock:
            lane = self.lanes.get(lane_name)
            return lane is not None and key in lane.requeued

    def dvr_recent_up(self, lane_name: str, dvr_key: str, within_s: float = 600.0) -> bool:
        with self._lock:
            lane = self.lanes.get(lane_name)
            d = lane.dvrs.get(dvr_key) if lane else None
            return d is not None and self._clock() - d.last_up < within_s

    # ------------------------------------------------------------------ safety inputs
    def token_health(self, in_backoff: dict) -> None:
        """Token server /health: pause lanes whose DVR login is in backoff; resume early when it ends."""
        now = self._clock()
        events = []
        with self._lock:
            for lane in self.lanes.values():
                if lane.kind != "rtsp":
                    continue
                remaining = in_backoff.get(lane.name)
                if remaining is not None:
                    events += self._pause_lane(lane, now, float(remaining) + 5.0, "token_server_backoff")
                elif lane.pause_reason in ("token_unavailable", "token_server_backoff") and now < lane.paused_until:
                    lane.paused_until = now
                    events.append(("server", lane.name, "resume", "token_server_ok", None))
        self._emit(events)

    def pause(self, source: str, reason: str) -> None:
        with self._lock:
            if source in self._pauses:
                return
            if not self._pauses:
                self._pause_since = self._clock()
            self._pauses[source] = reason
        log.warning("Checker paused by %s: %s", source, reason)
        self._emit([("global", source, "pause", reason, None)])

    def resume(self, source: str) -> None:
        with self._lock:
            if source not in self._pauses:
                return
            self._pauses.pop(source)
            if not self._pauses and self._pause_since is not None:
                self.paused_seconds += self._clock() - self._pause_since
                self._pause_since = None
        log.info("Checker resumed (%s)", source)
        self._emit([("global", source, "resume", None, None)])

    def set_blocked_exams(self, exam_ids: set[int]) -> None:
        with self._lock:
            if exam_ids == self.blocked_exams:
                return
            added, removed = exam_ids - self.blocked_exams, self.blocked_exams - exam_ids
            self.blocked_exams = set(exam_ids)
        events = [("exam", str(e), "pause", "vision_running", None) for e in sorted(added)]
        events += [("exam", str(e), "resume", "vision_done", None) for e in sorted(removed)]
        self._emit(events)

    def _emit(self, events: list) -> None:
        if self.on_event:
            for ev in events:
                try:
                    self.on_event(*ev)
                except Exception:
                    log.exception("event callback failed")

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        now = self._clock()
        with self._lock:
            lanes = []
            for name in sorted(self.lanes):
                ln = self.lanes[name]
                total = sum(len(d.cams) for d in ln.dvrs.values())
                eta = len(ln.pending) * ln.avg_check_s / max(1, ln.max_inflight) if ln.avg_check_s else None
                lanes.append({
                    "lane": name,
                    "kind": ln.kind,
                    "cameras": total,
                    "dvrs": len(ln.dvrs),
                    "pass": ln.pass_no,
                    "pending": len(ln.pending),
                    "pass_checks": ln.pass_checks,
                    "pass_elapsed_s": round(now - ln.pass_started),
                    "last_pass_s": round(ln.last_pass_s) if ln.last_pass_s is not None else None,
                    "inflight": ln.inflight,
                    "max_inflight": ln.max_inflight,
                    "paused_for_s": round(ln.paused_until - now) if now < ln.paused_until else 0,
                    "pause_reason": ln.pause_reason if now < ln.paused_until else None,
                    "dvrs_backing_off": sum(1 for d in ln.dvrs.values() if now < d.backoff_until),
                    "avg_check_s": round(ln.avg_check_s, 2),
                    "eta_pass_s": round(eta) if eta is not None else None,
                    "counts": dict(ln.counts),
                    "carried": ln.carried,
                })
            paused_s = self.paused_seconds + (now - self._pause_since if self._pause_since is not None else 0.0)
            return {
                "paused": dict(self._pauses),
                "paused_seconds": round(paused_s),
                "cameras": len(self.cams),
                "inflight": len(self.inflight),
                "new_streams_last_min": sum(1 for t in self._starts if now - t <= 60),
                "blocked_exams": sorted(self.blocked_exams),
                "lanes": lanes,
            }
