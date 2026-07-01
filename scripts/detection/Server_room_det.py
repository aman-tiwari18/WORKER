import cv2
import time
import os
_DET_GPU_ID = int(os.environ["WORKER_INDEX"]) if os.environ.get("WORKER_INDEX", "").strip().isdigit() else int("".join(c for c in os.environ.get("WORKER_ID", "main").split("-")[-1] if c.isdigit()) or "0")
import logging
import sys
import threading
import torch
from datetime import datetime
from ultralytics import YOLO

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= CONFIG ==================
# PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
MODEL_PATH = os.path.join(PROJECT_ROOT, "models", "yolov10m.pt")
SAVE_FOLDER = os.path.join(PROJECT_ROOT, "results/tcs_detections/server_room_detection")

# CAMERA CLASSIFICATION: Server Room
# This detection runs on cameras classified as:
# - Server Room / Strong Room

COOLDOWN_TIME = 60        # Seconds between saves per camera (1 minute - reduced for faster alerts)
CONF_THRESHOLD = 0.50     # Confidence for person detection (increased for accuracy)

# Per-TC-code thresholds loaded from server_room_constants.py
from server_room_constants import TC_CODE_THRESHOLDS, get_min_persons, DEFAULT_MIN_PERSONS as MIN_PERSONS

os.makedirs(SAVE_FOLDER, exist_ok=True)

# ================= YOLO ====================
yolo_model = None
_model_lock = threading.Lock()

def get_yolo_model():
    """Return a shared YOLO model instance for worker batch inference."""
    global yolo_model
    if yolo_model is None:
        with _model_lock:
            if yolo_model is None:
                logger.info(f"Loading YOLO model from {MODEL_PATH}...")
                yolo_model = YOLO(MODEL_PATH)
                if torch.cuda.is_available():
                    yolo_model.to('cuda')
                    logger.info("YOLO model moved to CUDA")
                else:
                    logger.info("YOLO model running on CPU")
    return yolo_model

import uuid

# ================= STATE ===================
last_save_times = {}
_state_lock = threading.Lock()

# =====================================================
# CORE DETECTION LOGIC
# =====================================================
def detect_persons_in_frame(frame, device_id="unknown", camera_no="unknown", script_name="server"):
    """Standalone detection for a single frame."""
    model = get_yolo_model()
    results = model(frame, conf=CONF_THRESHOLD, verbose=False)[0]

    person_detections = [
        box for box in results.boxes
        if int(box.cls[0]) == 0 and box.conf[0] >= CONF_THRESHOLD
    ]

    person_count = len(person_detections)

    # Draw detections
    for box in person_detections:
        x1, y1, x2, y2 = map(int, box.xyxy[0])
        conf = float(box.conf[0])
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
        cv2.putText(frame, f"Person {conf:.2f}", (x1, y1 - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

    server_detected = person_count >= MIN_PERSONS

    # Status overlay
    overlay = frame.copy()
    cv2.rectangle(overlay, (10, 10), (460, 100), (0, 0, 0), -1)
    cv2.addWeighted(frame, 0.7, overlay, 0.3, 0, frame)
    status_color = (0, 0, 255) if server_detected else (0, 255, 0)
    status_text = f"SERVER ROOM BREACH! ({person_count})" if server_detected else "All Clear"
    cv2.putText(frame, status_text, (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
    cv2.putText(frame, f"Persons: {person_count}", (20, 62),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    cv2.putText(frame, f"{device_id} - Cam {camera_no}", (20, 87),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

    key = f"{device_id}_{camera_no}"
    now = time.time()

    with _state_lock:
        time_elapsed = now - last_save_times.get(key, 0)
        if server_detected and time_elapsed >= COOLDOWN_TIME:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            uid = str(uuid.uuid4())
            fname = f"{script_name}_{device_id}_{camera_no}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"
            filename = os.path.join(SAVE_FOLDER, fname)
            cv2.imwrite(filename, frame)
            last_save_times[key] = now
            logger.warning(f"[{device_id}-{camera_no}] 🚨 Server room breach (person detected): {filename}")

    return frame, server_detected, person_count

def detect_persons_batch(items, results=None):
    """
    Run true batch inference for server-room detection.
    Used by workers for high-throughput processing.
    Returns: List[(annotated_frame, server_detected, person_count)]
    """
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
        tc_code = item.get('tc_code', '')
        min_persons = get_min_persons(tc_code)
        server_detected = person_count >= min_persons

        for box in person_boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            conf = float(box.conf[0])
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
            cv2.putText(frame, f"Person {conf:.2f}", (x1, y1 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

        ch_no = item.get('ch_no', item.get('camera_id', 'unknown'))

        # Status overlay
        overlay = frame.copy()
        cv2.rectangle(overlay, (10, 10), (460, 100), (0, 0, 0), -1)
        cv2.addWeighted(frame, 0.7, overlay, 0.3, 0, frame)
        status_color = (0, 0, 255) if server_detected else (0, 255, 0)
        status_text = f"SERVER ROOM BREACH! ({person_count}/{min_persons})" if server_detected else "All Clear"
        cv2.putText(frame, status_text, (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
        cv2.putText(frame, f"Persons: {person_count}", (20, 62),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.putText(frame, f"CH: {ch_no}", (20, 87),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

        outputs.append((frame, server_detected, person_count))

    return outputs

# =====================================================
# DAV PROCESSOR (for local testing)
# =====================================================
def process_dav_video(video_path):
    cap = cv2.VideoCapture(video_path, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        logger.error(f"❌ Failed to open video: {video_path}")
        return

    frame_count = 0
    logger.info(f"Starting Server Room Detection on: {video_path}")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_count += 1
        if frame_count % 5 != 0:
            continue

        frame = cv2.resize(frame, (640, 360))
        processed, detected, count = detect_persons_in_frame(frame, "FILE", "LOCAL")

        if frame_count % 50 == 0:
            if detected:
                logger.warning(f"🚨 Server room breach! Count={count}")
            else:
                logger.info("Monitoring... No breach")

        cv2.imshow("Server Room Detection", processed)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        process_dav_video(sys.argv[1])
    else:
        print("❌ Usage: python3 Server_room_det.py <video_file>")
