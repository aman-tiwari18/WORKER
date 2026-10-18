import cv2
import numpy as np
import time
import os
import logging
import threading
import torch
from datetime import datetime
from collections import deque
from ultralytics import YOLO

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= CONFIG ==================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# UNIFIED_PERSON_MODEL=finetuned -> the fine-tuned person+monitor model (person = class 0)
from detection_models import person_model_path
MODEL_PATH = person_model_path(os.path.join(BASE_DIR, "models", "yolov10m.pt"))
SAVE_FOLDER = os.path.join(BASE_DIR, "results/blind_spot_detection")

# CAMERA CLASSIFICATION: Inside Lab
# This detection runs on cameras classified as:
# - Computer Lab / Inside Lab / Exam Lab / Inside Classroom
# Detects blind spots and obstructions in camera view

EDGE_OBSTRUCTION_THRESHOLD = 0.15
CONF_THRESHOLD = 0.40
# One camera never alerts more often than BLIND_SPOT_MIN_COOLDOWN (default 7 min), whatever the
# assignment's Frequency says: the rule is true for most lab cameras almost all the time.
COOLDOWN_TIME = max(int(os.environ.get("COOLDOWN_TIME", 180)), int(os.environ.get("BLIND_SPOT_MIN_COOLDOWN", "420")))

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
class BlindSpotDetector:
    def __init__(self, camera_id):
        self.camera_id = camera_id
        self.last_save_time = 0.0

    def analyze_frame(self, frame, person_boxes):
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        edge_size = int(min(h, w) * 0.1)

        edges = {
            "top":    gray[0:edge_size, :],
            "bottom": gray[h - edge_size:h, :],
            "left":   gray[:, 0:edge_size],
            "right":  gray[:, w - edge_size:w],
        }

        total_obstruction = 0.0
        annotated = frame.copy()

        for name, region in edges.items():
            mean = float(np.mean(region))
            std  = float(np.std(region))
            obstructed = mean < 30 or std < 15
            if obstructed:
                total_obstruction += 0.25
                if name == "top": cv2.rectangle(annotated, (0, 0), (w, edge_size), (0, 255, 0), 2)
                if name == "bottom": cv2.rectangle(annotated, (0, h - edge_size), (w, h), (0, 255, 0), 2)
                if name == "left": cv2.rectangle(annotated, (0, 0), (edge_size, h), (0, 255, 0), 2)
                if name == "right": cv2.rectangle(annotated, (w - edge_size, 0), (w, h), (0, 255, 0), 2)

        has_obstruction = total_obstruction >= EDGE_OBSTRUCTION_THRESHOLD
        
        # Check for partial persons near edges
        partial_count = 0
        margin = int(min(h, w) * 0.05)
        for box in person_boxes:
            x1, y1, x2, y2 = box
            is_near_edge = (x1 < margin or x2 > w - margin or y1 < margin or y2 > h - margin)
            if is_near_edge:
                partial_count += 1
                cv2.rectangle(annotated, (int(x1), int(y1)), (int(x2), int(y2)), (0, 0, 255), 2)

        violation = has_obstruction or partial_count > 0
        
        cv2.putText(annotated, f"Blind Spot: {total_obstruction*100:.0f}% | Partial: {partial_count}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255) if violation else (0, 255, 0), 2)
        
        return annotated, violation

# ================= SHARED STATE ============
detectors = {}
_state_lock = threading.Lock()

# ================= CORE LOGIC ============
def detect_blind_spot_batch(items, results=None):
    if results is None:
        model = get_yolo_model()
        frames = [item['frame'].copy() for item in items]
        results = model(frames, conf=CONF_THRESHOLD, verbose=False)

    outputs = []
    for item, result in zip(items, results):
        frame = item['frame'].copy()
        person_boxes = [box.xyxy[0].cpu().numpy() for box in result.boxes 
                        if int(box.cls[0]) == 0 and float(box.conf[0]) >= CONF_THRESHOLD]
        
        cam_id = item.get('device_id') or item.get('camera_id') or 'unknown'
        with _state_lock:
            if cam_id not in detectors:
                detectors[cam_id] = BlindSpotDetector(cam_id)
            detector = detectors[cam_id]
            
        annotated, violation = detector.analyze_frame(frame, person_boxes)
        outputs.append((annotated, violation, len(person_boxes)))
        
    return outputs
