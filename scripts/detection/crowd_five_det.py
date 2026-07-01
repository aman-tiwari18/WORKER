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
from collections import defaultdict
from ultralytics import YOLO

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= CONFIG ==================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "models", "yolov10m.pt")
SAVE_FOLDER = os.path.join(BASE_DIR, "results/crowd_detection_threshold")

# CAMERA CLASSIFICATION: Inside Lab
# This detection runs on cameras classified as:
# - Computer Lab / Inside Lab / Exam Lab / Inside Classroom
# Detects when less than 5 candidates are present in the room.
# Fires even in empty rooms (0 < smoothed_count < 5) — an empty exam room
# during a scheduled exam window is exactly the signal we want operators to see.

# --- Threshold ---
COOLDOWN_TIME = 120              # G9: seconds between saves per camera
MAX_PERSONS   = 5                # Violation when 0 < smoothed_count < MAX_PERSONS

# G2: Confidence (raised from 0.35 — low CONF was inflating count via ghost detections
# (jackets/shadows/monitors), masking real low-occupancy events. Parity with close_proximity).
CONF_THRESHOLD = 0.45

# G3: Box size — resolution-scaled with absolute floor + ceiling.
MIN_BOX_AREA_RATIO = 0.0006      # >= 0.06% of frame area
MIN_BOX_AREA_ABS   = 600         # ... AND >= 600 px² absolute (rejects sub-resolution shadow noise)
MAX_BOX_AREA_RATIO = 0.45        # Reject single boxes > 45% of frame (operator at lens, not candidate)

# G4: Aspect-ratio sanity. Looser than invigilator's standing-only gate — we
# still want to count seated candidates here. Goal: reject obviously-not-a-person shapes.
ASPECT_W_OVER_H_MAX = 1.20       # Reject very horizontal boxes (furniture mis-classified)
ASPECT_W_OVER_H_MIN = 0.25       # Reject impossibly thin verticals

# G5: Edge handling — partial person at frame border counts as EDGE_WEIGHT (not 1.0)
# so a person walking in/out isn't over-credited and the count is stable at doorways.
EDGE_MARGIN_PX = 8
EDGE_WEIGHT    = 0.5

# G6: Secondary IoMin dedup on top of YOLO's NMS — catches double-counts where
# YOLO emits head + torso boxes for the same seated candidate.
IOMIN_DEDUP_THRESHOLD = 0.70

# G1: Frame-quality gates (applied in worker before GPU inference).
FRAME_BLACK_MEAN  = 15           # frame.mean() below this AND...
FRAME_BLACK_STD   = 5            # ... frame.std() below this  →  "black"
FRAME_BLUR_LAPVAR = 60           # Laplacian variance below this  →  "too blurry"

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
# GATE HELPERS
# =====================================================
def _is_frame_corrupt(frame, prev_hash):
    """G1: returns ('black' | 'blur' | 'frozen' | None, new_hash).

    Defends against camera glitches / PTZ pans that produce a degraded frame —
    YOLO then finds 0–few "persons" (often ghosts) and would falsely fire crowd_5.
    Legitimately-empty exam rooms still pass this gate (the frame is well-exposed,
    sharp, and changing between captures), so this does NOT mask the empty-room case.
    """
    if frame is None or getattr(frame, 'size', 0) == 0:
        return 'black', prev_hash
    if frame.mean() < FRAME_BLACK_MEAN and frame.std() < FRAME_BLACK_STD:
        return 'black', prev_hash
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if cv2.Laplacian(gray, cv2.CV_64F).var() < FRAME_BLUR_LAPVAR:
        return 'blur', prev_hash
    small = cv2.resize(gray, (16, 16))
    h = hash(small.tobytes())
    if prev_hash is not None and h == prev_hash:
        return 'frozen', h
    return None, h


def _box_weight(xyxy, frame_w, frame_h):
    """G3 + G4 + G5: returns weighted contribution (0.0 reject, EDGE_WEIGHT edge, 1.0 full)."""
    x1, y1, x2, y2 = xyxy
    w = x2 - x1
    h = y2 - y1
    if w <= 0 or h <= 0:
        return 0.0, 'shape_reject'
    area = w * h
    area_ratio = area / max(frame_w * frame_h, 1)
    # G3: size
    if area < MIN_BOX_AREA_ABS or area_ratio < MIN_BOX_AREA_RATIO:
        return 0.0, 'size_reject'
    if area_ratio > MAX_BOX_AREA_RATIO:
        return 0.0, 'size_reject'
    # G4: aspect
    woh = w / h
    if woh > ASPECT_W_OVER_H_MAX or woh < ASPECT_W_OVER_H_MIN:
        return 0.0, 'aspect_reject'
    # G5: edge — partial person at frame edge counted at EDGE_WEIGHT
    if (x1 <= EDGE_MARGIN_PX or y1 <= EDGE_MARGIN_PX or
        x2 >= frame_w - EDGE_MARGIN_PX or y2 >= frame_h - EDGE_MARGIN_PX):
        return EDGE_WEIGHT, 'edge'
    return 1.0, None


def _iomin_dedup(detections):
    """G6: drop lower-conf overlapping boxes (IoMin > threshold).

    detections: list of (xyxy_tuple, conf, weight). Returns deduped list.
    Defends against YOLO emitting two boxes (head + torso) for one seated person.
    """
    if len(detections) <= 1:
        return detections
    ordered = sorted(detections, key=lambda d: d[1], reverse=True)
    kept = []
    for cand in ordered:
        (cx1, cy1, cx2, cy2), _c, _w = cand
        cand_area = max((cx2 - cx1) * (cy2 - cy1), 1)
        drop = False
        for keep in kept:
            (kx1, ky1, kx2, ky2), _kc, _kw = keep
            ix1 = max(cx1, kx1); iy1 = max(cy1, ky1)
            ix2 = min(cx2, kx2); iy2 = min(cy2, ky2)
            iw = max(ix2 - ix1, 0); ih = max(iy2 - iy1, 0)
            inter = iw * ih
            if inter == 0:
                continue
            keep_area = max((kx2 - kx1) * (ky2 - ky1), 1)
            iomin = inter / min(cand_area, keep_area)
            if iomin > IOMIN_DEDUP_THRESHOLD:
                drop = True
                break
        if not drop:
            kept.append(cand)
    return kept


def _apply_gate_chain(yolo_boxes, frame_w, frame_h, telemetry):
    """Walk G2 → G3 → G4 → G5 → G6 over YOLO boxes for one frame.

    Returns (kept_detections, effective_count) where kept_detections is a list of
    (xyxy_tuple, conf, weight). effective_count is a float (sum of weights).
    """
    candidates = []
    for box in yolo_boxes:
        cls = int(box.cls[0])
        if cls != 0:
            continue
        conf = float(box.conf[0])
        # G2: confidence
        if conf < CONF_THRESHOLD:
            telemetry['conf_reject'] += 1
            continue
        xyxy = box.xyxy[0].cpu().numpy().tolist()
        weight, reject_reason = _box_weight(xyxy, frame_w, frame_h)
        if weight == 0.0:
            telemetry[reject_reason] += 1
            continue
        if weight < 1.0:
            telemetry['edge'] += 1
        candidates.append((tuple(xyxy), conf, weight))

    # G6: dedup
    before = len(candidates)
    candidates = _iomin_dedup(candidates)
    telemetry['dedup'] += max(before - len(candidates), 0)
    effective_count = sum(c[2] for c in candidates)
    return candidates, effective_count


def _draw_overlay(frame, candidates, effective_count, label_lines):
    for (x1, y1, x2, y2), conf, weight in candidates:
        x1i, y1i, x2i, y2i = int(x1), int(y1), int(x2), int(y2)
        color = (0, 0, 255) if weight >= 1.0 else (0, 165, 255)
        suffix = "*" if weight < 1.0 else ""
        cv2.rectangle(frame, (x1i, y1i), (x2i, y2i), color, 2)
        cv2.putText(frame, f"P {conf:.2f}{suffix}", (x1i, max(y1i - 8, 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

    box_h = 20 + 25 * len(label_lines)
    overlay = frame.copy()
    cv2.rectangle(overlay, (10, 10), (440, 10 + box_h), (0, 0, 0), -1)
    cv2.addWeighted(frame, 0.7, overlay, 0.3, 0, frame)
    y = 35
    for text, color, scale in label_lines:
        cv2.putText(frame, text, (20, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1)
        y += 25


def _status_for(effective_count):
    """Pick overlay status text + color for the given effective_count."""
    if effective_count == 0:
        return "NO PERSON DETECTED", (0, 165, 255)
    if 0 < effective_count < MAX_PERSONS:
        return "LOW PERSON COUNT!", (0, 0, 255)
    return "All Clear", (0, 255, 0)


# =====================================================
# CORE DETECTION (per-frame) — standalone path
# =====================================================
def detect_crowd_five_in_frame(frame, device_id="unknown", camera_no="unknown", script_name="crowd_five"):
    """Standalone detection for a single frame.

    Applies G2–G6 here (per-frame gates). G1 (frame quality), G7 (temporal smoothing),
    and G8 (consecutive-violations) live in the worker because they require multi-frame state.
    The standalone path keeps G9 (cooldown) via the module-level last_save_times.
    """
    model = get_yolo_model()
    frame_h, frame_w = frame.shape[:2]
    results = model(frame, conf=CONF_THRESHOLD, verbose=False)[0]

    telemetry = defaultdict(int)
    candidates, effective_count = _apply_gate_chain(results.boxes, frame_w, frame_h, telemetry)
    person_count = int(round(effective_count))
    violation_detected = 0 < effective_count < MAX_PERSONS

    status_text, status_color = _status_for(effective_count)
    _draw_overlay(frame, candidates, effective_count, [
        (status_text, status_color, 0.8),
        (f"Persons: {effective_count:.1f}  (raw {len(candidates)}) / Min: {MAX_PERSONS}", (255, 255, 255), 0.6),
        (f"{device_id} - Cam {camera_no}", (255, 255, 255), 0.6),
    ])

    key = f"{device_id}_{camera_no}"
    now = time.time()

    with _state_lock:
        time_elapsed = now - last_save_times.get(key, 0)
        if violation_detected and time_elapsed >= COOLDOWN_TIME:
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
                    folder="crowd_detection_threshold",
                )
            except Exception as e:
                logger.error(f"[{device_id}-{camera_no}] Crowd Five DB reporting error: {e}")
            last_save_times[key] = now
            logger.warning(f"[{device_id}-{camera_no}] 🚨 Less than 5 persons detected: {filename}")

    return frame, violation_detected, person_count


# =====================================================
# BATCH DETECTION — called by worker
# =====================================================
def detect_crowd_five_batch(items, results=None):
    """Run true batch inference applying G2–G6 per frame.

    Returns list of (annotated_frame, violation_detected, effective_count, telemetry) tuples.
    G1, G7 (deque smoothing), G8 (CONSEC), G9 (cooldown) live in the worker.
    """
    if results is None:
        model = get_yolo_model()
        frames = [item['frame'].copy() for item in items]
        results = model(frames, conf=CONF_THRESHOLD, verbose=False)

    outputs = []
    for item, result in zip(items, results):
        frame = item['frame'].copy()
        frame_h, frame_w = frame.shape[:2]
        telemetry = defaultdict(int)
        candidates, effective_count = _apply_gate_chain(result.boxes, frame_w, frame_h, telemetry)

        violation_detected = 0 < effective_count < MAX_PERSONS
        ch_no = item.get('ch_no', item.get('camera_id', 'unknown'))

        status_text, status_color = _status_for(effective_count)
        _draw_overlay(frame, candidates, effective_count, [
            (status_text, status_color, 0.8),
            (f"Persons: {effective_count:.1f}  (raw {len(candidates)}) / Min: {MAX_PERSONS}", (255, 255, 255), 0.6),
            (f"CH: {ch_no}", (255, 255, 255), 0.6),
        ])

        outputs.append((frame, violation_detected, effective_count, dict(telemetry)))

    return outputs
