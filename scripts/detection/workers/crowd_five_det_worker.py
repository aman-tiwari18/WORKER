"""
Crowd Five Worker — supports both RTSP (via master_pipeline producer)
and HLS (via its own internal OpenCV reader).

Applies G1 (frame-quality short-circuit) before GPU inference, G7 (median-of-5
temporal smoothing) and G8 (CONSEC_VIOLATIONS) after, and G9 (per-camera cooldown)
at save time. Per-frame gates G2–G6 live in crowd_five_det.detect_crowd_five_batch.
"""

import queue
import time
import os
_DET_GPU_ID = int(os.environ["WORKER_INDEX"]) if os.environ.get("WORKER_INDEX", "").strip().isdigit() else int("".join(c for c in os.environ.get("WORKER_ID", "main").split("-")[-1] if c.isdigit()) or "0")
import logging
import threading
import signal
from datetime import datetime
from collections import deque, defaultdict
from statistics import median
import cv2
import torch
from concurrent.futures import ThreadPoolExecutor

# Ensure detection root is on path for imports
import sys
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DETECTION_ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
if DETECTION_ROOT not in sys.path:
    sys.path.insert(0, DETECTION_ROOT)

# Import core detection module
import crowd_five_det as crowd_five_core

# ================= GPU MEMORY GUARD =================
from workers.gpu_memory_guard import apply_gpu_memory_limit
apply_gpu_memory_limit()  # FIXED: cap per-process VRAM before any CUDA alloc

# ================= GRACEFUL STOP FLAG =================
_STOP_EVENT = threading.Event()

def _handle_stop(sig, frame):
    _STOP_EVENT.set()

if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGINT,  _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

import uuid

# ================= CONFIG ==================
RTSP_SAVE_FOLDER = os.path.join(DETECTION_ROOT, "results/crowd_detection_threshold")
HLS_SAVE_FOLDER  = os.path.join(DETECTION_ROOT, "results/tcs_detections/crowd_detection_threshold")

os.makedirs(RTSP_SAVE_FOLDER, exist_ok=True)
os.makedirs(HLS_SAVE_FOLDER,  exist_ok=True)

# GPU Queue Config
GPU_BATCH_SIZE      = 16  # FIXED: was 64, reduces VRAM per worker
GPU_QUEUE_SIZE      = 32   # FIXED: was 128, reduces frame buffer VRAM
BATCH_WAIT_TIMEOUT  = 0.2
THREAD_DELAY        = 0.1
HLS_SAMPLE_EVERY    = 5

# G8: sustained violation required before save (~1s RTSP, ~3s HLS at 5 FPS effective)
CONSEC_VIOLATIONS   = 3

# G7: median-smoothing window length (frames). Defends against single-frame YOLO misses
# and brief occlusions inflating the smoothed count downward.
COUNT_WINDOW_LEN    = 5

# Telemetry log throttling — one summary line per camera every N seconds.
TELEMETRY_LOG_EVERY = 60.0

_gpu_queue = queue.Queue(maxsize=GPU_QUEUE_SIZE)


def _init_camera_state():
    """Fresh per-camera state dict. All gate state lives here so it survives across frames."""
    return {
        "last_save":           0,
        "consec_violations":   0,
        "count_window":        deque(maxlen=COUNT_WINDOW_LEN),   # G7
        "frame_hash":          None,                              # G1
        "gate_counters":       defaultdict(int),                  # telemetry
        "last_telemetry_log":  0.0,
    }


def _maybe_log_telemetry(state, label):
    """Emit one summary line of gate-counter activity per camera, throttled to once per TELEMETRY_LOG_EVERY."""
    now = time.time()
    if now - state.get("last_telemetry_log", 0.0) < TELEMETRY_LOG_EVERY:
        return
    state["last_telemetry_log"] = now
    c = state.get("gate_counters") or {}
    if not c:
        return
    summary = " ".join(f"{k}={v}" for k, v in sorted(c.items()))
    logger.info(f"[CrowdFive][{label}] gate telemetry: {summary}")
    c.clear()

# =====================================================
# 🚀 GPU BATCH WORKER
# =====================================================
def _gpu_batch_worker(stop_event, save_folder, cooldown_time=120):
    cooldown_time = int(os.environ.get("COOLDOWN_TIME", cooldown_time))
    """GPU batch worker — gate-chain mode.

    Pipeline per batch:
      1. G1 frame-quality short-circuit — corrupt frames bypass GPU.
      2. detect_crowd_five_batch applies G2–G6 (conf, size, aspect, edge, IoMin-dedup).
      3. G7 pushes effective_count into per-camera deque, takes median.
      4. G8 requires CONSEC_VIOLATIONS consecutive low-smoothed frames.
      5. G9 enforces cooldown_time between saves per camera.
    """
    logger.info("🚀 [CrowdFive] GPU worker started (gate-chain mode)")
    
    try:
        crowd_five_core.get_yolo_model()
    except Exception:
        logger.exception("[CrowdFive] YOLO model warmup failed")
        stop_event.set()
        return
    log_counter = 0

    while not stop_event.is_set():
        batch = []
        try:
            batch.append(_gpu_queue.get(timeout=0.1))
        except queue.Empty:
            continue

        # Fill batch
        deadline = time.time() + BATCH_WAIT_TIMEOUT
        while len(batch) < GPU_BATCH_SIZE and time.time() < deadline:
            try:
                batch.append(_gpu_queue.get_nowait())
            except queue.Empty:
                time.sleep(0.002)

        # G1: frame-quality short-circuit BEFORE GPU inference
        ok_items = []
        for item in batch:
            state = item.get('state') or {}
            reason, new_hash = crowd_five_core._is_frame_corrupt(
                item.get('frame'), state.get('frame_hash')
            )
            state['frame_hash'] = new_hash
            if reason is not None:
                state.setdefault('gate_counters', defaultdict(int))[reason] += 1
                # Corrupt frames must NOT accumulate consec_violations
                state['consec_violations'] = 0
                item.pop('frame', None)
                continue
            ok_items.append(item)

        if not ok_items:
            del batch
            continue

        # Inference
        inference_items = [
            {
                'frame':     item['frame'],
                'ch_no':     item['ch_no'],
                'camera_id': item.get('device_id') or item.get('camera_id'),
                'state':     item.get('state', {}),
            }
            for item in ok_items
        ]

        try:
            batch_results = crowd_five_core.detect_crowd_five_batch(inference_items)
        except Exception as e:
            logger.error(f"[CrowdFive] Batch inference error: {e}")
            for item in batch:
                item.pop('frame',None)
            del batch
            continue
        finally:
            inference_items = []

        for item in batch:
            item.pop('frame', None)

        # Process results
        for item, (annotated_frame, _initial_violation, effective_count, telemetry) in zip(ok_items, batch_results):
            state = item.get('state', {})

            # Merge per-frame G2–G6 telemetry into per-camera counters
            counters = state.setdefault('gate_counters', defaultdict(int))
            for k, v in telemetry.items():
                counters[k] += v
            counters['frames'] += 1

            cam_label = f"{item.get('device_id') or item.get('camera_id')}-{item.get('ch_no')}"
            _maybe_log_telemetry(state, cam_label)

            # G7: median smoothing over recent effective_counts for this camera
            cw = state.setdefault('count_window', deque(maxlen=COUNT_WINDOW_LEN))
            cw.append(effective_count)
            smoothed = median(cw)

            is_violation = 0 < smoothed < crowd_five_core.MAX_PERSONS
            if not is_violation:
                state['consec_violations'] = 0
                del annotated_frame
                continue

            # G8: require CONSEC_VIOLATIONS consecutive violation frames
            state['consec_violations'] = state.get('consec_violations', 0) + 1
            if state['consec_violations'] < CONSEC_VIOLATIONS:
                del annotated_frame
                continue

            # G9: per-camera cooldown
            now = time.time()
            if now - state.get('last_save', 0) < cooldown_time:
                del annotated_frame
                continue

            state['last_save'] = now
            state['consec_violations'] = 0
            ts = time.strftime("%Y%m%d_%H%M%S")
            uid = str(uuid.uuid4())

            dev_id = item.get("device_id") or item.get("camera_id")
            ch_no = item.get("ch_no")
            exam_id = item.get("exam_id") or "0"
            shift_id = item.get("shift_id") or os.environ.get("SHIFT_ID", "0")
            script_name = item.get("script_name", "crowd_five")

            if item.get("source_type") == "HLS":
                fname = f"{script_name}_ch{ch_no}_exam{exam_id}_shift{shift_id}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"
            else:
                fname = f"{script_name}_{dev_id}_{ch_no}_exam{exam_id}_shift{shift_id}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"

            path = os.path.join(save_folder, fname)
            cv2.imwrite(path, annotated_frame)
            logger.warning(
                f"[CrowdFive] 🚨 SAVED low-standing smoothed={smoothed:.1f} → {path}"
            )

            # Report to database
            if item.get("source_type") == "HLS":
                try:
                    from utils.db_client import report_to_db_hls
                    report_to_db_hls(
                        ch_code=str(ch_no),
                        image_name=fname,
                        folder="crowd_detection_threshold"
                    )
                except Exception as e:
                    logger.error(f"[CrowdFive] HLS DB Reporting Error: {e}")
            else:  # RTSP
                try:
                    from utils.db_client import report_to_db_rtsp
                    report_to_db_rtsp(
                        device_id=str(dev_id),
                        camera_no=str(ch_no),
                        image_name=fname,
                        folder="crowd_detection_threshold"
                    )
                except Exception as e:
                    logger.error(f"[CrowdFive] RTSP DB Reporting Error: {e}")

            del annotated_frame

        del batch
        # Periodically flush CUDA cache to avoid memory fragmentation
        log_counter += 1
        if log_counter % 50 == 0:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            logger.debug(f"[Worker] GPU batch #{log_counter} processed")

    logger.info("🔴 [CrowdFive] GPU worker stopped")

# =====================================================
# 📡 RTSP PATH (called by master_pipeline)
# =====================================================
def crowd_five_det_worker(frame_queue):
    """RTSP Worker entry point."""
    logger.info("🟢 [CrowdFive] RTSP worker started")
    stop_event = threading.Event()

    gpu_t = threading.Thread(
        target=_gpu_batch_worker,
        args=(stop_event, RTSP_SAVE_FOLDER),
        daemon=True
    )
    gpu_t.start()

    camera_states = {}

    try:
        while True:
            packet = frame_queue.get()
            if packet is None:
                break

            if isinstance(packet, dict):
                frame        = packet.get("frame")
                device_id    = str(packet.get("device_id", "unknown"))
                camera_no    = str(packet.get("camera_no") or packet.get("ch_no") or "0")
                exam_id      = str(packet.get("exam_id", "0"))
            else:
                frame, device_id, camera_no, _, exam_id = packet[:5]

            if frame is None:
                continue

            cam_key = (device_id, camera_no)
            if cam_key not in camera_states:
                camera_states[cam_key] = _init_camera_state()

            try:
                _gpu_queue.put({
                    "frame":        frame.copy(),
                    "device_id":    device_id,
                    "camera_no":    camera_no,
                    "ch_no":        camera_no,
                    "exam_id":      exam_id,
                    "shift_id":     os.environ.get("SHIFT_ID", "0"),
                    "script_name":  "crowd_five",
                    "source_type":  "RTSP",
                    "state":        camera_states[cam_key],
                }, timeout=0.1)
            except queue.Full:
                pass
            finally:
                del frame
    finally:
        stop_event.set()
        gpu_t.join(timeout=5)
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

# =====================================================
# 📡 HLS PATH
# =====================================================
def _hls_camera_thread(cfg, stop_event, state=None):
    """HLS Camera reader thread."""
    from tcs_hls_utils import extract_hls_streaming_url, test_hls_stream, open_hls_stream

    camera_id   = cfg['camera_id']
    script_name = cfg.get('script_code', 'crowd_five')
    ch_no       = cfg.get('ch_no') or cfg.get('camera_no') or camera_id
    exam_id     = cfg.get('exam_id', '0')
    shift_id    = cfg.get('shift_id', '0')
    initial_url = cfg.get('cctv_url') or cfg.get('stream_url') or cfg.get('url', '')

    hls = extract_hls_streaming_url(initial_url)
    if not hls or not test_hls_stream(hls):
        return

    cap = open_hls_stream(hls)
    if cap is None:
        return

    frame_count = 0
    if state is None:
        state = _init_camera_state()

    while not stop_event.is_set():
        ret, frame = cap.read()
        if not ret:
            break

        frame_count += 1
        if frame_count % HLS_SAMPLE_EVERY != 0:
            continue

        if frame is None or frame.size == 0:
            continue

        try:
            _gpu_queue.put_nowait({
                "frame":       frame.copy(),
                "camera_id":   camera_id,
                "ch_no":       ch_no,
                "exam_id":     exam_id,
                "shift_id":    shift_id,
                "script_name": script_name,
                "source_type": "HLS",
                "state":       state,
            })
        except queue.Full:
            pass
        finally:
            del frame

    cap.release()

def crowd_five_det_worker_hls(camera_configs, stop_event=None):
    """HLS worker entry point."""
    if isinstance(camera_configs, dict):
        camera_configs = [camera_configs]

    if stop_event is None:
        stop_event = _STOP_EVENT

    # Scene filtering - Crowd Five detection for Inside Lab cameras (less than 5 candidates)
    valid_configs = []
    for cfg in camera_configs:
        cls_val = str(cfg.get('classification', '')).lower()
        if not cls_val or any(s in cls_val for s in (
            'computer lab', 'inside lab', 'exam lab', 'lab',
            'inside classroom', 'inside_classroom', 'classroom'
        )):
            valid_configs.append(cfg)
            
    if not valid_configs:
        return

    gpu_t = threading.Thread(
        target=_gpu_batch_worker,
        args=(stop_event, HLS_SAVE_FOLDER),
        daemon=True
    )
    gpu_t.start()

    from workers.hls_scheduler import run_hls_cameras
    run_hls_cameras(valid_configs, stop_event, _hls_camera_thread,
                    label="CrowdFive", state_factory=_init_camera_state)
    stop_event.set()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
