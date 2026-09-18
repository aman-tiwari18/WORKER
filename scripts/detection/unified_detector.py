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

# ============================================================
# Shared model — loaded ONCE, used for every analytic
# ============================================================
MODEL_PATH = os.path.join(BASE_DIR, "models", "yolov10m.pt")
CONF_THRESHOLD = 0.45          # base detector confidence; per-rule filters tighten further

_model = None
_model_lock = threading.Lock()


def get_shared_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                logger.info(f"[Unified] Loading shared YOLO model {MODEL_PATH}")
                _model = YOLO(MODEL_PATH)
                if torch.cuda.is_available():
                    _model.to("cuda")
                    logger.info("[Unified] Shared model on CUDA")
    return _model


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
}
_GLOBAL_COOLDOWN = os.environ.get("COOLDOWN_TIME")
COOLDOWNS = {
    key: int(os.environ.get(f"COOLDOWN_TIME_{key.upper()}", _GLOBAL_COOLDOWN or default))
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
                     exam_id, shift_id, source_type="RTSP"):
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
            report_to_db_hls(ch_code=str(camera_no), image_name=fname, folder=folder)
        else:
            from utils.db_client import report_to_db_rtsp
            report_to_db_rtsp(device_id=str(device_id), camera_no=str(camera_no),
                              image_name=fname, folder=folder)
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
        _save_and_report(
            annotated, folder=folder, script_name=script_code,
            device_id=item["device_id"], camera_no=item["camera_no"],
            exam_id=item.get("exam_id", "0"), shift_id=item.get("shift_id", "0"),
            source_type=item.get("source_type", "RTSP"),
        )

    return _eval


# Registry: script_code -> evaluator (all analytics, driven by the shared inference).
EVALUATORS = {
    code: _make_evaluator(mod, fn, folder, code)
    for code, (mod, fn, folder) in ANALYTIC_SPEC.items()
}


# ============================================================
# Unified worker — ONE inference per batch, dispatched per camera/script
# ============================================================
def unified_worker(frame_queue):
    """
    RTSP worker entry (drop-in replacement for the N per-script workers).

    Expects packets (dict) carrying:
        frame, device_id, ch_no, camera_id_pk, exam_id, shift_id,
        enabled_scripts: list[str]   # the analytics this camera should run
    """
    logger.info("🟢 [Unified] worker started")
    get_shared_model()  # warm up
    # per-(camera, script) cooldown/consec state
    states = {}

    def state_for(dev, ch, script):
        return states.setdefault((dev, ch, script), {})

    while True:
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

        # ---- ONE inference for the whole batch ----
        model = get_shared_model()
        frames = [p["frame"] for p in batch]
        try:
            results = model(frames, conf=CONF_THRESHOLD, verbose=False)
        except Exception as e:
            logger.error(f"[Unified] inference error: {e}")
            for p in batch:
                p.pop("frame", None)
            continue

        # ---- dispatch boxes to each camera's enabled analytics ----
        for pkt, result in zip(batch, results):
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
            }
            for script in pkt.get("enabled_scripts", []):
                fn = EVALUATORS.get(script)
                if fn is None:
                    continue  # not yet converted to the unified path
                try:
                    fn(result, item, state_for(dev, cam_no, script))
                except Exception as e:
                    logger.error(f"[Unified] evaluator '{script}' error ({dev}_{cam_no}): {e}")

        for p in batch:
            p.pop("frame", None)

        if stop:
            break

    logger.info("🔴 [Unified] worker stopped")
