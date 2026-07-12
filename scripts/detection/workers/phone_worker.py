"""
Phone Worker — supports both RTSP (via master_pipeline producer)
and HLS (via its own internal OpenCV reader).
"""

import queue
import time
import os
_DET_GPU_ID = int(os.environ["WORKER_INDEX"]) if os.environ.get("WORKER_INDEX", "").strip().isdigit() else int("".join(c for c in os.environ.get("WORKER_ID", "main").split("-")[-1] if c.isdigit()) or "0")
import logging
import threading
import signal
from datetime import datetime
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
import phone_det_complete as phone_core

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
RTSP_SAVE_FOLDER = os.path.join(DETECTION_ROOT, "results/phone_detection")
HLS_SAVE_FOLDER  = os.path.join(DETECTION_ROOT, "results/tcs_detections/phone_detection")

os.makedirs(RTSP_SAVE_FOLDER, exist_ok=True)
os.makedirs(HLS_SAVE_FOLDER,  exist_ok=True)

# GPU Queue Config
GPU_BATCH_SIZE      = 16  # FIXED: was 64, reduces VRAM per worker
GPU_QUEUE_SIZE      = 32   # FIXED: was 128, reduces frame buffer VRAM
BATCH_WAIT_TIMEOUT  = 0.2
THREAD_DELAY        = 0.1
HLS_SAMPLE_EVERY    = 5
CONSEC_THRESHOLD    = 2   # Consecutive frames with phone before saving (CONF=0.25 is low, avoids 1-frame false positives)

_gpu_queue = queue.Queue(maxsize=GPU_QUEUE_SIZE)

# =====================================================
# 🚀 GPU BATCH WORKER
# =====================================================
def _gpu_batch_worker(stop_event, save_folder, cooldown_time=90):
    cooldown_time = int(os.environ.get("COOLDOWN_TIME", cooldown_time))
    """
    GPU batch worker — TRUE batch inference.
    """
    logger.info("🚀 [Phone] GPU worker started (true batch mode)")
    
    # Warm up model
    phone_core.get_yolo_model()
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

        # Inference
        inference_items = [
            {
                'frame':     item['frame'],
                'ch_no':     item['ch_no'],
                'camera_id': item.get('device_id') or item.get('camera_id'),
                'state':     item.get('state', {}),
            }
            for item in batch
        ]

        try:
            batch_results = phone_core.detect_phone_batch(inference_items)
        except Exception as e:
            logger.error(f"[Phone] Batch inference error: {e}")
            for item in batch:
                item.pop('frame',None)
            del batch
            continue
        finally:
            inference_items = []

        for item in batch:
            item.pop('frame', None)

        # Process results
        for item, (annotated_frame, detected, phone_count) in zip(batch, batch_results):
            state = item.get('state', {})

            if not detected:
                state["consec_violations"] = 0
                del annotated_frame
                continue

            # Require CONSEC_THRESHOLD consecutive frames before saving
            # (CONF_THRESHOLD=0.25 is intentionally low — frame confirmation prevents false positives)
            state["consec_violations"] = state.get("consec_violations", 0) + 1
            if state["consec_violations"] < CONSEC_THRESHOLD:
                del annotated_frame
                continue

            now = time.time()
            if now - state.get("last_save", 0) < cooldown_time:
                del annotated_frame
                continue

            state["last_save"] = now
            state["consec_violations"] = 0
            ts = time.strftime("%Y%m%d_%H%M%S")
            uid = str(uuid.uuid4())

            dev_id = item.get("device_id") or item.get("camera_id")
            ch_no = item.get("ch_no")
            exam_id = item.get("exam_id") or "0"
            shift_id = item.get("shift_id") or os.environ.get("SHIFT_ID", "0")
            script_name = item.get("script_name", "phone")

            if item.get("source_type") == "HLS":
                fname = f"{script_name}_ch{ch_no}_exam{exam_id}_shift{shift_id}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"
            else:
                fname = f"{script_name}_{dev_id}_{ch_no}_exam{exam_id}_shift{shift_id}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"

            path = os.path.join(save_folder, fname)
            cv2.imwrite(path, annotated_frame)
            logger.warning(f"[Phone] 🚨 SAVED phones={phone_count} → {path}")

            # Report to database
            if item.get("source_type") == "HLS":
                try:
                    from utils.db_client import report_to_db_hls
                    report_to_db_hls(
                        ch_code=str(ch_no),
                        image_name=fname,
                        folder="phone_detection"
                    )
                except Exception as e:
                    logger.error(f"[Phone] HLS DB Reporting Error: {e}")
            else:  # RTSP
                try:
                    from utils.db_client import report_to_db_rtsp
                    report_to_db_rtsp(
                        device_id=str(dev_id),
                        camera_no=str(ch_no),
                        image_name=fname,
                        folder="phone_detection"
                    )
                except Exception as e:
                    logger.error(f"[Phone] RTSP DB Reporting Error: {e}")

            del annotated_frame

        del batch
        # Periodically flush CUDA cache to avoid memory fragmentation
        log_counter += 1
        if log_counter % 50 == 0:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            logger.debug(f"[Worker] GPU batch #{log_counter} processed")

    logger.info("🔴 [Phone] GPU worker stopped")

# =====================================================
# 📡 RTSP PATH (called by master_pipeline)
# =====================================================
def phone_worker(frame_queue):
    """RTSP Worker entry point."""
    logger.info("🟢 [Phone] RTSP worker started")
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
                camera_states[cam_key] = {"last_save": 0}

            try:
                _gpu_queue.put({
                    "frame":        frame.copy(),
                    "device_id":    device_id,
                    "camera_no":    camera_no,
                    "ch_no":        camera_no,
                    "exam_id":      exam_id,
                    "shift_id":     os.environ.get("SHIFT_ID", "0"),
                    "script_name":  "phone",
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
def _hls_camera_thread(cfg, stop_event):
    """HLS Camera reader thread."""
    from tcs_hls_utils import extract_hls_streaming_url, test_hls_stream, open_hls_stream

    camera_id   = cfg['camera_id']
    script_name = cfg.get('script_code', 'phone')
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
    state = {"last_save": 0}

    while not stop_event.is_set():
        ret, frame = cap.read()
        if not ret:
            break

        frame_count += 1
        if frame_count % HLS_SAMPLE_EVERY != 0:
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

def phone_worker_hls(camera_configs, stop_event=None):
    """HLS worker entry point."""
    if isinstance(camera_configs, dict):
        camera_configs = [camera_configs]

    if stop_event is None:
        stop_event = _STOP_EVENT

    # Scene filtering - Phone detection for Inside Lab and Inside Server Room cameras
    valid_configs = []
    for cfg in camera_configs:
        cls_val = str(cfg.get('classification', '')).lower()
        if not cls_val or any(s in cls_val for s in (
            'computer lab', 'inside lab', 'exam lab', 'lab',
            'inside classroom', 'inside_classroom', 'classroom',
            'server room', 'server_room', 'serverroom', 'inside server room'
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

    with ThreadPoolExecutor(max_workers=16) as executor:  # FIXED: was 64, reduces concurrent HLS stream threads
        for cfg in valid_configs:
            executor.submit(_hls_camera_thread, cfg, stop_event)
            time.sleep(THREAD_DELAY)

    stop_event.wait()
    stop_event.set()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
