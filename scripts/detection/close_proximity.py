import cv2
import time
import os
_DET_GPU_ID = int(os.environ["WORKER_INDEX"]) if os.environ.get("WORKER_INDEX", "").strip().isdigit() else int("".join(c for c in os.environ.get("WORKER_ID", "main").split("-")[-1] if c.isdigit()) or "0")
import logging
import sys
import threading
import torch
import math
from datetime import datetime
from ultralytics import YOLO

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= CONFIG ==================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "models", "yolov10m.pt")
SAVE_FOLDER = os.path.join(BASE_DIR, "results/proximity_detection")

# CAMERA CLASSIFICATION: Inside Lab
# This detection runs on cameras classified as:
# - Computer Lab / Inside Lab / Exam Lab / Inside Classroom

COOLDOWN_TIME       = int(os.environ.get("COOLDOWN_TIME", 120))     # Seconds between saves (2 min — per detection frequency spec)
PROXIMITY_THRESHOLD = 30      # Pixels — small absolute safety floor only, to stop the scaled
                              # threshold collapsing to near-zero on tiny/far boxes. NOT a flat
                              # "close" distance — a 100px gap between small background figures
                              # is a much larger real-world gap than 100px between large
                              # near-camera figures (perspective), so a big flat floor here was
                              # wrongly flagging aisles-apart pairs in the back rows as violations.
PROXIMITY_SCALE     = 0.50    # Adaptive: effective_threshold = max(PROXIMITY_THRESHOLD, avg_box_height * PROXIMITY_SCALE)
CONF_THRESHOLD      = 0.55    # Raised from 0.45 — drops weak phantom detections (chairs/bags usually land at conf 0.50–0.70; real students ≥ 0.70)
MIN_BOX_AREA        = 1500    # ~40×38 px — drops unreliable tiny boxes (matches crowd_zero_det.py convention)
MIN_ASPECT_RATIO    = 1.30    # h/w ≥ this — kills chair/bag/monitor shapes. Sitting students at cubicles still pass (h/w typically ≥ 1.4)
IOU_NMS             = 0.40    # Tighter NMS than Ultralytics default 0.7 — collapses duplicate detections of the same person
DUPLICATE_IOU       = 0.30    # Pre-pair dedup: equal-size duplicates with IoU>this are merged
DUPLICATE_IOMIN     = 0.70    # Pre-pair dedup: catches small-box-inside-large-box (e.g. head-only inside head+shoulders) where IoU stays low due to size disparity
MIN_PAIR_DISTANCE   = 20      # Geometric floor: real adjacent people can't have foot-centers within this many pixels — anything below is residual same-person noise
Y_ROW_FLOOR         = 40      # Depth-row guard floor: minimum absolute y-diff before two persons are considered to be in different rows
Y_ROW_RATIO         = 0.30    # Adaptive depth-row threshold: y_diff > avg_bbox_height × this → different rows / different depths (2D pixel distance is misleading due to perspective flattening)

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

# ================= UTILS ===================
def get_box_bottom_center(box):
    return ((box[0] + box[2]) / 2, box[3])

def calculate_bottom_distance(box1, box2):
    x1, y1 = get_box_bottom_center(box1)
    x2, y2 = get_box_bottom_center(box2)
    return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)

def _box_overlap_metrics(a, b):
    """Return (IoU, IoMin) for two [x1,y1,x2,y2] boxes.

    IoMin = inter / min(area_a, area_b) — equals 1.0 when one box is fully
    contained inside the other regardless of size disparity. Used to catch
    the head-only-inside-head+shoulders case where IoU stays low due to
    the size gap but containment is near-complete.
    """
    inter_x1 = max(a[0], b[0]); inter_y1 = max(a[1], b[1])
    inter_x2 = min(a[2], b[2]); inter_y2 = min(a[3], b[3])
    inter = max(0.0, inter_x2 - inter_x1) * max(0.0, inter_y2 - inter_y1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    smaller = min(area_a, area_b)
    iou = inter / union if union > 0 else 0.0
    iomin = inter / smaller if smaller > 0 else 0.0
    return iou, iomin

def _dedup_person_boxes(boxes):
    """Drop same-person duplicate detections.

    Greedy: iterate boxes in descending area order. Keep the first (largest)
    box, then for each subsequent box, drop it if it overlaps heavily with
    any already-kept box by EITHER IoU (similar-size dups) or IoMin (small
    box contained inside a larger kept box).

    Preferring larger boxes is the right policy because YOLO's larger box
    usually covers the actual person while smaller overlapping boxes tend
    to be partial detections of the same person.
    """
    if len(boxes) <= 1:
        return list(boxes)
    indexed = sorted(
        boxes,
        key=lambda b: -((b[2] - b[0]) * (b[3] - b[1])),
    )
    kept = []
    for box in indexed:
        is_dup = False
        for kept_box in kept:
            iou, iomin = _box_overlap_metrics(box, kept_box)
            if iou > DUPLICATE_IOU or iomin > DUPLICATE_IOMIN:
                is_dup = True
                break
        if not is_dup:
            kept.append(box)
    return kept

def _annotate_violations(frame, person_boxes, violations):
    """Draw bounding boxes around violating persons, lines between them, and distance labels."""
    violating_indices = {idx for i, j, _ in violations for idx in (i, j)}

    for idx in violating_indices:
        box = person_boxes[idx]
        x1, y1, x2, y2 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
        cv2.putText(frame, f"P{idx + 1}", (x1, max(y1 - 8, 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

    for i, j, d in violations:
        c1 = get_box_bottom_center(person_boxes[i])
        c2 = get_box_bottom_center(person_boxes[j])
        cv2.line(frame,
                 (int(c1[0]), int(c1[1])),
                 (int(c2[0]), int(c2[1])),
                 (0, 0, 255), 2)
        mid_x = int((c1[0] + c2[0]) / 2)
        mid_y = int((c1[1] + c2[1]) / 2)
        cv2.putText(frame, f"{d:.1f}px", (mid_x, mid_y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)

# ================= STATE ===================
last_save_times = {}
_state_lock = threading.Lock()

# =====================================================
# CORE DETECTION LOGIC
# =====================================================
def detect_proximity_in_frame(frame, device_id="unknown", camera_no="unknown", script_name="proximity"):
    """Standalone detection for a single frame."""
    model = get_yolo_model()
    results = model(frame, conf=CONF_THRESHOLD, iou=IOU_NMS, verbose=False)[0]

    person_boxes = []
    for box in results.boxes:
        if int(box.cls[0]) == 0 and box.conf[0] >= CONF_THRESHOLD:
            xyxy = box.xyxy[0].cpu().numpy()
            w = xyxy[2] - xyxy[0]
            h = xyxy[3] - xyxy[1]
            # Shape filter: real persons are taller than wide.
            # Chairs/bags/monitors are roughly square or wider → drop them here.
            if w * h >= MIN_BOX_AREA and h >= w * MIN_ASPECT_RATIO:
                person_boxes.append(xyxy)

    # Same-person dedup: handles both equal-size duplicates (IoU) and
    # small-box-inside-large-box duplicates (IoMin). Done once before pairing
    # so the violation indices and "Persons: N" overlay both reflect the
    # cleaned-up box list.
    person_boxes = _dedup_person_boxes(person_boxes)

    violations = []
    for i in range(len(person_boxes)):
        for j in range(i + 1, len(person_boxes)):
            d = calculate_bottom_distance(person_boxes[i], person_boxes[j])
            # Geometric impossibility: two real adjacent people cannot have
            # foot-centers within MIN_PAIR_DISTANCE pixels of each other.
            # Anything below this is residual same-person noise.
            if d < MIN_PAIR_DISTANCE:
                continue
            h_i = person_boxes[i][3] - person_boxes[i][1]
            h_j = person_boxes[j][3] - person_boxes[j][1]
            # Depth-row guard: in a tilted CCTV view, bottom-y position is a
            # perspective-free depth indicator. If the two persons' foot-y values
            # differ significantly relative to their average bbox height, they're
            # in different rows (different depths) — 2D pixel distance is misleading.
            y_diff = abs(person_boxes[i][3] - person_boxes[j][3])
            if y_diff > max(Y_ROW_FLOOR, ((h_i + h_j) / 2) * Y_ROW_RATIO):
                continue
            eff_threshold = max(PROXIMITY_THRESHOLD, ((h_i + h_j) / 2) * PROXIMITY_SCALE)
            if d < eff_threshold:
                violations.append((i, j, d))

    violation_detected = len(violations) > 0
    _annotate_violations(frame, person_boxes, violations)

    # Status overlay
    overlay = frame.copy()
    cv2.rectangle(overlay, (10, 10), (420, 100), (0, 0, 0), -1)
    cv2.addWeighted(frame, 0.7, overlay, 0.3, 0, frame)
    status_color = (0, 0, 255) if violation_detected else (0, 255, 0)
    status_text = "PROXIMITY VIOLATION!" if violation_detected else "All Clear"
    cv2.putText(frame, status_text, (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
    cv2.putText(frame, f"Persons: {len(person_boxes)}  Violations: {len(violations)}", (20, 60),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    cv2.putText(frame, f"{device_id} - Cam {camera_no}", (20, 85),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

    key = f"{device_id}_{camera_no}"
    now = time.time()

    with _state_lock:
        time_elapsed = now - last_save_times.get(key, 0)
        if violation_detected and time_elapsed >= COOLDOWN_TIME:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            import uuid
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
                    folder="proximity_detection",
                )
            except Exception as e:
                logger.error(f"[{device_id}-{camera_no}] Proximity DB reporting error: {e}")
            last_save_times[key] = now
            logger.warning(f"[{device_id}-{camera_no}] 🚨 Proximity violation detected: {filename}")

    return frame, violation_detected, len(violations)

def detect_proximity_batch(items, results=None):
    """Run true batch inference."""
    if results is None:
        model = get_yolo_model()
        frames = [item['frame'].copy() for item in items]
        results = model(frames, conf=CONF_THRESHOLD, iou=IOU_NMS, verbose=False)

    outputs = []
    for item, result in zip(items, results):
        frame = item['frame'].copy()
        person_boxes = []
        for box in result.boxes:
            if int(box.cls[0]) == 0 and float(box.conf[0]) >= CONF_THRESHOLD:
                xyxy = box.xyxy[0].cpu().numpy()
                w = xyxy[2] - xyxy[0]
                h = xyxy[3] - xyxy[1]
                # Shape filter: real persons are taller than wide.
                # Chairs/bags/monitors are roughly square or wider → drop them here.
                if w * h >= MIN_BOX_AREA and h >= w * MIN_ASPECT_RATIO:
                    person_boxes.append(xyxy)

        # Same-person dedup: handles both equal-size duplicates (IoU) and
        # small-box-inside-large-box duplicates (IoMin). Done once before pairing
        # so the violation indices and "Persons: N" overlay both reflect the
        # cleaned-up box list.
        person_boxes = _dedup_person_boxes(person_boxes)

        violations = []
        for i in range(len(person_boxes)):
            for j in range(i + 1, len(person_boxes)):
                d = calculate_bottom_distance(person_boxes[i], person_boxes[j])
                # Geometric impossibility: two real adjacent people cannot have
                # foot-centers within MIN_PAIR_DISTANCE pixels of each other.
                # Anything below this is residual same-person noise.
                if d < MIN_PAIR_DISTANCE:
                    continue
                h_i = person_boxes[i][3] - person_boxes[i][1]
                h_j = person_boxes[j][3] - person_boxes[j][1]
                # Depth-row guard: in a tilted CCTV view, bottom-y position is a
                # perspective-free depth indicator. If the two persons' foot-y values
                # differ significantly relative to their average bbox height, they're
                # in different rows (different depths) — 2D pixel distance is misleading.
                y_diff = abs(person_boxes[i][3] - person_boxes[j][3])
                if y_diff > max(Y_ROW_FLOOR, ((h_i + h_j) / 2) * Y_ROW_RATIO):
                    continue
                eff_threshold = max(PROXIMITY_THRESHOLD, ((h_i + h_j) / 2) * PROXIMITY_SCALE)
                if d < eff_threshold:
                    violations.append((i, j, d))

        violation_detected = len(violations) > 0
        _annotate_violations(frame, person_boxes, violations)

        ch_no = item.get('ch_no', item.get('camera_id', 'unknown'))

        # Status overlay
        overlay = frame.copy()
        cv2.rectangle(overlay, (10, 10), (420, 100), (0, 0, 0), -1)
        cv2.addWeighted(frame, 0.7, overlay, 0.3, 0, frame)
        status_color = (0, 0, 255) if violation_detected else (0, 255, 0)
        status_text = "PROXIMITY VIOLATION!" if violation_detected else "All Clear"
        cv2.putText(frame, status_text, (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
        cv2.putText(frame, f"Persons: {len(person_boxes)}  Violations: {len(violations)}", (20, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.putText(frame, f"CH: {ch_no}", (20, 85),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

        outputs.append((frame, violation_detected, len(violations)))

    return outputs
