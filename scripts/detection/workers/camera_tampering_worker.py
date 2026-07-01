"""
Camera Tampering Worker — supports both RTSP (via master_pipeline producer)
and HLS (via its own internal OpenCV reader).

Detects camera tampering via frame analysis:
  - Brightness check  → "Camera Blocked"
  - Blur score (Laplacian variance) → "Camera Blurred"
  - Edge density → "Camera Covered"
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
import numpy as np
from concurrent.futures import ThreadPoolExecutor

# Ensure detection root is on path for imports
import sys
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DETECTION_ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
if DETECTION_ROOT not in sys.path:
    sys.path.insert(0, DETECTION_ROOT)

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

# ================= CONFIG ==================
RTSP_SAVE_FOLDER = os.path.join(DETECTION_ROOT, "results/camera_tampering_detection")
HLS_SAVE_FOLDER  = os.path.join(DETECTION_ROOT, "results/tcs_detections/camera_tampering_detection")

os.makedirs(RTSP_SAVE_FOLDER, exist_ok=True)
os.makedirs(HLS_SAVE_FOLDER,  exist_ok=True)

COOLDOWN_TIME = 60
THREAD_DELAY  = 0.1
HLS_SAMPLE_EVERY = 10

import uuid

# ================= SHARED STATE ============
_last_save_time = {}
_state_lock = threading.Lock()

# =====================================================
# 🔍 TAMPERING DETECTION (pure OpenCV, CPU-only)
# =====================================================
def _detect_camera_tampering(frame):
    """
    Detect tampering via brightness / blur / edge density.
    Returns (status, brightness, blur_score, edge_density).
    """
    resized    = cv2.resize(frame, (320, 240))
    brightness = np.mean(resized)
    gray       = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    blur_score = cv2.Laplacian(gray, cv2.CV_64F).var()
    edges      = cv2.Canny(gray, 50, 150)
    edge_density = np.sum(edges > 0) / (320 * 240)

    if brightness < 60:
        return "Camera Blocked", brightness, blur_score, edge_density
    if blur_score < 300:
        return "Camera Blurred", brightness, blur_score, edge_density
    if edge_density < 0.03:
        return "Camera Covered", brightness, blur_score, edge_density
    return "Normal", brightness, blur_score, edge_density

# ================= CPU WORKER =================
def _cpu_worker(stop_event, frame_queue, save_folder):
    """
    Asynchronous CPU worker for tampering detection.
    """
    logger.info("🚀 [Tampering] CPU worker started")
    
    with ThreadPoolExecutor(max_workers=32) as executor:
        while not stop_event.is_set():
            try:
                packet = frame_queue.get(timeout=0.1)
                if packet is None: break
                
                executor.submit(_process_packet, packet, save_folder)
            except queue.Empty:
                continue

    logger.info("🔴 [Tampering] CPU worker stopped")

def _process_packet(item, save_folder):
    if isinstance(item, dict):
        frame        = item.get("frame")
        device_id    = str(item.get("device_id") or item.get("camera_id") or "unknown")
        camera_no    = str(item.get("camera_no") or item.get("ch_no") or "0")
        source_type  = item.get("source_type", "RTSP")
    else:
        frame, device_id, camera_no = item[:3]
        source_type = "RTSP"

    if frame is None: return

    try:
        status, brightness, blur_score, edge_density = _detect_camera_tampering(frame)
        if status != "Normal":
            now = time.time()
            with _state_lock:
                if now - _last_save_time.get(device_id, 0) < COOLDOWN_TIME:
                    return
                _last_save_time[device_id] = now

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            uid = str(uuid.uuid4())
            script_name = "tampering"
            exam_id = "0"  # Tampering doesn't have exam_id in packet
            shift_id = os.environ.get("SHIFT_ID", "0")
            
            if source_type == "HLS":
                fname = f"{script_name}_ch{camera_no}_exam{exam_id}_shift{shift_id}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"
            else:
                fname = f"{script_name}_{device_id}_{camera_no}_exam{exam_id}_shift{shift_id}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"
            
            path = os.path.join(save_folder, fname)
            cv2.imwrite(path, frame)
            logger.warning(f"[Tampering] 🚨 {status} | {device_id} → {path}")
            
            # Report to database
            if source_type == "HLS":
                try:
                    from utils.db_client import report_to_db_hls
                    report_to_db_hls(
                        ch_code=str(camera_no),
                        image_name=fname,
                        folder="camera_tampering_detection"
                    )
                except Exception as e:
                    logger.error(f"[Tampering] HLS DB Reporting Error: {e}")
            else:  # RTSP
                try:
                    from utils.db_client import report_to_db_rtsp
                    report_to_db_rtsp(
                        device_id=str(device_id),
                        camera_no=str(camera_no),
                        image_name=fname,
                        folder="camera_tampering_detection"
                    )
                except Exception as e:
                    logger.error(f"[Tampering] RTSP DB Reporting Error: {e}")
            
    except Exception as e:
        logger.error(f"[Tampering] Error: {e}")
    finally:
        del frame

# =====================================================
# 📡 RTSP PATH (called by master_pipeline)
# =====================================================
def camera_tampering_worker(frame_queue):
    """RTSP Worker entry point."""
    logger.info("🟢 [Tampering] RTSP worker started")
    stop_event = threading.Event()

    # Tampering is fast enough on CPU that we can use a thread pool
    # instead of a dedicated GPU worker architecture.
    _cpu_worker(stop_event, frame_queue, RTSP_SAVE_FOLDER)

# =====================================================
# 📡 HLS PATH
# =====================================================
def _hls_camera_thread(cfg, stop_event, q):
    """HLS Camera reader thread."""
    from tcs_hls_utils import extract_hls_streaming_url, test_hls_stream, open_hls_stream

    camera_id   = cfg['camera_id']
    ch_no       = cfg.get('ch_no') or cfg.get('camera_no') or camera_id
    initial_url = cfg.get('cctv_url') or cfg.get('stream_url') or cfg.get('url', '')

    hls = extract_hls_streaming_url(initial_url)
    if not hls or not test_hls_stream(hls):
        return

    cap = open_hls_stream(hls)
    if cap is None:
        return

    frame_count = 0
    while not stop_event.is_set():
        ret, frame = cap.read()
        if not ret:
            break

        frame_count += 1
        if frame_count % HLS_SAMPLE_EVERY != 0:
            continue

        try:
            q.put_nowait({
                "frame":       frame.copy(),
                "camera_id":   camera_id,
                "ch_no":       ch_no,
                "source_type": "HLS",
            })
        except queue.Full:
            pass
        finally:
            del frame

    cap.release()

def camera_tampering_worker_hls(camera_configs, stop_event=None):
    """HLS worker entry point."""
    if isinstance(camera_configs, dict):
        camera_configs = [camera_configs]

    if stop_event is None:
        stop_event = _STOP_EVENT

    q = queue.Queue(maxsize=128)
    
    # Start CPU worker thread
    t = threading.Thread(target=_cpu_worker, args=(stop_event, q, HLS_SAVE_FOLDER), daemon=True)
    t.start()

    with ThreadPoolExecutor(max_workers=16) as executor:  # FIXED: was 64, reduces concurrent HLS stream threads
        for cfg in camera_configs:
            executor.submit(_hls_camera_thread, cfg, stop_event, q)
            time.sleep(THREAD_DELAY)

    stop_event.wait()
    stop_event.set()
    q.put(None)
    t.join(timeout=5)
