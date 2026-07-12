# ---------------------------------------------------------------------------
# stdlib
# ---------------------------------------------------------------------------
import asyncio
import json
import logging
import multiprocessing
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# third-party
# ---------------------------------------------------------------------------
import aiohttp
import cv2
import numpy as np
import pandas as pd
import requests
from tqdm.asyncio import tqdm as atqdm

# ---------------------------------------------------------------------------
# internal — DB client
# ---------------------------------------------------------------------------
try:
    from utils.db_client import (
        get_hls_cameras,
        get_rtsp_cameras,
        update_hls_classification,
        update_rtsp_classification,
        update_hls_status,
        update_rtsp_status,
        insert_hls_classification_history,
    )
except ImportError:
    sys.path.append(os.path.join(os.path.dirname(__file__), "utils"))
    from db_client import (
        get_hls_cameras,
        get_rtsp_cameras,
        update_hls_classification,
        update_rtsp_classification,
        update_hls_status,
        update_rtsp_status,
        insert_hls_classification_history,
    )

# ===========================================================================
# Logging
# ===========================================================================
# Configure logging to stdout/stderr only - script_runner will capture and format
# This ensures logs appear in the main.log with proper timestamps and formatting
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],  # Only StreamHandler - no file logging
)
logger = logging.getLogger(__name__)

# ===========================================================================
# Configuration
# ===========================================================================
_CPU_CORES = multiprocessing.cpu_count()

# Project root = script-server/ (this file lives at scripts/detection/). Used to
# anchor SAVE_FOLDER and the vLLM start script. Mirrors the RTSP/NEET scripts.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))

MAX_WORKERS_NETWORK: int = int(os.getenv("VISION_MAX_WORKERS_NETWORK", "256"))
MAX_WORKERS_VLM:     int = int(os.getenv("VISION_MAX_WORKERS_VLM",     "16"))
THREAD_POOL_SIZE:    int = int(os.getenv("VISION_THREAD_POOL",         str(min(_CPU_CORES * 2, 128))))

TIMEOUT_EXTRACT_URL: int = 3
TIMEOUT_TEST_STREAM: int = 3
TIMEOUT_OPENCV_MS:   int = 3_000
TIMEOUT_FFMPEG:      int = 5
TIMEOUT_VLM:         int = int(os.getenv("VISION_TIMEOUT_VLM", "120"))

# Classification now uses the SAME vLLM-served model as the RTSP path
# (Qwen/Qwen3-VL-30B-A3B-Instruct via the OpenAI-compatible vLLM endpoint),
# replacing the previous Ollama qwen3-vl:8b backend.
VLLM_URL:          str  = os.getenv("VISION_VLLM_URL",          "http://localhost:8001/v1/chat/completions")
VLLM_MODEL:        str  = os.getenv("VISION_VLLM_MODEL",        "Qwen/Qwen3-VL-30B-A3B-Instruct")
STOP_VLLM_ON_EXIT: bool = os.getenv("VISION_STOP_VLLM_ON_EXIT", "1") == "1"

DEFAULT_EXAM_ID:   int = 2
DEFAULT_SHIFT_ID:  int = 7
DEFAULT_EXAM_DATE: str = datetime.now().strftime("%Y-%m-%d")

VISION_ASSIGNMENT_PAYLOAD: Optional[str] = os.getenv("VISION_ASSIGNMENT_PAYLOAD")

SAVE_FOLDER         = os.path.join(PROJECT_ROOT, "results/tcs_classification")
CHECKPOINT_FILE     = "checkpoint_progress.json"
CHECKPOINT_INTERVAL = 500
RESUME_FROM_CHECKPOINT = True
SCENE_COLUMN_TARGET = "Scene Classification"

# Captured classification frames are saved here and referenced by camera_links_hls.frame_path
# / camera_classification_hls.frame_path. The backend serves them from paths containing
# `/classification_frames/` — keep this dir name in sync with the RTSP path.
_SCRIPT_DIR         = os.path.dirname(os.path.abspath(__file__))
FRAMES_FOLDER       = os.path.join(_SCRIPT_DIR, "results", "classification_frames")

os.makedirs(SAVE_FOLDER, exist_ok=True)
os.makedirs(FRAMES_FOLDER, exist_ok=True)

# One shared thread-pool for all blocking I/O (OpenCV, FFmpeg, HLS extract)
_THREAD_POOL = ThreadPoolExecutor(max_workers=THREAD_POOL_SIZE)

logger.info("=" * 80)
logger.info("TCS Camera Classification — DB Mode | H200 Optimized")
logger.info(f"  CPU cores        : {_CPU_CORES}")
logger.info(f"  Network workers  : {MAX_WORKERS_NETWORK}")
logger.info(f"  VLM workers      : {MAX_WORKERS_VLM}")
logger.info(f"  Thread pool      : {THREAD_POOL_SIZE}")
logger.info(f"  VLM timeout      : {TIMEOUT_VLM}s")
logger.info(f"  vLLM model       : {VLLM_MODEL}")
logger.info(f"  vLLM URL         : {VLLM_URL}")
logger.info("=" * 80)

# ===========================================================================
# VLM prompt
# ===========================================================================
CATEGORIES = [
    "Server Room",
    "Outdoor Scene",
    "Outside Area",
    "Parking Lot",
    "Entry",
    "Computer Lab",
]

VLM_PROMPT = f"""You are an expert CCTV scene classifier for an Indian exam-center
deployment. You MUST classify every image into EXACTLY ONE of these six
categories (return the category name verbatim, case-sensitive):

{', '.join(CATEGORIES)}.

Category definitions (pick the closest match — exam-center vocabulary in
parentheses):
- "Computer Lab": an indoor room with rows or a grid of desktop computers,
  monitors and chairs/cubicles intended for candidates. Even partial views of
  multiple monitors with desks qualify. (lab / hall / exam hall / classroom
  with computers)
- "Server Room": a small back-office or technical room dominated by server
  racks, switches, network cabinets, UPS units, blinking LEDs, patch panels,
  or a single admin desk with one workstation and equipment. (UPS room,
  hub room, store room, observer/control/office room, biometric room)
- "Entry": a building entrance — main doors, gates, reception desk,
  signboards, security check or biometric counter facing the doorway.
- "Outside Area": building surroundings or interior circulation — corridors,
  hallways, washroom pathways, lobbies, staircases, boundary walls,
  external building view.
- "Parking Lot": open area with cars, bikes or painted parking lines.
- "Outdoor Scene": open natural scene dominated by sky, trees, gardens, or
  roads with no building context.

Decision policy — read carefully:
1. You MUST commit to one of the six categories above whenever any scene
   structure is visible. Pick the single closest match even if the image is
   dim, blurry, low-contrast, has a timestamp/date overlay, or shows an
   unfamiliar angle. These are NOT reasons to say Uncertain.
2. Return "Uncertain" ONLY if the image is completely black, completely
   white, completely uniform grey, or so scrambled/corrupted that no scene
   structure can be identified at all. If you can name even one object
   (chair, door, computer, wall, sky), you MUST pick a real category.
3. Do not invent categories. Do not return Markdown, prose, or code fences.

Output format — return ONLY this JSON object on a single line:
{{"category": "<one of the six categories above, or Uncertain>", "reason": "<one short sentence citing the strongest visual evidence>"}}"""


# ===========================================================================
# Helpers
# ===========================================================================

def _parse_payload() -> dict:
    if not VISION_ASSIGNMENT_PAYLOAD:
        return {}
    try:
        return json.loads(VISION_ASSIGNMENT_PAYLOAD)
    except Exception as e:
        logger.error(f"Failed to parse VISION_ASSIGNMENT_PAYLOAD: {e}")
        return {}


def _get_runtime_config() -> dict:
    payload         = _parse_payload()
    assignment_id   = payload.get("assignment_id")
    exam_id         = payload.get("exam_id")
    shift_id        = payload.get("shift_id")
    assignment_date = payload.get("assignment_date")
    link_type       = str(payload.get("link_type") or "HLS").upper()

    if assignment_id is None:
        raise ValueError("VISION_ASSIGNMENT_PAYLOAD must include assignment_id")
    if exam_id is None:
        raise ValueError("VISION_ASSIGNMENT_PAYLOAD must include exam_id")
    if link_type not in {"HLS", "RTSP"}:
        raise ValueError("link_type must be HLS or RTSP")
    if link_type == "HLS" and (shift_id is None or assignment_date is None):
        raise ValueError("HLS link_type requires shift_id and assignment_date")

    return {
        "assignment_id":   int(assignment_id),
        "exam_id":         int(exam_id),
        "shift_id":        int(shift_id) if shift_id is not None else None,
        "assignment_date": str(assignment_date) if assignment_date else None,
        "link_type":       link_type,
    }


def _camera_id(camera: dict) -> str:
    """Always returns a string — never None or empty unless DB row is broken."""
    return str(
        camera.get("ch_code") or
        camera.get("device_id") or
        camera.get("camera_id") or
        ""
    ).strip()


def _make_connector() -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(
        limit=MAX_WORKERS_NETWORK + MAX_WORKERS_VLM + 64,
        limit_per_host=0,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )


# ===========================================================================
# HLS URL extraction — 3-method (ported from Excel script)
# ===========================================================================

def _sync_extract_hls_url(initial_url: str) -> Optional[str]:
    """
    Blocking — runs in thread-pool.
    Tries 3 methods to find a .m3u8 URL in the camera portal response.
    """
    if not initial_url:
        return None
    try:
        import requests as _req
        resp = _req.get(initial_url, timeout=TIMEOUT_EXTRACT_URL, allow_redirects=True)
        if resp.status_code != 200:
            return None
        text = resp.text

        # Method 1: bare .m3u8 in response body
        m = re.findall(r'https?://[^\s<>"]+?\.m3u8[^\s<>"]*', text)
        if m:
            return m[0]

        # Method 2: JSON response with known keys
        try:
            jdata = resp.json()
            if isinstance(jdata, dict):
                for key in ("url", "stream_url", "hls_url", "playback_url", "streaming_url"):
                    if jdata.get(key):
                        return str(jdata[key])
        except Exception:
            pass

        # Method 3: quoted .m3u8 URL
        if ".m3u8" in text:
            m = re.findall(r'(["\'])(https?://[^"\']+\.m3u8[^"\']*)(["\'])', text)
            if m:
                return m[0][1]

        return None
    except Exception as e:
        logger.debug(f"HLS extract error ({initial_url[:60]}): {e}")
        return None


async def _async_extract_hls_url(
    initial_url: str,
    session: aiohttp.ClientSession,
) -> Optional[str]:
    """
    Fully async — uses the shared aiohttp session so all 256 concurrent
    Pass-1 checks run in true async I/O without queuing for thread-pool
    workers.  The old run_in_executor path only had THREAD_POOL_SIZE (~48)
    slots; tasks waiting >4 s in the queue were killed by wait_for before
    they ever ran, producing 100 % "timeout" failures.
    """
    if not initial_url:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_EXTRACT_URL)
        async with session.get(initial_url, timeout=timeout, allow_redirects=True) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()

            # Method 1: bare .m3u8 URL in response body
            m = re.findall(r'https?://[^\s<>"]+?\.m3u8[^\s<>"]*', text)
            if m:
                return m[0]

            # Method 2: JSON response with known keys
            try:
                jdata = json.loads(text)
                if isinstance(jdata, dict):
                    for key in ("url", "stream_url", "hls_url", "playback_url", "streaming_url"):
                        if jdata.get(key):
                            return str(jdata[key])
            except Exception:
                pass

            # Method 3: quoted .m3u8 URL
            if ".m3u8" in text:
                m = re.findall(r'(["\'])(https?://[^"\']+\.m3u8[^"\']*)(["\'])', text)
                if m:
                    return m[0][1]

            return None
    except Exception as e:
        logger.debug(f"HLS extract error ({str(initial_url)[:60]}): {e}")
        return None


# ===========================================================================
# HLS stream test
# ===========================================================================

async def _async_test_hls_stream(
    hls_url: str,
    session: aiohttp.ClientSession,
) -> Tuple[bool, Optional[int]]:
    try:
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_TEST_STREAM)
        async with session.get(hls_url, timeout=timeout) as resp:
            return resp.status == 200, resp.status
    except Exception:
        return False, None


# ===========================================================================
# Frame capture — in-memory (zero disk I/O per frame)
# ===========================================================================

def _sync_opencv_capture(url: str) -> Optional[bytes]:
    """Blocking — runs in thread-pool. Returns JPEG bytes or None."""
    try:
        cap = cv2.VideoCapture(url)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, TIMEOUT_OPENCV_MS)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, TIMEOUT_OPENCV_MS)
        if not cap.isOpened():
            return None
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        return buf.tobytes() if ok else None
    except Exception as e:
        logger.debug(f"OpenCV error: {e}")
        return None


def _sync_ffmpeg_capture(url: str, is_rtsp: bool = False) -> Optional[bytes]:
    """Blocking — runs in thread-pool. FFmpeg writes to stdout pipe, not disk."""
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
        res = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT_FFMPEG
        )
        return res.stdout if res.returncode == 0 and res.stdout else None
    except Exception as e:
        logger.debug(f"FFmpeg error: {e}")
        return None


async def _capture_frame_bytes(url: str, is_rtsp: bool = False) -> Optional[bytes]:
    """Async wrapper — tries OpenCV, falls back to FFmpeg."""
    loop = asyncio.get_event_loop()
    jpeg = await loop.run_in_executor(_THREAD_POOL, _sync_opencv_capture, url)
    if jpeg:
        return jpeg
    logger.debug("OpenCV failed → FFmpeg fallback")
    return await loop.run_in_executor(_THREAD_POOL, _sync_ffmpeg_capture, url, is_rtsp)


# ===========================================================================
# Grey / obstruction check — in-memory numpy (no disk read)
# ===========================================================================

def _is_grey_or_obstructed(jpeg_bytes: bytes) -> Tuple[bool, str]:
    try:
        arr = np.frombuffer(jpeg_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return True, "Failed to decode frame"
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if gray.std() < 5:
            return True, "Uniform grey screen"
        mean = gray.mean()
        if mean < 10:
            return True, "Very dark / blocked camera"
        if mean > 245:
            return True, "Overexposed / white screen"
        if cv2.Laplacian(gray, cv2.CV_64F).var() < 10:
            return True, "Low detail / obstructed"
        return False, ""
    except Exception as e:
        logger.debug(f"Grey check error: {e}")
        return False, ""


# ===========================================================================
# VLM classification — async, concurrent, KV-cache capped
# ===========================================================================

async def _classify_with_vlm(
    jpeg_bytes: bytes,
    session: aiohttp.ClientSession,
) -> Tuple[str, str]:
    """
    Fully async VLM call via shared aiohttp session (connection pool).
    Uses the same vLLM-served Qwen3-VL-30B model as the RTSP path.
    Handles qwen3 <think>…</think> prefix before JSON.
    """
    loop = asyncio.get_event_loop()

    # Grey check in thread-pool
    is_bad, reason = await loop.run_in_executor(
        _THREAD_POOL, _is_grey_or_obstructed, jpeg_bytes
    )
    if is_bad:
        return "Uncertain/Obstructed", reason

    # Base64 in thread-pool (CPU work)
    image_b64 = await loop.run_in_executor(
        _THREAD_POOL, lambda: b64encode(jpeg_bytes).decode("utf-8")
    )

    payload = {
        "model": VLLM_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": VLM_PROMPT
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}"
                        }
                    }
                ]
            }
        ],
        "max_tokens": 256,
        "temperature": 0.0,
    }

    # Retry once on transient parse/empty failures — Qwen-VL occasionally
    # emits a truncated/empty turn that succeeds on a clean re-prompt.
    last_raw = ""
    for attempt in range(2):
        try:
            timeout = aiohttp.ClientTimeout(total=TIMEOUT_VLM)
            async with session.post(VLLM_URL, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    if attempt == 0:
                        last_raw = f"HTTP {resp.status}"
                        continue
                    logger.warning(f"VLM HTTP {resp.status}: {body[:200]}")
                    return "Uncertain", f"VLM HTTP {resp.status}"

                data    = await resp.json(content_type=None)
                choices = data.get("choices", [])
                if not choices:
                    if attempt == 0:
                        last_raw = "empty choices"
                        continue
                    return "Uncertain", "Empty choices from VLM"

                result = choices[0].get("message", {}).get("content", "").strip()
                last_raw = result
                if not result:
                    if attempt == 0:
                        continue
                    return "Uncertain", "Empty VLM response"

                # Strip qwen3 chain-of-thought prefix if present
                result = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL).strip()

                # Extract JSON block robustly
                start = result.find("{")
                end   = result.rfind("}") + 1
                if start == -1 or end == 0:
                    if attempt == 0:
                        continue
                    logger.warning(f"VLM returned no JSON block. Raw: {result[:300]!r}")
                    return "Uncertain", "No JSON in VLM response"

                try:
                    parsed = json.loads(result[start:end])
                except json.JSONDecodeError:
                    if attempt == 0:
                        continue
                    logger.warning(f"VLM JSON parse failed. Raw: {result[:300]!r}")
                    return "Uncertain", "Invalid JSON from VLM"

                category = str(parsed.get("category", "")).strip()
                reason   = str(parsed.get("reason", "")).strip()

                if (not category
                        or "uncertain" in category.lower()
                        or "unclear" in category.lower()
                        or "unknown" in category.lower()):
                    logger.info(f"VLM bailed to Uncertain. Raw: {result[:300]!r}")
                    return "Uncertain/Obstructed", reason or "model_returned_uncertain"

                return category, reason

        except asyncio.TimeoutError:
            if attempt == 0:
                last_raw = "timeout"
                continue
            logger.warning(f"VLM timeout after {TIMEOUT_VLM}s")
            return "Uncertain", "VLM timeout"
        except Exception as e:
            if attempt == 0:
                last_raw = f"exc: {e}"
                continue
            logger.warning(f"VLM error: {e}; last_raw={last_raw!r}")
            return "Uncertain", str(e)


# ===========================================================================
# Pass-1: accessibility check
#
# BUG FIX — camera_id extracted BEFORE any await
# -----------------------------------------------
# Previous version wrapped the entire coroutine in asyncio.wait_for(), so a
# TimeoutError fired before cam_id was extracted → camera_id="unknown", reason="".
# Fix: cam_id is set synchronously at the very top, before any network call.
# The timeout only wraps the awaitable network operations, not ID extraction.
# ===========================================================================

async def _check_hls_camera(
    camera: dict,
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
) -> dict:
    # ── Step 1: extract camera_id synchronously — cannot time out ─────────
    cam_id     = _camera_id(camera)          # always known from this line on
    source_url = str(camera.get("cctv_url") or "").strip()

    if not source_url:
        return {"camera_id": cam_id, "cam": camera,
                "accessible": False, "resolved_url": None, "reason": "missing_cctv_url"}

    # ── Step 2: network calls inside semaphore — can time out ────────────
    async with sem:
        try:
            hls_url = await asyncio.wait_for(
                _async_extract_hls_url(source_url, session),
                timeout=TIMEOUT_EXTRACT_URL + 1,
            )
            if not hls_url:
                return {"camera_id": cam_id, "cam": camera,
                        "accessible": False, "resolved_url": None, "reason": "hls_extract_failed"}

            is_ok, status_code = await asyncio.wait_for(
                _async_test_hls_stream(hls_url, session),
                timeout=TIMEOUT_TEST_STREAM + 1,
            )
            if not is_ok:
                return {"camera_id": cam_id, "cam": camera, "accessible": False,
                        "resolved_url": hls_url, "reason": f"http_{status_code or 'no_response'}"}

            return {"camera_id": cam_id, "cam": camera,
                    "accessible": True, "resolved_url": hls_url, "reason": "ok"}

        except asyncio.TimeoutError:
            # cam_id is still correct here — set before any await
            return {"camera_id": cam_id, "cam": camera,
                    "accessible": False, "resolved_url": None, "reason": "timeout"}
        except Exception as e:
            return {"camera_id": cam_id, "cam": camera,
                    "accessible": False, "resolved_url": None, "reason": str(e) or "error"}


async def _check_rtsp_camera(
    camera: dict,
    sem: asyncio.Semaphore,
) -> dict:
    """RTSP: no HTTP accessibility check needed — pass straight to Pass-2."""
    cam_id     = _camera_id(camera)
    source_url = str(camera.get("cctv_url") or "").strip()
    if not source_url:
        return {"camera_id": cam_id, "cam": camera,
                "accessible": False, "resolved_url": None, "reason": "missing_cctv_url"}
    return {"camera_id": cam_id, "cam": camera,
            "accessible": True, "resolved_url": source_url, "reason": "ok"}


# ===========================================================================
# Pass-2: frame capture + VLM
# ===========================================================================

async def _process_camera(
    access_result: dict,
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    is_rtsp: bool = False,
    frames_dir: str = FRAMES_FOLDER,
) -> dict:
    """
    Semaphore limits concurrent Ollama requests to MAX_WORKERS_VLM.
    The captured frame is saved under frames_dir (an assignment_<id> subdir of
    FRAMES_FOLDER) so its path can be persisted to camera_links_hls.frame_path /
    camera_classification_hls (mirrors RTSP).
    """
    cam_id = access_result["camera_id"]
    cam    = access_result["cam"]
    url    = access_result["resolved_url"]

    async with sem:
        try:
            jpeg = await _capture_frame_bytes(url, is_rtsp=is_rtsp)
            if not jpeg:
                return {
                    "camera_id":      cam_id,
                    "camera_pk":      cam.get("id"),
                    "camera_name":    cam.get("camera_name"),
                    "classification": "Uncertain",
                    "status":         "Not Accessible",
                    "detail":         "frame_capture_failed",
                    "frame_path":     None,
                }

            frame_path = await _save_frame_to_disk(jpeg, cam_id, frames_dir)

            label, reason = await _classify_with_vlm(jpeg, session)
            return {
                "camera_id":      cam_id,
                "camera_pk":      cam.get("id"),
                "camera_name":    cam.get("camera_name"),
                "classification": label,
                "status":         "Accessible",
                "detail":         reason or "classified",
                "frame_path":     frame_path,
            }

        except Exception as e:
            logger.error(f"Processing error for {cam_id}: {e}")
            return {
                "camera_id":      cam_id,
                "camera_pk":      cam.get("id"),
                "camera_name":    cam.get("camera_name"),
                "classification": "Uncertain",
                "status":         "Accessible",
                "detail":         str(e),
                "frame_path":     None,
            }


def _frames_dir_for_assignment(assignment_id=None) -> str:
    """Per-assignment subdir of FRAMES_FOLDER (mirrors RTSP), so frames from
    different runs don't collide and the NFS layout matches the RTSP path."""
    subdir = f"assignment_{assignment_id}" if assignment_id else "default"
    path = os.path.join(FRAMES_FOLDER, subdir)
    os.makedirs(path, exist_ok=True)
    return path


async def _save_frame_to_disk(jpeg: bytes, cam_id: str, frames_dir: str = FRAMES_FOLDER) -> Optional[str]:
    """Persist a captured JPEG to frames_dir; return the path (None on failure)."""
    loop     = asyncio.get_event_loop()
    safe_id  = re.sub(r'[^\w\-]', '_', str(cam_id))[:60]
    fname    = f"{safe_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    fpath    = os.path.join(frames_dir, fname)
    try:
        await loop.run_in_executor(_THREAD_POOL, lambda: open(fpath, "wb").write(jpeg))
        return fpath
    except Exception as e:
        logger.debug(f"Frame save failed for {cam_id}: {e}")
        return None


# ===========================================================================
# Bulk DB persist — one call after all tasks, not one per camera
# ===========================================================================

def _bulk_persist(runtime: dict, classifications: List[dict]) -> int:
    updated = 0
    history_inserted = 0
    for item in classifications:
        cam_id     = str(item.get("camera_id") or "").strip()
        label      = str(item.get("classification") or "").strip()
        status     = str(item.get("status") or "").strip() or None
        frame_path = str(item.get("frame_path") or "").strip() or None
        camera_pk  = item.get("camera_pk")
        if not cam_id or not label:
            continue
        try:
            if runtime["link_type"] == "HLS":
                updated += update_hls_classification(
                    runtime["exam_id"], runtime["shift_id"],
                    runtime["assignment_date"], cam_id, label, status, frame_path
                )
                history_inserted += insert_hls_classification_history(
                    camera_pk, runtime.get("shift_id"),
                    runtime.get("assignment_date"), frame_path, label,
                )
            else:
                updated += update_rtsp_classification(runtime["exam_id"], cam_id, label, status)
        except Exception as e:
            logger.error(f"DB persist failed for {cam_id}: {e}")
    logger.info(
        f"Bulk DB persist complete: {updated} rows updated, "
        f"{history_inserted} history rows inserted"
    )
    return updated


def _bulk_persist_status(runtime: dict, statuses: List[dict]) -> int:
    updated = 0
    for item in statuses:
        cam_id = str(item.get("camera_id") or "").strip()
        status = str(item.get("status") or "").strip()
        if not cam_id or not status:
            continue
        try:
            if runtime["link_type"] == "HLS":
                updated += update_hls_status(
                    runtime["exam_id"], runtime["shift_id"],
                    runtime["assignment_date"], cam_id, status
                )
            else:
                updated += update_rtsp_status(runtime["exam_id"], cam_id, status)
        except Exception as e:
            logger.error(f"DB status persist failed for {cam_id}: {e}")
    logger.info(f"Bulk DB status persist complete: {updated} rows updated")
    return updated


# ===========================================================================
# Checkpoint helpers
# ===========================================================================
_df_lock          = threading.Lock()
_checkpoint_count = 0
_df               = pd.DataFrame()


def _update_df(row_id: int, label: str, status: str, detail: str) -> None:
    global _checkpoint_count
    with _df_lock:
        mask = _df["Row_ID"] == row_id
        if mask.any():
            _df.loc[mask, SCENE_COLUMN_TARGET] = label
            _df.loc[mask, "Status"]            = status
            _df.loc[mask, "Status Detail"]     = detail
            _checkpoint_count += 1
            if _checkpoint_count % CHECKPOINT_INTERVAL == 0:
                _save_checkpoint()


def _save_checkpoint() -> None:
    try:
        data = {
            "timestamp":      datetime.now().isoformat(),
            "processed_rows": int((_df["Status"] != "").sum()),
            "total_rows":     len(_df),
            "dataframe":      _df.to_dict("records"),
        }
        with open(CHECKPOINT_FILE, "w") as f:
            json.dump(data, f)
        logger.info(f"Checkpoint: {data['processed_rows']}/{data['total_rows']}")
    except Exception as e:
        logger.error(f"Checkpoint save failed: {e}")


def _load_checkpoint() -> bool:
    global _df
    if not os.path.exists(CHECKPOINT_FILE):
        return False
    try:
        with open(CHECKPOINT_FILE) as f:
            data = json.load(f)
        logger.info(f"Checkpoint found ({data['timestamp']}): "
                    f"{data['processed_rows']}/{data['total_rows']} done")
        restored = pd.DataFrame(data["dataframe"])
        if len(restored) == len(_df):
            _df = restored
            return True
        logger.warning("Checkpoint row count mismatch — starting fresh")
        return False
    except Exception as e:
        logger.error(f"Checkpoint load failed: {e}")
        return False


# ===========================================================================
# Results export
# ===========================================================================

def _save_results(df: pd.DataFrame) -> None:
    os.makedirs(SAVE_FOLDER, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    accessible_df = df[
        (df["Status"] == "Accessible") &
        (~df["Status Detail"].str.contains("Uncertain|Obstructed", na=False))
    ].copy()
    uncertain_df = df[
        (df["Status"] == "Accessible") &
        (df["Status Detail"].str.contains("Uncertain|Obstructed", na=False))
    ].copy()
    not_accessible_df = df[df["Status"] == "Not Accessible"].copy()

    for frame in [accessible_df, uncertain_df, not_accessible_df]:
        frame.drop(columns=["Row_ID", "cam_id_ref"], errors="ignore", inplace=True)

    complete_file = os.path.join(SAVE_FOLDER, f"complete_results_{ts}.xlsx")
    with pd.ExcelWriter(complete_file, engine="openpyxl") as writer:
        if len(accessible_df):
            accessible_df.to_excel(writer, sheet_name="Classified",     index=False)
        if len(uncertain_df):
            uncertain_df.to_excel(writer,  sheet_name="Uncertain",      index=False)
        if len(not_accessible_df):
            not_accessible_df.to_excel(writer, sheet_name="Not Accessible", index=False)
        export = df.copy()
        export.drop(columns=["Row_ID", "cam_id_ref"], errors="ignore", inplace=True)
        export.to_excel(writer, sheet_name="All Results", index=False)

    logger.info(f"Results → {complete_file}")
    print(f"\n📁 Results: {complete_file}")


# ===========================================================================
# Assignment mode  (VISION_ASSIGNMENT_PAYLOAD is set)
# ===========================================================================

async def run_assignment_mode() -> dict:
    runtime = _get_runtime_config()
    is_rtsp = runtime["link_type"] == "RTSP"

    # Fetch from DB — exactly once
    cameras = (
        get_rtsp_cameras(runtime["exam_id"])
        if is_rtsp else
        get_hls_cameras(runtime["exam_id"], runtime["shift_id"], runtime["assignment_date"])
    ) or []

    logger.info(
        f"Assignment | id={runtime['assignment_id']} "
        f"type={runtime['link_type']} cameras={len(cameras)}"
    )

    if not cameras:
        return {
            "assignment_id":                    runtime["assignment_id"],
            "link_type":                        runtime["link_type"],
            "processed":                        0,
            "failed":                           [],
            "classifications":                  [],
            "persisted_classification_updates": 0,
        }

    sem_net = asyncio.Semaphore(MAX_WORKERS_NETWORK)
    sem_vlm = asyncio.Semaphore(MAX_WORKERS_VLM)

    accessible: List[dict] = []
    failed:     List[dict] = []

    async with aiohttp.ClientSession(connector=_make_connector()) as session:

        # ── Pass-1 ────────────────────────────────────────────────────────
        logger.info(f"Pass-1 | {len(cameras)} cameras | {MAX_WORKERS_NETWORK} concurrent")
        p1_coros = (
            [_check_rtsp_camera(c, sem_net) for c in cameras]
            if is_rtsp else
            [_check_hls_camera(c, session, sem_net) for c in cameras]
        )
        p1_start = time.time()

        async for fut in atqdm(
            asyncio.as_completed(p1_coros),
            total=len(p1_coros),
            desc="Pass-1 accessibility",
        ):
            res = await fut
            if res["accessible"]:
                accessible.append(res)
            else:
                failed.append({"camera_id": res["camera_id"], "reason": res["reason"]})

        logger.info(
            f"Pass-1 done {time.time()-p1_start:.1f}s | "
            f"accessible={len(accessible)} failed={len(failed)}"
        )
        
        statuses_to_persist = []
        for x in accessible:
            statuses_to_persist.append({"camera_id": x["camera_id"], "status": "Accessible"})
        for x in failed:
            statuses_to_persist.append({"camera_id": x["camera_id"], "status": "Not Accessible"})
        _bulk_persist_status(runtime, statuses_to_persist)

        if not accessible:
            logger.warning("No accessible cameras — nothing to classify")
            return {
                "assignment_id":                    runtime["assignment_id"],
                "link_type":                        runtime["link_type"],
                "processed":                        0,
                "failed":                           failed,
                "classifications":                  [],
                "persisted_classification_updates": 0,
            }

        # ── Pass-2 ────────────────────────────────────────────────────────
        frames_dir = _frames_dir_for_assignment(runtime.get("assignment_id"))
        logger.info(
            f"Pass-2 | {len(accessible)} cameras | "
            f"{MAX_WORKERS_VLM} concurrent VLM | model={VLLM_MODEL}"
        )
        p2_coros = [
            _process_camera(r, session, sem_vlm, is_rtsp=is_rtsp, frames_dir=frames_dir)
            for r in accessible
        ]
        classifications: List[dict] = []
        p2_start = time.time()

        async for fut in atqdm(
            asyncio.as_completed(p2_coros),
            total=len(p2_coros),
            desc="Pass-2 classifying",
        ):
            res = await fut
            classifications.append({
                "camera_id":      res["camera_id"],
                "camera_pk":      res.get("camera_pk"),
                "camera_name":    res.get("camera_name"),
                "classification": res["classification"],
                "status":         res.get("status"),
                "frame_path":     res.get("frame_path"),
            })
            if res.get("status") != "Accessible":
                failed.append({"camera_id": res["camera_id"], "reason": res.get("detail")})

        logger.info(f"Pass-2 done {time.time()-p2_start:.1f}s")

    # Bulk persist — one round-trip at the end
    updated = _bulk_persist(runtime, classifications)

    return {
        "assignment_id":                    runtime["assignment_id"],
        "link_type":                        runtime["link_type"],
        "processed":                        len(classifications),
        "failed":                           failed,
        "classifications":                  classifications,
        "persisted_classification_updates": updated,
    }


# ===========================================================================
# Default mode  (no VISION_ASSIGNMENT_PAYLOAD)
# ===========================================================================

async def _default_pipeline(cameras: List[dict], runtime: dict) -> None:
    global _df
    is_rtsp = runtime["link_type"] == "RTSP"
    sem_net = asyncio.Semaphore(MAX_WORKERS_NETWORK)
    sem_vlm = asyncio.Semaphore(MAX_WORKERS_VLM)

    accessible: List[dict] = []

    async with aiohttp.ClientSession(connector=_make_connector()) as session:

        # Pass-1
        logger.info(f"Pass-1 | {len(cameras)} cameras | {MAX_WORKERS_NETWORK} concurrent")
        p1_coros = (
            [_check_rtsp_camera(c, sem_net) for c in cameras]
            if is_rtsp else
            [_check_hls_camera(c, session, sem_net) for c in cameras]
        )
        p1_start = time.time()

        async for fut in atqdm(
            asyncio.as_completed(p1_coros),
            total=len(p1_coros),
            desc="Pass-1",
        ):
            res = await fut
            row_match = _df[_df["cam_id_ref"] == res["camera_id"]]
            if row_match.empty:
                continue
            row_id = int(row_match.iloc[0]["Row_ID"])
            if res["accessible"]:
                accessible.append({**res, "row_id": row_id})
            else:
                _update_df(row_id, "Failed to Classify", "Not Accessible", res["reason"])

        logger.info(
            f"Pass-1 done {time.time()-p1_start:.1f}s | "
            f"accessible={len(accessible)} not_accessible={len(cameras)-len(accessible)}"
        )
        _save_checkpoint()

        if not accessible:
            logger.warning("No accessible cameras")
            return

        # Pass-2
        frames_dir = _frames_dir_for_assignment(runtime.get("assignment_id"))
        logger.info(
            f"Pass-2 | {len(accessible)} cameras | "
            f"{MAX_WORKERS_VLM} VLM workers | model={VLLM_MODEL}"
        )
        p2_coros = [
            _process_camera(r, session, sem_vlm, is_rtsp=is_rtsp, frames_dir=frames_dir)
            for r in accessible
        ]
        p2_start = time.time()

        async for fut in atqdm(
            asyncio.as_completed(p2_coros),
            total=len(p2_coros),
            desc="Pass-2",
        ):
            res = await fut
            row_match = _df[_df["cam_id_ref"] == res["camera_id"]]
            if row_match.empty:
                continue
            row_id = int(row_match.iloc[0]["Row_ID"])
            _update_df(row_id, res["classification"], res["status"], res["detail"])

        logger.info(f"Pass-2 done {time.time()-p2_start:.1f}s")

    _save_checkpoint()


def main() -> None:
    global _df

    payload    = _parse_payload()
    exam_id    = payload.get("exam_id",         DEFAULT_EXAM_ID)
    shift_id   = payload.get("shift_id",        DEFAULT_SHIFT_ID)
    exam_date  = payload.get("assignment_date", DEFAULT_EXAM_DATE)
    link_type  = str(payload.get("link_type") or "HLS").upper()
    is_rtsp    = link_type == "RTSP"

    # Fetch from DB — exactly once
    logger.info(f"Loading cameras | exam={exam_id} shift={shift_id} date={exam_date}")
    cameras = (
        get_rtsp_cameras(exam_id)
        if is_rtsp else
        get_hls_cameras(exam_id, shift_id, exam_date)
    ) or []

    if not cameras:
        logger.warning("No cameras found — exiting")
        return

    logger.info(f"Loaded {len(cameras)} cameras from DB")

    # Build internal DataFrame for checkpoint + result tracking
    _df = pd.DataFrame(cameras)
    if "cctv_url"       in _df.columns: _df["CCTV URL"]          = _df["cctv_url"]
    if "classification" in _df.columns: _df[SCENE_COLUMN_TARGET] = _df["classification"]
    _df["Status"]        = ""
    _df["Status Detail"] = ""
    _df["Row_ID"]        = range(len(_df))
    _df["cam_id_ref"]    = _df.apply(lambda r: _camera_id(r.to_dict()), axis=1)
    _df.fillna("", inplace=True)

    if RESUME_FROM_CHECKPOINT and _load_checkpoint():
        remaining = int((_df["Status"] == "").sum())
        if remaining == 0:
            logger.info("All cameras already processed")
            _save_results(_df)
            return
        logger.info(f"Resuming — {remaining} cameras remaining")

    runtime = {
        "exam_id":         exam_id,
        "shift_id":        shift_id,
        "assignment_date": exam_date,
        "link_type":       link_type,
    }

    start = time.time()
    asyncio.run(_default_pipeline(cameras, runtime))

    # Bulk DB persist
    classifications = [
        {"camera_id": row["cam_id_ref"], "classification": row[SCENE_COLUMN_TARGET]}
        for _, row in _df[_df["Status"] == "Accessible"].iterrows()
    ]
    _bulk_persist(runtime, classifications)

    total      = time.time() - start
    accessible = int((_df["Status"] == "Accessible").sum())
    not_acc    = int((_df["Status"] == "Not Accessible").sum())
    uncertain  = int(_df["Status Detail"].str.contains("Uncertain|Obstructed", na=False).sum())

    summary = f"""
{'='*80}
FINAL SUMMARY
{'='*80}
Total Cameras            : {len(_df)}
Classified (clean)       : {accessible - uncertain}
Accessible but Uncertain : {uncertain}
Not Accessible           : {not_acc}
Total Time               : {int(total//60)}m {int(total%60)}s
Success Rate             : {accessible / max(len(_df), 1) * 100:.1f}%
{'='*80}"""
    print(summary)
    logger.info(summary)

    _save_results(_df)
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)


# ===========================================================================
# vLLM server lifecycle — shared with the RTSP classification path.
# Both scripts hit the same OpenAI-compatible server on :8001 serving
# Qwen/Qwen3-VL-30B-A3B-Instruct. If it's already up we reuse it; otherwise
# we start it via start_vllm.sh and stop it on exit to free GPU memory.
# ===========================================================================
_VLLM_PROC: Optional[subprocess.Popen] = None
_VLLM_CLEANUP_DONE = False


def _find_vllm_pids() -> List[int]:
    """Find local vLLM processes for the classification server."""
    patterns = (
        "vllm serve",
        "vllm.entrypoints.openai.api_server",
        "VLLM::EngineCore",
    )
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,cmd"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except Exception as e:
        logger.debug(f"Unable to inspect vLLM processes: {e}")
        return []

    pids: List[int] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue

        pid_text, cmd = parts
        if not any(pattern in cmd for pattern in patterns):
            continue

        # Keep the match scoped to this classification server/model.
        if "VLLM::EngineCore" not in cmd and "--port 8001" not in cmd and VLLM_MODEL not in cmd:
            continue

        try:
            pid = int(pid_text)
        except ValueError:
            continue

        if pid != os.getpid() and pid not in pids:
            pids.append(pid)

    return pids


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _shutdown_vllm(reason: str) -> None:
    """
    Stop the vLLM server used by classification.

    The scheduler stops classification with SIGTERM at shift end. Without a
    signal-aware cleanup path, the vLLM server can stay alive and keep GPU RAM.
    """
    global _VLLM_CLEANUP_DONE
    if _VLLM_CLEANUP_DONE:
        return
    _VLLM_CLEANUP_DONE = True

    pids = set()
    if _VLLM_PROC and _VLLM_PROC.poll() is None:
        pids.add(_VLLM_PROC.pid)

    if STOP_VLLM_ON_EXIT:
        pids.update(_find_vllm_pids())

    if not pids:
        return

    logger.info(f"Shutting down vLLM server ({reason}) to free GPU memory: pids={sorted(pids)}")

    if _VLLM_PROC and _VLLM_PROC.poll() is None:
        try:
            os.killpg(os.getpgid(_VLLM_PROC.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.warning(f"Failed to SIGTERM owned vLLM process group: {e}")

    for pid in sorted(pids):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.warning(f"Failed to SIGTERM vLLM pid={pid}: {e}")

    deadline = time.time() + 15
    while time.time() < deadline:
        if not any(_pid_alive(pid) for pid in pids):
            return
        time.sleep(0.5)

    alive = [pid for pid in pids if _pid_alive(pid)]
    if not alive:
        return

    logger.warning(f"vLLM did not stop after SIGTERM; escalating to SIGKILL: pids={alive}")
    if _VLLM_PROC and _VLLM_PROC.poll() is None:
        try:
            os.killpg(os.getpgid(_VLLM_PROC.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.warning(f"Failed to SIGKILL owned vLLM process group: {e}")

    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.warning(f"Failed to SIGKILL vLLM pid={pid}: {e}")

    # ── Orphan reaper ────────────────────────────────────────────────────
    # vLLM's tensor-parallel workers are Python multiprocessing.spawn children
    # that sometimes survive SIGKILL on the parent and keep holding GPU/RAM.
    try:
        subprocess.run(
            ["pkill", "-9", "-f", "spawn_main.*--multiprocessing-fork"],
            check=False, timeout=5,
        )
        logger.info("orphan reaper: dispatched SIGKILL to lingering spawn_main subprocesses")
    except Exception as e:
        logger.debug(f"orphan reaper failed: {e}")


def _handle_shutdown_signal(signum, frame):
    logger.warning(f"Received signal {signum}; cleaning up classification vLLM before exit")
    _shutdown_vllm(f"signal {signum}")
    raise SystemExit(128 + signum)


def _ensure_vllm_running():
    global _VLLM_PROC
    try:
        if requests.get("http://localhost:8001/health", timeout=2).status_code == 200:
            if STOP_VLLM_ON_EXIT:
                logger.info("vLLM server is already running; it will be stopped when classification exits.")
            else:
                logger.info("vLLM server is already running in background and will be left running.")
            return None
    except Exception:
        pass

    logger.info("Starting temporary vLLM server process. Please wait (~20 seconds)...")
    _vllm_script = os.getenv("VLLM_START_SCRIPT", os.path.join(PROJECT_ROOT, "start_vllm.sh"))
    logger.info(f"Using vLLM start script: {_vllm_script}")
    proc = subprocess.Popen(
        ["/bin/bash", _vllm_script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid
    )
    _VLLM_PROC = proc
    for _ in range(300):
        try:
            if requests.get("http://localhost:8001/health", timeout=2).status_code == 200:
                logger.info("Temporary vLLM server successfully started!")
                return proc
        except Exception:
            pass
        time.sleep(2)
        if proc.poll() is not None:
            logger.error("vLLM server crashed during startup.")
            return proc
    logger.error("vLLM server timed out during startup.")
    return proc


# ===========================================================================
# Entry point
# ===========================================================================
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    logger.info("=" * 80)
    logger.info("TCS Camera Classification — starting")
    logger.info("=" * 80)

    _ensure_vllm_running()

    try:
        if VISION_ASSIGNMENT_PAYLOAD:
            result = asyncio.run(run_assignment_mode())
            print(json.dumps(result, indent=2))
        else:
            main()
        logger.info("Done.")
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        _shutdown_vllm("classification exit")