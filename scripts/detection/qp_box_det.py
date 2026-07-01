"""
qp_box_det.py — Two-stage GPU QP Box detection.

STAGE 1 — YOLO (GPU):
  Detect candidate bounding boxes (broad class list, lower conf threshold).

STAGE 2 — GPU Color Verification (PyTorch tensor HSV ops):
  For each YOLO bbox, verify it contains enough of the target color.
  This prevents false "MISSING" when YOLO finds the box but with wrong class,
  and prevents false "PRESENT" when YOLO finds a non-box with right shape.

FALLBACK — Global GPU Color Count:
  If YOLO finds nothing at all but a large colored region exists in the frame,
  treat it as present. Catches boxes YOLO misclassifies entirely.

COLOR CONFIG (no code change needed — set env vars):
  QP_BOX_COLOR_NAME   = "yellow"   label shown on frame
  QP_BOX_HUE_LOW      = 15         OpenCV hue 0-179
  QP_BOX_HUE_HIGH     = 45
  QP_BOX_SAT_LOW      = 80
  QP_BOX_SAT_HIGH     = 255
  QP_BOX_VAL_LOW      = 80
  QP_BOX_VAL_HIGH     = 255
  QP_BOX_MIN_AREA     = 3000       min YOLO bbox area (px²) to consider
  QP_BOX_COLOR_RATIO  = 0.10       min fraction of bbox that must be target color
  QP_BOX_MIN_COLOR_PX = 2000       global fallback: min colored pixels in full frame
"""

import cv2
import numpy as np
import time
import os
import logging
import sys
import threading
import torch
import uuid
from datetime import datetime
from ultralytics import YOLO

_DET_GPU_ID = (
    int(os.environ["WORKER_INDEX"])
    if os.environ.get("WORKER_INDEX", "").strip().isdigit()
    else int(
        "".join(c for c in os.environ.get("WORKER_ID", "main").split("-")[-1] if c.isdigit())
        or "0"
    )
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH  = os.path.join(BASE_DIR, "models", "yolov10m.pt")
SAVE_FOLDER = os.path.join(BASE_DIR, "results/qp_box_missing")
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
os.makedirs(SAVE_FOLDER, exist_ok=True)

COOLDOWN_TIME  = 240    # seconds between saved violations per camera
CONF_THRESHOLD = 0.30   # lowered from 0.45 — yellow boxes often score lower

# Expanded set of COCO classes that can plausibly look like a QP packet
# 24=backpack 25=umbrella 26=handbag 28=suitcase 73=book 84=scissors
# Also include: 39=bottle 41=cup 43=knife 56=chair (flat box on chair) 60=diningtable
BOX_CLASS_IDS = {24, 25, 26, 28, 39, 41, 43, 56, 60, 73, 84}

# ── Color config ─────────────────────────────────────────────────────────────
QP_BOX_COLOR_NAME   = os.environ.get("QP_BOX_COLOR_NAME",  "yellow")
QP_BOX_HUE_LOW      = int(os.environ.get("QP_BOX_HUE_LOW",   15))
QP_BOX_HUE_HIGH     = int(os.environ.get("QP_BOX_HUE_HIGH",  45))
QP_BOX_SAT_LOW      = int(os.environ.get("QP_BOX_SAT_LOW",   80))
QP_BOX_SAT_HIGH     = int(os.environ.get("QP_BOX_SAT_HIGH", 255))
QP_BOX_VAL_LOW      = int(os.environ.get("QP_BOX_VAL_LOW",   80))
QP_BOX_VAL_HIGH     = int(os.environ.get("QP_BOX_VAL_HIGH", 255))
QP_BOX_MIN_AREA     = int(os.environ.get("QP_BOX_MIN_AREA",  3000))
COLOR_RATIO_THRESH  = float(os.environ.get("QP_BOX_COLOR_RATIO", "0.10"))
MIN_COLOR_PX_GLOBAL = int(os.environ.get("QP_BOX_MIN_COLOR_PX", "2000"))

# ── Target colours ────────────────────────────────────────────────────────────
# QP boxes are wrapped in different coloured cloth at different centres. The
# legacy config matched only YELLOW, but many centres wrap the box in a
# PINK / MAGENTA cloth (see field photo), which the yellow hue band (15-45)
# completely misses. So we now match a LIST of colour ranges and treat a box as
# valid if it contains enough of ANY of them. Each entry is
# (name, hue_low, hue_high, sat_low, sat_high, val_low, val_high) in OpenCV HSV
# (H 0-179). Set QP_BOX_INCLUDE_PINK=0 to revert to yellow-only.
QP_BOX_COLOR_RANGES = [
    (QP_BOX_COLOR_NAME, QP_BOX_HUE_LOW, QP_BOX_HUE_HIGH,
     QP_BOX_SAT_LOW, QP_BOX_SAT_HIGH, QP_BOX_VAL_LOW, QP_BOX_VAL_HIGH),
]
if os.environ.get("QP_BOX_INCLUDE_PINK", "1") == "1":
    QP_BOX_COLOR_RANGES.append((
        "pink",
        int(os.environ.get("QP_BOX_PINK_HUE_LOW",  140)),
        int(os.environ.get("QP_BOX_PINK_HUE_HIGH", 175)),
        int(os.environ.get("QP_BOX_PINK_SAT_LOW",   60)),
        255,
        int(os.environ.get("QP_BOX_PINK_VAL_LOW",   80)),
        255,
    ))

# Combined name used on the annotated frame / logs (e.g. "yellow+pink").
QP_BOX_DISPLAY_NAME = "+".join(r[0] for r in QP_BOX_COLOR_RANGES)

logger.info(
    f"[QPBox] colors={QP_BOX_DISPLAY_NAME} "
    f"ranges={[(n, hl, hh, sl, sh, vl, vh) for (n, hl, hh, sl, sh, vl, vh) in QP_BOX_COLOR_RANGES]} "
    f"min_area={QP_BOX_MIN_AREA} ratio_thresh={COLOR_RATIO_THRESH}"
)

# ── YOLO model (singleton) ────────────────────────────────────────────────────
_yolo_model = None
_model_lock  = threading.Lock()

def get_yolo_model():
    global _yolo_model
    if _yolo_model is None:
        with _model_lock:
            if _yolo_model is None:
                logger.info(f"[QPBox] Loading YOLO from {MODEL_PATH}")
                _yolo_model = YOLO(MODEL_PATH)
                if torch.cuda.is_available():
                    _yolo_model.to("cuda")
                    logger.info("[QPBox] YOLO on CUDA")
    return _yolo_model

# ── State ─────────────────────────────────────────────────────────────────────
last_save_times: dict = {}
_state_lock = threading.Lock()

# ── GPU HSV conversion ────────────────────────────────────────────────────────
def _bgr_to_hsv_torch(bgr_np: np.ndarray, device: str = "cuda") -> torch.Tensor:
    """
    Convert HxWx3 BGR uint8 numpy array → HSV torch tensor on GPU.
    H: 0-179, S: 0-255, V: 0-255  (OpenCV convention).
    """
    t = torch.from_numpy(bgr_np).to(device).float() / 255.0   # [H,W,3] BGR 0-1
    # BGR → RGB channel order for max/min ops
    r, g, b = t[..., 2], t[..., 1], t[..., 0]
    rgb = torch.stack([r, g, b], dim=-1)

    max_c, max_idx = rgb.max(dim=-1)
    min_c          = rgb.min(dim=-1).values
    delta          = (max_c - min_c).clamp(min=1e-7)

    h = torch.zeros_like(max_c)
    m0 = max_idx == 0                           # red is max
    m1 = max_idx == 1                           # green is max
    m2 = max_idx == 2                           # blue is max
    h[m0] = ((g[m0] - b[m0]) / delta[m0]) % 6
    h[m1] = (b[m1] - r[m1]) / delta[m1] + 2
    h[m2] = (r[m2] - g[m2]) / delta[m2] + 4
    h = (h / 6.0 * 179.0).clamp(0, 179)

    s = torch.where(max_c > 0.001, (max_c - min_c) / max_c, torch.zeros_like(max_c))
    s = (s * 255.0).clamp(0, 255)
    v = (max_c * 255.0).clamp(0, 255)

    return torch.stack([h, s, v], dim=-1)   # [H, W, 3]


def _count_color_pixels_gpu(frame_np: np.ndarray, roi=None) -> int:
    """
    Count pixels matching the configured QP-box color entirely on GPU.
    roi: (x1, y1, x2, y2) or None for full frame.
    Falls back to CPU OpenCV if CUDA unavailable.
    """
    patch = frame_np if roi is None else frame_np[roi[1]:roi[3], roi[0]:roi[2]]
    if patch.size == 0:
        return 0

    if not torch.cuda.is_available():
        # CPU fallback — OR every configured colour range together.
        hsv  = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        mask = None
        for (_n, hl, hh, sl, sh, vl, vh) in QP_BOX_COLOR_RANGES:
            lo = np.array([hl, sl, vl], np.uint8)
            hi = np.array([hh, sh, vh], np.uint8)
            m  = cv2.inRange(hsv, lo, hi)
            mask = m if mask is None else cv2.bitwise_or(mask, m)
        return int(mask.sum() // 255) if mask is not None else 0

    hsv = _bgr_to_hsv_torch(patch)           # [H,W,3] on GPU
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    # A pixel counts if it falls inside ANY of the target colour ranges.
    mask = None
    for (_n, hl, hh, sl, sh, vl, vh) in QP_BOX_COLOR_RANGES:
        m = (h >= hl) & (h <= hh) & (s >= sl) & (s <= sh) & (v >= vl) & (v <= vh)
        mask = m if mask is None else (mask | m)
    return int(mask.sum().item()) if mask is not None else 0


# ── Core two-stage detector ───────────────────────────────────────────────────
def _detect_qp_box(frame: np.ndarray):
    """
    Stage 1: YOLO on GPU finds bounding boxes.
    Stage 2: GPU color check confirms each bbox contains target color.
    Fallback: global GPU color density if YOLO finds nothing.

    Returns: (found: bool, annotated_frame: np.ndarray, box_count: int)
    """
    model   = get_yolo_model()
    results = model(frame, conf=CONF_THRESHOLD, verbose=False)[0]

    annotated  = frame.copy()
    found      = False
    box_count  = 0

    for box in results.boxes:
        class_id = int(box.cls[0])
        if class_id not in BOX_CLASS_IDS:
            continue

        x1, y1, x2, y2 = map(int, box.xyxy[0])
        roi_area = max(1, (x2 - x1) * (y2 - y1))
        if roi_area < QP_BOX_MIN_AREA:
            continue

        # GPU color verification within this YOLO bbox
        color_px    = _count_color_pixels_gpu(frame, roi=(x1, y1, x2, y2))
        color_ratio = color_px / roi_area

        if color_ratio >= COLOR_RATIO_THRESH:
            found      = True
            box_count += 1
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
            label = f"QP Box [{QP_BOX_DISPLAY_NAME}] {color_ratio:.0%}"
            cv2.putText(annotated, label, (x1, max(y1 - 8, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
        else:
            # YOLO found something but wrong color — draw dim red so we can debug
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 80, 180), 1)

    # Fallback: YOLO missed it but large colored region exists
    if not found:
        global_px = _count_color_pixels_gpu(frame)
        if global_px >= MIN_COLOR_PX_GLOBAL:
            found      = True
            box_count  = 1
            cv2.putText(annotated,
                        f"QP Box [{QP_BOX_DISPLAY_NAME}] color={global_px}px (fallback)",
                        (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 0), 2)

    return found, annotated, box_count


# ── Public API ────────────────────────────────────────────────────────────────
def detect_qp_box_in_frame(frame, device_id="unknown", camera_no="unknown",
                            script_name="qp_box"):
    """Single-frame detection with violation saving."""
    qp_box_found, annotated, box_count = _detect_qp_box(frame)
    violation_detected = not qp_box_found

    status_text  = "QP BOX PRESENT" if qp_box_found else "QP BOX MISSING!"
    status_color = (0, 255, 0)       if qp_box_found else (0, 0, 255)
    cv2.putText(annotated, status_text, (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, status_color, 2)
    cv2.putText(annotated, f"{device_id} - {camera_no}", (10, 60),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

    key = f"{device_id}_{camera_no}"
    now = time.time()
    with _state_lock:
        if violation_detected and (now - last_save_times.get(key, 0)) >= COOLDOWN_TIME:
            ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
            uid   = str(uuid.uuid4())
            fname = (f"{script_name}_{device_id}_{camera_no}"
                     f"_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg")
            path  = os.path.join(SAVE_FOLDER, fname)
            cv2.imwrite(path, annotated)
            try:
                from utils.db_client import report_to_db_rtsp
                report_to_db_rtsp(device_id=str(device_id), camera_no=str(camera_no),
                                   image_name=fname, folder="qp_box_missing")
            except Exception as e:
                logger.error(f"[{device_id}-{camera_no}] DB report error: {e}")
            last_save_times[key] = now
            logger.warning(f"[{device_id}-{camera_no}] 🚨 QP BOX MISSING → {path}")

    return annotated, violation_detected, box_count


def detect_qp_box_batch(items, results=None):
    """
    Batch detection (called by the GPU batch worker).
    `results` param kept for API compatibility — ignored since we run
    YOLO internally with color verification per item.
    Returns list of (annotated_frame, violation_detected, box_count).
    """
    outputs = []
    for item in items:
        frame = item["frame"].copy()
        qp_box_found, annotated, box_count = _detect_qp_box(frame)
        violation_detected = not qp_box_found

        status_text  = "QP BOX PRESENT" if qp_box_found else "QP BOX MISSING!"
        status_color = (0, 255, 0)       if qp_box_found else (0, 0, 255)
        ch_no = item.get("ch_no", item.get("camera_id", "unknown"))
        cv2.putText(annotated, status_text, (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, status_color, 2)
        cv2.putText(annotated, f"CH: {ch_no}", (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        outputs.append((annotated, violation_detected, box_count))
    return outputs
