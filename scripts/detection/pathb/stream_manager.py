"""Decide which cameras hold a stream right now.

- Held cameras (persistent) keep their stream for the whole shift.
- Rotating cameras get a stream for `dwell_s`, then hand the slot to the next
  camera in line (fair queue) — "sampled ingest" for the long tail.
- Every start needs a free source slot in its pipeline AND a slot on its
  server/device budget; servers with an open circuit breaker are skipped.
- A stream that errors, ends, or delivers no frame for `stall_s` is dropped and
  retried later with backoff; its cached DSS URL is invalidated so the retry gets
  a fresh token.

The manager is driven by `tick()` (the runner calls it once a second), and URI
resolution (DSS StartVideo / HLS page fetch — network calls) runs in a thread pool
so a slow DSS never blocks the loop.
"""
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from pathb.boxes import to_detections
from pathb.sources import camera_key

log = logging.getLogger(__name__)

IDLE, RESOLVING, ACTIVE = "idle", "resolving", "active"


class Cam:
    __slots__ = ("key", "cfg", "group", "server", "device", "persistent", "state", "sid",
                 "since", "last_frame", "last_eval", "error", "fails", "retry_at", "future")

    def __init__(self, key, cfg, group, server, device, persistent):
        self.key, self.cfg, self.group = key, cfg, group
        self.server, self.device, self.persistent = server, device, persistent
        self.state, self.sid, self.future = IDLE, None, None
        self.since = self.last_frame = 0.0
        self.last_eval = float("-inf")                   # never evaluated
        self.error, self.fails, self.retry_at = None, 0, 0.0


class StreamManager:
    def __init__(self, cams, pipelines, max_sources, budget, breaker, resolver, evaluator,
                 manifest, link_type, mux_width=1920, mux_height=1080, dwell_s=20.0,
                 stall_s=15.0, eval_interval_s=2.0, resolve_workers=16, clock=time.monotonic,
                 executor=None, max_starts_per_tick=0, max_swaps_per_tick=0, slot_reuse_s=0.0):
        """max_starts_per_tick / max_swaps_per_tick (0 = no limit) and slot_reuse_s pace
        stream churn: on DeepStream, swapping dozens of sources at once made every new
        decoder fail to allocate ("Failed to allocate required memory") and then took
        the whole pipeline down (cudaErrorIllegalAddress). Rotation is spread over
        ticks, and a freed slot is only reused once its old decoder had time to go."""
        self.link_type = link_type
        self.pipelines = pipelines                       # group -> DsPipeline
        self.budget, self.breaker, self.resolver = budget, breaker, resolver
        self.evaluator, self.manifest = evaluator, manifest
        self.mux_width, self.mux_height = mux_width, mux_height
        self.dwell_s, self.stall_s, self.eval_interval_s = dwell_s, stall_s, eval_interval_s
        self.clock = clock
        self.cams = cams
        self.max_starts_per_tick, self.max_swaps_per_tick = max_starts_per_tick, max_swaps_per_tick
        self.slot_reuse_s = slot_reuse_s
        self.free = {g: deque((0.0, sid) for sid in range(max_sources)) for g in pipelines}  # (ready_at, sid)
        self.by_sid = {}                                  # (group, sid) -> Cam
        self.rotation = deque(c for c in cams if not c.persistent)
        self.executor = executor if executor is not None else ThreadPoolExecutor(
            max_workers=resolve_workers, thread_name_prefix="PathB-resolve")
        self._lock = threading.Lock()
        self.stats = {"frames": 0, "evaluations": 0, "starts": 0, "failures": 0,
                      "resolve_failed": 0, "budget_blocked": 0, "breaker_skipped": 0}

    # ------------------------------------------------ pipeline callbacks (GStreamer thread)
    def want_frame(self, group, sid):
        cam = self.by_sid.get((group, sid))
        return cam is not None and self.clock() - cam.last_eval >= self.eval_interval_s

    def on_frame(self, group, sid, raw, frame, space=None):
        """space: (w, h) the raw boxes are in; default the mux size."""
        cam = self.by_sid.get((group, sid))
        if cam is None:
            return
        now = self.clock()
        first = cam.last_frame < cam.since
        cam.last_frame = now
        with self._lock:
            self.stats["frames"] += 1
        if first:
            cam.fails = 0
            self.breaker.record_success(cam.server)
        if frame is None:
            return
        cam.last_eval = now
        h, w = frame.shape[:2]
        sw, sh = space or (self.mux_width, self.mux_height)
        dets = to_detections(raw, self.manifest, w, h, sw, sh)
        self.evaluator.submit({"key": cam.key, "cfg": cam.cfg, "frame": frame,
                               "detections": dets, "source_type": self.link_type})
        with self._lock:
            self.stats["evaluations"] += 1

    def on_source_error(self, group, sid, message):
        cam = self.by_sid.get((group, sid))
        if cam is not None and cam.error is None:
            cam.error = message

    # --------------------------------------------------------------------- loop
    def tick(self):
        now = self.clock()
        waiting = any(c.state == IDLE and c.retry_at <= now for c in self.rotation)
        swaps = 0
        for cam in self.cams:
            if cam.state == RESOLVING and cam.future.done():
                self._finish_resolve(cam, now)
            elif cam.state == ACTIVE:
                stalled = now - max(cam.last_frame, cam.since) > self.stall_s
                if cam.error or stalled:
                    reason = cam.error or f"no frame for {self.stall_s:.0f}s"
                    log.info("[PathB] %s: dropping stream (%s)", cam.key, reason)
                    self._stop(cam, now, failed=True)
                elif (not cam.persistent and waiting and now - cam.since >= self.dwell_s
                      and not (self.max_swaps_per_tick and swaps >= self.max_swaps_per_tick)):
                    self._stop(cam, now, failed=False)
                    swaps += 1
        starts = 0
        for cam in self._candidates(now):
            if self.max_starts_per_tick and starts >= self.max_starts_per_tick:
                break
            free = self.free[cam.group]
            if not free or free[0][0] > now:
                continue
            if self.breaker.is_open(cam.server):
                self.stats["breaker_skipped"] += 1
                continue
            if not self.budget.try_acquire(cam.server, cam.device):
                self.stats["budget_blocked"] += 1
                continue
            cam.sid = free.popleft()[1]
            starts += 1
            cam.state = RESOLVING
            cam.future = self.executor.submit(self.resolver.resolve, cam.cfg)

    def _candidates(self, now):
        held = [c for c in self.cams if c.persistent and c.state == IDLE and c.retry_at <= now]
        rotating = [c for c in self.rotation if c.state == IDLE and c.retry_at <= now]
        return held + rotating

    def _finish_resolve(self, cam, now):
        try:
            uris = cam.future.result()
        except Exception as exc:
            log.debug("[PathB] %s: resolve error %s", cam.key, exc)
            uris = []
        cam.future = None
        if not uris:
            self.stats["resolve_failed"] += 1
            self._release(cam)
            self._fail(cam, now)
            return
        cam.state, cam.since, cam.error = ACTIVE, now, None
        cam.last_frame, cam.last_eval = 0.0, float("-inf")
        self.by_sid[(cam.group, cam.sid)] = cam
        self.pipelines[cam.group].add_source(cam.sid, uris[0])
        self.stats["starts"] += 1

    def _stop(self, cam, now, failed):
        self.pipelines[cam.group].remove_source(cam.sid)
        self.by_sid.pop((cam.group, cam.sid), None)
        self._release(cam)
        if failed:
            self.resolver.invalidate(cam.cfg)
            self._fail(cam, now)
        elif not cam.persistent:
            self.rotation.remove(cam)
            self.rotation.append(cam)                    # to the back of the line

    def _release(self, cam):
        self.budget.release(cam.server, cam.device)
        self.free[cam.group].append((self.clock() + self.slot_reuse_s, cam.sid))
        cam.sid, cam.state, cam.error = None, IDLE, None

    def _fail(self, cam, now):
        cam.fails += 1
        cam.retry_at = now + min(300.0, 10.0 * 2 ** (cam.fails - 1))
        self.stats["failures"] += 1
        if self.breaker.record_failure(cam.server):
            log.warning("[PathB] server %s: too many failures, pausing it", cam.server)

    def active_count(self):
        return sum(1 for c in self.cams if c.state == ACTIVE)

    def shutdown(self):
        """Stop resolving. The pipelines themselves are stopped by DsPipeline.stop(),
        which takes every source down at once (no per-source removal races)."""
        self.executor.shutdown(wait=False, cancel_futures=True)


def make_cams(cfgs, link_type, manifest, resolver, persistent_keys):
    """cfgs: one config per camera with `enabled_scripts`. Returns [Cam]."""
    cams = []
    for cfg in cfgs:
        key = camera_key(cfg, link_type)
        group = manifest.models_for(cfg["enabled_scripts"])
        cams.append(Cam(key, cfg, group, resolver.server(cfg), resolver.device(cfg), key in persistent_keys))
    return cams
