import cv2
import time
import os
_DET_GPU_ID = int(os.environ["WORKER_INDEX"]) if os.environ.get("WORKER_INDEX", "").strip().isdigit() else int("".join(c for c in os.environ.get("WORKER_ID", "main").split("-")[-1] if c.isdigit()) or "0")
import logging
import sys
import threading
import torch
import uuid
from datetime import datetime
from ultralytics import YOLO

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= CONFIG ==================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# UNIFIED_PERSON_MODEL=finetuned -> the fine-tuned person+monitor model (person = class 0)
from detection_models import person_model_path
MODEL_PATH = person_model_path(os.path.join(BASE_DIR, "models", "yolov10m.pt"))
SAVE_FOLDER = os.path.join(BASE_DIR, "results/crowd_detection")
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# CAMERA CLASSIFICATION: Entry/Exit, Outside/Biometric Area
# This detection runs on cameras classified as:
# - Entry / Entry-Exit / Entry Gate / Exit Gate
# - Outdoor Scene / Outside Area / Biometric Area

COOLDOWN_TIME = int(os.environ.get("COOLDOWN_TIME", 120))       # Seconds between saves (2 min — per detection frequency spec)
MIN_PERSONS = 10          # Crowd violation when STANDING persons >= 10
CONF_THRESHOLD = 0.45     # Confidence for person detection

# Standing-only — seated persons are NOT counted. Same bbox rule as
# invigilator_tracking (seated boxes sit at h/w ~1.2-2.5; standing ~3-4).
STANDING_MIN_HEIGHT   = 100   # px; below this is far-away YOLO noise
STANDING_ASPECT_RATIO = 2.5   # h/w >= 2.5 → standing

# 13 Oct 2026: Entry / Outside / Parking / Biometric cameras see people small and far, so the strict rule above
# (>= 100 px tall AND h/w >= 2.5) counted only a few of a real crowd and almost no camera reached 10. For those
# camera types a person counts when >= OUTDOOR_MIN_HEIGHT px tall and h/w >= OUTDOOR_ASPECT. Lab cameras
# (Inside Lab run) keep the strict rule, otherwise seated candidates would count as a crowd.
OUTDOOR_TYPES = ("entry", "outside", "outdoor", "parking", "biometric")
OUTDOOR_MIN_HEIGHT = int(os.environ.get("CROWD_OUTDOOR_MIN_HEIGHT", "40"))
OUTDOOR_ASPECT = float(os.environ.get("CROWD_OUTDOOR_ASPECT", "1.8"))


# 14 Oct 2026: on LAB cameras (Inside Lab run) "standing" is counted by the posture model
# (posture_rfdetr, standing >= LAB_POSTURE_STANDING_MIN), not by box shape: seated candidates passed the
# shape rule and every lab alert checked on 13 Oct was a room of seated candidates (3,276 alerts, the
# posture model found none with 10+ standing). Entry / Outside / cameras of unknown type keep the old count.
LAB_TYPES = ("computer lab", "inside lab", "classroom", "exam lab")
LAB_POSTURE_STANDING_MIN = float(os.environ.get("CROWD_LAB_POSTURE_STANDING_MIN", "0.6"))
LAB_POSTURE = os.environ.get("CROWD_LAB_POSTURE", "1").strip() not in ("0", "false", "no", "")


def is_lab(classification):
    c = str(classification or "").lower()
    return any(t in c for t in LAB_TYPES)


def _iou(a, b):
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _posture_standing(posture):
    """Posture boxes [(xyxy, class, conf)] -> standing people [(x1,y1,x2,y2,conf)], one box per person."""
    st = sorted([(b, c) for b, cl, c in posture if cl == 1 and c >= LAB_POSTURE_STANDING_MIN], key=lambda x: -x[1])
    keep = []
    for b, c in st:
        if all(_iou(b, k[0]) < 0.5 for k in keep):
            keep.append((b, c))
    return [(int(b[0]), int(b[1]), int(b[2]), int(b[3]), c) for b, c in keep]


def _is_outdoor(classification):
    c = str(classification or "").lower()
    return any(t in c for t in OUTDOOR_TYPES)

os.makedirs(SAVE_FOLDER, exist_ok=True)

# ================= YOLO ====================
yolo_model = None
_model_lock = threading.Lock()

def get_yolo_model():
    """Return a shared YOLO model instance."""
    global yolo_model
    if yolo_model is None:
        with _model_lock:
            if yolo_model is None:
                logger.info(f"Loading YOLO model from {MODEL_PATH}...")
                yolo_model = YOLO(MODEL_PATH)
                if torch.cuda.is_available():
                    yolo_model.to('cuda')
                    logger.info("YOLO model moved to CUDA")
    return yolo_model

# ================= STATE ===================
last_save_times = {}
_state_lock = threading.Lock()

# =====================================================
# CORE DETECTION LOGIC
# =====================================================
def _split_standing_seated(boxes, outdoor=False):
    """Return (standing, seated) lists of (x1, y1, x2, y2, conf) person boxes."""
    min_h = OUTDOOR_MIN_HEIGHT if outdoor else STANDING_MIN_HEIGHT
    min_ratio = OUTDOOR_ASPECT if outdoor else STANDING_ASPECT_RATIO
    standing, seated = [], []
    for box in boxes:
        if int(box.cls[0]) != 0:
            continue
        conf = float(box.conf[0])
        if conf < CONF_THRESHOLD:
            continue
        x1, y1, x2, y2 = map(int, box.xyxy[0])
        w, h = x2 - x1, y2 - y1
        if w <= 0 or h <= 0:
            continue
        if h >= min_h and (h / w) >= min_ratio:
            standing.append((x1, y1, x2, y2, conf))
        else:
            seated.append((x1, y1, x2, y2, conf))
    return standing, seated


def _annotate(frame, standing, seated, crowd_detected, footer):
    """Draw standing (red, counted) and seated (thin green, ignored) boxes + status overlay."""
    for x1, y1, x2, y2, conf in seated:
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 1)
        cv2.putText(frame, f"S {conf:.2f}", (x1, max(y1 - 8, 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)
    for x1, y1, x2, y2, conf in standing:
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
        cv2.putText(frame, f"P {conf:.2f}", (x1, max(y1 - 8, 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

    standing_count = len(standing)
    overlay = frame.copy()
    cv2.rectangle(overlay, (10, 10), (440, 100), (0, 0, 0), -1)
    cv2.addWeighted(frame, 0.7, overlay, 0.3, 0, frame)
    status_color = (0, 0, 255) if crowd_detected else (0, 255, 0)
    status_text = f"CROWD DETECTED! ({standing_count})" if crowd_detected else "All Clear"
    cv2.putText(frame, status_text, (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
    cv2.putText(frame, f"Standing: {standing_count} (seated {len(seated)}) / Min: {MIN_PERSONS}", (20, 62),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    cv2.putText(frame, footer, (20, 87),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)


def detect_crowd_in_frame(frame, device_id="unknown", camera_no="unknown", script_name="crowd"):
    """Standalone detection for a single frame."""
    model = get_yolo_model()
    results = model(frame, conf=CONF_THRESHOLD, verbose=False)[0]

    standing, seated = _split_standing_seated(results.boxes)
    person_count = len(standing)
    crowd_detected = person_count >= MIN_PERSONS

    _annotate(frame, standing, seated, crowd_detected, f"{device_id} - Cam {camera_no}")

    key = f"{device_id}_{camera_no}"
    now = time.time()

    with _state_lock:
        time_elapsed = now - last_save_times.get(key, 0)
        if crowd_detected and time_elapsed >= COOLDOWN_TIME:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            uid = str(uuid.uuid4())
            fname = f"{script_name}_{device_id}_{camera_no}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"
            filename = os.path.join(SAVE_FOLDER, fname)
            cv2.imwrite(filename, frame)
            try:
                from utils.db_client import report_to_db_rtsp
                report_to_db_rtsp(
                    device_id=str(device_id),
                    camera_no=str(camera_no),
                    image_name=fname,
                    folder="crowd_detection",
                )
            except Exception as e:
                logger.error(f"[{device_id}-{camera_no}] Crowd DB reporting error: {e}")
            last_save_times[key] = now
            logger.warning(f"[{device_id}-{camera_no}] 🚨 Crowd detected: {filename}")

    return frame, crowd_detected, person_count

def detect_crowd_batch(items, results=None):
    """Run true batch inference.

    UNIFIED-DETECTOR SUPPORT: if `results` is provided (a list of YOLO Results, one
    per item, produced by a SHARED single inference pass), the model is NOT run here —
    we just apply the crowd rule over the supplied boxes. When `results` is None this
    behaves exactly as before (runs its own model), so existing callers are unchanged.
    """
    if results is None:
        model = get_yolo_model()
        frames = [item['frame'].copy() for item in items]
        results = model(frames, conf=CONF_THRESHOLD, verbose=False)

    outputs = []
    for item, result in zip(items, results):
        frame = item['frame'].copy()
        standing, seated = _split_standing_seated(result.boxes, _is_outdoor(item.get('classification')))
        if LAB_POSTURE and is_lab(item.get('classification')) and item.get('posture') is not None:
            standing = _posture_standing(item['posture'])
            seated = [p for p in seated + [] if all(_iou(p[:4], s_[:4]) < 0.5 for s_ in standing)]
        person_count = len(standing)
        crowd_detected = person_count >= MIN_PERSONS

        ch_no = item.get('ch_no', item.get('camera_id', 'unknown'))
        _annotate(frame, standing, seated, crowd_detected, f"CH: {ch_no}")

        outputs.append((frame, crowd_detected, person_count))

    return outputs
