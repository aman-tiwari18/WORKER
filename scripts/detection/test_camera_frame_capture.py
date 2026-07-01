#!/usr/bin/env python3
"""
Test Camera Frame Capture & Classification
============================================
Loads 20 RTSP cameras for exam_id=20 from DB, authenticates with Camview,
grabs one frame per camera, and classifies it via VLM (Ollama).

Usage:
    python test_camera_frame_capture.py
    python test_camera_frame_capture.py --exam-id 20 --limit 20
    python test_camera_frame_capture.py --exam-id 20 --limit 5 --skip-vlm   # just test frames
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

import cv2
import numpy as np
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Path setup ───────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, "../.."))
sys.path.insert(0, BASE_DIR)

from utils.db_client import get_rtsp_cameras

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_DIR = os.path.join(PROJECT_ROOT, "scripts/logs/test_frames")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"test_frames_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
CAMVIEW_USER = os.getenv("CAMVIEW_AUTH_USER", "system")
CAMVIEW_PASS = os.getenv("CAMVIEW_AUTH_PASS", "Sasd@12345")
TIMEOUT_API  = 10
TIMEOUT_FFMPEG = 15
OLLAMA_URL   = os.getenv("VISION_OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("VISION_OLLAMA_MODEL", "qwen3-vl:8b")

FRAMES_DIR = os.path.join(PROJECT_ROOT, "results/test_frame_capture")
os.makedirs(FRAMES_DIR, exist_ok=True)

# Force RTSP over TCP
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

# ── Token cache (shared with other scripts) ──────────────────────────────────
TOKEN_CACHE_FILE = os.path.join(BASE_DIR, "..", "..", "server_tokens.json")
TOKEN_LOCK_FILE  = TOKEN_CACHE_FILE + ".lock"
_tokens: Dict[str, str]   = {}
_timestamps: Dict[str, float] = {}
_lock = threading.Lock()


# ===========================================================================
# Camview Authentication (same logic as vision_classification_rtsp.py)
# ===========================================================================

def _camview_signature(user: str, password: str, realm: str, random_key: str) -> str:
    t = hashlib.md5(password.encode()).hexdigest()
    t = hashlib.md5((user + t).encode()).hexdigest()
    t = hashlib.md5(t.encode()).hexdigest()
    t = hashlib.md5((user + ":" + realm + ":" + t).encode()).hexdigest()
    return hashlib.md5((t + ":" + random_key).encode()).hexdigest()


def _is_private_ip(url: str) -> bool:
    m = re.search(r'://(\d+\.\d+\.\d+\.\d+)', url)
    if not m:
        return False
    p = list(map(int, m.group(1).split('.')))
    return (p[0] == 10 or
            (p[0] == 172 and 16 <= p[1] <= 31) or
            (p[0] == 192 and p[1] == 168))


def _probe_port(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def authenticate_server(server: str) -> Optional[str]:
    """Two-step Camview challenge/response auth. Returns token or None."""
    auth_url = f"https://{server}/admin/API/accounts/authorize"
    headers  = {"Content-Type": "application/json"}
    payload  = {"userName": CAMVIEW_USER, "ipAddress": "", "clientType": "WINPC_V1"}

    for attempt in range(1, 3):
        try:
            r1 = requests.post(auth_url, headers=headers, json=payload,
                               timeout=TIMEOUT_API, verify=False)
            if r1.status_code not in (200, 401):
                logger.warning(f"  Auth step1 HTTP {r1.status_code} for {server}")
                continue

            j1 = r1.json()
            realm      = j1.get("realm")
            random_key = j1.get("randomKey")
            public_key = j1.get("publickey") or j1.get("publicKey")
            cookie     = r1.headers.get("Set-Cookie", "")

            if not all([realm, random_key, public_key]):
                logger.warning(f"  Auth step1 missing fields for {server}")
                continue

            sig = _camview_signature(CAMVIEW_USER, CAMVIEW_PASS, realm, random_key)
            p2 = {
                "mac": "", "signature": sig, "userName": CAMVIEW_USER,
                "randomKey": random_key, "publicKey": public_key,
                "encryptType": "MD5", "ipAddress": "", "clientType": "WINPC_V1",
                "userType": "0",
            }
            r2 = requests.post(auth_url,
                               headers={"Content-Type": "application/json", "Cookie": cookie},
                               json=p2, timeout=TIMEOUT_API, verify=False)
            j2 = r2.json()
            token = j2.get("token")
            if token:
                logger.info(f"  ✅ Authenticated: {server}")
                return token
            else:
                logger.warning(f"  Auth step2 no token for {server}: code={j2.get('code')}")
        except Exception as e:
            logger.warning(f"  Auth attempt {attempt} error for {server}: {e}")
        time.sleep(attempt)

    logger.error(f"  ❌ Auth FAILED: {server}")
    return None


def get_token(server: str) -> Optional[str]:
    """Get cached token or authenticate fresh. Thread-safe."""
    with _lock:
        tok = _tokens.get(server)
        ts  = _timestamps.get(server, 0)
        if tok and tok != "FAILED" and time.time() - ts < 3600:
            return tok

    # Try disk cache first
    try:
        import fcntl
        with open(TOKEN_LOCK_FILE, "w") as fl:
            fcntl.flock(fl, fcntl.LOCK_EX)
            if os.path.exists(TOKEN_CACHE_FILE):
                with open(TOKEN_CACHE_FILE) as f:
                    data = json.load(f)
                disk_tok = data.get("tokens", {}).get(server)
                disk_ts  = float(data.get("timestamps", {}).get(server, 0))
                if disk_tok and disk_tok != "FAILED" and time.time() - disk_ts < 3600:
                    with _lock:
                        _tokens[server] = disk_tok
                        _timestamps[server] = disk_ts
                    fcntl.flock(fl, fcntl.LOCK_UN)
                    logger.info(f"  Using cached token for {server}")
                    return disk_tok
            fcntl.flock(fl, fcntl.LOCK_UN)
    except Exception:
        pass

    # Authenticate fresh
    token = authenticate_server(server)
    now = time.time()
    with _lock:
        _tokens[server] = token if token else "FAILED"
        _timestamps[server] = now

    # Save to disk
    try:
        import fcntl
        with open(TOKEN_LOCK_FILE, "w") as fl:
            fcntl.flock(fl, fcntl.LOCK_EX)
            with open(TOKEN_CACHE_FILE, "w") as f:
                json.dump({"tokens": _tokens, "timestamps": _timestamps}, f)
            fcntl.flock(fl, fcntl.LOCK_UN)
    except Exception:
        pass

    return token


def start_video(server: str, device_id: str, camera_no: str) -> Tuple[Optional[List[str]], bool]:
    """Call Camview StartVideo API. Returns (url_list_with_tokens, success)."""
    token = get_token(server)
    if not token:
        return None, False

    sv_url  = f"https://{server}/admin/API/MTS/Video/StartVideo?token={token}"
    headers = {"Content-Type": "application/json", "X-Subject-Token": token}
    payload = {
        "clientType": "WINPC_V1", "project": "PSDK",
        "method": "MTS.Video.StartVideo",
        "data": {
            "streamType":  "1",
            "optional":    f"/admin/API/MTS/Video/StartVideo?token={token}",
            "channelId":   f"{device_id}$1$0${camera_no}",
            "dataType":    "2",
            "enableRtsps": "0",
        },
    }

    try:
        res = requests.post(sv_url, headers=headers, json=payload,
                            timeout=TIMEOUT_API, verify=False)

        if res.status_code == 401:
            logger.warning(f"  StartVideo 401 for {device_id}_{camera_no} — token expired, re-auth")
            with _lock:
                current_token = _tokens.get(server)
                if current_token == token:
                    _tokens.pop(server, None)
                    _timestamps.pop(server, None)
                    # We are the first to hit 401, so we authenticate
                    fresh_token = authenticate_server(server)
                    if fresh_token:
                        _tokens[server] = fresh_token
                        _timestamps[server] = time.time()
                    token = fresh_token
                else:
                    # Another thread already refreshed the token
                    token = current_token

            if not token:
                return None, False
            
            # Retry once with the new token
            sv_url = f"https://{server}/admin/API/MTS/Video/StartVideo?token={token}"
            headers["X-Subject-Token"] = token
            payload["data"]["optional"] = f"/admin/API/MTS/Video/StartVideo?token={token}"
            res = requests.post(sv_url, headers=headers, json=payload,
                                timeout=TIMEOUT_API, verify=False)

        if res.status_code != 200:
            logger.warning(f"  StartVideo HTTP {res.status_code} for {device_id}_{camera_no}")
            return None, False

        body = res.json()
        data = body.get("data") or {}
        urls: List[str] = []
        for key in ("url", "url2"):
            raw = data.get(key)
            if raw:
                urls.extend(p.strip() for p in raw.split("|") if p.strip())

        stream_token = data.get("token") or token

        if not urls:
            logger.warning(f"  StartVideo no URLs for {device_id}_{camera_no}: {json.dumps(body)[:200]}")
            return None, False

        # Filter private IPs — prefer public
        public = [u for u in urls if not _is_private_ip(u)]
        urls = public if public else urls

        # Port probe
        m = re.match(r'[a-z]+://([^/:]+)(?::(\d+))?', urls[0])
        if m:
            host = m.group(1)
            port = int(m.group(2)) if m.group(2) else 554
            if not _probe_port(host, port):
                logger.warning(f"  Port {port} on {host} unreachable for {device_id}_{camera_no}")
                return None, False

        final = [f"{u}?token={stream_token}" for u in urls]
        return final, True

    except Exception as e:
        logger.error(f"  StartVideo exception for {device_id}_{camera_no}: {e}")
        return None, False


# ===========================================================================
# Frame Capture (FFmpeg first for RTSP, OpenCV fallback)
# ===========================================================================

def capture_frame_ffmpeg(url: str) -> Optional[bytes]:
    """Capture one frame via FFmpeg. Returns JPEG bytes or None."""
    if not shutil.which("ffmpeg"):
        return None
    cmd = [
        "ffmpeg", "-y",
        "-rtsp_transport", "tcp",
        "-timeout", str(TIMEOUT_FFMPEG * 1_000_000),
        "-i", url,
        "-frames:v", "1",
        "-f", "image2",
        "-vcodec", "mjpeg",
        "-loglevel", "error",
        "pipe:1",
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=TIMEOUT_FFMPEG + 5)
        if res.returncode == 0 and res.stdout:
            stderr_txt = res.stderr.decode(errors="replace")
            if "401" in stderr_txt or "Unauthorized" in stderr_txt:
                return None
            return res.stdout
    except Exception as e:
        logger.debug(f"  FFmpeg error: {e}")
    return None


def capture_frame_opencv(url: str) -> Optional[bytes]:
    """Capture one frame via OpenCV. Returns JPEG bytes or None."""
    result_box: List[Optional[bytes]] = [None]

    def _worker():
        try:
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
            if not cap.isOpened():
                return
            for _ in range(2):
                cap.read()
            ret, frame = cap.read()
            cap.release()
            if not ret or frame is None:
                return
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if ok:
                result_box[0] = buf.tobytes()
        except Exception:
            pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=10)
    return result_box[0]


def capture_frame(url: str) -> Optional[bytes]:
    """Try FFmpeg first (reliable for RTSP), fall back to OpenCV."""
    # DO NOT unquote! DSS servers require %24 for $ and %26 for &
    jpeg = capture_frame_ffmpeg(url)
    if jpeg:
        return jpeg
    logger.debug("  FFmpeg failed, trying OpenCV...")
    return capture_frame_opencv(url)


# ===========================================================================
# VLM Classification
# ===========================================================================

CATEGORIES = [
    "Server Room", "Outdoor Scene", "Outside Area",
    "Parking Lot", "Entry", "Computer Lab",
]

VLM_PROMPT = f"""You are an expert computer vision classifier.
Classify the given image into one of the following categories:

{', '.join(CATEGORIES)}.

Rules:
- "Server Room": small room with limited visibility with laptop or computers.
- "Computer Lab": large room with many computers, chairs, people or desks.
- "Outdoor Scene": nature, roads, gardens, sky, trees.
- "Outside Area": surroundings of a building, campus, boundary, hallway, outdoor environment.
- "Parking Lot": cars, bikes, open area with marked spaces or vehicles.
- "Entry": building entrances, gates, doors, signboards, or main access points.

If the image is grey, blank, heavily obstructed, or unclear, respond with "Uncertain".

Return **only** the JSON response:
{{"category": "<one of the above categories or Uncertain>", "reason": "<short explanation>"}}"""


def classify_frame(jpeg_bytes: bytes) -> Tuple[str, str]:
    """Classify a JPEG frame using Ollama VLM. Returns (category, reason)."""
    # Quick grey check
    arr = np.frombuffer(jpeg_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return "Uncertain", "decode_failed"
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if gray.std() < 5:
        return "Uncertain/Obstructed", "uniform_grey"
    if gray.mean() < 10:
        return "Uncertain/Obstructed", "very_dark"

    image_b64 = b64encode(jpeg_bytes).decode("utf-8")
    payload = {
        "model":  OLLAMA_MODEL,
        "prompt": VLM_PROMPT,
        "images": [image_b64],
        "stream": False,
        "options": {"num_ctx": 2048, "num_predict": 256, "temperature": 0.0},
    }

    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
        if resp.status_code != 200:
            return "Uncertain", f"VLM HTTP {resp.status_code}"
        result = resp.json().get("response", "").strip()
        # Strip qwen3 <think> prefix
        result = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL).strip()
        start = result.find("{")
        end   = result.rfind("}") + 1
        if start == -1 or end == 0:
            return "Uncertain", "no_json"
        parsed = json.loads(result[start:end])
        cat = parsed.get("category", "Uncertain").strip()
        reason = parsed.get("reason", "").strip()
        return cat, reason
    except Exception as e:
        return "Uncertain", str(e)


# ===========================================================================
# Process One Camera
# ===========================================================================

def process_camera(cam: dict, skip_vlm: bool = False) -> dict:
    """Full pipeline for one camera: auth → RTSP URL → frame → classify."""
    device_id = str(cam.get("device_id") or "").strip()
    camera_no = str(cam.get("camera_no") or "").strip()
    raw_url   = str(cam.get("cctv_url") or "").strip()
    cam_name  = cam.get("camera_name", "")
    label     = f"{device_id}_{camera_no}"

    result = {
        "label": label,
        "device_id": device_id,
        "camera_no": camera_no,
        "camera_name": cam_name,
        "server": "",
        "rtsp_url": "",
        "frame_captured": False,
        "frame_path": "",
        "classification": "",
        "reason": "",
        "error": "",
    }

    if not raw_url:
        result["error"] = "missing_cctv_url"
        return result

    # Direct RTSP URL
    if raw_url.lower().startswith("rtsp://"):
        result["rtsp_url"] = raw_url
        urls_to_try = [raw_url]
    else:
        # Camview server — needs auth
        if not device_id or not camera_no:
            result["error"] = "missing_device_id_or_camera_no"
            return result

        server = re.sub(r'^https?://', '', raw_url).rstrip('/').lower()
        result["server"] = server

        urls, ok = start_video(server, device_id, camera_no)
        if not ok or not urls:
            result["error"] = "start_video_failed"
            return result
        result["rtsp_url"] = urls[0][:80] + "..."
        urls_to_try = urls

    # Capture frame
    jpeg = None
    for url in urls_to_try:
        jpeg = capture_frame(url)
        if jpeg:
            break

    if not jpeg:
        result["error"] = "frame_capture_failed"
        return result

    result["frame_captured"] = True

    # Save frame
    safe_label = re.sub(r'[^\w\-]', '_', label)[:60]
    fname = f"{safe_label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    fpath = os.path.join(FRAMES_DIR, fname)
    with open(fpath, "wb") as f:
        f.write(jpeg)
    result["frame_path"] = fpath
    logger.info(f"  📸 Frame saved: {fpath} ({len(jpeg)} bytes)")

    # Classify
    if skip_vlm:
        result["classification"] = "SKIPPED"
        result["reason"] = "vlm_skipped_by_flag"
    else:
        cat, reason = classify_frame(jpeg)
        result["classification"] = cat
        result["reason"] = reason

    return result


# ===========================================================================
# Main
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="Test RTSP camera frame capture & classification")
    parser.add_argument("--exam-id", type=int, default=20, help="Exam ID to load cameras for")
    parser.add_argument("--limit", type=int, default=20, help="Max cameras to test")
    parser.add_argument("--skip-vlm", action="store_true", help="Skip VLM classification, just test frame capture")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent camera workers")
    args = parser.parse_args()

    logger.info("=" * 80)
    logger.info(f"Test Camera Frame Capture — exam_id={args.exam_id}, limit={args.limit}")
    logger.info(f"  VLM: {'SKIPPED' if args.skip_vlm else OLLAMA_MODEL}")
    logger.info(f"  Workers: {args.workers}")
    logger.info(f"  Frames dir: {FRAMES_DIR}")
    logger.info(f"  Log: {LOG_FILE}")
    logger.info("=" * 80)

    # 1. Load cameras from DB
    logger.info(f"Loading RTSP cameras for exam_id={args.exam_id}...")
    cameras = get_rtsp_cameras(args.exam_id)
    if not cameras:
        logger.error("No cameras found — check exam_id and DB connection")
        sys.exit(1)
    logger.info(f"Loaded {len(cameras)} cameras from DB")

    # Limit to N
    cameras = cameras[:args.limit]
    logger.info(f"Testing {len(cameras)} cameras")

    # 2. Pre-authenticate unique servers
    servers = sorted({
        re.sub(r'^https?://', '', str(c.get('cctv_url') or '')).rstrip('/').lower()
        for c in cameras
        if c.get('cctv_url') and not str(c.get('cctv_url', '')).lower().startswith('rtsp://')
    })
    if servers:
        logger.info(f"Pre-authenticating {len(servers)} server(s): {servers}")
        for srv in servers:
            get_token(srv)

    # 3. Process cameras concurrently
    results = []
    start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_camera, cam, args.skip_vlm): i
            for i, cam in enumerate(cameras)
        }
        for future in as_completed(futures):
            idx = futures[future]
            cam = cameras[idx]
            label = f"{cam.get('device_id', '?')}_{cam.get('camera_no', '?')}"
            try:
                r = future.result()
                results.append(r)
                status = "✅" if r["frame_captured"] else "❌"
                cls_info = f" → {r['classification']}" if r["classification"] else ""
                err_info = f" [{r['error']}]" if r["error"] else ""
                logger.info(f"[{len(results):>2}/{len(cameras)}] {status} {label}{cls_info}{err_info}")
            except Exception as e:
                logger.error(f"[{len(results):>2}/{len(cameras)}] 💥 {label}: {e}")
                results.append({"label": label, "frame_captured": False, "error": str(e)})

    elapsed = time.time() - start

    # 4. Summary
    captured = sum(1 for r in results if r.get("frame_captured"))
    failed   = sum(1 for r in results if not r.get("frame_captured"))

    summary_lines = [
        "",
        "=" * 80,
        "RESULTS SUMMARY",
        "=" * 80,
        f"  Total cameras tested : {len(results)}",
        f"  Frames captured      : {captured}",
        f"  Failed               : {failed}",
        f"  Success rate         : {captured / max(len(results), 1) * 100:.1f}%",
        f"  Time                 : {elapsed:.1f}s",
        f"  Frames saved to      : {FRAMES_DIR}",
        "",
    ]

    # Per-camera detail table
    summary_lines.append(f"{'Label':<30} {'Frame':>5} {'Classification':<25} {'Error'}")
    summary_lines.append("-" * 90)
    for r in sorted(results, key=lambda x: x.get("label", "")):
        lbl   = r.get("label", "?")[:30]
        frame = "YES" if r.get("frame_captured") else "NO"
        cls   = r.get("classification", "")[:25]
        err   = r.get("error", "")[:30]
        summary_lines.append(f"{lbl:<30} {frame:>5} {cls:<25} {err}")
    summary_lines.append("=" * 80)

    summary = "\n".join(summary_lines)
    print(summary)
    logger.info(summary)

    # Save JSON report
    report_path = os.path.join(FRAMES_DIR, f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info(f"Report saved: {report_path}")


if __name__ == "__main__":
    main()
