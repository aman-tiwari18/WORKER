import cv2
import os
import re
import logging
import threading
from collections import defaultdict, deque
from statistics import median

import crowd_five_det as crowd_five
import invigilator_tracking as invigilator

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= CONFIG ==================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAVE_FOLDER = os.path.join(BASE_DIR, "results/crowd_five_invigilator_detection")

# CAMERA CLASSIFICATION: Inside Lab (Computer Lab / Inside Classroom)
#
# Fires when BOTH hold on the same camera:
#   1. Seated persons: 0 < smoothed_count < MAX_SEATED (1-4 candidates, all sitting)
#   2. Invigilator STATIC: the same standing person has stayed at one spot for
#      invigilator.STATIC_TIME_THRESH (7 min)
#
# Standing / seated split uses the invigilator rule (h >= 100px AND h/w >= 2.5 →
# standing). Standing boxes are invigilator candidates; every other person box that
# passes crowd_five's size + aspect gates is a seated candidate.
#
# The invigilator's position is kept in a JSON file per camera (STATE_DIR) by
# invigilator.PersonTracker: each checkpoint (~every 60 s) compares the standing
# person's foot position + clothing colours against the stored anchor; within
# max(40px, 0.25 * box height) and same colours → not moved.

MAX_SEATED       = 5             # Violation needs 0 < smoothed seated count < MAX_SEATED
COUNT_WINDOW_LEN = 5             # Median of the last N seated counts per camera

# Own state dir so this analytic's timers never collide with the invigilator analytic's.
# CROWD_FIVE_INVIGILATOR_STATE_DIR="" keeps the state in memory only.
STATE_DIR = os.environ.get("CROWD_FIVE_INVIGILATOR_STATE_DIR",
                           os.path.join(BASE_DIR, "results/crowd_five_invigilator_state"))

os.makedirs(SAVE_FOLDER, exist_ok=True)
if STATE_DIR:
    os.makedirs(STATE_DIR, exist_ok=True)

# ================= STATE ===================
# key "{device_id}_{camera_no}" -> {'tracker', 'count_window', 'frame_hash'}
cameras = {}
_state_lock = threading.Lock()


def get_yolo_model():
    """Same person model (and shared instance) as crowd_five."""
    return crowd_five.get_yolo_model()


def _camera_state(key):
    with _state_lock:
        if key not in cameras:
            path = None
            if STATE_DIR:
                path = os.path.join(STATE_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")
            cameras[key] = {
                'tracker':      invigilator.PersonTracker(state_path=path),
                'count_window': deque(maxlen=COUNT_WINDOW_LEN),
                'frame_hash':   None,
            }
        return cameras[key]


def _split_persons(yolo_boxes, frame_w, frame_h, telemetry):
    """Split person boxes into standing (invigilator candidates) and seated.

    Returns (standing_boxes, standing_confs, seated, seated_count) where seated is a
    list of (xyxy_tuple, conf, weight) and seated_count is the sum of weights.
    """
    standing_boxes, standing_confs, seated = [], [], []
    cand = []
    for box in yolo_boxes:
        if int(box.cls[0]) != 0:
            continue
        conf = float(box.conf[0])
        if conf < crowd_five.CONF_THRESHOLD:
            telemetry['conf_reject'] += 1
            continue
        cand.append((box.xyxy[0].cpu().numpy(), conf))
    # Standing: the shared invigilator rule (sure h/w >= 2.5, or soft >= 2.0 not shorter than its
    # neighbours; partial boxes at the frame edge are not standing) - see invigilator_tracking
    flags = invigilator.standing_flags([c for c, _ in cand], frame_w, frame_h, soft=False)   # STATIC only: sure rule
    for (coords, conf), is_std in zip(cand, flags):
        if is_std:
            standing_boxes.append(coords)
            standing_confs.append(conf)
            continue
        # Seated: crowd_five's size / aspect / edge gates
        xyxy = coords.tolist()
        reason = crowd_five._shape_reject(xyxy, frame_w, frame_h)
        if reason is not None:
            telemetry[reason] += 1
            continue
        seated.append((tuple(xyxy), conf, crowd_five._edge_weight(xyxy, frame_w, frame_h)))

    before = len(seated)
    seated = crowd_five._iomin_dedup(seated)
    telemetry['dedup'] += max(before - len(seated), 0)
    return standing_boxes, standing_confs, seated, sum(s[2] for s in seated)


def _draw(frame, standing_boxes, standing_confs, static_violation, static_feet, seated,
          status, status_color, seated_smoothed, ch_label):
    for (x1, y1, x2, y2), conf, weight in seated:
        x1i, y1i, x2i, y2i = int(x1), int(y1), int(x2), int(y2)
        suffix = "*" if weight < 1.0 else ""
        cv2.rectangle(frame, (x1i, y1i), (x2i, y2i), (0, 200, 255), 1)
        cv2.putText(frame, f"Seated {conf:.2f}{suffix}", (x1i, max(y1i - 8, 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 200, 255), 1)

    # The STATIC invigilator (matched by foot position) in thick red, other standees thin green.
    for box, conf in zip(standing_boxes, standing_confs):
        x1, y1, x2, y2 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
        fx, fy = (x1 + x2) / 2.0, float(y2)
        match_r = max((y2 - y1) * 0.3, 25)
        is_static_box = static_violation and any(
            ((fx - sx) ** 2 + (fy - sy) ** 2) ** 0.5 < match_r for sx, sy in static_feet)
        if is_static_box:
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 3)
            cv2.putText(frame, "INVIGILATOR STATIC", (x1, max(y1 - 8, 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        else:
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 1)
            cv2.putText(frame, f"Invig {conf:.2f}", (x1, max(y1 - 8, 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

    overlay = frame.copy()
    cv2.rectangle(overlay, (10, 10), (470, 135), (0, 0, 0), -1)
    cv2.addWeighted(frame, 0.7, overlay, 0.3, 0, frame)
    cv2.putText(frame, status, (20, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
    cv2.putText(frame, f"Seated: {seated_smoothed:.1f} (< {MAX_SEATED})", (20, 65),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    cv2.putText(frame, f"Invigilator static: {'YES' if static_violation else 'no'}", (20, 90),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    cv2.putText(frame, ch_label, (20, 115), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)


def _evaluate(frame, result, device_id, camera_no, telemetry, posture=None):
    """Run the combined rule on one frame. Returns (annotated_frame, violation, seated_smoothed)."""
    state = _camera_state(f"{device_id}_{camera_no}")

    # Black / blurry / frozen frame: a frozen feed would make the invigilator look
    # static, so these never reach the tracker or the seated count.
    reason, state['frame_hash'] = crowd_five._is_frame_corrupt(frame, state['frame_hash'])
    if reason is not None:
        telemetry[reason] += 1
        return frame.copy(), False, 0.0

    annotated = frame.copy()
    frame_h, frame_w = frame.shape[:2]
    standing_boxes, standing_confs, seated, seated_count = _split_persons(
        result.boxes, frame_w, frame_h, telemetry)

    state['count_window'].append(seated_count)
    seated_smoothed = median(state['count_window'])
    few_seated = 0 < seated_smoothed < MAX_SEATED

    # Tracker gets the un-annotated frame (colour signatures) and stores positions in JSON.
    _absent, static_violation, static_feet = state['tracker'].update(standing_boxes, frame)
    # 14 Oct 2026: STATIC only on a person the posture model calls standing (see invigilator.posture_gate)
    static_feet = invigilator.posture_gate(static_feet, list(standing_boxes), posture)
    static_violation = bool(static_feet)

    violation = few_seated and static_violation
    if violation:
        status, color = "LOW CROWD + INVIGILATOR STATIC", (0, 0, 255)
    elif static_violation:
        status, color = "Invigilator static (seated >= 5 or 0)", (0, 165, 255)
    elif few_seated:
        status, color = "Low crowd (invigilator moving)", (0, 165, 255)
    else:
        status, color = "All Clear", (0, 255, 0)

    _draw(annotated, standing_boxes, standing_confs, static_violation, static_feet, seated,
          status, color, seated_smoothed, f"{device_id} - Cam {camera_no}")
    return annotated, violation, seated_smoothed


# =====================================================
# BATCH DETECTION — called by the worker and by unified_detector
# =====================================================
def detect_crowd_five_invigilator_batch(items, results=None):
    """Returns list of (annotated_frame, violation_detected, seated_count, telemetry).
    Per-camera state (tracker JSON, seated-count window) lives in this module, so the
    per-script worker and the unified detector behave the same."""
    if results is None:
        model = get_yolo_model()
        frames = [item['frame'] for item in items]
        results = model(frames, conf=crowd_five.CONF_THRESHOLD, verbose=False)

    outputs = []
    for item, result in zip(items, results):
        telemetry = defaultdict(int)
        device_id = item.get('device_id') or item.get('camera_id') or 'unknown'
        camera_no = item.get('ch_no') or item.get('camera_no') or 'unknown'
        annotated, violation, seated = _evaluate(item['frame'], result, device_id, camera_no, telemetry,
                                                 posture=item.get('posture'))
        outputs.append((annotated, violation, seated, dict(telemetry)))
    return outputs
