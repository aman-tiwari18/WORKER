import argparse
import base64
import cv2
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ============================================================
# DB Import
# ============================================================
try:
    from utils.db_client import (
        get_rtsp_cameras,
        update_rtsp_classification,
    )
except ImportError:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from utils.db_client import (
        get_rtsp_cameras,
        update_rtsp_classification,
    )

# ============================================================
# Logging Setup
# ============================================================

# Use StreamHandler only. script-server will natively capture this to its own singular execution log.
logging.basicConfig(
    level=logging.INFO, # Using Info to avoid massive debug spam in terminal
    format='%(asctime)s - [%(threadName)-10s] - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================
# Configuration - OPTIMIZED SETTINGS
# ============================================================
MAX_WORKERS_NETWORK = 64  
MAX_WORKERS_VLM = 4       

TIMEOUT_API    = 10    # API request timeout (seconds)
TIMEOUT_OPENCV = 5000  # OpenCV timeout (milliseconds) - may be ignored by some builds
TIMEOUT_OPENCV_REAL = 8    # Hard thread timeout for OpenCV (seconds)
TIMEOUT_FFMPEG = 8     # FFmpeg timeout (seconds)
TIMEOUT_VLM    = 60    # VLM classification timeout (seconds)

AUTH_USERNAME = "system"
# AUTH_PASSWORD = "Sasd@12345"
AUTH_PASSWORD = "Sasd@12345"


TOKEN_EXPIRY_SECONDS = 90  

# Server tokens storage (thread-safe)
server_tokens = {}
server_token_timestamps = {}
token_lock = threading.Lock()
keepalive_threads = {}

TWO_PASS_MODE = True
SKIP_PRIVATE_IPS = True

# RTSP Proxy — set to map "host:port" → "proxy_host:proxy_port" if needed
# Leave empty ({}) to connect directly
RTSP_PROXY_MAP = {
    # "15.207.163.63:9100": "10.x.x.x:19100",   # example: proxy through trusted relay
}

# ============================================================
# Authentication for Camview
# ============================================================

def make_api_call(url, payload, headers, timeout=None):
    """Make API call to camview.io server (treats 200 and 401 as success)"""
    if timeout is None:
        timeout = TIMEOUT_API
    
    try:
        response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=timeout)
        # For authentication, both 200 and 401 contain valid JSON responses
        if response.status_code in (200, 401):
            return response
        else:
            return None
    except Exception:
        return None

def parse_json_response(response, context):
    """Parse JSON response from API call (handles both 200 and 401 responses)"""
    if response is None:
        return None
    try:
        return json.loads(response.content.decode("utf-8"))
    except Exception:
        return None

def authenticate_server(server_address, max_attempts=3):
    """Authenticate with a camview.io server and get access token"""
    auth_url = f"https://{server_address}/admin/API/accounts/authorize"
    logger.info(f"Authenticating with {server_address}...")
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "userName": AUTH_USERNAME,
        "ipAddress": "",
        "clientType": "WINPC_V1"
    }
    
    for attempt in range(1, max_attempts + 1):
        # Step 1: Get realm and randomKey
        res = make_api_call(auth_url, payload, headers)
        if res is None:
            time.sleep(1)
            continue
        
        resp_json = parse_json_response(res, "initial authorization")
        if resp_json is None:
            time.sleep(1)
            continue
        
        if resp_json.get("code") not in (None, 1000, 0):
            logger.debug(f"Step 1 failed for {server_address}: {resp_json}")
            time.sleep(2)
            continue
        
        # Extract auth parameters
        realm = resp_json.get("realm")
        randomKey = resp_json.get("randomKey")
        publicKey = resp_json.get("publickey")
        cookie = res.headers.get("Set-Cookie", "")
        
        if not all([realm, randomKey, publicKey]):
            time.sleep(1)
            continue
        
        # Step 2: Generate signature and get token
        sig = generate_signature(AUTH_USERNAME, AUTH_PASSWORD, realm, randomKey)
        
        payload2 = {
            "mac": "",
            "signature": sig,
            "userName": AUTH_USERNAME,
            "randomKey": randomKey,
            "publicKey": publicKey,
            "encryptType": "MD5",
            "ipAddress": "", "clientType": "WINPC_V1", "userType": "0"
        }
        
        headers2 = {"Content-Type": "application/json", "Cookie": cookie}
        res2 = make_api_call(auth_url, payload2, headers2)
        
        if res2 is None:
            time.sleep(1)
            continue
        
        resp_json2 = parse_json_response(res2, "token exchange")
        if resp_json2 is None:
            time.sleep(1)
            continue
        
        # Log Step 2 failure reason
        if resp_json2.get("code") not in (None, 1000, 0):
            logger.error(f"❌ DSS auth Step 2 failed for {server_address}: {resp_json2.get('desc', 'Unknown error')} (code: {resp_json2.get('code')})")
            if resp_json2.get("code") == 2002: # User locked
                logger.warning(f"⚠️ User '{AUTH_USERNAME}' is locked on {server_address}. Waiting 60s for auto-unlock...")
                time.sleep(60)
                return None
            time.sleep(2)
            continue
        
        token = resp_json2.get("token")
        if token:
            logger.info(f"✅ Authenticated with {server_address}")
            return token
            
    logger.error(f"❌ Authentication failed for {server_address} after {max_attempts} attempts")
    return None

def get_server_token(server_address):
    """Get valid token for a server (with auto-refresh)"""
    global server_tokens, server_token_timestamps
    
    with token_lock:
        if server_address in server_tokens:
            elapsed = time.time() - server_token_timestamps.get(server_address, 0)
            if elapsed < TOKEN_EXPIRY_SECONDS:
                return server_tokens[server_address]
        
        token = authenticate_server(server_address)
        if token:
            server_tokens[server_address] = token
            server_token_timestamps[server_address] = time.time()
            start_keepalive(server_address)
        return token

def keepalive_loop(server_address, stop_event, interval=22):
    """Background thread: sends PUT keepalive every ~22s to prevent session expiry."""
    while not stop_event.wait(interval):
        token = server_tokens.get(server_address)
        if not token:
            continue
        try:
            requests.put(
                f"https://{server_address}/admin/API/accounts/keepalive",
                headers={"Content-Type": "application/json", "X-Subject-Token": token},
                json={},
                timeout=5
            )
        except Exception:
            pass

def start_keepalive(server_address):
    """Start a keepalive background thread for a server if not already running."""
    if server_address not in keepalive_threads:
        stop = threading.Event()
        t = threading.Thread(target=keepalive_loop, args=(server_address, stop), daemon=True)
        t.start()
        keepalive_threads[server_address] = (t, stop)
        logger.info(f"Keepalive thread started for {server_address}")

def stop_all_keepalives():
    for server, (t, stop) in keepalive_threads.items():
        stop.set()


def is_private_ip_url(url):
    """Return True if the URL contains a private/LAN IP address."""
    match = re.search(r'://(\d+\.\d+\.\d+\.\d+)', url)
    if not match:
        return False
    ip = match.group(1)
    parts = list(map(int, ip.split('.')))
    return (
        parts[0] == 10 or
        (parts[0] == 172 and 16 <= parts[1] <= 31) or
        (parts[0] == 192 and parts[1] == 168)
    )


def probe_tcp_port(host, port, timeout=2.0):
    """Return True if TCP port is reachable within timeout seconds."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def extract_host_port(url):
    """Extract (host, port) from rtsp:// or http:// URL. Returns (None, None) on failure."""
    m = re.match(r'[a-z]+://([^/:]+)(?::(\d+))?', url)
    if not m:
        return None, None
    host = m.group(1)
    port = int(m.group(2)) if m.group(2) else (554 if url.startswith('rtsp') else 443)
    return host, port


def generate_signature(userName, password, realm, randomKey):
    temp = hashlib.md5(password.encode()).hexdigest()
    temp = hashlib.md5((userName + temp).encode()).hexdigest()
    temp = hashlib.md5(temp.encode()).hexdigest()
    temp = hashlib.md5((userName + ":" + realm + ":" + temp).encode()).hexdigest()
    return hashlib.md5((temp + ":" + randomKey).encode()).hexdigest()

def add_token_to_url(rtsp_url, token):
    """Append session token as ?token= query parameter.
    DSS API docs show exact format: ...cameraid=X%24Y%26substream=1?token=NNN
    The URL must remain encoded (%24, %26) and token goes after a literal '?'.
    """
    return f"{rtsp_url}?token={token}"

def apply_rtsp_proxy(url):
    """Rewrite RTSP URL to go through a trusted relay if configured in RTSP_PROXY_MAP."""
    if not RTSP_PROXY_MAP:
        return url
    h, p = extract_host_port(url)
    if not h or not p:
        return url
    key = f"{h}:{p}"
    if key in RTSP_PROXY_MAP:
        proxy = RTSP_PROXY_MAP[key]
        new_url = url.replace(f"{h}:{p}", proxy, 1)
        logger.debug(f"RTSP proxy: {key} → {proxy}")
        return new_url
    return url

def start_video_stream(server_address, device_id, camera_no):
    """
    Call Camview StartVideo API and return ALL available RTSP URLs.
    Returns: (list_of_urls, stream_token, success)
    """
    token = get_server_token(server_address)
    if not token:
        return None, None, False

    url     = f"https://{server_address}/admin/API/MTS/Video/StartVideo?token={token}"
    headers = {"Content-Type": "application/json", "X-Subject-Token": token}
    payload = {
        "clientType": "WINPC_V1", "project": "PSDK", "method": "MTS.Video.StartVideo",
        "data": {
            "streamType": "1",
            "optional":   f"/admin/API/MTS/Video/StartVideo?token={token}",
            "channelId":  f"{device_id}$1$0${camera_no}",
            "dataType":   "2",
            "enableRtsps":"0"
        }
    }

    res = make_api_call(url, payload, headers)
    if res is None:
        return None, None, False

    if res.status_code == 401:
        with token_lock:
            server_tokens.pop(server_address, None)
        return None, None, False

    if res.status_code != 200:
        return None, None, False

    try:
        resp_json    = res.json()
        data         = resp_json.get("data")
        if not data:
            return None, None, False

        # Collect ALL URLs from both "url" and "url2" fields
        # Each field may also be pipe-separated
        urls = []
        for key in ("url", "url2"):
            raw = data.get(key)
            if not raw:
                continue
            parts = [part.strip() for part in raw.split('|') if part.strip()]
            urls.extend(parts)

        stream_token = data.get("token") or token

        if urls:
            return urls, stream_token, True
        return None, None, False

    except Exception:
        return None, None, False


def start_video_stream_hls(server_address, device_id, camera_no):
    """
    Request HLS (HTTP) stream URL instead of RTSP.
    HLS uses HTTP/443 and bypasses port-9100 firewall blocks.
    DSS API dataType "1" returns an HTTP/HLS URL.
    """
    token = get_server_token(server_address)
    if not token:
        return None, None, False

    url = f"https://{server_address}/admin/API/MTS/Video/StartVideo?token={token}"
    headers = {"Content-Type": "application/json", "X-Subject-Token": token}

    payload = {
        "clientType": "WINPC_V1",
        "project": "PSDK",
        "method": "MTS.Video.StartVideo",
        "data": {
            "streamType": "1",
            "optional": f"/admin/API/MTS/Video/StartVideo?token={token}",
            "channelId": f"{device_id}$1$0${camera_no}",
            "dataType": "1",   # 1 = HLS/HTTP, 2 = RTSP
            "enableRtsps": "0"
        }
    }

    try:
        res = make_api_call(url, payload, headers)
        if res is None or res.status_code != 200:
            return None, None, False

        data = res.json().get("data") or {}
        urls = []
        for key in ("url", "url2"):
            raw = data.get(key)
            if not raw:
                continue
            urls.extend([p.strip() for p in raw.split('|') if p.strip()])

        stream_token = data.get("token") or token
        if urls:
            return urls, stream_token, True
    except Exception as e:
        logger.debug(f"HLS response parse error: {e}")

    return None, None, False


def build_rtsp_url(server_address, device_id, camera_no):
    """
    Returns a LIST of RTSP URLs (all with auth token appended).
    Caller should try each URL in sequence until one works.
    Falls back to HLS if RTSP port is blocked.
    """
    try:
        if not server_address or not device_id or not camera_no:
            return None, False
        
        # Try RTSP first
        rtsp_urls, stream_token, success = start_video_stream(
            server_address, device_id, camera_no
        )
        
        if success and rtsp_urls and stream_token:
            # Filter out private IPs if configured
            if SKIP_PRIVATE_IPS:
                public_urls = [u for u in rtsp_urls if not is_private_ip_url(u)]
                if public_urls:
                    rtsp_urls = public_urls
                    logger.debug(f"Filtered to {len(rtsp_urls)} public URL(s)")
            
            # Check if port is reachable
            if rtsp_urls:
                host, port = extract_host_port(rtsp_urls[0])
                if host and port:
                    if not probe_tcp_port(host, port, timeout=2.0):
                        logger.warning(f"Port {port} on {host} is BLOCKED — trying HLS fallback")
                        # Try HLS fallback
                        hls_urls, hls_token, hls_success = start_video_stream_hls(
                            server_address, device_id, camera_no
                        )
                        if hls_success and hls_urls and hls_token:
                            logger.info(f"✅ HLS fallback succeeded for {device_id}_{camera_no}")
                            return [add_token_to_url(url, hls_token) for url in hls_urls], True
                        return None, False
            
            # Add token + apply proxy rewrite (if RTSP_PROXY_MAP is configured)
            rtsp_urls_with_token = [
                apply_rtsp_proxy(add_token_to_url(url, stream_token)) 
                for url in rtsp_urls
            ]
            return rtsp_urls_with_token, True
        
        # RTSP failed — try HLS fallback
        logger.debug(f"RTSP unavailable for {device_id}_{camera_no}, trying HLS...")
        hls_urls, hls_token, hls_success = start_video_stream_hls(
            server_address, device_id, camera_no
        )
        if hls_success and hls_urls and hls_token:
            logger.info(f"✅ HLS fallback succeeded for {device_id}_{camera_no}")
            return [add_token_to_url(url, hls_token) for url in hls_urls], True
        
        return None, False
    except Exception as e:
        logger.debug(f"Failed to build URL for {device_id}_{camera_no}: {e}")
        return None, False

# ============================================================
# Frame Capture
# ============================================================
def capture_frame_from_stream(stream_url, id_str, timeout_ms=None):
    """
    Try OpenCV first (with thread-level hard timeout),
    then fall back to FFmpeg.
    """
    if timeout_ms is None:
        timeout_ms = TIMEOUT_OPENCV
    
    img_path = capture_frame_with_opencv_stream(stream_url, id_str, timeout_ms)
    if img_path:
        return img_path
    
    logger.debug(f"OpenCV failed for {id_str} — trying FFmpeg fallback")
    return capture_frame_with_ffmpeg_stream(stream_url, id_str)

def capture_frame_with_opencv_stream(stream_url, id_str, timeout_ms):
    """
    Capture frame via OpenCV.
    
    IMPORTANT: CAP_PROP_OPEN_TIMEOUT_MSEC / CAP_PROP_READ_TIMEOUT_MSEC are
    silently ignored by many OpenCV builds (logs show 30s default firing
    instead of the requested timeout). We therefore run the capture in a daemon
    thread and join() it with a hard wall-clock timeout so the caller is
    always unblocked within TIMEOUT_OPENCV_REAL seconds.
    """
    result = [None]  # mutable container — thread writes here
    
    def _do_capture():
        try:
            cap = cv2.VideoCapture(stream_url)
            # Set props anyway — respected when the build supports it
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms)
            
            if not cap.isOpened():
                return
            
            # Skip a few frames to get a fresh one
            for _ in range(3):
                cap.read()
            
            ret, frame = cap.read()
            cap.release()
            
            if not ret or frame is None:
                return
            
            save_dir = f"frames/multi_server/{id_str}"
            os.makedirs(save_dir, exist_ok=True)
            img_path = f"{save_dir}/frame_{uuid.uuid4().hex}.jpg"
            cv2.imwrite(img_path, frame)
            result[0] = img_path
            
        except Exception as e:
            logger.debug(f"OpenCV capture error for {id_str}: {e}")
    
    thread = threading.Thread(target=_do_capture, daemon=True)
    thread.start()
    thread.join(timeout=TIMEOUT_OPENCV_REAL)  # hard wall-clock kill
    
    if thread.is_alive():
        logger.warning(
            f"OpenCV hard timeout ({TIMEOUT_OPENCV_REAL}s) triggered for {id_str} "
            f"— stream likely hanging"
        )
        return None
    
    return result[0]

def capture_frame_with_ffmpeg_stream(stream_url, id_str):
    """
    Capture frame via FFmpeg subprocess.
    
    Uses -timeout (general I/O timeout, microseconds) for connection/read timeouts.
    Plus subprocess.run(timeout=...) as the final hard kill.
    """
    if not shutil.which("ffmpeg"):
        logger.debug("ffmpeg not found in PATH")
        return None

    save_dir = f"frames/multi_server/{id_str}"
    os.makedirs(save_dir, exist_ok=True)
    img_path = f"{save_dir}/frame_{uuid.uuid4().hex}.jpg"

    timeout_us = TIMEOUT_FFMPEG * 1_000_000  # convert seconds → microseconds

    cmd = ["ffmpeg", "-y"]
    if stream_url.startswith("rtsp"):
        cmd += [
            "-rtsp_transport", "tcp",
        ]
    cmd += [
        "-timeout",   str(timeout_us),      # general I/O timeout (works for RTSP too)
        "-i",         stream_url,
        "-frames:v",  "1",
        "-loglevel",  "warning",            # warning shows RTSP-level errors
        img_path,
    ]

    try:
        # subprocess timeout is TIMEOUT_FFMPEG + 5 so FFmpeg's own internal timeout
        # always fires first — this guarantees we capture its stderr error message
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=TIMEOUT_FFMPEG + 5,
        )
        if result.returncode != 0 or not os.path.exists(img_path):
            stderr_out = result.stderr.decode('utf-8', errors='replace').strip()
            logger.warning(f"FFmpeg failed for {id_str} (exit={result.returncode}): {stderr_out[:200]}")
            return None
        logger.debug(f"✅ Frame captured with FFmpeg: {img_path}")
        return img_path

    except subprocess.TimeoutExpired as e:
        # Capture whatever stderr was written before timeout
        stderr_out = (e.stderr or b'').decode('utf-8', errors='replace').strip()
        logger.warning(f"FFmpeg hard timeout ({TIMEOUT_FFMPEG}s) for {id_str}: {stderr_out or 'no stderr'}")
        return None
    except Exception as e:
        logger.debug(f"FFmpeg error for {id_str}: {e}")
        return None

# ============================================================
# Classification
# ============================================================
def is_grey_or_obstructed(image_path):
    try:
        img = cv2.imread(image_path)
        if img is None: return True, "Failed to read image"
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if gray.std() < 5: return True, "Uniform grey screen"
        if gray.mean() < 10: return True, "Very dark/blocked camera"
        if gray.mean() > 245: return True, "Overexposed/white screen"
        if cv2.Laplacian(gray, cv2.CV_64F).var() < 10: return True, "Low detail/obstructed"
        return False, None
    except:
        return False, None

def classify_with_vlm(image_path):
    is_bad, reason = is_grey_or_obstructed(image_path)
    if is_bad:
        return "Uncertain/Obstructed", reason

    CATEGORIES = [
        "Server Room", "Outdoor Scene", "Outside Area",
        "Parking Lot", "Entry", "Computer Lab"
    ]
    PROMPT = f"""You are an expert computer vision classifier.
Classify the given image into one of the following categories: {', '.join(CATEGORIES)}.
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

    try:
        with open(image_path, 'rb') as f:
            image_data = base64.b64encode(f.read()).decode('utf-8')
    except Exception:
        return "Uncertain", "Failed to read image"

    payload = {
        "model":  "qwen3-vl:8b",
        "prompt": PROMPT,
        "images": [image_data],
        "stream": False,
    }
    vlm_url = "http://localhost:11434/api/generate"

    try:
        response = requests.post(vlm_url, json=payload, timeout=TIMEOUT_VLM)
        if response.status_code != 200:
            return "Uncertain", "VLM request failed"

        raw = response.json().get("response", "").strip()
        
        # Strip qwen3 chain-of-thought tags if present
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

        result_json = json.loads(raw)
        category    = result_json.get("category", "Uncertain")
        if "uncertain" in category.lower() or "unclear" in category.lower():
            return "Uncertain/Obstructed", result_json.get("reason", "")
        return category, "Successfully classified"

    except requests.exceptions.Timeout:
        return "Uncertain", "VLM timeout"
    except json.JSONDecodeError:
        logger.debug(f"Invalid JSON from VLM: {raw[:200] if 'raw' in locals() else 'N/A'}")
        return "Uncertain", "Invalid JSON from VLM"
    except Exception as e:
        return "Uncertain", str(e)

# ============================================================
# Runtime config logic
# ============================================================
def _get_runtime_config():
    payload_env = os.environ.get("VISION_ASSIGNMENT_PAYLOAD")
    if payload_env:
        return json.loads(payload_env)

    parser = argparse.ArgumentParser()
    parser.add_argument("--assignment_id", default="local_dev")
    parser.add_argument("--exam_id", type=int, required=True)
    parser.add_argument("--shift_id", type=int)
    parser.add_argument("--assignment_date")
    parser.add_argument("--link_type", default="RTSP")
    args = parser.parse_args()
    return vars(args)

# ============================================================
# Processing logic
# ============================================================
def quick_accessibility_check(cam):
    """
    Pass-1: authenticate with server and get RTSP URL list.
    No frame capture here — just confirms the API is reachable.
    Returns list of URLs so Pass-2 can try each one.
    """
    server_address = cam.get("cctv_url")  # Map CCTV URL -> Server Address in RTSP context
    device_id      = cam.get("device_id")
    camera_no      = cam.get("camera_no")

    stream_urls, success = build_rtsp_url(server_address, device_id, camera_no)
    if success and stream_urls:
        return cam, stream_urls, True, None
    else:
        return cam, None, False, "Failed to get stream URLs from API"

def process_accessible_camera(cam, stream_urls, exam_id):
    """
    Pass-2: try every URL in the list until a frame is captured,
    then classify with VLM.
    """
    device_id = cam.get("device_id")
    camera_no = cam.get("camera_no")
    id_str    = f"{device_id}_{camera_no}"

    result = {
        'cam':    cam,
        'status': 'Accessible',
        'label':  'Uncertain',
        'error':  None,
    }

    # Normalize: accept a single URL string or a list
    if isinstance(stream_urls, str):
        stream_urls = [stream_urls]

    try:
        # ── Try each URL until one yields a frame ────────────────────
        img_path = None
        for idx, url in enumerate(stream_urls, 1):
            logger.info(
                f"[{id_str}] Trying URL {idx}/{len(stream_urls)}: "
                f"{url[:70]}..."
            )
            img_path = capture_frame_from_stream(url, id_str)
            if img_path:
                logger.info(f"[{id_str}] ✅ Frame captured from URL {idx}")
                break
            else:
                logger.warning(
                    f"[{id_str}] ❌ URL {idx} failed"
                    + (f", trying URL {idx+1}..." if idx < len(stream_urls) else " — all URLs exhausted")
                )

        if not img_path:
            result['status'] = 'Not Accessible'
            result['label']  = 'Failed to Classify'
            result['error']  = 'Frame capture failed on all URLs'
        else:
            label, reason    = classify_with_vlm(img_path)
            result['status'] = 'Accessible'
            result['label']  = (
                'Uncertain/Obstructed'
                if ("Uncertain" in label or "Obstructed" in label)
                else label
            )

        update_rtsp_classification(exam_id, device_id, result['label'], result['status'])
        return result

    except Exception as e:
        result['status'] = 'Accessible'
        result['label']  = 'Uncertain'
        result['error']  = str(e)
        update_rtsp_classification(exam_id, device_id, result['label'], result['status'])
        return result

def main():
    logger.info("="*80)
    logger.info("🚀 OPTIMIZED DB-Based Scene Classification (RTSP) - TWO-PASS MODE")
    logger.info("="*80)
    
    runtime = _get_runtime_config()
    exam_id = runtime["exam_id"]
    
    cameras = get_rtsp_cameras(exam_id)
    if not cameras:
        logger.warning(f"No active RTSP cameras found in DB for exam_id={exam_id}")
        return
        
    logger.info(f"Total cameras loaded from DB: {len(cameras)}")
    logger.info(f"Network workers: {MAX_WORKERS_NETWORK}")
    logger.info(f"VLM workers: {MAX_WORKERS_VLM}")
    logger.info(f"Timeouts → API:{TIMEOUT_API}s  OpenCV(hard):{TIMEOUT_OPENCV_REAL}s  "
                f"FFmpeg:{TIMEOUT_FFMPEG}s  VLM:{TIMEOUT_VLM}s")
    if RTSP_PROXY_MAP:
        logger.info(f"RTSP Proxy enabled: {len(RTSP_PROXY_MAP)} mapping(s)")
    logger.info("="*80)
    
    start_time = time.time()
    
    # Store results for statistics
    processed_results = []
    
    if TWO_PASS_MODE:
        # ========== PASS 1: Quick Accessibility Check ==========
        logger.info("\n" + "="*80)
        logger.info("PASS 1: Quick Accessibility Check (No frame capture)")
        logger.info("="*80)
        
        accessible_cameras = []
        pass1_start = time.time()
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS_NETWORK) as executor:
            futures = [executor.submit(quick_accessibility_check, cam) for cam in cameras]
            total_pass1 = len(futures)
            completed_pass1 = 0
            
            for future in as_completed(futures):
                try:
                    cam, stream_url, is_accessible, error = future.result()
                    device_id = cam.get("device_id")
                    
                    if is_accessible and stream_url:
                        accessible_cameras.append((cam, stream_url))
                    else:
                        # Direct update to DB for not-accessible cameras
                        update_rtsp_classification(exam_id, device_id, 'Failed to Classify', 'Not Accessible')
                        processed_results.append({
                            'server_address': cam.get("cctv_url"),
                            'status': 'Not Accessible',
                            'label': 'Failed to Classify'
                        })
                        
                    completed_pass1 += 1
                    # Print progress every 10 cameras, or if it's the last one
                    if completed_pass1 % 10 == 0 or completed_pass1 == total_pass1:
                        logger.info(f"⏳ Pass 1 Progress: {completed_pass1}/{total_pass1} ({(completed_pass1/total_pass1)*100:.1f}%) processed")
                except Exception as e:
                    logger.error(f"Pass 1 task failed: {e}")
                    completed_pass1 += 1
        
        pass1_time = time.time() - pass1_start
        
        logger.info(f"\n✅ Pass 1 Complete!")
        logger.info(f"   Time: {int(pass1_time//60)}m {int(pass1_time%60)}s")
        logger.info(f"   Accessible: {len(accessible_cameras)}")
        logger.info(f"   Not Accessible: {len(cameras) - len(accessible_cameras)}")
        
        if len(accessible_cameras) == 0:
            logger.warning("No accessible cameras found!")
            return
            
        # ========== PASS 2: Frame Capture & Classification ==========
        logger.info("\n" + "="*80)
        logger.info(f"PASS 2: Frame Capture & Classification ({len(accessible_cameras)} cameras)")
        logger.info("="*80)
        
        pass2_start = time.time()
        classified_count = 0
        uncertain_count = 0
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS_VLM) as executor:
            tasks = []
            for cam, stream_urls in accessible_cameras:
                tasks.append(executor.submit(process_accessible_camera, cam, stream_urls, exam_id))
            
            total_pass2 = len(tasks)
            completed_pass2 = 0
            
            for future in as_completed(tasks):
                try:
                    result = future.result()
                    cam = result['cam']
                    
                    processed_results.append({
                        'server_address': cam.get("cctv_url"),
                        'status': result['status'],
                        'label': result['label']
                    })
                    
                    if 'Uncertain' in result['label']:
                        uncertain_count += 1
                    else:
                        classified_count += 1
                        
                    completed_pass2 += 1
                    if completed_pass2 % 10 == 0 or completed_pass2 == total_pass2:
                        logger.info(f"⏳ Pass 2 Progress: {completed_pass2}/{total_pass2} ({(completed_pass2/total_pass2)*100:.1f}%) classified")
                except Exception as e:
                    logger.error(f"Pass 2 task failed: {e}")
                    completed_pass2 += 1
        
        pass2_time = time.time() - pass2_start
        
        logger.info(f"\n✅ Pass 2 Complete!")
        logger.info(f"   Time: {int(pass2_time//60)}m {int(pass2_time%60)}s")
        logger.info(f"   Classified: {classified_count}")
        logger.info(f"   Uncertain/Obstructed: {uncertain_count}")
        
    # ========== Final Summary ==========
    total_time = time.time() - start_time
    
    accessible_total = classified_count + uncertain_count
    not_accessible = len(cameras) - accessible_total
    
    # Calculate server-wise stats manually since no pandas
    server_stats = {}
    for res in processed_results:
        srv = res['server_address'] or "Unknown"
        if srv not in server_stats:
            server_stats[srv] = {'Total': 0, 'Accessible': 0}
        server_stats[srv]['Total'] += 1
        if res['status'] == 'Accessible':
            server_stats[srv]['Accessible'] += 1
            
    server_stats_str = ""
    for srv, stats in server_stats.items():
        success_rate = (stats['Accessible'] / stats['Total'] * 100) if stats['Total'] > 0 else 0
        server_stats_str += f"Server: {srv} | Total: {stats['Total']} | Accessible: {stats['Accessible']} | Rate: {success_rate:.1f}%\n"
        
    success_rate = (accessible_total / len(cameras) * 100) if len(cameras) > 0 else 0
    
    summary = f"""
{'='*80}
🎉 FINAL SUMMARY
{'='*80}
Total Cameras: {len(cameras)}
✅ Accessible (Classified): {classified_count}
⚠️  Accessible (Uncertain/Obstructed): {uncertain_count}
❌ Not Accessible: {not_accessible}
⏱️  Total Time: {int(total_time//60)}m {int(total_time%60)}s
📊 Success Rate: {success_rate:.1f}%

Server-wise Statistics:
{server_stats_str}
{'='*80}
"""
    print(summary)
    logger.info(summary)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.warning("Processing interrupted by user")
        stop_all_keepalives()
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        stop_all_keepalives()
        sys.exit(1)
    finally:
        stop_all_keepalives()


# import argparse
# import base64
# import cv2
# import hashlib
# import json
# import logging
# import os
# import shutil
# import subprocess
# import sys
# import threading
# import time
# import urllib.parse
# import uuid
# import requests
# from concurrent.futures import ThreadPoolExecutor, as_completed
# from datetime import datetime

# # ============================================================
# # DB Import
# # ============================================================
# try:
#     from utils.db_client import (
#         get_rtsp_cameras,
#         update_rtsp_classification,
#     )
# except ImportError:
#     sys.path.append(os.path.dirname(os.path.abspath(__file__)))
#     from utils.db_client import (
#         get_rtsp_cameras,
#         update_rtsp_classification,
#     )

# # ============================================================
# # Logging Setup
# # ============================================================
# logging.basicConfig(
#     level=logging.INFO,
#     format='%(asctime)s - [%(threadName)-10s] - %(levelname)s - %(message)s',
#     handlers=[
#         logging.StreamHandler()
#     ]
# )
# logger = logging.getLogger(__name__)

# # ============================================================
# # Configuration
# # ============================================================
# MAX_WORKERS_NETWORK = 64
# MAX_WORKERS_VLM     = 4

# TIMEOUT_API         = 10        # seconds — Camview REST API calls
# TIMEOUT_OPENCV      = 10000     # milliseconds — passed to cv2 (may be ignored, see thread wrapper)
# TIMEOUT_OPENCV_HARD = 10        # seconds — thread-level hard kill for OpenCV
# TIMEOUT_FFMPEG      = 10        # seconds — FFmpeg subprocess hard kill
# TIMEOUT_VLM         = 120       # seconds — Ollama VLM inference
# TIMEOUT_CAMERA      = 90        # seconds — overall per-camera hard cap (Pass-2)

# AUTH_USERNAME        = "system"
# # AUTH_PASSWORD        = "Sasd@12345"
# AUTH_USERNAME = "Sasd@12345"
# TOKEN_EXPIRY_SECONDS = 90

# TWO_PASS_MODE = True

# # ============================================================
# # Authentication for Camview
# # ============================================================
# server_tokens           = {}
# server_token_timestamps = {}
# token_lock              = threading.Lock()


# def generate_signature(userName, password, realm, randomKey):
#     temp = hashlib.md5(password.encode()).hexdigest()
#     temp = hashlib.md5((userName + temp).encode()).hexdigest()
#     temp = hashlib.md5(temp.encode()).hexdigest()
#     temp = hashlib.md5((userName + ":" + realm + ":" + temp).encode()).hexdigest()
#     return hashlib.md5((temp + ":" + randomKey).encode()).hexdigest()


# def make_api_call(url, payload, headers, timeout=None):
#     if timeout is None:
#         timeout = TIMEOUT_API
#     try:
#         response = requests.post(
#             url, headers=headers, data=json.dumps(payload), timeout=timeout
#         )
#         if response.status_code in (200, 401):
#             return response
#         return None
#     except Exception:
#         return None


# def parse_json_response(response, context):
#     if response is None:
#         return None
#     try:
#         return json.loads(response.content.decode("utf-8"))
#     except Exception:
#         return None


# def authenticate_server(server_address, max_attempts=3):
#     """
#     Two-step Camview auth.
#     Includes exponential backoff between retries so a temporarily
#     overloaded server is not hammered immediately.
#     """
#     auth_url = f"https://{server_address}/admin/API/accounts/authorize"
#     headers  = {"Content-Type": "application/json"}
#     payload  = {"userName": AUTH_USERNAME, "ipAddress": "", "clientType": "WINPC_V1"}

#     for attempt in range(1, max_attempts + 1):
#         # ── Step 1: get realm + randomKey ────────────────────────────────
#         res = make_api_call(auth_url, payload, headers)
#         if res is None:
#             time.sleep(attempt * 2)   # 2s, 4s, 6s backoff
#             continue

#         resp_json = parse_json_response(res, "initial authorization")
#         if resp_json is None or resp_json.get("code") not in (None, 1000):
#             time.sleep(attempt * 2)
#             continue

#         realm     = resp_json.get("realm")
#         randomKey = resp_json.get("randomKey")
#         publicKey = resp_json.get("publickey")
#         cookie    = res.headers.get("Set-Cookie", "")

#         if not all([realm, randomKey, publicKey]):
#             time.sleep(attempt * 2)
#             continue

#         # ── Step 2: exchange signature for token ─────────────────────────
#         sig      = generate_signature(AUTH_USERNAME, AUTH_PASSWORD, realm, randomKey)
#         payload2 = {
#             "mac": "", "signature": sig, "userName": AUTH_USERNAME,
#             "randomKey": randomKey, "publicKey": publicKey, "encryptType": "MD5",
#             "ipAddress": "", "clientType": "WINPC_V1", "userType": "0"
#         }
#         headers2 = {"Content-Type": "application/json", "Cookie": cookie}
#         res2     = make_api_call(auth_url, payload2, headers2)

#         if res2 is None:
#             time.sleep(attempt * 2)
#             continue

#         resp_json2 = parse_json_response(res2, "token exchange")
#         if resp_json2 is None or resp_json2.get("code") not in (None, 1000):
#             time.sleep(attempt * 2)
#             continue

#         token = resp_json2.get("token")
#         if token:
#             return token

#         time.sleep(attempt * 2)

#     logger.error(f"Authentication failed for {server_address} after {max_attempts} attempts")
#     return None


# def get_server_token(server_address):
#     """Thread-safe token cache with auto-refresh."""
#     global server_tokens, server_token_timestamps
#     with token_lock:
#         if server_address in server_tokens:
#             elapsed = time.time() - server_token_timestamps.get(server_address, 0)
#             if elapsed < TOKEN_EXPIRY_SECONDS:
#                 return server_tokens[server_address]

#         token = authenticate_server(server_address)
#         if token:
#             server_tokens[server_address]           = token
#             server_token_timestamps[server_address] = time.time()
#         return token


# def add_token_to_url(rtsp_url, token):
#     parsed    = urllib.parse.urlparse(rtsp_url)
#     query     = urllib.parse.parse_qs(parsed.query)
#     query['token'] = [token]
#     new_query = urllib.parse.urlencode(query, doseq=True)
#     return urllib.parse.urlunparse(parsed._replace(query=new_query))


# def start_video_stream(server_address, device_id, camera_no):
#     """
#     Call Camview StartVideo API and return ALL available RTSP URLs.
#     Returns: (list_of_urls, stream_token, success)
#     """
#     token = get_server_token(server_address)
#     if not token:
#         return None, None, False

#     url     = f"https://{server_address}/admin/API/MTS/Video/StartVideo?token={token}"
#     headers = {"Content-Type": "application/json", "X-Subject-Token": token}
#     payload = {
#         "clientType": "WINPC_V1", "project": "PSDK", "method": "MTS.Video.StartVideo",
#         "data": {
#             "streamType": "1",
#             "optional":   f"/admin/API/MTS/Video/StartVideo?token={token}",
#             "channelId":  f"{device_id}$1$0${camera_no}",
#             "dataType":   "2",
#             "enableRtsps":"0"
#         }
#     }

#     res = make_api_call(url, payload, headers)
#     if res is None:
#         return None, None, False

#     if res.status_code == 401:
#         with token_lock:
#             server_tokens.pop(server_address, None)
#         return None, None, False

#     if res.status_code != 200:
#         return None, None, False

#     try:
#         resp_json    = res.json()
#         data         = resp_json.get("data")
#         if not data:
#             return None, None, False

#         # Collect ALL URLs from both "url" and "url2" fields
#         # Each field may also be pipe-separated
#         urls = []
#         for key in ("url", "url2"):
#             raw = data.get(key)
#             if not raw:
#                 continue
#             parts = [part.strip() for part in raw.split('|') if part.strip()]
#             urls.extend([urllib.parse.unquote(part) for part in parts])

#         stream_token = data.get("token") or token

#         if urls:
#             return urls, stream_token, True
#         return None, None, False

#     except Exception:
#         return None, None, False


# def build_rtsp_url(server_address, device_id, camera_no):
#     """
#     Returns a LIST of RTSP URLs (all with auth token appended).
#     Caller should try each URL in sequence until one works.
#     """
#     try:
#         if not server_address or not device_id or not camera_no:
#             return None, False
#         rtsp_urls, stream_token, success = start_video_stream(
#             server_address, device_id, camera_no
#         )
#         if success and rtsp_urls and stream_token:
#             return [add_token_to_url(url, stream_token) for url in rtsp_urls], True
#         return None, False
#     except Exception:
#         return None, False


# # ============================================================
# # Frame Capture
# # ============================================================

# def capture_frame_from_stream(stream_url, id_str, timeout_ms=None):
#     """
#     Try OpenCV first (with thread-level hard timeout),
#     then fall back to FFmpeg.
#     """
#     if timeout_ms is None:
#         timeout_ms = TIMEOUT_OPENCV

#     img_path = capture_frame_with_opencv_stream(stream_url, id_str, timeout_ms)
#     if img_path:
#         return img_path

#     logger.debug(f"OpenCV failed for {id_str} — trying FFmpeg fallback")
#     return capture_frame_with_ffmpeg_stream(stream_url, id_str)


# def capture_frame_with_opencv_stream(stream_url, id_str, timeout_ms):
#     """
#     Capture frame via OpenCV.

#     IMPORTANT: CAP_PROP_OPEN_TIMEOUT_MSEC / CAP_PROP_READ_TIMEOUT_MSEC are
#     silently ignored by many OpenCV builds (logs show 30s default firing
#     instead of the requested 10s).  We therefore run the capture in a daemon
#     thread and join() it with a hard wall-clock timeout so the caller is
#     always unblocked within TIMEOUT_OPENCV_HARD seconds.
#     """
#     result = [None]  # mutable container — thread writes here

#     def _do_capture():
#         try:
#             cap = cv2.VideoCapture(stream_url)
#             # Set props anyway — respected when the build supports it
#             cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms)
#             cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms)

#             if not cap.isOpened():
#                 return

#             # Skip a few frames to get a fresh one
#             for _ in range(3):
#                 cap.read()

#             ret, frame = cap.read()
#             cap.release()

#             if not ret or frame is None:
#                 return

#             save_dir = f"frames/multi_server/{id_str}"
#             os.makedirs(save_dir, exist_ok=True)
#             img_path = f"{save_dir}/frame_{uuid.uuid4().hex}.jpg"
#             cv2.imwrite(img_path, frame)
#             result[0] = img_path

#         except Exception as e:
#             logger.debug(f"OpenCV capture error for {id_str}: {e}")

#     thread = threading.Thread(target=_do_capture, daemon=True)
#     thread.start()
#     thread.join(timeout=TIMEOUT_OPENCV_HARD)  # hard wall-clock kill

#     if thread.is_alive():
#         logger.warning(
#             f"OpenCV hard timeout ({TIMEOUT_OPENCV_HARD}s) triggered for {id_str} "
#             f"— stream likely hanging"
#         )
#         return None

#     return result[0]


# def capture_frame_with_ffmpeg_stream(stream_url, id_str):
#     """
#     Capture frame via FFmpeg subprocess.

#     Uses both:
#       -timeout  (general I/O timeout, microseconds)
#       -stimeout (RTSP-specific session timeout, microseconds)
#     Plus subprocess.run(timeout=...) as the final hard kill.
#     """
#     if not shutil.which("ffmpeg"):
#         logger.debug("ffmpeg not found in PATH")
#         return None

#     save_dir = f"frames/multi_server/{id_str}"
#     os.makedirs(save_dir, exist_ok=True)
#     img_path = f"{save_dir}/frame_{uuid.uuid4().hex}.jpg"

#     timeout_us = TIMEOUT_FFMPEG * 1_000_000  # convert seconds → microseconds

#     cmd = ["ffmpeg", "-y"]
#     if stream_url.startswith("rtsp"):
#         cmd += [
#             "-rtsp_transport", "tcp",
#             "-stimeout", str(timeout_us),   # RTSP session timeout
#         ]
#     cmd += [
#         "-timeout",   str(timeout_us),      # general I/O timeout
#         "-i",         stream_url,
#         "-frames:v",  "1",
#         "-loglevel",  "error",
#         img_path,
#     ]

#     try:
#         result = subprocess.run(
#             cmd,
#             stdout=subprocess.PIPE,
#             stderr=subprocess.PIPE,
#             timeout=TIMEOUT_FFMPEG + 2,     # +2s buffer over internal timeout
#         )
#         if result.returncode != 0 or not os.path.exists(img_path):
#             logger.debug(f"FFmpeg failed for {id_str}: {result.stderr.decode()[:200]}")
#             return None
#         return img_path

#     except subprocess.TimeoutExpired:
#         logger.warning(f"FFmpeg hard timeout ({TIMEOUT_FFMPEG}s) for {id_str}")
#         return None
#     except Exception as e:
#         logger.debug(f"FFmpeg error for {id_str}: {e}")
#         return None


# # ============================================================
# # Classification
# # ============================================================

# def is_grey_or_obstructed(image_path):
#     try:
#         img = cv2.imread(image_path)
#         if img is None:
#             return True, "Failed to read image"
#         gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
#         if gray.std() < 5:
#             return True, "Uniform grey screen"
#         if gray.mean() < 10:
#             return True, "Very dark/blocked camera"
#         if gray.mean() > 245:
#             return True, "Overexposed/white screen"
#         if cv2.Laplacian(gray, cv2.CV_64F).var() < 10:
#             return True, "Low detail/obstructed"
#         return False, None
#     except Exception:
#         return False, None


# def classify_with_vlm(image_path):
#     is_bad, reason = is_grey_or_obstructed(image_path)
#     if is_bad:
#         return "Uncertain/Obstructed", reason

#     CATEGORIES = [
#         "Server Room", "Outdoor Scene", "Outside Area",
#         "Parking Lot", "Entry", "Computer Lab"
#     ]
#     PROMPT = f"""You are an expert computer vision classifier.
# Classify the given image into one of the following categories: {', '.join(CATEGORIES)}.
# Rules:
# - "Server Room": small room with limited visibility with laptop or computers.
# - "Computer Lab": large room with many computers, chairs, people or desks.
# - "Outdoor Scene": nature, roads, gardens, sky, trees.
# - "Outside Area": surroundings of a building, campus, boundary, hallway, outdoor environment.
# - "Parking Lot": cars, bikes, open area with marked spaces or vehicles.
# - "Entry": building entrances, gates, doors, signboards, or main access points.

# If the image is grey, blank, heavily obstructed, or unclear, respond with "Uncertain".
# Return **only** the JSON response:
# {{"category": "<one of the above categories or Uncertain>", "reason": "<short explanation>"}}"""

#     try:
#         with open(image_path, 'rb') as f:
#             image_data = base64.b64encode(f.read()).decode('utf-8')
#     except Exception:
#         return "Uncertain", "Failed to read image"

#     payload = {
#         "model":  "qwen3-vl:8b",
#         "prompt": PROMPT,
#         "images": [image_data],
#         "stream": False,
#     }
#     vlm_url = "http://localhost:11434/api/generate"

#     try:
#         response = requests.post(vlm_url, json=payload, timeout=TIMEOUT_VLM)
#         if response.status_code != 200:
#             return "Uncertain", "VLM request failed"

#         raw      = response.json().get("response", "").strip()
#         # Strip qwen3 chain-of-thought tags if present
#         import re
#         raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

#         result_json = json.loads(raw)
#         category    = result_json.get("category", "Uncertain")
#         if "uncertain" in category.lower() or "unclear" in category.lower():
#             return "Uncertain/Obstructed", result_json.get("reason", "")
#         return category, "Successfully classified"

#     except requests.exceptions.Timeout:
#         return "Uncertain", "VLM timeout"
#     except json.JSONDecodeError:
#         return "Uncertain", "Invalid JSON from VLM"
#     except Exception as e:
#         return "Uncertain", str(e)


# # ============================================================
# # Runtime config
# # ============================================================

# def _get_runtime_config():
#     payload_env = os.environ.get("VISION_ASSIGNMENT_PAYLOAD")
#     if payload_env:
#         return json.loads(payload_env)

#     parser = argparse.ArgumentParser()
#     parser.add_argument("--assignment_id", default="local_dev")
#     parser.add_argument("--exam_id",       type=int, required=True)
#     parser.add_argument("--shift_id",      type=int)
#     parser.add_argument("--assignment_date")
#     parser.add_argument("--link_type",     default="RTSP")
#     args = parser.parse_args()
#     return vars(args)


# # ============================================================
# # Processing logic
# # ============================================================

# def quick_accessibility_check(cam):
#     """
#     Pass-1: authenticate with server and get RTSP URL list.
#     No frame capture here — just confirms the API is reachable.
#     Returns list of URLs so Pass-2 can try each one.
#     """
#     server_address = cam.get("cctv_url")
#     device_id      = cam.get("device_id")
#     camera_no      = cam.get("camera_no")

#     stream_urls, success = build_rtsp_url(server_address, device_id, camera_no)
#     if success and stream_urls:
#         return cam, stream_urls, True, None
#     else:
#         return cam, None, False, "Failed to get stream URLs from API"


# def process_accessible_camera(cam, stream_urls, exam_id):
#     """
#     Pass-2: try every URL in the list until a frame is captured,
#     then classify with VLM.

#     Wrapped in an overall TIMEOUT_CAMERA hard cap so a single stuck
#     camera cannot monopolise a VLM worker slot indefinitely.
#     """
#     device_id = cam.get("device_id")
#     camera_no = cam.get("camera_no")
#     id_str    = f"{device_id}_{camera_no}"

#     result = {
#         'cam':    cam,
#         'status': 'Accessible',
#         'label':  'Uncertain',
#         'error':  None,
#     }

#     # Normalise: accept a single URL string or a list
#     if isinstance(stream_urls, str):
#         stream_urls = [stream_urls]

#     def _process():
#         try:
#             # ── Try each URL until one yields a frame ────────────────────
#             img_path = None
#             for idx, url in enumerate(stream_urls, 1):
#                 logger.info(
#                     f"[{id_str}] Trying URL {idx}/{len(stream_urls)}: "
#                     f"{url[:70]}..."
#                 )
#                 img_path = capture_frame_from_stream(url, id_str)
#                 if img_path:
#                     logger.info(f"[{id_str}] ✅ Frame captured from URL {idx}")
#                     break
#                 else:
#                     logger.warning(
#                         f"[{id_str}] ❌ URL {idx} failed"
#                         + (f", trying URL {idx+1}..." if idx < len(stream_urls) else " — all URLs exhausted")
#                     )

#             if not img_path:
#                 result['status'] = 'Not Accessible'
#                 result['label']  = 'Failed to Classify'
#                 result['error']  = 'Frame capture failed on all URLs'
#             else:
#                 label, reason    = classify_with_vlm(img_path)
#                 result['status'] = 'Accessible'
#                 result['label']  = (
#                     'Uncertain/Obstructed'
#                     if ("Uncertain" in label or "Obstructed" in label)
#                     else label
#                 )

#             update_rtsp_classification(exam_id, device_id, result['label'], result['status'])

#         except Exception as e:
#             result['status'] = 'Accessible'
#             result['label']  = 'Uncertain'
#             result['error']  = str(e)
#             update_rtsp_classification(exam_id, device_id, result['label'], result['status'])

#     # ── Hard overall timeout per camera ──────────────────────────────────
#     worker = threading.Thread(target=_process, daemon=True)
#     worker.start()
#     worker.join(timeout=TIMEOUT_CAMERA)

#     if worker.is_alive():
#         logger.error(
#             f"[{id_str}] ⏰ Overall camera timeout ({TIMEOUT_CAMERA}s) — "
#             f"marking as Uncertain and moving on"
#         )
#         result['status'] = 'Accessible'
#         result['label']  = 'Uncertain'
#         result['error']  = f'Overall timeout after {TIMEOUT_CAMERA}s'
#         try:
#             update_rtsp_classification(exam_id, device_id, result['label'], result['status'])
#         except Exception:
#             pass

#     return result


# # ============================================================
# # Main
# # ============================================================

# def main():
#     logger.info("=" * 80)
#     logger.info("🚀 OPTIMIZED DB-Based Scene Classification (RTSP) - TWO-PASS MODE")
#     logger.info("=" * 80)

#     runtime = _get_runtime_config()
#     exam_id = runtime["exam_id"]

#     cameras = get_rtsp_cameras(exam_id)
#     if not cameras:
#         logger.warning(f"No active RTSP cameras found in DB for exam_id={exam_id}")
#         return

#     logger.info(f"Total cameras loaded from DB : {len(cameras)}")
#     logger.info(f"Network workers              : {MAX_WORKERS_NETWORK}")
#     logger.info(f"VLM workers                  : {MAX_WORKERS_VLM}")
#     logger.info(f"Timeouts → API:{TIMEOUT_API}s  OpenCV(hard):{TIMEOUT_OPENCV_HARD}s  "
#                 f"FFmpeg:{TIMEOUT_FFMPEG}s  VLM:{TIMEOUT_VLM}s  Camera:{TIMEOUT_CAMERA}s")
#     logger.info("=" * 80)

#     start_time        = time.time()
#     processed_results = []

#     if TWO_PASS_MODE:

#         # ══════════════════════════════════════════════════════════════════
#         # PASS 1 — Quick accessibility check (no frame capture)
#         # ══════════════════════════════════════════════════════════════════
#         logger.info("\n" + "=" * 80)
#         logger.info("PASS 1: Quick Accessibility Check (API reachability only)")
#         logger.info("=" * 80)

#         accessible_cameras = []
#         pass1_start        = time.time()

#         with ThreadPoolExecutor(max_workers=MAX_WORKERS_NETWORK) as executor:
#             futures         = [executor.submit(quick_accessibility_check, cam) for cam in cameras]
#             total_pass1     = len(futures)
#             completed_pass1 = 0

#             for future in as_completed(futures):
#                 try:
#                     cam, stream_urls, is_accessible, error = future.result()
#                     device_id = cam.get("device_id")

#                     if is_accessible and stream_urls:
#                         accessible_cameras.append((cam, stream_urls))
#                     else:
#                         update_rtsp_classification(
#                             exam_id, device_id, 'Failed to Classify', 'Not Accessible'
#                         )
#                         processed_results.append({
#                             'server_address': cam.get("cctv_url"),
#                             'status':         'Not Accessible',
#                             'label':          'Failed to Classify',
#                         })

#                     completed_pass1 += 1
#                     if completed_pass1 % 10 == 0 or completed_pass1 == total_pass1:
#                         logger.info(
#                             f"⏳ Pass 1 Progress: {completed_pass1}/{total_pass1} "
#                             f"({completed_pass1/total_pass1*100:.1f}%) processed"
#                         )

#                 except Exception as e:
#                     logger.error(f"Pass 1 task failed: {e}")
#                     completed_pass1 += 1

#         pass1_time = time.time() - pass1_start
#         logger.info(f"\n✅ Pass 1 Complete!")
#         logger.info(f"   Time          : {int(pass1_time//60)}m {int(pass1_time%60)}s")
#         logger.info(f"   Accessible    : {len(accessible_cameras)}")
#         logger.info(f"   Not Accessible: {len(cameras) - len(accessible_cameras)}")

#         if not accessible_cameras:
#             logger.warning("No accessible cameras found — exiting")
#             return

#         # ══════════════════════════════════════════════════════════════════
#         # PASS 2 — Frame capture + VLM classification
#         # ══════════════════════════════════════════════════════════════════
#         logger.info("\n" + "=" * 80)
#         logger.info(f"PASS 2: Frame Capture & Classification ({len(accessible_cameras)} cameras)")
#         logger.info("=" * 80)

#         pass2_start      = time.time()
#         classified_count = 0
#         uncertain_count  = 0

#         with ThreadPoolExecutor(max_workers=MAX_WORKERS_VLM) as executor:
#             tasks = [
#                 executor.submit(process_accessible_camera, cam, stream_urls, exam_id)
#                 for cam, stream_urls in accessible_cameras
#             ]
#             total_pass2     = len(tasks)
#             completed_pass2 = 0

#             for future in as_completed(tasks):
#                 try:
#                     result    = future.result()
#                     cam       = result['cam']

#                     processed_results.append({
#                         'server_address': cam.get("cctv_url"),
#                         'status':         result['status'],
#                         'label':          result['label'],
#                     })

#                     if 'Uncertain' in result['label']:
#                         uncertain_count += 1
#                     else:
#                         classified_count += 1

#                     completed_pass2 += 1
#                     if completed_pass2 % 10 == 0 or completed_pass2 == total_pass2:
#                         logger.info(
#                             f"⏳ Pass 2 Progress: {completed_pass2}/{total_pass2} "
#                             f"({completed_pass2/total_pass2*100:.1f}%) classified"
#                         )

#                 except Exception as e:
#                     logger.error(f"Pass 2 task failed: {e}")
#                     completed_pass2 += 1

#         pass2_time = time.time() - pass2_start
#         logger.info(f"\n✅ Pass 2 Complete!")
#         logger.info(f"   Time                    : {int(pass2_time//60)}m {int(pass2_time%60)}s")
#         logger.info(f"   Classified              : {classified_count}")
#         logger.info(f"   Uncertain / Obstructed  : {uncertain_count}")

#     # ══════════════════════════════════════════════════════════════════════
#     # Final Summary
#     # ══════════════════════════════════════════════════════════════════════
#     total_time      = time.time() - start_time
#     accessible_total = classified_count + uncertain_count
#     not_accessible   = len(cameras) - accessible_total
#     success_rate     = (accessible_total / len(cameras) * 100) if cameras else 0

#     # Server-wise stats
#     server_stats = {}
#     for res in processed_results:
#         srv = res['server_address'] or "Unknown"
#         if srv not in server_stats:
#             server_stats[srv] = {'Total': 0, 'Accessible': 0}
#         server_stats[srv]['Total'] += 1
#         if res['status'] == 'Accessible':
#             server_stats[srv]['Accessible'] += 1

#     server_stats_str = ""
#     for srv, stats in server_stats.items():
#         rate = (stats['Accessible'] / stats['Total'] * 100) if stats['Total'] else 0
#         server_stats_str += (
#             f"  {srv} | Total: {stats['Total']} | "
#             f"Accessible: {stats['Accessible']} | Rate: {rate:.1f}%\n"
#         )

#     summary = f"""
# {'='*80}
# 🎉 FINAL SUMMARY
# {'='*80}
# Total Cameras               : {len(cameras)}
# ✅ Accessible (Classified)  : {classified_count}
# ⚠️  Accessible (Uncertain)   : {uncertain_count}
# ❌ Not Accessible            : {not_accessible}
# ⏱️  Total Time               : {int(total_time//60)}m {int(total_time%60)}s
# 📊 Success Rate              : {success_rate:.1f}%

# Server-wise Statistics:
# {server_stats_str}
# {'='*80}
# """
#     print(summary)
#     logger.info(summary)


# # ============================================================
# # Entry point
# # ============================================================
# if __name__ == "__main__":
#     try:
#         main()
#     except KeyboardInterrupt:
#         logger.warning("Processing interrupted by user")
#         sys.exit(1)
#     except Exception as e:
#         logger.error(f"Fatal error: {e}", exc_info=True)
#         sys.exit(1)