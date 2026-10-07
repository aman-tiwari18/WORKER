"""
Camera Inversion Worker — supports both RTSP (via master_pipeline producer)
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

# ================= GPU MEMORY GUARD =================
from workers.gpu_memory_guard import apply_gpu_memory_limit
apply_gpu_memory_limit()  # FIXED: cap per-process VRAM before any CUDA alloc
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

import uuid

# ================= CONFIG ==================
RTSP_SAVE_FOLDER = os.path.join(DETECTION_ROOT, "results/camera_inversion_detection")
HLS_SAVE_FOLDER  = os.path.join(DETECTION_ROOT, "results/tcs_detections/camera_inversion_detection")

os.makedirs(RTSP_SAVE_FOLDER, exist_ok=True)
os.makedirs(HLS_SAVE_FOLDER,  exist_ok=True)

# GPU Queue Config
GPU_BATCH_SIZE      = 8  # FIXED: was 32, reduced further (doubles for rotation) to save VRAM
GPU_QUEUE_SIZE      = 16  # FIXED: was 64, reduces frame buffer VRAM
BATCH_WAIT_TIMEOUT  = 0.2
THREAD_DELAY        = 0.1
HLS_SAMPLE_EVERY    = 10

# Orientation evidence comes only from YOLO classes that look clearly
# different upside down: people, and furniture that sits on the floor.
# Other classes (suitcase, traffic light, kite, keyboard, clock…) get
# hallucinated on empty walls/floors in *either* orientation, and a single
# such box used to flip the verdict on empty rooms. Places365 scene
# confidence was dropped too — it often scored the upside-down frame higher
# (e.g. 0.12 upright vs 0.64 rotated on a crowded lab), so it added noise,
# not signal.
ORIENTATION_CLASS_WEIGHTS = {
    "person": 1.0,
    "chair": 0.5, "couch": 0.5, "bench": 0.5, "bed": 0.5,
    "dining table": 0.5, "laptop": 0.5, "toilet": 0.5,
    "tv": 0.3,  # monitors: frequent in labs but still detected upside down sometimes
}
DET_CONF = 0.35

# The rotated candidate needs at least this much evidence (≈ one confident
# person plus a chair) and must beat the original by this ratio. Empty
# rooms produce no evidence either way and are never flagged — the camera
# gets caught once anyone walks into view.
INVERSION_MIN_EVIDENCE   = float(os.environ.get("INVERSION_MIN_EVIDENCE", "1.0"))
INVERSION_EVIDENCE_RATIO = float(os.environ.get("INVERSION_EVIDENCE_RATIO", "2.0"))

# A camera must come out inverted on this many consecutive sampled frames
# before we alert. YOLO results shift slightly with batch composition
# (letterbox padding depends on the other frames in the batch), so one
# borderline frame shouldn't be enough.
INVERSION_CONFIRM_FRAMES = int(os.environ.get("INVERSION_CONFIRM_FRAMES", "3"))

_gpu_queue = queue.Queue(maxsize=GPU_QUEUE_SIZE)

# ================= MODELS (lazy load) =================
_yolo_model = None

def _load_models():
    global _yolo_model
    if _yolo_model is not None: return

    from ultralytics import YOLO
    
    # YOLO
    yolo_path = os.path.join(DETECTION_ROOT, "models/yolo11n.pt")
    if not os.path.exists(yolo_path):
        yolo_path = "yolo11n.pt"
    _yolo_model = YOLO(yolo_path)
    if torch.cuda.is_available():
        _yolo_model.to('cuda')

# ================= BATCH DETECTION =================
def _orientation_evidence(result):
    """Weighted sum of confidences over orientation-informative classes."""
    names = result.names
    return sum(
        ORIENTATION_CLASS_WEIGHTS.get(names[int(c)], 0.0) * float(conf)
        for c, conf in zip(result.boxes.cls, result.boxes.conf)
    )

def _detect_orientation_batch(frames):
    _load_models()
    if not frames: return []

    all_rotations = []
    for frame in frames:
        all_rotations.append(frame)
        all_rotations.append(cv2.rotate(frame, cv2.ROTATE_180))

    # YOLO BATCH — results alternate original, 180, original, 180, …
    results = _yolo_model(all_rotations, conf=DET_CONF, verbose=False)
    evidence = [_orientation_evidence(r) for r in results]

    del results, all_rotations
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    # Only call it "inverted" when the 180° candidate shows real evidence
    # (people/furniture the right way up) and clearly beats the original.
    # No evidence on either side (empty room, dark IR frame) → "original".
    outputs = []
    for i in range(len(frames)):
        orig_score = evidence[2 * i]
        rot_score = evidence[2 * i + 1]
        is_inverted = (
            rot_score >= INVERSION_MIN_EVIDENCE
            and rot_score >= orig_score * INVERSION_EVIDENCE_RATIO
        )
        outputs.append({
            "orientation": "180" if is_inverted else "original",
            "orig_score": orig_score,
            "rot_score": rot_score,
        })

    return outputs

# ================= GPU WORKER =================
def _gpu_batch_worker(stop_event, save_folder, cooldown_time=240):
    cooldown_time = int(os.environ.get("COOLDOWN_TIME", cooldown_time))
    """
    GPU batch worker — TRUE batch inference.
    """
    logger.info("🚀 [Inversion] GPU worker started (true batch mode)")
    _load_models()
    
    last_save_time = {}
    inverted_streak = {}  # (cam_id, ch_no) -> consecutive "180" verdicts

    while not stop_event.is_set():
        batch = []
        try:
            batch.append(_gpu_queue.get(timeout=0.1))
        except queue.Empty:
            continue

        deadline = time.time() + BATCH_WAIT_TIMEOUT
        while len(batch) < GPU_BATCH_SIZE and time.time() < deadline:
            try:
                batch.append(_gpu_queue.get_nowait())
            except queue.Empty:
                time.sleep(0.002)

        try:
            frames = [ item['frame'] for item in batch ]
            results = _detect_orientation_batch(frames)

            for item, result in zip(batch, results):
                cam_id = item.get('device_id') or item.get('camera_id', 'unknown')
                ch_no = item.get('ch_no', '0')
                orientation = result["orientation"]
                key = (cam_id, ch_no)
                if orientation == "original":
                    inverted_streak[key] = 0
                else:
                    inverted_streak[key] = inverted_streak.get(key, 0) + 1
                logger.info(
                    f"[Inversion] scores cam={cam_id} ch={ch_no} "
                    f"orig={result['orig_score']:.3f} rot180={result['rot_score']:.3f} "
                    f"min_evidence={INVERSION_MIN_EVIDENCE} ratio={INVERSION_EVIDENCE_RATIO} "
                    f"-> {orientation} (streak {inverted_streak[key]}/{INVERSION_CONFIRM_FRAMES})"
                )

                if inverted_streak[key] < INVERSION_CONFIRM_FRAMES:
                    continue

                now = time.time()
                if now - last_save_time.get(cam_id, 0) < cooldown_time:
                    continue
                
                last_save_time[cam_id] = now
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                uid = str(uuid.uuid4())
                
                ch_no = item.get("ch_no") or "0"
                exam_id = item.get("exam_id", "0")
                shift_id = item.get("shift_id") or os.environ.get("SHIFT_ID", "0")
                script_name = "inversion"
                
                if item.get("source_type") == "HLS":
                    fname = f"{script_name}_ch{ch_no}_exam{exam_id}_shift{shift_id}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"
                else:
                    fname = f"{script_name}_{cam_id}_{ch_no}_exam{exam_id}_shift{shift_id}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"
                
                # The queue entry serves both sources (HLS frames come from the unified
                # HLS capture), so the folder follows the frame, not the worker.
                path = os.path.join(HLS_SAVE_FOLDER if item.get("source_type") == "HLS" else save_folder, fname)
                # Save the raw frame as received — not the 180°-rotated candidate —
                # so the snapshot shows the camera's actual orientation as proof,
                # instead of a software-flipped copy with an upside-down OSD.
                cv2.imwrite(path, item['frame'])
                logger.warning(f"[Inversion] 🚨 SAVED inversion → {path}")
                
                # Report to database
                if item.get("source_type") == "HLS":
                    try:
                        from utils.db_client import report_to_db_hls
                        report_to_db_hls(
                            ch_code=str(ch_no),
                            image_name=fname,
                            folder="camera_inversion_detection"
                        )
                    except Exception as e:
                        logger.error(f"[Inversion] HLS DB Reporting Error: {e}")
                else:  # RTSP
                    try:
                        from utils.db_client import report_to_db_rtsp
                        report_to_db_rtsp(
                            device_id=str(cam_id),
                            camera_no=str(ch_no),
                            image_name=fname,
                            folder="camera_inversion_detection"
                        )
                    except Exception as e:
                        logger.error(f"[Inversion] RTSP DB Reporting Error: {e}")
                
                if 'frame' in item: del item['frame']

            del batch

        except Exception as e:
            logger.error(f"[Inversion] Batch inference error: {e}")
            for item in batch:
                item.pop('frame', None)
            del batch
            continue

    logger.info("🔴 [Inversion] GPU worker stopped")

# =====================================================
# 📡 RTSP PATH (called by master_pipeline)
# =====================================================
def camera_inversion_worker(frame_queue):
    """RTSP Worker entry point."""
    logger.info("🟢 [Inversion] RTSP worker started")
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
                shift_id     = str(packet.get("shift_id") or os.environ.get("SHIFT_ID", "0"))
                source_type  = packet.get("source_type") or "RTSP"
            else:
                frame, device_id, camera_no, _, exam_id = packet[:5]
                shift_id, source_type = os.environ.get("SHIFT_ID", "0"), "RTSP"

            if frame is None:
                continue

            try:
                _gpu_queue.put({
                    "frame":        frame.copy(),
                    "device_id":    device_id,
                    "ch_no":        camera_no,
                    "exam_id":      exam_id,
                    "shift_id":     shift_id,
                    "source_type":  source_type,
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
def _hls_camera_thread(cfg, stop_event, state=None):  # state: unused, accepted for scheduler compat
    """HLS Camera reader thread."""
    from tcs_hls_utils import extract_hls_streaming_url, test_hls_stream, open_hls_stream

    camera_id   = cfg['camera_id']
    ch_no       = cfg.get('ch_no') or cfg.get('camera_no') or camera_id
    exam_id     = cfg.get('exam_id', '0')
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
            _gpu_queue.put_nowait({
                "frame":       frame.copy(),
                "camera_id":   camera_id,
                "ch_no":       ch_no,
                "exam_id":     exam_id,
                "source_type": "HLS",
            })
        except queue.Full:
            pass
        finally:
            del frame

    cap.release()

def camera_inversion_worker_hls(camera_configs, stop_event=None):
    """HLS worker entry point - Camera Inversion detection for all camera types."""
    if isinstance(camera_configs, dict):
        camera_configs = [camera_configs]

    if stop_event is None:
        stop_event = _STOP_EVENT

    # No scene filtering - Camera inversion can happen on any camera type
    # (Inside Lab, Server Room, Outside, etc.)

    gpu_t = threading.Thread(
        target=_gpu_batch_worker,
        args=(stop_event, HLS_SAVE_FOLDER),
        daemon=True
    )
    gpu_t.start()

    from workers.hls_scheduler import run_hls_cameras
    run_hls_cameras(camera_configs, stop_event, _hls_camera_thread, label="CameraInversion")
    stop_event.set()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
