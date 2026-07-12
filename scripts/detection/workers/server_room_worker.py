"""
Server Room Worker — supports both RTSP (via master_pipeline producer)
and HLS (via its own internal OpenCV reader).

Merged from:
  - Server_room_det.py      (RTSP / DSS flow)
  - server_room_det_tcs.py  (HLS / TCS flow)
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
import Server_room_det as server_core

# ================= GPU MEMORY GUARD =================
from workers.gpu_memory_guard import apply_gpu_memory_limit
apply_gpu_memory_limit()  # FIXED: cap per-process VRAM before any CUDA alloc

# Import shared threshold constants
from server_room_constants import TC_CODE_THRESHOLDS, get_min_persons, DEFAULT_MIN_PERSONS

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
RTSP_SAVE_FOLDER = os.path.join(DETECTION_ROOT, "results/server_room_detection")
HLS_SAVE_FOLDER  = os.path.join(DETECTION_ROOT, "results/tcs_detections/server_room_detection")

os.makedirs(RTSP_SAVE_FOLDER, exist_ok=True)
os.makedirs(HLS_SAVE_FOLDER,  exist_ok=True)

# GPU Queue Config
GPU_BATCH_SIZE      = 16  # FIXED: was 64, reduces VRAM per worker
GPU_QUEUE_SIZE      = 32   # FIXED: was 128, reduces frame buffer VRAM
BATCH_WAIT_TIMEOUT  = 0.2
THREAD_DELAY        = 0.1
HLS_SAMPLE_EVERY    = 30  # Sample every 30th frame (1 frame per second at 30fps)

_gpu_queue = queue.Queue(maxsize=GPU_QUEUE_SIZE)

# =====================================================
# 🚀 GPU BATCH WORKER
# =====================================================
def _gpu_batch_worker(stop_event, save_folder, cooldown_time=60):
    cooldown_time = int(os.environ.get("COOLDOWN_TIME", cooldown_time))
    """
    GPU batch worker — TRUE batch inference.
    Pulls frames from _gpu_queue and processes them in batches.
    Cooldown reduced to 60s (1 minute) for faster server room alerts.
    """
    logger.info("🚀 [ServerRoom] GPU worker started (true batch mode)")
    
    # Warm up model
    server_core.get_yolo_model()
    log_counter = 0
    
    # Global cooldown tracking per camera (device_id + ch_no)
    camera_cooldowns = {}

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
                'tc_code':   item.get('tc_code', ''),
                'state':     item.get('state', {}),
            }
            for item in batch
        ]

        try:
            batch_results = server_core.detect_persons_batch(inference_items)
        except Exception as e:
            logger.error(f"[ServerRoom] Batch inference error: {e}")
            for item in batch:
                item.pop('frame',None)
            del batch
            continue
        finally:
            inference_items = []

        for item in batch:
            item.pop('frame', None)

        # Memory logging
        log_counter += 1
        if torch.cuda.is_available() and log_counter % 50 == 0:
            allocated = torch.cuda.memory_allocated() / 1e9
            logger.info(f"[ServerRoom][GPU] batch={len(batch)} | allocated={allocated:.2f}GB")

        # Process results
        for item, (annotated_frame, detected, person_count) in zip(batch, batch_results):
            if not detected:
                del annotated_frame
                continue

            dev_id = item.get("device_id") or item.get("camera_id")
            ch_no = item.get("ch_no")

            camera_key = f"{dev_id}_{ch_no}"

            now = time.time()
            last_save = camera_cooldowns.get(camera_key, 0)

            if now - last_save < cooldown_time:
                time_remaining = int(cooldown_time - (now - last_save))
                if log_counter % 100 == 0:
                    logger.debug(f"[ServerRoom] {camera_key} in cooldown ({time_remaining}s remaining)")
                del annotated_frame
                continue

            camera_cooldowns[camera_key] = now

            ts = time.strftime("%Y%m%d_%H%M%S")
            uid = str(uuid.uuid4())

            exam_id = item.get("exam_id") or "0"
            shift_id = item.get("shift_id") or os.environ.get("SHIFT_ID", "0")
            script_name = item.get("script_name", "server")

            if item.get("source_type") == "HLS":
                fname = f"{script_name}_ch{ch_no}_exam{exam_id}_shift{shift_id}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"
            else:
                fname = f"{script_name}_{dev_id}_{ch_no}_exam{exam_id}_shift{shift_id}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"

            path = os.path.join(save_folder, fname)
            cv2.imwrite(path, annotated_frame)
            logger.warning(f"[ServerRoom] 🚨 SAVED persons={person_count} camera={camera_key} → {path}")

            # Report to database
            if item.get("source_type") == "HLS":
                try:
                    from utils.db_client import report_to_db_hls
                    report_to_db_hls(
                        ch_code=str(ch_no),
                        image_name=fname,
                        folder="server_room_detection"
                    )
                except Exception as e:
                    logger.error(f"[ServerRoom] HLS DB Reporting Error: {e}")
            else:  # RTSP
                try:
                    from utils.db_client import report_to_db_rtsp
                    report_to_db_rtsp(
                        device_id=str(dev_id),
                        camera_no=str(ch_no),
                        image_name=fname,
                        folder="server_room_detection"
                    )
                except Exception as e:
                    logger.error(f"[ServerRoom] RTSP DB Reporting Error: {e}")

            del annotated_frame

        del batch
        # Periodically flush CUDA cache to avoid memory fragmentation
        log_counter += 1
        if log_counter % 50 == 0:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            logger.debug(f"[Worker] GPU batch #{log_counter} processed")

    logger.info("🔴 [ServerRoom] GPU worker stopped")

# =====================================================
# 📡 RTSP PATH (called by master_pipeline)
# =====================================================
def server_room_worker(frame_queue):
    """RTSP Worker entry point."""
    logger.info("🟢 [ServerRoom] RTSP worker started")
    stop_event = threading.Event()

    gpu_t = threading.Thread(
        target=_gpu_batch_worker,
        args=(stop_event, RTSP_SAVE_FOLDER),
        daemon=True
    )
    gpu_t.start()

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
                # Updated packet structure: (frame, device_id, camera_no, camera_id_pk, exam_id)
                frame, device_id, camera_no, _, exam_id = packet[:5]

            if frame is None:
                continue

            tc_code = str(packet.get("tc_code", "")) if isinstance(packet, dict) else ""
            try:
                _gpu_queue.put({
                    "frame":        frame.copy(),
                    "device_id":    device_id,
                    "camera_no":    camera_no,
                    "ch_no":        camera_no,
                    "exam_id":      exam_id,
                    "shift_id":     os.environ.get("SHIFT_ID", "0"),
                    "tc_code":      tc_code,
                    "script_name":  "server",
                    "source_type":  "RTSP",
                    "state":        {},
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
    script_name = cfg.get('script_code', 'server')
    ch_no       = cfg.get('ch_no') or cfg.get('camera_no') or camera_id
    exam_id     = cfg.get('exam_id', '0')
    shift_id    = cfg.get('shift_id', '0')
    tc_code     = str(cfg.get('tc_code') or cfg.get('TC_CODE') or '')
    initial_url = cfg.get('cctv_url') or cfg.get('stream_url') or cfg.get('url', '')

    hls = extract_hls_streaming_url(initial_url)
    if not hls or not test_hls_stream(hls):
        logger.warning(f"[ServerRoom] HLS stream test failed for camera {ch_no}")
        return

    cap = open_hls_stream(hls)
    if cap is None:
        logger.error(f"[ServerRoom] Failed to open HLS stream for camera {ch_no}")
        return

    logger.info(f"[ServerRoom] HLS camera {ch_no} started (sampling every {HLS_SAMPLE_EVERY} frames)")
    frame_count = 0

    while not stop_event.is_set():
        ret, frame = cap.read()
        if not ret:
            logger.warning(f"[ServerRoom] HLS camera {ch_no} read failed, reconnecting...")
            time.sleep(5)
            cap.release()
            cap = open_hls_stream(hls)
            if cap is None:
                break
            continue

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
                "tc_code":     tc_code,
                "script_name": script_name,
                "source_type": "HLS",
            })
        except queue.Full:
            # Queue full - skip this frame
            pass
        finally:
            del frame

    cap.release()
    logger.info(f"[ServerRoom] HLS camera {ch_no} stopped")

def server_room_worker_hls(camera_configs, stop_event=None):
    """HLS worker entry point."""
    if isinstance(camera_configs, dict):
        camera_configs = [camera_configs]

    if stop_event is None:
        stop_event = _STOP_EVENT

    # Scene filtering
    valid_configs = []
    for cfg in camera_configs:
        cls_val = str(cfg.get('classification', '')).lower()
        if not cls_val or any(s in cls_val for s in ('server room', 'computer lab', 'lab')):
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
