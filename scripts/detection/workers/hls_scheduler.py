"""
HLS capture scheduler — "Scaling Path A" for the HLS pipeline.

Mirrors the RTSP round-robin in ``pipeline/master_pipeline.py`` (``_run_round_robin`` /
``_rr_capture_once``). The legacy HLS model ran one *persistent* ``_hls_camera_thread``
per camera inside a ``ThreadPoolExecutor(max_workers=16)``; every camera past the 16th
slot was submitted but never started (each task loops for the whole shift), so those
cameras were silently never captured.

This scheduler keeps the **exact same** per-camera capture / auth / stream code
(``_hls_camera_thread`` is passed in unchanged in behaviour) and only changes how
cameras are scheduled onto a bounded pool:

* ``len(cameras) <= pool``  -> unchanged: one persistent capture thread per camera.
* ``len(cameras) >  pool``  -> a bounded pool of ``pool`` slot threads rotates through
  the **full** camera list. Each visit runs the camera's capture function for one
  ``HLS_RR_SLICE_SECONDS`` slice, then releases its HLS stream so the slot rotates to
  the next camera. Per-camera ``state`` (detection cooldown, consecutive-frame gates)
  is created once per camera and reused across visits.

Concurrent HLS connections never exceed ``pool`` in either mode, matching the old cap.

Env knobs (all optional) — the RTSP round-robin equivalents are noted for parity:
  HLS_RR_ENABLED               default 1   — set 0 to force the legacy per-camera model
  HLS_RR_POOL_SIZE             default 16  — capture slot threads              (RTSP: RR_POOL_SIZE=256)
  HLS_RR_MAX_STREAMS_PER_HOST  default 0   — 0 = unlimited; per-origin cap so a
                                             large pool fans out across HLS
                                             origins without storming one     (RTSP: MAX_STREAMS_PER_DVR=6)
  HLS_RR_SLICE_SECONDS         default 90  — seconds a slot dwells on one camera before
                                             rotating. This is the HLS "per-visit". Keep
                                             it in the tens of seconds: an HLS reconnect
                                             (m3u8 resolve + segment download + ffmpeg
                                             spin-up) costs 3-10s, so RTSP's ~0.5s
                                             2-frame burst does NOT port over.  (RTSP: RR_BURST_FRAMES=2)
  HLS_RR_STAGGER              default 0.1 — delay between starting slot / camera threads

Effective concurrency  = min(POOL_SIZE, n_cameras), further capped to
                         MAX_STREAMS_PER_HOST per distinct origin host.
Per-camera revisit gap ~= ceil(n_cameras / effective_concurrency) * SLICE_SECONDS.

RTSP-like config (big fan-out, short dwell), e.g. 200 cameras spread over many origins:
  HLS_RR_POOL_SIZE=64  HLS_RR_MAX_STREAMS_PER_HOST=6  HLS_RR_SLICE_SECONDS=20
  -> 6 concurrent streams per origin, every camera revisited ~ every 60-80s.
"""

import collections
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _int_env(key, default):
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _float_env(key, default):
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


HLS_RR_ENABLED              = os.environ.get("HLS_RR_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off", "")
HLS_RR_POOL_SIZE            = max(1, _int_env("HLS_RR_POOL_SIZE", 16))
HLS_RR_MAX_STREAMS_PER_HOST = max(0, _int_env("HLS_RR_MAX_STREAMS_PER_HOST", 0))   # 0 = unlimited
HLS_RR_SLICE_SECONDS        = max(1.0, _float_env("HLS_RR_SLICE_SECONDS", 90))
HLS_RR_STAGGER              = max(0.0, _float_env("HLS_RR_STAGGER", 0.1))
HLS_RR_PROGRESS_EVERY       = max(0.0, _float_env("HLS_RR_PROGRESS_EVERY", 30))   # 0 disables
# A visit that returns in under this many seconds never streamed (stream failed to
# open / EOF'd instantly). Park that camera for HLS_RR_FAIL_COOLDOWN seconds so the
# rotation spends its slots on cameras that actually deliver frames, instead of
# re-hitting the same dead streams every sweep. (RTSP round-robin has the same
# negative-cache idea.) Set HLS_RR_FAIL_COOLDOWN=0 to disable.
HLS_RR_UNPRODUCTIVE_UNDER_S = max(0.0, _float_env("HLS_RR_UNPRODUCTIVE_UNDER_S", 4.0))
HLS_RR_FAIL_COOLDOWN        = max(0.0, _float_env("HLS_RR_FAIL_COOLDOWN", 60))
# A capture that blocks inside cv2/FFmpeg `cap.read()` cannot be interrupted from
# Python — `_VisitStop` is only checked between reads. If a visit hasn't returned
# `HLS_RR_VISIT_GRACE` seconds past its slice deadline, the slot stops waiting and
# rotates on; the stuck thread drains on its own (it releases its origin-slot and
# `cap` when FFmpeg finally times out) so it can't wedge the pool.
HLS_RR_VISIT_GRACE         = max(1.0, _float_env("HLS_RR_VISIT_GRACE", 8.0))


def _host_of(cfg):
    url = cfg.get("cctv_url") or cfg.get("stream_url") or cfg.get("url", "") or ""
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


class _HostLimiter:
    """Per-origin concurrency cap — the HLS twin of RTSP's MAX_STREAMS_PER_DVR.

    PROCESS-GLOBAL: the poller runs every analytic of a shift as threads in ONE
    master_pipeline process, so N analytics each with their own cap would hit the
    origin with N x cap connections and the CDN collapses (segment fetches stall,
    only a handful of streams sustain). One shared limiter keyed by host means
    HLS_RR_MAX_STREAMS_PER_HOST is the *real* ceiling on connections to that
    origin, fairly shared across all analytics.

    A slot that finds a camera's origin already saturated rotates on to another
    camera instead of blocking (mirrors ``_rr_capture_once`` returning False).
    """

    def __init__(self, per_host):
        self._per_host = per_host
        self._sems = {}
        self._mux = threading.Lock()

    def _sem(self, host):
        with self._mux:
            s = self._sems.get(host)
            if s is None:
                s = threading.Semaphore(self._per_host)
                self._sems[host] = s
            return s

    def acquire(self, host):
        if self._per_host <= 0:
            return True
        return self._sem(host).acquire(blocking=False)

    def release(self, host):
        if self._per_host > 0:
            self._sem(host).release()


# One limiter for the whole process, sized by the env at import. Every
# run_hls_cameras() call shares it, so the per-origin cap holds across analytics.
_GLOBAL_HOST_LIMITER = _HostLimiter(HLS_RR_MAX_STREAMS_PER_HOST)


class _VisitStop:
    """A ``stop_event``-shaped view that also trips when this visit's time slice ends.

    ``_hls_camera_thread`` only ever calls ``.is_set()`` on its stop event as the
    read-loop guard, so handing it one of these makes a visit return (and release its
    HLS ``cap``) either when the shift ends or when the slice deadline passes — with no
    change to the capture code itself.
    """

    __slots__ = ("_real", "_deadline")

    def __init__(self, real_stop, deadline):
        self._real = real_stop
        self._deadline = deadline

    def is_set(self):
        return self._real.is_set() or time.time() >= self._deadline

    def wait(self, timeout=None):
        return self._real.wait(timeout)

    def set(self):  # no-op: only the real shift stop_event may be set
        pass


def run_hls_cameras(configs, stop_event, camera_fn, *, label="HLS",
                    extra_args=(), state_factory=None):
    """Drive HLS capture for **every** camera in ``configs``.

    camera_fn signature: ``camera_fn(cfg, stop_event, *extra_args, state=<dict>)``
    (the ``state`` kwarg is only passed in round-robin mode; legacy calls omit it so
    the worker builds its own, exactly as before).

    Blocks until ``stop_event`` is set (shift end).
    """
    configs = list(configs or [])
    if not configs:
        return

    pool = HLS_RR_POOL_SIZE
    make_state = state_factory or (lambda: {"last_save": 0})

    # -- fits in the pool (or RR disabled): legacy persistent one-thread-per-camera ----
    if not HLS_RR_ENABLED or len(configs) <= pool:
        logger.info("[%s] %d HLS cameras, persistent capture (pool=%d)",
                    label, len(configs), pool)
        with ThreadPoolExecutor(max_workers=min(pool, len(configs))) as executor:
            for cfg in configs:
                executor.submit(camera_fn, cfg, stop_event, *extra_args)
                if HLS_RR_STAGGER:
                    time.sleep(HLS_RR_STAGGER)
        stop_event.wait()
        return

    # -- more cameras than slots: rotate the full list through a bounded pool ----------
    n = len(configs)
    states = [make_state() for _ in configs]
    hosts = [_host_of(c) for c in configs]
    limiter = _GLOBAL_HOST_LIMITER   # shared across every analytic in this process
    work = collections.deque(range(n))
    work_mux = threading.Lock()
    n_hosts = len(set(hosts))
    logger.info("[%s RR] %d HLS cameras / %d origins over %d slots, %.0fs slice, "
                "%s per-origin cap",
                label, n, n_hosts, pool, HLS_RR_SLICE_SECONDS,
                HLS_RR_MAX_STREAMS_PER_HOST or "no")

    # -- progress instrumentation (proves the rotation is actually sweeping) -----------
    m = {"visits": 0, "visit_ok": 0, "unproductive": 0, "abandoned": 0,
         "live": 0, "live_peak": 0, "started": time.time()}
    visit_count = [0] * n           # per-camera visit tally
    last_visit_ts = [0.0] * n       # per-camera last-visit epoch
    cooldown_until = [0.0] * n      # per-camera negative-cache expiry (dead stream)
    m_mux = threading.Lock()

    def _progress_snapshot():
        with m_mux:
            visited = sum(1 for v in visit_count if v)
            revisited = sum(1 for v in visit_count if v > 1)
            now = time.time()
            parked = sum(1 for u in cooldown_until if u > now)
            elapsed = now - m["started"]
            return (visited, revisited, m["visits"], m["visit_ok"], m["unproductive"],
                    m["abandoned"], parked, m["live"], m["live_peak"], elapsed,
                    min(visit_count), max(visit_count))

    def reporter():
        while not stop_event.wait(HLS_RR_PROGRESS_EVERY):
            (visited, revisited, tv, ok, unp, aban, parked,
             live, peak, el, vmin, vmax) = _progress_snapshot()
            sweeps = tv / n if n else 0
            logger.info("[%s RR] progress %ds | covered %d/%d (%d revisited) | "
                        "visits=%d ok=%d dead=%d stuck=%d parked=%d | ~%.1f sweeps | "
                        "per-cam visits min/max=%d/%d | live=%d peak=%d",
                        label, int(el), visited, n, revisited, tv, ok, unp, aban, parked,
                        sweeps, vmin, vmax, live, peak)

    def _run_visit(idx, host, visit_stop, done, outcome):
        """One camera visit. Runs in its own thread so the slot can abandon the
        wait if cv2/FFmpeg wedges inside cap.read(); this thread still cleans up
        (releases the origin slot, drops `live`) whenever it finally returns.

        A camera_fn may RETURN True/False to state outright whether it got
        frames; that is recorded in `outcome` and beats the duration heuristic
        below (see the note there). Returning None keeps the old behaviour."""
        try:
            outcome["reported"] = camera_fn(configs[idx], visit_stop, *extra_args, state=states[idx])
        except Exception as exc:
            logger.error("[%s RR] camera visit failed: %s", label, exc)
        finally:
            limiter.release(host)
            with m_mux:
                m["live"] -= 1
            done.set()

    def slot_loop():
        while not stop_event.is_set():
            with work_mux:
                if not work:
                    return
                idx = work[0]
                work.rotate(-1)          # head -> tail: fair rotation, no starvation
            # Skip a camera whose stream just failed — let the rotation spend this
            # slot on one that actually delivers frames.
            if cooldown_until[idx] > time.time():
                time.sleep(0.02)
                continue
            host = hosts[idx]
            if not limiter.acquire(host):
                # origin already at its (process-global) concurrency cap — wait a
                # beat rather than hot-spin; every analytic's slots share this cap.
                time.sleep(0.25)
                continue
            with m_mux:
                m["visits"] += 1
                visit_count[idx] += 1
                last_visit_ts[idx] = time.time()
                m["live"] += 1
                m["live_peak"] = max(m["live_peak"], m["live"])
            visit_stop = _VisitStop(stop_event, time.time() + HLS_RR_SLICE_SECONDS)
            done = threading.Event()
            outcome = {}
            t0 = time.time()
            vt = threading.Thread(target=_run_visit, args=(idx, host, visit_stop, done, outcome),
                                  daemon=True, name=f"{label}-RR-visit-{idx}")
            vt.start()
            finished = done.wait(HLS_RR_SLICE_SECONDS + HLS_RR_VISIT_GRACE)
            took = time.time() - t0
            if not finished:
                # Wedged in cap.read(); leave it to drain and rotate on.
                with m_mux:
                    m["abandoned"] += 1
                if HLS_RR_FAIL_COOLDOWN:
                    cooldown_until[idx] = time.time() + HLS_RR_FAIL_COOLDOWN
            elif outcome.get("reported") is True or (
                outcome.get("reported") is None and took >= HLS_RR_UNPRODUCTIVE_UNDER_S
            ):
                # The duration heuristic below only holds for DWELL-style camera_fns,
                # where a real visit occupies the whole slice so anything short must
                # have failed. A burst-style capture (open, grab N frames, release —
                # the unified path) finishes a perfectly good visit in 2-3s and was
                # being misread as dead and parked for FAIL_COOLDOWN. Hence the
                # explicit report taking precedence.
                with m_mux:
                    m["visit_ok"] += 1
            else:
                # Stream never really streamed (failed to open, or instant EOF).
                # Negative-cache it and back off so a rotation full of dead
                # cameras can't hot-spin the pool.
                with m_mux:
                    m["unproductive"] += 1
                if HLS_RR_FAIL_COOLDOWN:
                    cooldown_until[idx] = time.time() + HLS_RR_FAIL_COOLDOWN
                if not stop_event.is_set():
                    time.sleep(0.5)

    if HLS_RR_PROGRESS_EVERY:
        threading.Thread(target=reporter, daemon=True, name=f"{label}-RR-progress").start()

    slots = [threading.Thread(target=slot_loop, daemon=True, name=f"{label}-RR-slot-{i}")
             for i in range(pool)]
    for t in slots:
        t.start()
        if HLS_RR_STAGGER:
            time.sleep(HLS_RR_STAGGER)

    while not stop_event.is_set() and any(t.is_alive() for t in slots):
        for t in slots:
            t.join(timeout=1.0)
        with work_mux:
            if not work:
                break

    (visited, revisited, tv, ok, unp, aban, parked,
     live, peak, el, vmin, vmax) = _progress_snapshot()
    logger.info("[%s RR] FINAL %ds | covered %d/%d cameras (%d revisited) | "
                "total visits=%d (ok=%d dead=%d stuck=%d) | ~%.1f sweeps | "
                "per-cam visits min/max=%d/%d | peak concurrency=%d",
                label, int(el), visited, n, revisited, tv, ok, unp, aban,
                (tv / n if n else 0), vmin, vmax, peak)
