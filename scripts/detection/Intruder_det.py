import cv2
import time
import os
_DET_GPU_ID = int(os.environ["WORKER_INDEX"]) if os.environ.get("WORKER_INDEX", "").strip().isdigit() else int("".join(c for c in os.environ.get("WORKER_ID", "main").split("-")[-1] if c.isdigit()) or "0")
import logging
import sys
import threading
import torch
from datetime import datetime
import numpy as np
from ultralytics import YOLO

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= CONFIG ==================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# UNIFIED_PERSON_MODEL=finetuned -> the fine-tuned person+monitor model (person = class 0)
from detection_models import person_model_path
MODEL_PATH = person_model_path(os.path.join(BASE_DIR, "models", "yolov10m.pt"))
SAVE_FOLDER = os.path.join(BASE_DIR, "results/intruder_detection")

# CAMERA CLASSIFICATION: Inside Lab, Server Room
# This detection runs on cameras classified as:
# - Computer Lab / Inside Lab / Exam Lab
# - Server Room / Strong Room

COOLDOWN_TIME = int(os.environ.get("COOLDOWN_TIME", 0))         # Immediate alert for intruder
MIN_PERSONS = 0           # Threshold for violation (>0)
CONF_THRESHOLD = 0.30     # Confidence for intruder detection

# --- Photo / poster filter ---
# A photo of a person on the wall is detected as a person but never moves; a real intruder
# has to walk in. Two checks per camera, both on a downscaled grayscale copy of the frame:
#  1. Motion gate: a person box only counts once the pixels inside it changed against a
#     reference frame at least REF_MIN_AGE_SEC old (brightness-compensated, so IR exposure
#     shifts don't count). Once it moved, the person stays "live" at that spot for
#     LIVE_HOLD_SEC after its last movement, so an intruder who stops still keeps counting.
#  2. Static zones: a box that was never seen moving and stays at the same spot for STATIC_LEARN_SEC is
#     learned as a static object (poster) and ignored from then on, even through a noise spike
#     that would pass the motion gate. Someone standing in front of it gives a bigger box (no
#     IoU match) or changes the pixels against the learned crop, so they are not ignored.
# Works with both feeds: a continuous 3-5 fps stream and round-robin bursts (2 frames every
# 10-60 s), since the reference is chosen by age, not by frame count.
# INTRUDER_MOTION_GATE=0 turns the whole filter off (every person box counts, as before).
MOTION_GATE_ENABLED = os.environ.get("INTRUDER_MOTION_GATE", "1") == "1"
WORK_WIDTH          = 400    # px — frames are downscaled to this width for the pixel checks
REF_MIN_AGE_SEC     = 2.0    # Reference frame must be at least this old (a person still for 0.2 s
                             # between burst frames must not read as "no motion")
REF_MAX_AGE_SEC     = 900.0  # ...and no older than this (older refs are dropped)
REF_KEEP            = 2      # Reference frames kept per camera, spaced >= REF_MIN_AGE_SEC apart
                             # (the older one is always >= REF_MIN_AGE_SEC old; ~240 KB per camera)
MOTION_DIFF_THRESH  = float(os.environ.get("INTRUDER_MOTION_DIFF", "12"))
                             # Mean abs gray-level change inside the box that counts as movement
SCENE_CHANGE_DIFF   = 35.0   # Whole-frame mean change above this = camera moved / day-night switch:
                             # forget refs, live tracks and zones, judge nothing this frame
LIVE_HOLD_SEC       = 120.0  # A person box that moved stays live this long after its last movement
LIVE_MATCH_IOU      = 0.30   # Box overlaps a live track this much → same person
STATIC_LEARN_SEC    = float(os.environ.get("INTRUDER_STATIC_LEARN_SEC", "300"))
                             # Same spot, no movement, this long → learned as a static object
STATIC_MIN_HITS     = 5      # ...and seen at that spot on at least this many frames
ZONE_MATCH_IOU      = 0.50   # Box overlaps a zone this much → same object (loose: the zone-region
                             # pixel check below still catches someone standing in front)
ZONE_BREAK_DIFF     = 18.0   # Box pixels this different from the learned crop → something in front
ZONE_TTL_SEC        = 6 * 3600   # Zone not seen this long is forgotten
MAX_ZONES           = 20     # Per camera

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

# ================= PHOTO / POSTER FILTER ===================
def _iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(ix2 - ix1, 0) * max(iy2 - iy1, 0)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class _StaticFilter:
    """Per-camera photo/poster filter. classify() splits person boxes into real and static."""

    def __init__(self):
        self.refs = []     # [(t, gray)] oldest first
        self.live = []     # [{"box", "last_motion"}]
        self.zones = []    # [{"box", "crop", "since", "last_seen", "hits", "static"}]

    def _reset(self):
        self.refs, self.live, self.zones = [], [], []

    @staticmethod
    def _crop(gray, box, scale):
        h, w = gray.shape
        x1, y1 = max(int(box[0] * scale), 0), max(int(box[1] * scale), 0)
        x2, y2 = min(int(box[2] * scale) + 1, w), min(int(box[3] * scale) + 1, h)
        if x2 - x1 < 2 or y2 - y1 < 2:
            return None
        return gray[y1:y2, x1:x2]

    @staticmethod
    def _diff(cur, ref, gain):
        """Mean abs gray change between two same-size crops, ref scaled by the global gain."""
        if cur is None or ref is None or cur.shape != ref.shape:
            return None
        return float(np.mean(np.abs(cur.astype(np.float32) - ref.astype(np.float32) * gain)))

    def classify(self, frame, boxes, now):
        """boxes: list of xyxy in frame pixels. Returns list of (is_real, reason) per box."""
        scale = WORK_WIDTH / float(frame.shape[1])
        small = cv2.resize(frame, (WORK_WIDTH, max(int(frame.shape[0] * scale), 1)),
                           interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (3, 3), 0)

        self.refs = [(t, g) for t, g in self.refs
                     if now - t <= REF_MAX_AGE_SEC and g.shape == gray.shape]
        ref_gray = next((g for t, g in reversed(self.refs) if now - t >= REF_MIN_AGE_SEC), None)

        gain, scene_changed = 1.0, False
        if ref_gray is not None:
            ref_mean = float(ref_gray.mean())
            gain = float(gray.mean()) / ref_mean if ref_mean > 1 else 1.0
            scene_changed = self._diff(gray, ref_gray, gain) > SCENE_CHANGE_DIFF
        if scene_changed:
            self._reset()
            ref_gray = None

        if not self.refs or now - self.refs[-1][0] >= REF_MIN_AGE_SEC:
            self.refs = (self.refs + [(now, gray)])[-REF_KEEP:]

        self.live = [lt for lt in self.live if now - lt["last_motion"] <= LIVE_HOLD_SEC]
        self.zones = [z for z in self.zones if now - z["last_seen"] <= ZONE_TTL_SEC]

        out = []
        for box in boxes:
            cur = self._crop(gray, box, scale)
            zone = max(self.zones, key=lambda z: _iou(z["box"], box), default=None)
            if zone is not None and _iou(zone["box"], box) < ZONE_MATCH_IOU:
                zone = None

            # 2. Learned static object: ignore unless something now covers it. The zone's own
            # region is compared (not this box — YOLO's box on a poster flips between face-only
            # and half-body), so the comparison is always the same pixels.
            if zone is not None and zone["static"]:
                zone_now = self._crop(gray, zone["box"], scale)
                zd = self._diff(zone_now, zone["crop"], 1.0)
                if zd is not None and zd < ZONE_BREAK_DIFF:
                    zone["last_seen"] = now
                    zone["crop"] = zone_now   # follow slow IR/lighting drift
                    out.append((False, "static"))
                    continue

            # 1. Motion gate.
            motion = None
            if ref_gray is not None:
                motion = self._diff(cur, self._crop(ref_gray, box, scale), gain)
            moved = motion is not None and motion >= MOTION_DIFF_THRESH

            track = max(self.live, key=lambda lt: _iou(lt["box"], box), default=None)
            if track is not None and _iou(track["box"], box) < LIVE_MATCH_IOU:
                track = None
            if moved:
                if track is None:
                    track = {"box": box}
                    self.live.append(track)
                track["box"], track["last_motion"] = box, now
            elif track is not None:
                track["box"] = box

            # Zone bookkeeping: a still box at the same spot ages towards "static"; movement resets it.
            # A spot where the box was ever live (someone walked in and sat down) never becomes
            # static — only things that were never seen moving, like a poster, do.
            if moved:
                if zone is not None:
                    self.zones.remove(zone)
            elif motion is not None:
                if zone is None:
                    if len(self.zones) < MAX_ZONES:
                        self.zones.append({"box": box, "crop": cur, "since": now, "last_seen": now,
                                           "hits": 1, "static": False, "was_live": track is not None})
                else:
                    zone["last_seen"] = now
                    zone["hits"] += 1
                    zone["was_live"] = zone["was_live"] or track is not None
                    if (now - zone["since"] >= STATIC_LEARN_SEC and zone["hits"] >= STATIC_MIN_HITS
                            and not zone["was_live"] and cur is not None):
                        zone["static"], zone["box"], zone["crop"] = True, box, cur

            if track is not None:
                out.append((True, "moved" if moved else "live"))
            else:
                out.append((False, "no-ref" if motion is None else "still"))
        return out


_filters = {}
_filters_lock = threading.Lock()


def _camera_key(item):
    dev = item.get("device_id") or item.get("camera_id") or "unknown"
    ch = item.get("camera_no") or item.get("ch_no") or "unknown"
    return f"{dev}_{ch}"


def _split_real_static(item, frame, boxes):
    """Run the camera's filter. Returns list of (is_real, reason) per box."""
    if not MOTION_GATE_ENABLED or frame is None:
        return [(True, "")] * len(boxes)
    key = _camera_key(item)
    with _filters_lock:
        f = _filters.get(key)
        if f is None:
            f = _filters[key] = _StaticFilter()
    ts = item.get("captured_at")
    return f.classify(frame, boxes, float(ts) if ts is not None else time.time())

# =====================================================
# CORE DETECTION LOGIC
# =====================================================
def detect_intruder_in_frame(frame, device_id="unknown", camera_no="unknown", script_name="intruder"):
    """Standalone detection for a single frame."""
    model = get_yolo_model()
    results = model(frame, conf=CONF_THRESHOLD, verbose=False)[0]

    person_detections = [
        box for box in results.boxes
        if int(box.cls[0]) == 0 and box.conf[0] >= CONF_THRESHOLD
    ]

    person_count = len(person_detections)
    intruder_detected = person_count > MIN_PERSONS

    for box in person_detections:
        x1, y1, x2, y2 = map(int, box.xyxy[0])
        conf = float(box.conf[0])
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
        cv2.putText(frame, f"Person {conf:.2f}", (x1, max(y1 - 8, 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

    # Status overlay
    overlay = frame.copy()
    cv2.rectangle(overlay, (10, 10), (460, 100), (0, 0, 0), -1)
    cv2.addWeighted(frame, 0.7, overlay, 0.3, 0, frame)
    status_color = (0, 0, 255) if intruder_detected else (0, 255, 0)
    status_text = f"INTRUDER DETECTED! ({person_count})" if intruder_detected else "All Clear"
    cv2.putText(frame, status_text, (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
    cv2.putText(frame, f"Persons: {person_count}", (20, 62),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    cv2.putText(frame, f"{device_id} - Cam {camera_no}", (20, 87),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

    key = f"{device_id}_{camera_no}"
    now = time.time()

    with _state_lock:
        time_elapsed = now - last_save_times.get(key, 0)
        if intruder_detected and time_elapsed >= COOLDOWN_TIME:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            import uuid
            uid = str(uuid.uuid4())
            fname = f"{script_name}_{device_id}_{camera_no}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"
            filename = os.path.join(SAVE_FOLDER, fname)
            cv2.imwrite(filename, frame)
            last_save_times[key] = now
            logger.warning(f"[{device_id}-{camera_no}] 🚨 Intruder detected: {filename}")

    return frame, intruder_detected, person_count

def detect_intruder_batch(items, results=None):
    """Run true batch inference."""
    if results is None:
        model = get_yolo_model()
        frames = [item['frame'].copy() for item in items]
        results = model(frames, conf=CONF_THRESHOLD, verbose=False)

    outputs = []
    for item, result in zip(items, results):
        frame = item['frame'].copy()
        candidates = [
            box for box in result.boxes
            if int(box.cls[0]) == 0 and float(box.conf[0]) >= CONF_THRESHOLD
        ]
        coords = [tuple(float(v) for v in box.xyxy[0]) for box in candidates]
        verdicts = _split_real_static(item, item['frame'], coords)
        person_boxes = [box for box, (real, _) in zip(candidates, verdicts) if real]
        person_count = len(person_boxes)
        intruder_detected = person_count > MIN_PERSONS

        for box, (real, reason) in zip(candidates, verdicts):
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            conf = float(box.conf[0])
            if real:
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
                cv2.putText(frame, f"Person {conf:.2f}", (x1, max(y1 - 8, 10)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
            else:
                # Photo / poster / not-yet-moved box: thin grey, never counted
                cv2.rectangle(frame, (x1, y1), (x2, y2), (160, 160, 160), 1)
                cv2.putText(frame, reason.upper(), (x1, max(y1 - 8, 10)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (160, 160, 160), 1)

        ch_no = item.get('ch_no', item.get('camera_id', 'unknown'))

        # Status overlay
        overlay = frame.copy()
        cv2.rectangle(overlay, (10, 10), (460, 100), (0, 0, 0), -1)
        cv2.addWeighted(frame, 0.7, overlay, 0.3, 0, frame)
        status_color = (0, 0, 255) if intruder_detected else (0, 255, 0)
        status_text = f"INTRUDER DETECTED! ({person_count})" if intruder_detected else "All Clear"
        cv2.putText(frame, status_text, (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
        cv2.putText(frame, f"Persons: {person_count}", (20, 62),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.putText(frame, f"CH: {ch_no}", (20, 87),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

        outputs.append((frame, intruder_detected, person_count))

    return outputs
