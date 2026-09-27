"""Plan B entry point: run a camera group through DeepStream + TensorRT.

Called from master_pipeline when SCALE_PATH=B. Cameras whose analytics the shared
detector serves go through Plan B; the rest (classical-CV tamper/inversion) stay
on the existing path (split_for_pathb).

Environment (all optional):
  PATHB_MAX_SOURCES_PER_PIPELINE  64   streams held at once per model group
  PATHB_MAX_STREAMS_PER_DSS       50   RTSP: per DSS server, per worker (ramp-test up)
  PATHB_MAX_STREAMS_PER_DEVICE     6   RTSP: per exam-centre recorder
  PATHB_MAX_STREAMS_PER_HLS_ORIGIN 32  HLS: per origin, per worker
  PATHB_PERSISTENT_ANALYTICS      ""   comma list; cameras running these are held open
                                       (all cameras are held when they fit)
  PATHB_PERSISTENT_SHARE          0.5  max share of a group's slots for held cameras
  PATHB_DWELL_S                   20   rotating camera's time on a slot
  PATHB_STALL_S                   15   no frame for this long = dead stream
  PATHB_EVAL_INTERVAL_S           2    run the rules on a camera at most this often
  PATHB_KEYFRAMES_ONLY            1    decode keyframes only (~1 frame / GOP)
  PATHB_EVAL_WORKERS / _QUEUE     8 / 64
  PATHB_BREAKER_FAILS / _COOLOFF_S 5 / 300
  PATHB_NEW_STREAMMUX             1    keep each camera's resolution (see ds_pipeline)
  PATHB_MUX_WIDTH / _HEIGHT       1920 / 1080  legacy streammux only
  PATHB_GPU_ID                    0
  PATHB_PROGRESS_EVERY            30
  PATHB_DRY_RUN_DIR               ""   save annotated hits here instead of DB/SMS (tests)
  PATHB_LOOP_FILES                0    loop file:// sources (tests)
"""
import logging
import os
import tempfile
import time

from pathb.evaluator import EvaluatorPool
from pathb.manifest import load_manifest
from pathb.nvinfer_config import write_configs
from pathb.sources import Resolver, camera_key
from pathb.stream_budget import CircuitBreaker, StreamBudget
from pathb.stream_manager import StreamManager, make_cams

log = logging.getLogger(__name__)


def _i(name, default):
    return int(os.environ.get(name, default))


def _f(name, default):
    return float(os.environ.get(name, default))


def split_for_pathb(camera_group):
    """(cameras Plan B serves, cameras that stay on the existing path)."""
    import unified_detector
    served = set(unified_detector.ANALYTIC_SPEC)
    pathb, rest = [], []
    for c in camera_group:
        (pathb if c.get("script_code") in served else rest).append(c)
    return pathb, rest


def collapse(camera_group, link_type):
    """One config per camera, carrying every analytic enabled for it."""
    by_key, order = {}, []
    for c in camera_group:
        key = camera_key(c, link_type)
        entry = by_key.get(key)
        if entry is None:
            entry = dict(c)
            entry["enabled_scripts"] = []
            by_key[key] = entry
            order.append(key)
        sc = c.get("script_code")
        if sc and sc not in entry["enabled_scripts"]:
            entry["enabled_scripts"].append(sc)
    return [by_key[k] for k in order]


def choose_persistent(cfgs, link_type, capacity, persistent_analytics, share):
    """Keys of cameras to hold open. All of them when they fit in the group."""
    keys = [camera_key(c, link_type) for c in cfgs]
    if len(cfgs) <= capacity:
        return set(keys)
    limit = int(capacity * share)
    picked = [camera_key(c, link_type) for c in cfgs
              if persistent_analytics & set(c["enabled_scripts"])]
    return set(picked[:limit])


def run_pathb(camera_group, link_type, helpers=None, should_stop=lambda: False):
    from pathb.ds_pipeline import DsPipeline, start_main_loop, stop_main_loop  # needs DeepStream

    link_type = link_type.upper()
    manifest = load_manifest()
    resolver = Resolver(link_type, helpers)
    max_sources = _i("PATHB_MAX_SOURCES_PER_PIPELINE", 64)
    mux_w, mux_h = _i("PATHB_MUX_WIDTH", 1920), _i("PATHB_MUX_HEIGHT", 1080)
    gpu_id = _i("PATHB_GPU_ID", 0)
    persistent_analytics = {a.strip() for a in os.environ.get("PATHB_PERSISTENT_ANALYTICS", "").split(",") if a.strip()}
    share = _f("PATHB_PERSISTENT_SHARE", 0.5)

    groups = {}
    for cfg in collapse(camera_group, link_type):
        g = manifest.models_for(cfg["enabled_scripts"])
        if not g:
            log.warning("[PathB] %s: no enabled model for %s — skipped",
                        camera_key(cfg, link_type), cfg["enabled_scripts"])
            continue
        groups.setdefault(g, []).append(cfg)
    if not groups:
        log.info("[PathB] %s: nothing to run", link_type)
        return

    held = set()
    for cfgs in groups.values():
        held |= choose_persistent(cfgs, link_type, max_sources, persistent_analytics, share)
    cams = make_cams([c for cfgs in groups.values() for c in cfgs], link_type, manifest, resolver, held)

    if link_type == "HLS":
        budget = StreamBudget(_i("PATHB_MAX_STREAMS_PER_HLS_ORIGIN", 32), 0)
    else:
        budget = StreamBudget(_i("PATHB_MAX_STREAMS_PER_DSS", 50), _i("PATHB_MAX_STREAMS_PER_DEVICE", 6))
    breaker = CircuitBreaker(_i("PATHB_BREAKER_FAILS", 5), _f("PATHB_BREAKER_COOLOFF_S", 300))
    evaluator = EvaluatorPool(manifest, workers=_i("PATHB_EVAL_WORKERS", 8),
                              queue_size=_i("PATHB_EVAL_QUEUE", 64),
                              dry_run_dir=os.environ.get("PATHB_DRY_RUN_DIR") or None)

    holder = {}
    cfg_dir = tempfile.mkdtemp(prefix=f"pathb_{link_type.lower()}_")
    pipelines = {}
    for g in groups:
        paths = write_configs(g, manifest, batch=max_sources, out_dir=cfg_dir, gpu_id=gpu_id)
        pipelines[g] = DsPipeline(
            name=f"{link_type.lower()}-{'+'.join(g)}", group=g,
            model_configs=[(n, paths[n]) for n in g], max_sources=max_sources,
            on_frame=lambda *a, **kw: holder["m"].on_frame(*a, **kw),
            want_frame=lambda *a: holder["m"].want_frame(*a),
            on_source_error=lambda *a: holder["m"].on_source_error(*a),
            mux_width=mux_w, mux_height=mux_h,
            keyframes_only=os.environ.get("PATHB_KEYFRAMES_ONLY", "1") == "1",
            loop_files=os.environ.get("PATHB_LOOP_FILES", "0") == "1", gpu_id=gpu_id)

    manager = holder["m"] = StreamManager(
        cams, pipelines, max_sources, budget, breaker, resolver, evaluator, manifest, link_type,
        mux_width=mux_w, mux_height=mux_h, dwell_s=_f("PATHB_DWELL_S", 20),
        stall_s=_f("PATHB_STALL_S", 15), eval_interval_s=_f("PATHB_EVAL_INTERVAL_S", 2))

    start_main_loop()
    evaluator.start()
    for p in pipelines.values():
        p.start()
    log.info("[PathB %s] %d cameras (%d held open) in %d model group(s): %s | %d sources per group",
             link_type, len(cams), sum(c.persistent for c in cams), len(groups),
             {"+".join(g): len(v) for g, v in groups.items()}, max_sources)

    every = _f("PATHB_PROGRESS_EVERY", 30)
    t0 = last = time.monotonic()
    last_frames = 0
    try:
        while not should_stop():
            manager.tick()
            now = time.monotonic()
            if every > 0 and now - last >= every:
                s, e = manager.stats, evaluator.stats
                fps = (s["frames"] - last_frames) / (now - last)
                last, last_frames = now, s["frames"]
                log.info(
                    "[PathB %s] %.0fs | active %d/%d | frames %.1f/s | evaluated %d (dropped %d, errors %d, hits %d) "
                    "| starts %d failures %d resolve_failed %d budget_blocked %d | paused servers %s",
                    link_type, now - t0, manager.active_count(), len(cams), fps, e["evaluated"],
                    e["dropped"], e["errors"], e["hits"], s["starts"], s["failures"],
                    s["resolve_failed"], s["budget_blocked"], breaker.open_keys() or "-")
            time.sleep(1.0)
    finally:
        manager.shutdown()
        time.sleep(0.5)
        for p in pipelines.values():
            p.stop()
        evaluator.stop()
        stop_main_loop()
        log.info("[PathB %s] stopped", link_type)
