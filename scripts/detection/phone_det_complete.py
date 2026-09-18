import cv2
import time
import os
_DET_GPU_ID = int(os.environ["WORKER_INDEX"]) if os.environ.get("WORKER_INDEX", "").strip().isdigit() else int("".join(c for c in os.environ.get("WORKER_ID", "main").split("-")[-1] if c.isdigit()) or "0")
import logging
import threading
import torch
import torchvision.transforms as transforms
import sys
import numpy as np
from torchvision import models
from ultralytics import YOLO
from PIL import Image
from datetime import datetime

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= CONFIG ==================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "models", "yolov10m.pt")
SAVE_FOLDER = os.path.join(BASE_DIR, "results/phone_detection")

# CAMERA CLASSIFICATION: Inside Lab, Inside Server Room
# This detection runs on cameras classified as:
# - Computer Lab / Inside Lab / Exam Lab / Inside Classroom
# - Server Room / Inside Server Room

COOLDOWN_TIME = int(os.environ.get("COOLDOWN_TIME", 90))        # Seconds between saves (1.5 min — per detection frequency spec)
CONF_THRESHOLD = 0.25     # Confidence for phone detection

os.makedirs(SAVE_FOLDER, exist_ok=True)

# ================= PLACES365 =================
_places_model = None
_places_classes = []

def load_places365():
    global _places_model, _places_classes
    if _places_model is not None:
        return _places_model, _places_classes

    logger.info("Loading Places365 model...")
    model = models.resnet18()
    num_ftrs = model.fc.in_features
    model.fc = torch.nn.Linear(num_ftrs, 365)

    try:
        checkpoint = torch.hub.load_state_dict_from_url(
            "http://places2.csail.mit.edu/models_places365/resnet18_places365.pth.tar",
            map_location=torch.device("cpu"),
            progress=True
        )
        state_dict = {k.replace("module.", ""): v for k, v in checkpoint["state_dict"].items()}
        model.load_state_dict(state_dict)
        model.eval()
        _places_model = model
        
        # Load categories
        cat_path = os.path.join(BASE_DIR, "categories_places365.txt")
        if os.path.exists(cat_path):
            with open(cat_path) as f:
                _places_classes = [line.strip().split(" ")[0][3:] for line in f]
        
        logger.info("Places365 model loaded successfully")
    except Exception as e:
        logger.warning(f"Places365 load failed: {e}")
        _places_model = None
    
    return _places_model, _places_classes

transform_places = transforms.Compose([
    transforms.Resize((256, 256)),
    transforms.CenterCrop(224),
    transforms.ToTensor(),
    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
])

def classify_scene(frame):
    model, classes = load_places365()
    if model is None or not classes:
        return ["scene_disabled"]

    try:
        img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        tensor = transform_places(img).unsqueeze(0).to(next(model.parameters()).device)

        with torch.no_grad():
            out = model(tensor)
            topk = torch.topk(out, 3)
            return [classes[i] for i in topk.indices[0]]
    except Exception as e:
        logger.error(f"Scene classification error: {e}")
        return ["error"]

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
def detect_phone_in_frame(frame, device_id="unknown", camera_no="unknown", script_name="phone"):
    """Standalone detection for a single frame."""
    model = get_yolo_model()
    results = model(frame, conf=CONF_THRESHOLD, verbose=False)[0]

    names = results.names
    phone_boxes = [
        box for box in results.boxes
        if names[int(box.cls[0])] == "cell phone" and box.conf[0] >= CONF_THRESHOLD
    ]

    phone_count = len(phone_boxes)
    phone_detected = phone_count > 0

    for box in phone_boxes:
        x1, y1, x2, y2 = map(int, box.xyxy[0])
        conf = float(box.conf[0])
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
        cv2.putText(frame, f"Phone {conf:.2f}", (x1, max(y1 - 8, 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

    # Only run expensive CPU scene classification when a phone is actually detected
    scene_labels = classify_scene(frame) if phone_detected else ["--"]

    # Status overlay
    overlay = frame.copy()
    cv2.rectangle(overlay, (10, 10), (480, 110), (0, 0, 0), -1)
    cv2.addWeighted(frame, 0.7, overlay, 0.3, 0, frame)
    status_color = (0, 0, 255) if phone_detected else (0, 255, 0)
    status_text = f"PHONE DETECTED! ({phone_count})" if phone_detected else "All Clear"
    cv2.putText(frame, status_text, (20, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
    cv2.putText(frame, f"Scene: {', '.join(scene_labels[:2])}", (20, 65),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
    cv2.putText(frame, f"{device_id} - Cam {camera_no}", (20, 90),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)

    key = f"{device_id}_{camera_no}"
    now = time.time()

    with _state_lock:
        time_elapsed = now - last_save_times.get(key, 0)
        if phone_detected and time_elapsed >= COOLDOWN_TIME:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            import uuid
            uid = str(uuid.uuid4())
            fname = f"{script_name}_{device_id}_{camera_no}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"
            filename = os.path.join(SAVE_FOLDER, fname)
            cv2.imwrite(filename, frame)
            last_save_times[key] = now
            logger.warning(f"[{device_id}-{camera_no}] 🚨 Phone detected: {filename}")

    return frame, phone_detected, phone_count

def detect_phone_batch(items, results=None):
    """Run true batch inference."""
    if results is None:
        model = get_yolo_model()
        frames = [item['frame'].copy() for item in items]
        results = model(frames, conf=CONF_THRESHOLD, verbose=False)

    outputs = []
    for item, result in zip(items, results):
        frame = item['frame'].copy()
        names = result.names
        phone_boxes = [
            box for box in result.boxes
            if names[int(box.cls[0])] == "cell phone" and float(box.conf[0]) >= CONF_THRESHOLD
        ]
        phone_count = len(phone_boxes)
        phone_detected = phone_count > 0

        for box in phone_boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            conf = float(box.conf[0])
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
            cv2.putText(frame, f"Phone {conf:.2f}", (x1, max(y1 - 8, 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

        # Only run expensive CPU scene classification when a phone is actually detected
        scene_labels = classify_scene(frame) if phone_detected else ["--"]
        ch_no = item.get('ch_no', item.get('camera_id', 'unknown'))

        # Status overlay
        overlay = frame.copy()
        cv2.rectangle(overlay, (10, 10), (480, 110), (0, 0, 0), -1)
        cv2.addWeighted(frame, 0.7, overlay, 0.3, 0, frame)
        status_color = (0, 0, 255) if phone_detected else (0, 255, 0)
        status_text = f"PHONE DETECTED! ({phone_count})" if phone_detected else "All Clear"
        cv2.putText(frame, status_text, (20, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
        cv2.putText(frame, f"Scene: {', '.join(scene_labels[:2])}", (20, 65),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
        cv2.putText(frame, f"CH: {ch_no}", (20, 90),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)

        outputs.append((frame, phone_detected, phone_count))

    return outputs
