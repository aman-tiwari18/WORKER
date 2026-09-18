"""
Improper Furniture Detection — core logic.

Flags an exam hall whose furniture is not fit for the exam:

  * overturned : a chair lying on its side / knocked over. An upright chair's
                 box is taller than it is wide; a toppled one is wider than it
                 is tall, so the box aspect ratio separates the two cleanly.
  * broken     : a chair whose box is far too small or too squat to be a whole
                 chair — a collapsed or partial piece of furniture.
  * missing    : the hall suddenly has far fewer seats than it normally shows.
                 Each camera builds its own baseline from the frames it has
                 already seen (median of recent counts), so no per-centre
                 configuration is needed; a drop below MISSING_RATIO of that
                 baseline is reported.

CAMERA CLASSIFICATION: Inside Lab
Runs on cameras classified as Computer Lab / Inside Classroom / Exam Lab.

Baselines and cooldowns are per camera. The worker owns the cooldown; this
module only decides whether the current frame is a violation.
"""

import logging
import os
import threading
from collections import deque

import cv2
import torch
from ultralytics import YOLO

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= CONFIG ==================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "models", "yolov10m.pt")
SAVE_FOLDER = os.path.join(BASE_DIR, "results/improper_furniture_detection")

CONF_THRESHOLD = float(os.environ.get("FURNITURE_CONF", "0.35"))

# COCO ids. Exam desks are usually detected as "dining table"; long shared desks
# come through as "bench".
CHAIR_CLASS_ID = 56
TABLE_CLASS_IDS = {60, 13}          # dining table, bench
FURNITURE_CLASS_IDS = {CHAIR_CLASS_ID} | TABLE_CLASS_IDS
CLASS_LABELS = {56: "chair", 60: "desk", 13: "bench"}

# A standing chair is taller than wide (w/h well below 1). Anything this wide is
# lying down. Kept generous so a chair seen from an angle is not flagged.
OVERTURNED_RATIO = float(os.environ.get("FURNITURE_OVERTURNED_RATIO", "1.6"))

# A chair box shorter than this fraction of the median chair height in the same
# frame is a collapsed / partial piece rather than a whole chair.
BROKEN_HEIGHT_RATIO = float(os.environ.get("FURNITURE_BROKEN_RATIO", "0.45"))
MIN_CHAIRS_FOR_BROKEN = 4           # need a few chairs before "too small" means anything

# Missing-seat baseline
BASELINE_FRAMES = int(os.environ.get("FURNITURE_BASELINE_FRAMES", "15"))
MIN_BASELINE_COUNT = int(os.environ.get("FURNITURE_MIN_BASELINE", "4"))
MISSING_RATIO = float(os.environ.get("FURNITURE_MISSING_RATIO", "0.6"))

COOLDOWN_TIME = int(os.environ.get("COOLDOWN_TIME", 300))

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


# ================= DETECTOR STATE ============
class FurnitureDetector:
    """Per-camera state: the rolling seat-count baseline for this view."""

    def __init__(self, camera_id):
        self.camera_id = camera_id
        self.counts = deque(maxlen=BASELINE_FRAMES)

    def baseline(self):
        """Median of the seat counts seen so far, or None while warming up."""
        if len(self.counts) < BASELINE_FRAMES:
            return None
        ordered = sorted(self.counts)
        return ordered[len(ordered) // 2]

    def observe(self, seat_count):
        self.counts.append(seat_count)


detectors = {}
_state_lock = threading.Lock()


def _get_detector(cam_id):
    with _state_lock:
        if cam_id not in detectors:
            detectors[cam_id] = FurnitureDetector(cam_id)
        return detectors[cam_id]


# ================= CORE LOGIC ============
def _analyse_boxes(boxes):
    """
    boxes: list of (class_id, x1, y1, x2, y2)
    Returns (issues, seat_count) where issues is a list of
    (reason, box, label) for every piece of furniture that looks wrong.
    """
    issues = []
    chairs = [b for b in boxes if b[0] == CHAIR_CLASS_ID]
    seat_count = len(chairs)

    chair_heights = sorted((b[4] - b[2]) for b in chairs)
    median_height = chair_heights[len(chair_heights) // 2] if chair_heights else 0

    for cls_id, x1, y1, x2, y2 in chairs:
        width = max(x2 - x1, 1)
        height = max(y2 - y1, 1)

        if width / height >= OVERTURNED_RATIO:
            issues.append(("overturned", (x1, y1, x2, y2), "chair"))
            continue

        if (len(chairs) >= MIN_CHAIRS_FOR_BROKEN and median_height > 0
                and height < median_height * BROKEN_HEIGHT_RATIO):
            issues.append(("broken", (x1, y1, x2, y2), "chair"))

    return issues, seat_count


def _annotate(frame, boxes, issues, note):
    """Draw every piece of furniture, highlight the offending ones, add a banner."""
    flagged = {(int(b[0]), int(b[1]), int(b[2]), int(b[3])) for _, b, _ in issues}

    for cls_id, x1, y1, x2, y2 in boxes:
        key = (int(x1), int(y1), int(x2), int(y2))
        is_bad = key in flagged
        color = (0, 0, 255) if is_bad else (0, 200, 0)
        cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
        if not is_bad:
            cv2.putText(frame, CLASS_LABELS.get(cls_id, "furniture"),
                        (int(x1), max(int(y1) - 6, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)

    for reason, (x1, y1, x2, y2), label in issues:
        cv2.putText(frame, f"{label}: {reason}", (int(x1), max(int(y1) - 6, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)

    if note:
        cv2.rectangle(frame, (0, 0), (frame.shape[1], 34), (0, 0, 0), -1)
        cv2.putText(frame, note, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    return frame


def detect_improper_furniture_batch(items, results=None):
    """
    Batch entry point, mirroring the other analytics' core modules.

    items   : list of dicts each holding a 'frame'
    results : optional pre-computed YOLO results (shared inference)

    Returns a list of (annotated_frame, violation, details) — details is a dict
    with the reasons found, the seat count and the camera's baseline.
    """
    if results is None:
        model = get_yolo_model()
        frames = [item['frame'].copy() for item in items]
        results = model(frames, conf=CONF_THRESHOLD, verbose=False)

    outputs = []
    for item, result in zip(items, results):
        frame = item['frame'].copy()

        boxes = []
        for box in result.boxes:
            cls_id = int(box.cls[0])
            if cls_id not in FURNITURE_CLASS_IDS:
                continue
            if float(box.conf[0]) < CONF_THRESHOLD:
                continue
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
            boxes.append((cls_id, float(x1), float(y1), float(x2), float(y2)))

        issues, seat_count = _analyse_boxes(boxes)

        cam_id = item.get('device_id') or item.get('camera_id') or 'unknown'
        detector = _get_detector(cam_id)
        baseline = detector.baseline()
        detector.observe(seat_count)

        note = ""
        reasons = sorted({reason for reason, _, _ in issues})

        # Missing seats: judged against this camera's own established baseline.
        if (baseline is not None and baseline >= MIN_BASELINE_COUNT
                and seat_count < baseline * MISSING_RATIO):
            reasons.append("missing")
            note = f"MISSING FURNITURE: {seat_count} seats, usually {baseline}"

        violation = bool(reasons)
        if violation and not note:
            note = "IMPROPER FURNITURE: " + ", ".join(reasons)

        annotated = _annotate(frame, boxes, issues, note) if violation else frame

        outputs.append((annotated, violation, {
            "reasons": reasons,
            "seat_count": seat_count,
            "baseline": baseline,
            "furniture_count": len(boxes),
        }))

    return outputs
