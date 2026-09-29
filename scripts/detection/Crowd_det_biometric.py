"""
Crowd Detection for Biometric Area
Detects crowd (>10 people) at biometric scanning areas
"""
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
SAVE_FOLDER = os.path.join(BASE_DIR, "results/crowd_detection_biometric")
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

COOLDOWN_TIME = int(os.environ.get("COOLDOWN_TIME", 120))       # Seconds between saves (2 min — per detection frequency spec)
MIN_PERSONS = 5           # Crowd violation when > 5 persons AND they are in one very-close group
CONF_THRESHOLD = 0.45     # Confidence for person detection

# "Very close proximity": a crowd is only flagged when MORE than MIN_PERSONS people are
# standing together in one connected group (chain of neighbours), not merely scattered
# across the frame. Two people are "neighbours" when their foot-centres are within
# max(CLOSE_MIN_PX, avg_box_height * CLOSE_SCALE) pixels (adaptive to perspective) and
# not in clearly different depth rows (y-gap <= max(CLOSE_ROW_FLOOR, avg_h * CLOSE_ROW_RATIO)).
CLOSE_MIN_PX    = 30
CLOSE_SCALE     = 0.50
CLOSE_ROW_FLOOR = 40
CLOSE_ROW_RATIO = 0.30

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


# ================= PROXIMITY GROUPING ==================
def _largest_close_group(xyxy_boxes):
    """Return indices of the largest group of people standing very close together.

    Single-linkage clustering: A-B close and B-C close => A, B, C are one group.
    """
    n = len(xyxy_boxes)
    if n == 0:
        return []
    feet = [((b[0] + b[2]) / 2.0, float(b[3])) for b in xyxy_boxes]
    heights = [float(b[3] - b[1]) for b in xyxy_boxes]
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            avg_h = (heights[i] + heights[j]) / 2.0
            dx = feet[i][0] - feet[j][0]
            dy = feet[i][1] - feet[j][1]
            if abs(dy) > max(CLOSE_ROW_FLOOR, avg_h * CLOSE_ROW_RATIO):
                continue
            if (dx * dx + dy * dy) ** 0.5 <= max(CLOSE_MIN_PX, avg_h * CLOSE_SCALE):
                parent[find(i)] = find(j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return max(groups.values(), key=len)

# ================= STATE ===================
last_save_times = {}
_state_lock = threading.Lock()

# =====================================================
# CORE DETECTION LOGIC
# =====================================================
def detect_crowd_biometric_in_frame(frame, device_id="unknown", camera_no="unknown", script_name="crowd_biometric"):
    """Standalone detection for a single frame at biometric area."""
    model = get_yolo_model()
    results = model(frame, conf=CONF_THRESHOLD, verbose=False)[0]

    person_detections = [
        box for box in results.boxes
        if int(box.cls[0]) == 0 and box.conf[0] >= CONF_THRESHOLD
    ]

    person_count = len(person_detections)
    xyxy = [box.xyxy[0].cpu().numpy() for box in person_detections]
    group = set(_largest_close_group(xyxy))
    crowd_detected = person_count > MIN_PERSONS and len(group) > MIN_PERSONS

    # Draw detections (red = in the close group, green = elsewhere)
    for idx, box in enumerate(person_detections):
        x1, y1, x2, y2 = map(int, box.xyxy[0])
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255) if idx in group else (0, 200, 0), 2)

    # Status overlay
    overlay = frame.copy()
    cv2.rectangle(overlay, (10, 10), (470, 100), (0, 0, 0), -1)
    cv2.addWeighted(frame, 0.7, overlay, 0.3, 0, frame)
    status_color = (0, 0, 255) if crowd_detected else (0, 255, 0)
    status_text = f"BIOMETRIC CROWD! ({person_count})" if crowd_detected else "All Clear"
    cv2.putText(frame, status_text, (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
    cv2.putText(frame, f"Biometric Area - Persons: {person_count} (close group: {len(group)})", (20, 62),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    cv2.putText(frame, f"{device_id} - Cam {camera_no}", (20, 87),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

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
                    folder="crowd_detection_biometric",
                )
            except Exception as e:
                logger.error(f"[{device_id}-{camera_no}] Biometric crowd DB reporting error: {e}")
            last_save_times[key] = now
            logger.warning(f"[{device_id}-{camera_no}] 🚨 Biometric Crowd detected: {filename}")

    return frame, crowd_detected, person_count

def detect_crowd_biometric_batch(items, results=None):
    """Run true batch inference for biometric area crowd detection."""
    if results is None:
        model = get_yolo_model()
        frames = [item['frame'].copy() for item in items]
        results = model(frames, conf=CONF_THRESHOLD, verbose=False)

    outputs = []
    for item, result in zip(items, results):
        frame = item['frame'].copy()
        person_boxes = [
            box for box in result.boxes
            if int(box.cls[0]) == 0 and float(box.conf[0]) >= CONF_THRESHOLD
        ]
        person_count = len(person_boxes)
        xyxy = [box.xyxy[0].cpu().numpy() for box in person_boxes]
        group = set(_largest_close_group(xyxy))
        crowd_detected = person_count > MIN_PERSONS and len(group) > MIN_PERSONS

        for idx, box in enumerate(person_boxes):
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255) if idx in group else (0, 200, 0), 2)

        ch_no = item.get('ch_no', item.get('camera_id', 'unknown'))

        # Status overlay
        overlay = frame.copy()
        cv2.rectangle(overlay, (10, 10), (470, 100), (0, 0, 0), -1)
        cv2.addWeighted(frame, 0.7, overlay, 0.3, 0, frame)
        status_color = (0, 0, 255) if crowd_detected else (0, 255, 0)
        status_text = f"BIOMETRIC CROWD! ({person_count})" if crowd_detected else "All Clear"
        cv2.putText(frame, status_text, (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
        cv2.putText(frame, f"Biometric - Persons: {person_count} (close group: {len(group)})", (20, 62),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.putText(frame, f"CH: {ch_no}", (20, 87),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

        outputs.append((frame, crowd_detected, person_count))

    return outputs
