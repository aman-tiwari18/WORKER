"""
camera_id_verifier.py
═══════════════════════════════════════════════════════════════
Reads camera URLs from CSV/Excel OR database, grabs one frame per
stream (HLS or RTSP), extracts the burned-in camera-ID text
from the bottom-left OR bottom-right corner via a 3-stage OCR
fallback chain:

  Stage 1 — EasyOCR      (primary,  best for DVR overlays)
  Stage 2 — Tesseract    (fallback, fast + free)
  Stage 3 — Claude Vision (final,   most accurate but costs tokens)

Compares extracted ID to the expected ID column and writes a
final Excel result file with per-stage debug info.

DEPENDENCIES
────────────
pip install pandas openpyxl aiohttp aiofiles opencv-python-headless \
            pytesseract easyocr numpy tqdm requests anthropic pymysql

System: apt install tesseract-ocr ffmpeg   (or brew install on Mac)

USAGE
─────
# From CSV/Excel:
python camera_id_verifier.py \
       --input cameras.csv \
       --url-col  "Stream URL" \
       --id-col   "Camera ID" \
       --output   results.xlsx

# From Database (RTSP):
python camera_id_verifier.py \
       --from-db \
       --link-type RTSP \
       --exam-id 2 \
       --output results.xlsx

# From Database (HLS):
python camera_id_verifier.py \
       --from-db \
       --link-type HLS \
       --exam-id 2 \
       --shift-id 7 \
       --exam-date 2026-04-28 \
       --output results.xlsx

INPUT CSV/EXCEL required columns (names configurable via CLI):
  • Stream URL  — HLS (.m3u8) or RTSP (rtsp://) URL
  • Camera ID   — expected ID to match (e.g. 11394_REG-D_C01)
═══════════════════════════════════════════════════════════════
"""

# ── stdlib ────────────────────────────────────────────────────────────────
import argparse
import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

# ── third-party ───────────────────────────────────────────────────────────
import aiohttp
import cv2
import numpy as np
import pandas as pd
import pytesseract
from tqdm import tqdm

# Database client
try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'utils'))
    from db_client import get_hls_cameras, get_rtsp_cameras
    _DB_AVAILABLE = True
except ImportError:
    _DB_AVAILABLE = False
    logging.warning("Database client not available — use --input for CSV/Excel mode")

# EasyOCR — lazy-imported so the script still works without it
try:
    import easyocr
    _EASYOCR_AVAILABLE = True
except ImportError:
    _EASYOCR_AVAILABLE = False

# Anthropic SDK — lazy-imported, only used for Stage 3
try:
    import anthropic as _anthropic_sdk
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

# ══════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════
LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs", "camera_id_verification"))
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"ocr_verify_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════
# TUNABLES  (override via ENV or CLI)
# ══════════════════════════════════════════════════════════════════════════
MAX_WORKERS_NET: int = int(os.getenv("OCR_WORKERS_NET", "300"))
MAX_WORKERS_OCR: int = int(os.getenv("OCR_WORKERS_OCR", "32"))
THREAD_POOL_SZ:  int = int(os.getenv("OCR_THREAD_POOL", "64"))

TIMEOUT_HLS_EXTRACT: int = 4
TIMEOUT_HLS_TEST:    int = 3
TIMEOUT_OPENCV_MS:   int = 4000
TIMEOUT_FFMPEG:      int = 6

CHECKPOINT_EVERY: int = 500

# ── ROI definitions ───────────────────────────────────────────────────────
# The cam ID is burned-in at a FIXED position — either bottom-left or
# bottom-right.  We crop BOTH strips and run OCR on each; whichever
# returns a regex-valid result wins.
ROI_Y1_FRAC  = 0.82   # top    of the ID text band
ROI_Y2_FRAC  = 0.97   # bottom of the ID text band
ROI_LEFT_X1  = 0.00   # left  strip start
ROI_LEFT_X2  = 0.45   # left  strip end
ROI_RIGHT_X1 = 0.55   # right strip start
ROI_RIGHT_X2 = 1.00   # right strip end

# Regex that a valid camera ID must satisfy (adjust to your naming schema)
CAM_ID_RE = re.compile(r"[\dA-Z]{3,}[_\-/\\][\dA-Z_\-/\\]{3,}", re.IGNORECASE)

# Tesseract config
TESS_CONFIG = r"--oem 3 --psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-/ "

# Claude Vision model
CLAUDE_VISION_MODEL = "claude-sonnet-4-20250514"

# Minimum EasyOCR confidence to trust a result (0–1)
EASYOCR_MIN_CONF = 0.35

# ── singletons ────────────────────────────────────────────────────────────
_POOL             = ThreadPoolExecutor(max_workers=THREAD_POOL_SZ)
_lock             = threading.Lock()
_easyocr_reader   = None   # initialised once in main()
_anthropic_client = None   # initialised once if key provided


# ══════════════════════════════════════════════════════════════════════════
# EASYOCR INIT  (done once — model load is slow)
# ══════════════════════════════════════════════════════════════════════════

def init_easyocr(gpu: bool = False):
    global _easyocr_reader
    if not _EASYOCR_AVAILABLE:
        log.warning("EasyOCR not installed — Stage 1 will be skipped.")
        return
    log.info(f"Loading EasyOCR model (gpu={gpu}) …")
    _easyocr_reader = easyocr.Reader(["en"], gpu=gpu, verbose=False)
    log.info("EasyOCR ready.")


def init_anthropic(api_key: str):
    global _anthropic_client
    if not _ANTHROPIC_AVAILABLE:
        log.warning("anthropic SDK not installed — Stage 3 will be skipped.")
        return
    _anthropic_client = _anthropic_sdk.Anthropic(api_key=api_key)
    log.info("Anthropic client ready (Stage 3 enabled).")


# ══════════════════════════════════════════════════════════════════════════
# INPUT LOADING
# ══════════════════════════════════════════════════════════════════════════

def load_from_database(link_type: str, exam_id: int, shift_id: int = None, exam_date: str = None) -> pd.DataFrame:
    """Load camera data from database tables."""
    if not _DB_AVAILABLE:
        log.error("Database client not available. Install pymysql and ensure db_client.py is accessible.")
        sys.exit(1)

    try:
        if link_type.upper() == "RTSP":
            rows = get_rtsp_cameras(exam_id)
            log.info(f"Loaded {len(rows)} RTSP cameras from database (exam_id={exam_id})")
            
            # Convert to DataFrame
            data = []
            for row in rows:
                device_id = str(row.get("device_id", "")).strip()
                camera_no = str(row.get("camera_no", "")).strip()
                camera_name = str(row.get("camera_name", "")).strip()
                
                # Expected ID format: device_id_camera_no or from camera_name
                expected_id = f"{device_id}_{camera_no}"
                
                data.append({
                    "_url": str(row.get("cctv_url", "")).strip(),
                    "_expect": expected_id.upper().replace(" ", ""),
                    "device_id": device_id,
                    "camera_no": camera_no,
                    "camera_name": camera_name,
                    "centre_name": row.get("centre_name", ""),
                    "city": row.get("city", ""),
                    "state": row.get("state", ""),
                    "tc_code": row.get("tc_code", ""),
                })
            
        elif link_type.upper() == "HLS":
            if not shift_id or not exam_date:
                log.error("HLS mode requires --shift-id and --exam-date")
                sys.exit(1)
            
            rows = get_hls_cameras(exam_id, shift_id, exam_date)
            log.info(f"Loaded {len(rows)} HLS cameras from database (exam_id={exam_id}, shift_id={shift_id}, date={exam_date})")
            
            # Convert to DataFrame
            data = []
            for row in rows:
                ch_code = str(row.get("ch_code", "")).strip()
                camera_name = str(row.get("camera_name", "")).strip()
                
                # Expected ID from camera_name or ch_code
                expected_id = camera_name if camera_name else ch_code
                
                data.append({
                    "_url": str(row.get("cctv_url", "")).strip(),
                    "_expect": expected_id.upper().replace(" ", ""),
                    "ch_code": ch_code,
                    "camera_name": camera_name,
                    "centre_name": row.get("centre_name", ""),
                    "city": row.get("city", ""),
                    "state": row.get("state", ""),
                    "tc_code": row.get("tc_code", ""),
                    "camera_category": row.get("camera_category", ""),
                })
        else:
            log.error(f"Invalid link_type: {link_type}. Use RTSP or HLS.")
            sys.exit(1)

        if not data:
            log.error("No cameras found in database")
            sys.exit(1)

        df = pd.DataFrame(data)
        df["_extracted"]  = ""
        df["_match"]      = ""
        df["_status"]     = ""
        df["_confidence"] = ""
        df["_ocr_stage"]  = ""
        df["_roi_side"]   = ""
        df["_row_id"]     = range(len(df))
        
        return df

    except Exception as e:
        log.error(f"Error loading from database: {e}")
        sys.exit(1)


def load_input(path: str, url_col: str, id_col: str) -> pd.DataFrame:
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str)
    else:
        df = pd.read_csv(path, dtype=str)

    df.columns = df.columns.str.strip()
    df.fillna("", inplace=True)

    missing = [c for c in [url_col, id_col] if c not in df.columns]
    if missing:
        log.error(f"Columns not found: {missing}. Available: {list(df.columns)}")
        sys.exit(1)

    df["_url"]        = df[url_col].str.strip()
    df["_expect"]     = df[id_col].str.strip().str.upper().str.replace(r"\s+", "", regex=True)
    df["_extracted"]  = ""
    df["_match"]      = ""
    df["_status"]     = ""
    df["_confidence"] = ""
    df["_ocr_stage"]  = ""   # which stage found the ID: easyocr / tesseract / claude
    df["_roi_side"]   = ""   # which side the ID was found on: left / right
    df["_row_id"]     = range(len(df))
    log.info(f"Loaded {len(df):,} cameras from {path}")
    return df


# ══════════════════════════════════════════════════════════════════════════
# STREAM TYPE DETECTION
# ══════════════════════════════════════════════════════════════════════════

def _is_rtsp(url: str) -> bool:
    return url.lower().startswith("rtsp://")

def _is_direct_hls(url: str) -> bool:
    return ".m3u8" in url.lower()


# ══════════════════════════════════════════════════════════════════════════
# HLS URL EXTRACTION
# ══════════════════════════════════════════════════════════════════════════

async def _resolve_hls(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    if _is_direct_hls(url):
        return url
    try:
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_HLS_EXTRACT)
        async with session.get(url, timeout=timeout, allow_redirects=True) as r:
            if r.status != 200:
                return None
            text = await r.text()

        m = re.findall(r'https?://[^\s<>"]+?\.m3u8[^\s<>"]*', text)
        if m:
            return m[0]

        try:
            jd = json.loads(text)
            for k in ("url", "stream_url", "hls_url", "playback_url"):
                if jd.get(k):
                    return str(jd[k])
        except Exception:
            pass

        if ".m3u8" in text:
            m = re.findall(r'["\']( https?://[^"\']+\.m3u8[^"\']*)["\']', text)
            if m:
                return m[0]
    except Exception as e:
        log.debug(f"HLS resolve failed ({url[:60]}): {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════
# FRAME CAPTURE
# ══════════════════════════════════════════════════════════════════════════

def _opencv_capture(url: str) -> Optional[bytes]:
    try:
        cap = cv2.VideoCapture(url)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, TIMEOUT_OPENCV_MS)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC,  TIMEOUT_OPENCV_MS)
        if not cap.isOpened():
            return None
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        return buf.tobytes() if ok else None
    except Exception as e:
        log.debug(f"OpenCV error: {e}")
        return None


def _ffmpeg_capture(url: str, is_rtsp: bool = False) -> Optional[bytes]:
    if not shutil.which("ffmpeg"):
        return None
    cmd = ["ffmpeg", "-y"]
    if is_rtsp:
        cmd += ["-rtsp_transport", "tcp"]
    cmd += [
        "-timeout", str(TIMEOUT_FFMPEG * 1_000_000),
        "-i", url,
        "-frames:v", "1",
        "-f", "image2",
        "-vcodec", "mjpeg",
        "-loglevel", "error",
        "pipe:1",
    ]
    try:
        r = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=TIMEOUT_FFMPEG
        )
        return r.stdout if r.returncode == 0 and r.stdout else None
    except Exception as e:
        log.debug(f"FFmpeg error: {e}")
        return None


async def _capture_frame(url: str, is_rtsp: bool) -> Optional[bytes]:
    loop = asyncio.get_event_loop()
    jpeg = await loop.run_in_executor(_POOL, _opencv_capture, url)
    if jpeg:
        return jpeg
    return await loop.run_in_executor(_POOL, _ffmpeg_capture, url, is_rtsp)


# ══════════════════════════════════════════════════════════════════════════
# ROI CROP HELPER
# Returns list of (cropped_img_bgr, side_label) for left and right strips
# ══════════════════════════════════════════════════════════════════════════

def _get_roi_crops(img: np.ndarray) -> List[Tuple[np.ndarray, str]]:
    h, w = img.shape[:2]
    y1 = int(h * ROI_Y1_FRAC)
    y2 = int(h * ROI_Y2_FRAC)

    crops = []

    # Left strip
    x1l = int(w * ROI_LEFT_X1)
    x2l = int(w * ROI_LEFT_X2)
    crops.append((img[y1:y2, x1l:x2l], "left"))

    # Right strip
    x1r = int(w * ROI_RIGHT_X1)
    x2r = int(w * ROI_RIGHT_X2)
    crops.append((img[y1:y2, x1r:x2r], "right"))

    return crops


def _preprocess_for_ocr(crop_bgr: np.ndarray) -> np.ndarray:
    """Grayscale → binary threshold → morph close → 2× upscale."""
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 160, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    scaled = cv2.resize(thresh, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return scaled


def _is_valid_cam_id(text: str) -> bool:
    return bool(CAM_ID_RE.search(text))


# ══════════════════════════════════════════════════════════════════════════
# STAGE 1 — EasyOCR
# ══════════════════════════════════════════════════════════════════════════

def _stage1_easyocr(img: np.ndarray) -> Tuple[str, float, str]:
    """
    Returns (extracted_text, confidence, side) or ("", 0, "") on failure.
    Tries left strip first, then right.
    """
    if _easyocr_reader is None:
        return "", 0.0, ""

    for crop, side in _get_roi_crops(img):
        try:
            results = _easyocr_reader.readtext(crop, detail=1)
            # results = [ ([bbox], text, conf), … ]
            candidates = []
            for (_, text, conf) in results:
                clean = text.strip().upper().replace(" ", "")
                if conf >= EASYOCR_MIN_CONF and _is_valid_cam_id(clean):
                    candidates.append((clean, conf))

            if candidates:
                best = max(candidates, key=lambda x: x[1])
                return best[0], round(best[1] * 100, 1), side

        except Exception as e:
            log.debug(f"EasyOCR error ({side}): {e}")

    return "", 0.0, ""


# ══════════════════════════════════════════════════════════════════════════
# STAGE 2 — Tesseract
# ══════════════════════════════════════════════════════════════════════════

def _stage2_tesseract(img: np.ndarray) -> Tuple[str, float, str]:
    """
    Returns (extracted_text, confidence, side) or ("", 0, "") on failure.
    Tries left strip first, then right.
    """
    for crop, side in _get_roi_crops(img):
        try:
            processed = _preprocess_for_ocr(crop)
            data = pytesseract.image_to_data(
                processed, config=TESS_CONFIG,
                output_type=pytesseract.Output.DICT
            )

            texts, confs = [], []
            for i, conf in enumerate(data["conf"]):
                try:
                    c = float(conf)
                except (ValueError, TypeError):
                    continue
                t = str(data["text"][i]).strip()
                if c > 0 and t:
                    texts.append(t)
                    confs.append(c)

            if not texts:
                continue

            combined = "".join(texts).upper().replace(" ", "")
            avg_conf  = sum(confs) / len(confs)

            if _is_valid_cam_id(combined):
                return combined, round(avg_conf, 1), side

        except Exception as e:
            log.debug(f"Tesseract error ({side}): {e}")

    return "", 0.0, ""


# ══════════════════════════════════════════════════════════════════════════
# STAGE 3 — Claude Vision API
# ══════════════════════════════════════════════════════════════════════════

def _stage3_claude_vision(jpeg_bytes: bytes) -> Tuple[str, float, str]:
    """
    Sends the FULL frame to Claude Vision with a targeted prompt.
    Returns (extracted_text, confidence=95.0, side) or ("", 0, "") on failure.
    Claude Vision is highly accurate but slower and costs tokens.
    Only called when Stages 1 & 2 both fail.
    """
    if _anthropic_client is None:
        return "", 0.0, ""

    try:
        b64 = base64.standard_b64encode(jpeg_bytes).decode("utf-8")

        prompt = (
            "This is a CCTV camera frame. There is a camera ID burned into the "
            "video overlay — it appears as white text on a dark background, "
            "typically in the bottom-left or bottom-right corner. "
            "The format looks like: 11394_REG-D_C01 or similar "
            "(digits, letters, underscores, hyphens, slashes). "
            "Extract ONLY the camera ID text. "
            "Return ONLY the raw camera ID string — no explanation, no quotes, "
            "no other text. If you cannot find it, return: NOT_FOUND"
        )

        response = _anthropic_client.messages.create(
            model=CLAUDE_VISION_MODEL,
            max_tokens=64,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )

        raw = response.content[0].text.strip().upper().replace(" ", "")
        if raw and raw != "NOT_FOUND" and _is_valid_cam_id(raw):
            return raw, 95.0, "claude_detected"

        return "", 0.0, ""

    except Exception as e:
        log.debug(f"Claude Vision error: {e}")
        return "", 0.0, ""


# ══════════════════════════════════════════════════════════════════════════
# COMBINED OCR — 3-stage fallback chain (runs in thread pool)
# ══════════════════════════════════════════════════════════════════════════

def _ocr_camera_id(jpeg_bytes: bytes) -> Tuple[str, float, str, str]:
    """
    Returns (extracted_text, confidence, stage_used, roi_side).
    Stage order: EasyOCR → Tesseract → Claude Vision
    """
    try:
        arr = np.frombuffer(jpeg_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return "", 0.0, "decode_error", ""

        # ── Stage 1: EasyOCR ──────────────────────────────────────────
        text, conf, side = _stage1_easyocr(img)
        if text:
            log.debug(f"Stage1 EasyOCR hit: {text} ({conf}%) [{side}]")
            return text, conf, "easyocr", side

        # ── Stage 2: Tesseract ────────────────────────────────────────
        text, conf, side = _stage2_tesseract(img)
        if text:
            log.debug(f"Stage2 Tesseract hit: {text} ({conf}%) [{side}]")
            return text, conf, "tesseract", side

        # ── Stage 3: Claude Vision ────────────────────────────────────
        text, conf, side = _stage3_claude_vision(jpeg_bytes)
        if text:
            log.debug(f"Stage3 Claude Vision hit: {text} ({conf}%)")
            return text, conf, "claude_vision", side

        return "", 0.0, "all_stages_failed", ""

    except Exception as e:
        log.debug(f"OCR pipeline error: {e}")
        return "", 0.0, f"pipeline_error:{e}", ""


async def _ocr_async(jpeg_bytes: bytes) -> Tuple[str, float, str, str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_POOL, _ocr_camera_id, jpeg_bytes)


# ══════════════════════════════════════════════════════════════════════════
# PER-CAMERA PIPELINE
# ══════════════════════════════════════════════════════════════════════════

async def _process_one(
    row: dict,
    session: aiohttp.ClientSession,
    sem_net: asyncio.Semaphore,
    sem_ocr: asyncio.Semaphore,
) -> dict:
    url    = row["_url"]
    expect = row["_expect"]
    row_id = row["_row_id"]

    base = {
        "row_id":     row_id,
        "extracted":  "",
        "match":      "NO",
        "status":     "FAILED",
        "confidence": 0.0,
        "ocr_stage":  "",
        "roi_side":   "",
    }

    if not url:
        base["status"] = "MISSING_URL"
        return base

    is_rtsp = _is_rtsp(url)

    # ── Step 1: Resolve stream URL ─────────────────────────────────────
    async with sem_net:
        try:
            if is_rtsp:
                resolved = url
            else:
                resolved = await asyncio.wait_for(
                    _resolve_hls(url, session),
                    timeout=TIMEOUT_HLS_EXTRACT + 1,
                )
            if not resolved:
                base["status"] = "HLS_RESOLVE_FAILED"
                return base
        except asyncio.TimeoutError:
            base["status"] = "HLS_TIMEOUT"
            return base
        except Exception as e:
            base["status"] = f"RESOLVE_ERR:{e}"
            return base

    # ── Step 2: Capture frame ──────────────────────────────────────────
    try:
        jpeg = await asyncio.wait_for(
            _capture_frame(resolved, is_rtsp),
            timeout=TIMEOUT_OPENCV_MS / 1000 + TIMEOUT_FFMPEG + 2,
        )
        if not jpeg:
            base["status"] = "FRAME_CAPTURE_FAILED"
            return base
    except asyncio.TimeoutError:
        base["status"] = "FRAME_TIMEOUT"
        return base

    # ── Step 3: OCR (3-stage fallback) ─────────────────────────────────
    async with sem_ocr:
        extracted, conf, stage, side = await _ocr_async(jpeg)

    if not extracted:
        base["status"]     = f"OCR_EMPTY:{stage}"
        base["ocr_stage"]  = stage
        base["confidence"] = conf
        return base

    match = extracted == expect

    return {
        "row_id":     row_id,
        "extracted":  extracted,
        "match":      "YES" if match else "NO",
        "status":     "OK",
        "confidence": conf,
        "ocr_stage":  stage,
        "roi_side":   side,
    }


# ══════════════════════════════════════════════════════════════════════════
# CHECKPOINT
# ══════════════════════════════════════════════════════════════════════════

def _save_checkpoint(df: pd.DataFrame, path: str) -> None:
    try:
        df.to_json(path, orient="records", force_ascii=False)
    except Exception as e:
        log.error(f"Checkpoint save failed: {e}")


def _load_checkpoint(df: pd.DataFrame, path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return df
    try:
        saved = pd.read_json(path, orient="records", dtype=str)
        saved.fillna("", inplace=True)
        done_mask = saved["_status"] != ""
        done_rows = saved[done_mask]
        log.info(f"Resuming checkpoint: {done_mask.sum():,} already done")
        for _, row in done_rows.iterrows():
            rid  = int(row["_row_id"])
            mask = df["_row_id"] == rid
            df.loc[mask, "_extracted"]  = row.get("_extracted",  "")
            df.loc[mask, "_match"]      = row.get("_match",      "")
            df.loc[mask, "_status"]     = row.get("_status",     "")
            df.loc[mask, "_confidence"] = row.get("_confidence", "")
            df.loc[mask, "_ocr_stage"]  = row.get("_ocr_stage",  "")
            df.loc[mask, "_roi_side"]   = row.get("_roi_side",   "")
        return df
    except Exception as e:
        log.warning(f"Checkpoint load failed ({e}) — starting fresh")
        return df


# ══════════════════════════════════════════════════════════════════════════
# RESULTS EXPORT
# ══════════════════════════════════════════════════════════════════════════

def _export_results(df: pd.DataFrame, output_path: str) -> None:
    export = df.drop(columns=["_url", "_expect", "_row_id"], errors="ignore").copy()
    export.rename(columns={
        "_extracted":  "Extracted ID",
        "_match":      "Match",
        "_status":     "Status",
        "_confidence": "OCR Confidence %",
        "_ocr_stage":  "OCR Stage Used",
        "_roi_side":   "ID Found On Side",
    }, inplace=True)

    matched    = export[export["Match"] == "YES"]
    mismatched = export[(export["Match"] == "NO") & (export["Status"] == "OK")]
    failed     = export[export["Status"] != "OK"]

    stage_counts = export["OCR Stage Used"].value_counts().to_dict()

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        export.to_excel(writer,     sheet_name="All Results", index=False)
        matched.to_excel(writer,    sheet_name="Matched",     index=False)
        mismatched.to_excel(writer, sheet_name="Mismatched",  index=False)
        failed.to_excel(writer,     sheet_name="Failed",      index=False)

    log.info(f"Results saved → {output_path}")
    print(f"\n📁 Results: {output_path}")
    print(f"   ✅ Matched        : {len(matched):,}")
    print(f"   ❌ Mismatched     : {len(mismatched):,}")
    print(f"   ⚠️  Failed         : {len(failed):,}")
    print(f"\n   OCR Stage Breakdown:")
    for stage, count in stage_counts.items():
        print(f"     {stage:<20}: {count:,}")


# ══════════════════════════════════════════════════════════════════════════
# ASYNC MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════

async def _run_pipeline(df: pd.DataFrame, workers_net: int, workers_ocr: int) -> pd.DataFrame:
    sem_net = asyncio.Semaphore(workers_net)
    sem_ocr = asyncio.Semaphore(workers_ocr)

    pending = df[df["_status"] == ""].to_dict("records")
    log.info(
        f"Pipeline start | total={len(df):,} | pending={len(pending):,} | "
        f"net_workers={workers_net} | ocr_workers={workers_ocr}"
    )

    connector = aiohttp.TCPConnector(
        limit=workers_net + 64,
        limit_per_host=0,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )

    done_count   = 0
    checkpoint_p = "checkpoint_ocr.json"
    t_start      = time.time()

    async with aiohttp.ClientSession(connector=connector) as session:
        coros = [
            _process_one(row, session, sem_net, sem_ocr)
            for row in pending
        ]

        with tqdm(total=len(coros), desc="Verifying cameras", unit="cam") as pbar:
            for fut in asyncio.as_completed(coros):
                res = await fut
                rid = res["row_id"]

                with _lock:
                    mask = df["_row_id"] == rid
                    df.loc[mask, "_extracted"]  = res["extracted"]
                    df.loc[mask, "_match"]      = res["match"]
                    df.loc[mask, "_status"]     = res["status"]
                    df.loc[mask, "_confidence"] = str(res["confidence"])
                    df.loc[mask, "_ocr_stage"]  = res["ocr_stage"]
                    df.loc[mask, "_roi_side"]   = res["roi_side"]

                done_count += 1
                pbar.update(1)

                elapsed   = time.time() - t_start
                rate      = done_count / elapsed if elapsed > 0 else 0
                remaining = len(coros) - done_count
                eta_s     = int(remaining / rate) if rate > 0 else 0
                pbar.set_postfix({
                    "rate": f"{rate:.0f}/s",
                    "ETA":  f"{eta_s//3600}h{(eta_s%3600)//60}m",
                })

                if done_count % CHECKPOINT_EVERY == 0:
                    _save_checkpoint(df, checkpoint_p)

    _save_checkpoint(df, checkpoint_p)
    total_time = time.time() - t_start

    matched  = int((df["_match"] == "YES").sum())
    mismatch = int(((df["_match"] == "NO") & (df["_status"] == "OK")).sum())
    failed_c = int((df["_status"] != "OK").sum())

    print(f"""
{'═'*60}
  FINAL SUMMARY
{'═'*60}
  Total cameras      : {len(df):,}
  ✅ Matched          : {matched:,}
  ❌ Mismatched       : {mismatch:,}
  ⚠️  Failed/Skipped   : {failed_c:,}
  Total time         : {int(total_time//3600)}h {int((total_time%3600)//60)}m {int(total_time%60)}s
  Throughput         : {len(df)/total_time:.1f} cameras/sec
{'═'*60}""")

    if os.path.exists(checkpoint_p):
        os.remove(checkpoint_p)

    return df


# ══════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(
        description="Verify burned-in camera IDs via 3-stage OCR fallback at scale"
    )
    
    # Input source
    input_group = p.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--input", help="Input CSV or Excel file")
    input_group.add_argument("--from-db", action="store_true", help="Load cameras from database")
    
    # Database options
    p.add_argument("--link-type", choices=["RTSP", "HLS"], help="Camera link type (required with --from-db)")
    p.add_argument("--exam-id", type=int, help="Exam ID (required with --from-db)")
    p.add_argument("--shift-id", type=int, help="Shift ID (required for HLS)")
    p.add_argument("--exam-date", help="Exam date YYYY-MM-DD (required for HLS)")
    
    # CSV/Excel options
    p.add_argument("--url-col",       default="Stream URL",       help="Column name for stream URL")
    p.add_argument("--id-col",        default="Camera ID",        help="Column name for expected ID")
    
    # Output
    p.add_argument("--output",        default="ocr_results.xlsx", help="Output Excel file")
    
    # Performance
    p.add_argument("--workers-net",   type=int, default=MAX_WORKERS_NET)
    p.add_argument("--workers-ocr",   type=int, default=MAX_WORKERS_OCR)
    p.add_argument("--resume",        action="store_true", help="Resume from checkpoint")
    
    # OCR options
    p.add_argument("--gpu",           action="store_true", help="Use GPU for EasyOCR")
    p.add_argument("--anthropic-key", default="",          help="Anthropic API key for Stage 3 Vision fallback")
    p.add_argument("--no-claude",     action="store_true", help="Disable Stage 3 Claude Vision")

    # ROI overrides
    p.add_argument("--roi-y1",  type=float, default=ROI_Y1_FRAC,  help="ID band top    (0–1 fraction of height)")
    p.add_argument("--roi-y2",  type=float, default=ROI_Y2_FRAC,  help="ID band bottom (0–1 fraction of height)")
    p.add_argument("--roi-lx1", type=float, default=ROI_LEFT_X1,  help="Left  strip X start (0–1)")
    p.add_argument("--roi-lx2", type=float, default=ROI_LEFT_X2,  help="Left  strip X end   (0–1)")
    p.add_argument("--roi-rx1", type=float, default=ROI_RIGHT_X1, help="Right strip X start (0–1)")
    p.add_argument("--roi-rx2", type=float, default=ROI_RIGHT_X2, help="Right strip X end   (0–1)")

    return p.parse_args()


def main():
    args = parse_args()

    # Validate database mode arguments
    if args.from_db:
        if not args.link_type or not args.exam_id:
            log.error("--from-db requires --link-type and --exam-id")
            sys.exit(1)
        if args.link_type == "HLS" and (not args.shift_id or not args.exam_date):
            log.error("HLS mode requires --shift-id and --exam-date")
            sys.exit(1)

    global ROI_Y1_FRAC, ROI_Y2_FRAC, ROI_LEFT_X1, ROI_LEFT_X2, ROI_RIGHT_X1, ROI_RIGHT_X2
    ROI_Y1_FRAC  = args.roi_y1
    ROI_Y2_FRAC  = args.roi_y2
    ROI_LEFT_X1  = args.roi_lx1
    ROI_LEFT_X2  = args.roi_lx2
    ROI_RIGHT_X1 = args.roi_rx1
    ROI_RIGHT_X2 = args.roi_rx2

    log.info("═" * 60)
    log.info("Camera ID OCR Verifier — 3-Stage Fallback | 1-Lakh Scale")
    
    if args.from_db:
        log.info(f"  Mode            : Database")
        log.info(f"  Link Type       : {args.link_type}")
        log.info(f"  Exam ID         : {args.exam_id}")
        if args.link_type == "HLS":
            log.info(f"  Shift ID        : {args.shift_id}")
            log.info(f"  Exam Date       : {args.exam_date}")
    else:
        log.info(f"  Mode            : CSV/Excel")
        log.info(f"  Input           : {args.input}")
        log.info(f"  URL column      : {args.url_col}")
        log.info(f"  Expected ID col : {args.id_col}")
    
    log.info(f"  Net workers     : {args.workers_net}")
    log.info(f"  OCR workers     : {args.workers_ocr}")
    log.info(f"  GPU (EasyOCR)   : {args.gpu}")
    log.info(f"  Stage 3 Vision  : {'disabled (--no-claude)' if args.no_claude else 'enabled' if args.anthropic_key else 'disabled (no key)'}")
    log.info(f"  ROI Y           : [{ROI_Y1_FRAC}, {ROI_Y2_FRAC}]")
    log.info(f"  ROI Left  X     : [{ROI_LEFT_X1}, {ROI_LEFT_X2}]")
    log.info(f"  ROI Right X     : [{ROI_RIGHT_X1}, {ROI_RIGHT_X2}]")
    log.info("═" * 60)

    init_easyocr(gpu=args.gpu)

    if args.anthropic_key and not args.no_claude:
        init_anthropic(args.anthropic_key)
    elif not args.no_claude:
        log.info("No --anthropic-key provided; Stage 3 (Claude Vision) disabled.")

    # Load data
    if args.from_db:
        df = load_from_database(args.link_type, args.exam_id, args.shift_id, args.exam_date)
    else:
        df = load_input(args.input, args.url_col, args.id_col)

    if args.resume:
        df = _load_checkpoint(df, "checkpoint_ocr.json")

    df = asyncio.run(_run_pipeline(df, args.workers_net, args.workers_ocr))
    _export_results(df, args.output)
    log.info(f"Log file: {LOG_FILE}")


if __name__ == "__main__":
    main()


# ── EXAMPLE COMMANDS ──────────────────────────────────────────────────────
#
# Basic (EasyOCR + Tesseract only, no API cost):
#   python camera_id_verifier.py \
#       --input cameras.csv \
#       --url-col "Stream URL" \
#       --id-col  "Camera ID" \
#       --output  results.xlsx
#
# Full 3-stage with Claude Vision fallback:
#   python camera_id_verifier.py \
#       --input cameras.csv \
#       --anthropic-key sk-ant-xxxxx \
#       --output results.xlsx
#
# GPU EasyOCR + custom ROI + resume:
#   python camera_id_verifier.py \
#       --input cameras.csv \
#       --gpu \
#       --roi-y1 0.80 --roi-y2 0.95 \
#       --roi-lx1 0.0 --roi-lx2 0.40 \
#       --roi-rx1 0.60 --roi-rx2 1.0 \
#       --resume \
#       --output results.xlsx
