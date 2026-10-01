"""
Unified Detector — ONE shared YOLO pass per frame, dispatched to many analytics.
====================================================================================

WHY THIS EXISTS
---------------
Today every analytic (crowd, proximity, intruder, ...) is a separate worker process
that loads the SAME model (`models/yolov10m.pt`) and runs it independently. An
inside-lab camera matches ~10 analytics, so its frame is run through YOLO ~10×.
~90% of that inference is duplicated work — the boxes are identical.

This module runs the detector **once** per frame and fans the resulting boxes out to
each enabled analytic as a cheap rule. Capacity gain ≈ M× (M = analytics/camera).

FLEXIBILITY PRESERVED
---------------------
Each packet carries `enabled_scripts` (the per-camera analytic set, from the SAME
classification config used today). An analytic runs only if it's in that set — so
"trigger crowd but not proximity on camera X" is fully supported; the model running
once does NOT mean every rule runs.

STATUS
------
Additive proof-of-concept. The existing per-script workers in `workers/` are left
intact. Crowd is implemented as the reference evaluator; other analytics follow the
same pattern (refactor each `detect_*_batch` to accept a precomputed `results`).

INTEGRATION (not wired yet — flip after evaluators exist for all needed scripts):
  In master_pipeline.run_rtsp_pipeline, instead of one queue+worker per script_code,
  create ONE queue + one `unified_worker` process, and have producers push packets
  carrying `enabled_scripts` = the camera's matched analytics.
"""

import os
_DET_GPU_ID = int(os.environ["WORKER_INDEX"]) if os.environ.get("WORKER_INDEX", "").strip().isdigit() else int("".join(c for c in os.environ.get("WORKER_ID", "main").split("-")[-1] if c.isdigit()) or "0")
import sys
import time
import uuid
import importlib
import queue as queue_module
import logging
import threading

import cv2
import torch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from ultralytics import YOLO

logger = logging.getLogger(__name__)

# Optional per-process VRAM cap for hosts whose GPU is shared with other workloads
# (e.g. worker-4). Unset = no cap, the behaviour every other worker keeps.
_VRAM_FRACTION = os.environ.get("UNIFIED_GPU_MEMORY_FRACTION", "").strip()
if _VRAM_FRACTION and torch.cuda.is_available():
    torch.cuda.set_per_process_memory_fraction(float(_VRAM_FRACTION))
    logger.info(f"[Unified] per-process VRAM cap: {float(_VRAM_FRACTION):.0%} of GPU")

# ============================================================
# Models — each analytic reads the boxes of exactly one model
# ============================================================
from detection_models import BACKENDS

CONF_THRESHOLD = 0.45          # base detector confidence; per-rule filters tighten further

# Analytics that only look at person boxes (class 0). They can read either
# yolov10m (COCO) or the fine-tuned person+monitor model, whose person is also class 0.
PERSON_ANALYTICS = {
    "crowd", "crowd_zero", "crowd_five", "crowd_biometric", "proximity", "intruder",
    "entry_exit", "server", "invigilator", "strongroom", "blind_spot",
    "crowd_five_invigilator",
}


def _env_choice(name, default, allowed):
    value = os.environ.get(name, default).strip().lower()
    if value not in allowed:
        logger.warning(f"[Unified] {name}={value!r} is not one of {allowed}; using {default!r}")
        return default
    return value


# Per-worker switches. The person default keeps the behaviour from before the
# fine-tuned models, so nothing changes on a worker until it is set. monitor_tamper
# defaults to RF-DETR, the model shipped with monitor_alerts_deploy_v2.
PERSON_MODEL = _env_choice("UNIFIED_PERSON_MODEL", "yolov10m", ("yolov10m", "finetuned"))
MONITOR_MODEL = _env_choice("UNIFIED_MONITOR_MODEL", "rfdetr", ("rfdetr", "yolo26m"))


def model_for(script):
    """The one model whose boxes this analytic reads. There is no fallback: if that
    model isn't loaded, the analytic doesn't run (and says so in the log) rather than
    quietly reading a different model's boxes."""
    if script == "phone":
        return "mobile"
    if script == "monitor_tamper":
        return "person_monitor_rfdetr" if MONITOR_MODEL == "rfdetr" else "person_monitor_yolo26m"
    if script in PERSON_ANALYTICS and PERSON_MODEL == "finetuned":
        return "person_monitor_yolo26m"
    return "yolov10m"


# Throttle for the "model not loaded, analytic skipped" error (model -> last log time).
_SKIP_LOG_EVERY_S = 60.0
_last_skip_log = {}


def _log_skipped(model, scripts):
    now = time.time()
    if now - _last_skip_log.get(model, 0) >= _SKIP_LOG_EVERY_S:
        _last_skip_log[model] = now
        logger.error(f"[Unified] {model} is not loaded — NOT running {sorted(scripts)} "
                     f"(load is retried in the background)")


# ============================================================
# Batch config (mirrors the per-script workers)
# ============================================================
# Env-overridable so a worker can be tuned to its host. Larger, fuller batches improve H200
# throughput; a deeper queue absorbs RR pool bursts without dropping frames before inference.
GPU_BATCH_SIZE     = int(os.environ.get("UNIFIED_GPU_BATCH_SIZE", "24"))
GPU_QUEUE_SIZE     = int(os.environ.get("UNIFIED_GPU_QUEUE_SIZE", "128"))
BATCH_WAIT_TIMEOUT = float(os.environ.get("UNIFIED_BATCH_WAIT_TIMEOUT", "0.2"))

# Per-analytic cooldowns. Mirrors the COOLDOWN_TIME constants in each detection
# script. Each is overridable via COOLDOWN_TIME_<KEY>, and script_runner.py's
# generic COOLDOWN_TIME (set from the UI's per-shift-script Frequency field)
# is used as a fallback for whichever analytic doesn't have its own override —
# this matches script_runner today only setting one global value per execution.
_COOLDOWN_DEFAULTS = {
    "crowd":      120,
    "crowd_zero": 1800,
    "crowd_five": 120,
    "proximity":  120,
    "intruder":   0,
    "entry_exit": 120,
    "server":     60,
    "phone":      90,
    "strongroom": 180,
    "qp_box":     240,
    "blind_spot": 180,
    "furniture":  300,
    "invigilator": 420,
    "crowd_five_invigilator": 420,
    "monitor_tamper": 15,   # monitor_alerts_deploy_v2; incident grouping stops repeats
}
_GLOBAL_COOLDOWN = os.environ.get("COOLDOWN_TIME")
# Analytics that group repeat alerts into incidents themselves and must NOT inherit the
# generic COOLDOWN_TIME (a merged group's representative Frequency, e.g. 120 s): only their
# own COOLDOWN_TIME_<KEY> or the default above applies.
_OWN_COOLDOWN_ONLY = {"monitor_tamper"}
COOLDOWNS = {
    key: int(os.environ.get(f"COOLDOWN_TIME_{key.upper()}",
                            default if key in _OWN_COOLDOWN_ONLY else (_GLOBAL_COOLDOWN or default)))
    for key, default in _COOLDOWN_DEFAULTS.items()
}

RESULTS_ROOT = os.path.join(BASE_DIR, "results")


# ============================================================
# Box helpers — extract once from the shared result
# ============================================================
def persons_from_result(result, conf=CONF_THRESHOLD):
    """COCO class 0 = person."""
    return [b for b in result.boxes
            if int(b.cls[0]) == 0 and float(b.conf[0]) >= conf]


def class_from_result(result, class_ids, conf=CONF_THRESHOLD):
    cls_set = set(class_ids)
    return [b for b in result.boxes
            if int(b.cls[0]) in cls_set and float(b.conf[0]) >= conf]


# ============================================================
# Reporting helper (shared by all evaluators)
# ============================================================
def _save_and_report(frame, *, folder, script_name, device_id, camera_no,
                     exam_id, shift_id, source_type="RTSP", violation_count=None):
    """Save the annotated detection and report it, using the schema-correct identifier:
       - RTSP: device_id + camera_no  (filename `{dev}_{camera_no}`, report_to_db_rtsp)
       - HLS : ch_code (= camera_no)  (filename `ch{ch_code}`,        report_to_db_hls)
    Matches the convention used by the per-script workers."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    uid = str(uuid.uuid4())

    if str(source_type).upper() == "HLS":
        save_folder = os.path.join(RESULTS_ROOT, "tcs_detections", folder)
        fname = f"{script_name}_ch{camera_no}_exam{exam_id}_shift{shift_id}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"
    else:
        save_folder = os.path.join(RESULTS_ROOT, folder)
        fname = f"{script_name}_{device_id}_{camera_no}_exam{exam_id}_shift{shift_id}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"

    os.makedirs(save_folder, exist_ok=True)
    path = os.path.join(save_folder, fname)
    cv2.imwrite(path, frame)
    try:
        if str(source_type).upper() == "HLS":
            from utils.db_client import report_to_db_hls
            report_to_db_hls(ch_code=str(camera_no), image_name=fname, folder=folder,
                             violation_count=violation_count)
        else:
            from utils.db_client import report_to_db_rtsp
            report_to_db_rtsp(device_id=str(device_id), camera_no=str(camera_no),
                              image_name=fname, folder=folder,
                              violation_count=violation_count)
    except Exception as e:
        logger.error(f"[Unified] DB report error ({script_name}): {e}")
    logger.warning(f"[Unified] 🚨 {script_name} saved → {path}")


# ============================================================
# Evaluators — each is a pure function over the SHARED result.
# Signature: (result, item, state) -> None  (handles its own save/report)
# Reuse the existing detection-script rule code wherever possible.
# ============================================================
CONSEC_THRESHOLD = 2  # require 2 consecutive hits (matches crowd_worker)


def _passed_gate(state, script, now):
    """Shared consecutive-hit + client-cooldown gate. Returns True if we should report."""
    cd = COOLDOWNS.get(script, 120)
    state["consec"] = state.get("consec", 0) + 1
    if state["consec"] < CONSEC_THRESHOLD:
        return False
    if now - state.get("last_save", 0) < cd:
        return False
    state["last_save"] = now
    state["consec"] = 0
    return True


# script_code -> (module, batch_fn, report_folder)
# Each batch_fn has been refactored to accept `results=` so it reuses the SHARED
# inference instead of re-running yolov10m. `report_folder` matches the basename of
# each script's existing SAVE_FOLDER (so DB/image reporting is unchanged).
ANALYTIC_SPEC = {
    "crowd":          ("Crowd_det_complete",  "detect_crowd_batch",           "crowd_detection"),
    "crowd_zero":     ("crowd_zero_det",      "detect_crowd_zero_batch",      "zero_person_detection"),
    "crowd_five":     ("crowd_five_det",      "detect_crowd_five_batch",      "crowd_detection_threshold"),
    "proximity":      ("close_proximity",     "detect_proximity_batch",       "proximity_detection"),
    "intruder":       ("Intruder_det",        "detect_intruder_batch",        "intruder_detection"),
    "entry_exit":     ("Entry_exit_det",      "detect_entry_exit_batch",      "entry_exit_detection"),
    "server":         ("Server_room_det",     "detect_persons_batch",         "server_room_detection"),
    "phone":          ("phone_det_complete",  "detect_phone_batch",           "phone_detection"),
    "qp_box":         ("qp_box_det",          "detect_qp_box_batch",          "qp_box_missing"),
    "strongroom":     ("StrongRoom_det",      "detect_strongroom_batch",      "strongroom_detection"),
    "invigilator":    ("invigilator_tracking","detect_invigilator_batch",     "invigilator_detection"),
    "blind_spot":     ("blind_spot_det",      "detect_blind_spot_batch",      "blind_spot_detection"),
    "crowd_biometric":("Crowd_det_biometric", "detect_crowd_biometric_batch", "crowd_detection_biometric"),
    "furniture":      ("improper_furniture_det", "detect_improper_furniture_batch", "improper_furniture_detection"),
    "crowd_five_invigilator": ("crowd_five_invigilator_det", "detect_crowd_five_invigilator_batch",
                               "crowd_five_invigilator_detection"),
    # NOTE: 'tamper' and 'inversion' are classical-CV checks (no YOLO boxes) and stay
    # as their own workers — they don't consume the shared detector output.
}


def _make_evaluator(module_name, fn_name, folder, script_code):
    """Build an evaluator that runs the script's rule over the SHARED result (no re-inference)."""
    _cache = {}

    def _eval(result, item, state):
        fn = _cache.get("fn")
        if fn is None:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, fn_name)
            _cache["fn"] = fn
        # Reuse the script's own rule + annotation by passing the shared result.
        outputs = fn([item], results=[result])
        out = outputs[0]
        annotated, detected = out[0], out[1]   # all return (annotated, detected, count[, ...])
        if not detected:
            state["consec"] = 0
            return
        if not _passed_gate(state, script_code, time.time()):
            return
        violation_count = None
        if script_code == "proximity" and len(out) > 3:
            # Same pair at the same spot as this camera's earlier report → 2, 3, ...
            # Keys match workers/close_proximity_worker.py so both paths share counts.
            from proximity_repeat import next_violation_count
            is_hls = str(item.get("source_type", "RTSP")).upper() == "HLS"
            camera_key = (f"hls_ch{item['camera_no']}" if is_hls
                          else f"rtsp_{item['device_id']}_{item['camera_no']}")
            violation_count = next_violation_count(
                camera_key, out[3], item.get("exam_id", "0"), item.get("shift_id", "0"))
        _save_and_report(
            annotated, folder=folder, script_name=script_code,
            device_id=item["device_id"], camera_no=item["camera_no"],
            exam_id=item.get("exam_id", "0"), shift_id=item.get("shift_id", "0"),
            source_type=item.get("source_type", "RTSP"),
            violation_count=violation_count,
        )

    return _eval


def _make_stateful_evaluator(module_name, folder, script_code):
    """For analytics that track each camera over time and decide themselves when to
    alert (own cooldown and incident grouping): no consecutive-hit gate.
    module.evaluate(result, frame, state, t, camera_id, cooldown_s, session) and
    module.flush(state, now) return [(annotated_frame, event_type)] to report; flush
    reports alerts the module held back (e.g. waiting for evidence frames), and is
    called by the worker about once a second and with now=None at shutdown."""
    _cache = {}

    def _module():
        mod = _cache.get("mod")
        if mod is None:
            mod = _cache["mod"] = importlib.import_module(module_name)
        return mod

    def _report(state, alerts):
        for annotated, _event_type in alerts:
            _save_and_report(annotated, folder=folder, script_name=script_code, **state["report_to"])

    def _eval(result, item, state):
        exam_id, shift_id = item.get("exam_id", "0"), item.get("shift_id", "0")
        state["report_to"] = dict(device_id=item["device_id"], camera_no=item["camera_no"],
                                  exam_id=exam_id, shift_id=shift_id,
                                  source_type=item.get("source_type", "RTSP"))
        camera_id = f"{item['device_id']}_{item['camera_no']}"
        # Saved per-camera state is only trusted within the same exam shift.
        session = None if str(exam_id) == "0" and str(shift_id) == "0" else f"exam{exam_id}_shift{shift_id}"
        _report(state, _module().evaluate(result, item["frame"], state, item.get("captured_at") or time.time(),
                                          camera_id, COOLDOWNS.get(script_code, 120), session))

    def _flush(state, now):
        if "report_to" in state:
            _report(state, _module().flush(state, now))

    _eval.flush = _flush
    return _eval


# script_code -> (module, report_folder); see _make_stateful_evaluator.
STATEFUL_SPEC = {
    "monitor_tamper": ("monitor_tamper_det", "monitor_tamper_detection"),
}

# Registry: script_code -> evaluator (all analytics, driven by the shared inference).
EVALUATORS = {
    code: _make_evaluator(mod, fn, folder, code)
    for code, (mod, fn, folder) in ANALYTIC_SPEC.items()
}
EVALUATORS.update({
    code: _make_stateful_evaluator(mod, folder, code)
    for code, (mod, folder) in STATEFUL_SPEC.items()
})


# ============================================================
# Unified worker — each model runs ONCE per batch, dispatched per camera/script
# ============================================================
STATS_EVERY_S = float(os.environ.get("UNIFIED_STATS_EVERY_S", "300"))
FLUSH_EVERY_S = 1.0  # how often stateful analytics may report alerts they held back


def _flush_stateful(states, now):
    """Report held alerts of every stateful analytic (now=None: all of them)."""
    for (dev, cam_no, script), state in states.items():
        flush = getattr(EVALUATORS.get(script), "flush", None)
        if flush is None:
            continue
        try:
            flush(state, now)
        except Exception as e:
            logger.error(f"[Unified] flush '{script}' error ({dev}_{cam_no}): {e}")


def _plan_batch(batch):
    """Map each (frame, analytic) to its model. Analytics whose model isn't loaded are
    skipped for this batch. Returns ({model: [frame idx]}, {(idx, script): model})."""
    need, assign, skipped = {}, {}, {}
    for idx, pkt in enumerate(batch):
        for script in pkt.get("enabled_scripts", []):
            if script not in EVALUATORS:
                continue  # not yet converted to the unified path
            model = model_for(script)
            if not BACKENDS[model].ready():
                skipped.setdefault(model, set()).add(script)
                continue
            assign[(idx, script)] = model
            need.setdefault(model, set()).add(idx)
    for model, scripts in skipped.items():
        _log_skipped(model, scripts)
    return {m: sorted(idxs) for m, idxs in need.items()}, assign


def _run_passes(need, frames, pool, stats):
    """Run every needed model in parallel over just the frames that need it.
    Returns ({(model, idx): result}, [models that failed])."""
    def run(model):
        t = time.time()
        res = BACKENDS[model].predict([frames[i] for i in need[model]])
        st = stats.setdefault(model, [0, 0, 0.0])
        st[0] += 1; st[1] += len(need[model]); st[2] += time.time() - t
        return res

    futures = {model: pool.submit(run, model) for model in need}
    out, failed = {}, []
    for model, fut in futures.items():
        try:
            for i, r in zip(need[model], fut.result()):
                out[(model, i)] = r
        except Exception as e:
            logger.error(f"[Unified] {model} inference error ({len(need[model])} frames): {e}")
            failed.append(model)
    return out, failed


def _person_view(result):
    """Person analytics see the fine-tuned model's boxes at the same confidence floor
    they had with yolov10m (that pass runs lower for the monitor analytic)."""
    return result[result.boxes.conf >= CONF_THRESHOLD]


def unified_worker(frame_queue, scripts=None):
    """
    RTSP worker entry (drop-in replacement for the N per-script workers).

    Expects packets (dict) carrying:
        frame, device_id, ch_no, camera_id_pk, exam_id, shift_id,
        enabled_scripts: list[str]   # the analytics this camera should run

    `scripts` = every analytic in this run. Only the models those analytics read are
    loaded, before the first frame (None = load each model the first time a frame needs it).
    """
    from concurrent.futures import ThreadPoolExecutor

    logger.info(f"🟢 [Unified] worker started (person model: {PERSON_MODEL}, monitor model: {MONITOR_MODEL})")
    by_model = {}
    for s in scripts or []:
        if s in EVALUATORS:
            by_model.setdefault(model_for(s), []).append(s)
    for name, users in sorted(by_model.items()):
        if BACKENDS[name].load_now():
            logger.info(f"[Unified] {name} loaded for {sorted(users)}")
        else:
            logger.error(f"[Unified] {name} FAILED to load — {sorted(users)} will not run until it does")
    pool = ThreadPoolExecutor(max_workers=len(BACKENDS), thread_name_prefix="unified-model")
    stats, last_stats = {}, time.time()
    last_flush = time.time()
    # per-(camera, script) cooldown/consec state
    states = {}

    def state_for(dev, ch, script):
        return states.setdefault((dev, ch, script), {})

    while True:
        # Also runs while no frames arrive, so a held alert is not stuck waiting for
        # its camera's next frame.
        if time.time() - last_flush >= FLUSH_EVERY_S:
            last_flush = time.time()
            _flush_stateful(states, last_flush)

        batch = []
        try:
            pkt = frame_queue.get(timeout=0.1)
        except queue_module.Empty:
            continue
        if pkt is None:
            break
        batch.append(pkt)

        stop = False
        deadline = time.time() + BATCH_WAIT_TIMEOUT
        while len(batch) < GPU_BATCH_SIZE and time.time() < deadline:
            try:
                nxt = frame_queue.get_nowait()
                if nxt is None:        # shutdown sentinel mid-batch: process what we have, then stop
                    stop = True
                    break
                batch.append(nxt)
            except queue_module.Empty:
                time.sleep(0.002)

        # ---- each needed model runs once, in parallel, over only its frames ----
        frames = [p["frame"] for p in batch]
        need, assign = _plan_batch(batch)
        # A model whose pass errored gives its analytics no result for this batch (logged
        # in _run_passes); they are skipped below, never re-run on another model.
        out, _failed = _run_passes(need, frames, pool, stats)

        # ---- dispatch boxes to each camera's enabled analytics ----
        person_views = {}
        for idx, pkt in enumerate(batch):
            dev = pkt.get("device_id", "unknown")
            # RTSP: camera_no is the camera number; HLS: it's the ch_code.
            cam_no = pkt.get("camera_no") or pkt.get("ch_no") or "1"
            src    = pkt.get("source_type", "RTSP")
            item = {
                "frame":       pkt["frame"],
                "camera_no":   cam_no,
                "ch_no":       cam_no,   # kept for detect_*_batch compatibility
                "device_id":   dev,
                "camera_id":   dev,
                "exam_id":     pkt.get("exam_id", "0"),
                "shift_id":    pkt.get("shift_id", "0"),
                "source_type": src,
                # Server_room_det reads tc_code to pick that centre's person
                # threshold (get_min_persons). Without it the lookup misses and
                # falls back to DEFAULT_MIN_PERSONS=1, i.e. a server-room alert
                # on ANY single person. The per-script workers pass it, so the
                # unified path has to as well.
                "tc_code":     pkt.get("tc_code", ""),
                # Set by the producers; stateful analytics time their rules on it.
                "captured_at": pkt.get("captured_at"),
            }
            for script in pkt.get("enabled_scripts", []):
                model = assign.get((idx, script))
                result = out.get((model, idx)) if model else None
                if result is None:
                    continue  # no model output for this frame (loading / failed)
                try:
                    if model == "person_monitor_yolo26m" and script in PERSON_ANALYTICS:
                        if idx not in person_views:
                            person_views[idx] = _person_view(result)
                        result = person_views[idx]
                    EVALUATORS[script](result, item, state_for(dev, cam_no, script))
                except Exception as e:
                    logger.error(f"[Unified] evaluator '{script}' error ({dev}_{cam_no}): {e}")

        for p in batch:
            p.pop("frame", None)

        if time.time() - last_stats >= STATS_EVERY_S:
            elapsed = time.time() - last_stats
            parts = [f"{m}: {n} passes, {f} frames, {1000 * sec / max(n, 1):.0f} ms/pass, "
                     f"GPU busy {100 * sec / elapsed:.0f}%" for m, (n, f, sec) in sorted(stats.items())]
            try:
                backlog = frame_queue.qsize()
            except Exception:
                backlog = "?"
            logger.info(f"[Unified] last {elapsed:.0f}s — queue backlog {backlog} — " + "; ".join(parts))
            stats.clear(); last_stats = time.time()

        if stop:
            break

    _flush_stateful(states, None)
    pool.shutdown(wait=False)
    logger.info("🔴 [Unified] worker stopped")
