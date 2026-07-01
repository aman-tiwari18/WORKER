import requests
import json
import cv2
import time
import pandas as pd
import os
import sys
import shutil
from tqdm import tqdm
from PIL import Image
import subprocess
import uuid
import logging
from datetime import datetime
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import re
import queue
from collections import defaultdict
import numpy as np
import urllib.parse

# ============================================================
# Logging Setup
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
log_dir = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "logs", "vision"))
os.makedirs(log_dir, exist_ok=True)
log_filename = os.path.join(log_dir, f"multi_server_classification_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - [%(threadName)-10s] - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

logger.info("="*80)
logger.info("Starting Multi-Server Camera Classification Process - OPTIMIZED VERSION")
logger.info("="*80)

# Force RTSP over TCP for OpenCV — prevents UDP data loss behind QEMU/KVM NAT
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

# ============================================================
# Configuration - OPTIMIZED SETTINGS
# ============================================================
# Performance settings
MAX_WORKERS_NETWORK = 64  # High parallelism for network I/O
MAX_WORKERS_VLM = 4       # Concurrent VLM requests (GPU limited)
CHECKPOINT_INTERVAL = 500  # Save progress every N cameras

# Timeout settings (reduced for speed)
TIMEOUT_API = 10           # API request timeout (seconds)
TIMEOUT_OPENCV = 5000      # OpenCV timeout (milliseconds)
TIMEOUT_OPENCV_REAL = 8    # Hard thread timeout for OpenCV (seconds)
TIMEOUT_FFMPEG = 8         # FFmpeg timeout (seconds)
TIMEOUT_VLM = 60           # VLM classification timeout (seconds)

# Skip private/internal IP ranges — not reachable from outside the LAN
SKIP_PRIVATE_IPS = True

# Processing mode
TWO_PASS_MODE = True       # Enable two-pass processing for efficiency
RESUME_FROM_CHECKPOINT = True  # Resume from last checkpoint if exists

# Authentication settings
AUTH_USERNAME = "system"
# AUTH_PASSWORD = "Sasd@12345"
AUTH_PASSWORD = "Sasd@12345"
TOKEN_EXPIRY_SECONDS = 90  # Refresh token after 90 seconds

# ============================================================ 
# Excel Loading - Multi-Server Format 
# ============================================================ 
INPUT_EXCEL = os.path.join(os.path.dirname(SCRIPT_DIR), "RTSP Link (NEET 03rd May26).xlsx") 
EXCEL_SHEET = "Sheet1"
SCENE_COLUMN_TARGET = "Scene Classification"
CHECKPOINT_FILE = os.path.join(SCRIPT_DIR, "checkpoint_multi_server_progress.json")

# Check if input file exists
if not os.path.exists(INPUT_EXCEL):
    logger.error(f"Input Excel file not found: {INPUT_EXCEL}")
    logger.error("Please ensure the Excel file is placed at the correct location")
    sys.exit(1)

# Load Excel file
try:
    excel_file = pd.ExcelFile(INPUT_EXCEL)
    logger.info(f"Loaded Excel file: {INPUT_EXCEL}")
    logger.info(f"Available sheets: {excel_file.sheet_names}")
    
    # Use the specified sheet
    df = excel_file.parse(EXCEL_SHEET)
    df.columns = [col.strip() for col in df.columns]

    # Normalize known typo in column name
    df.rename(columns={'Devie ID': 'Device ID'}, inplace=True)

    logger.info(f"Using sheet: {EXCEL_SHEET}")
    logger.info(f"Columns found: {list(df.columns)}")

    # Verify required columns exist
    required_columns = ['Device ID', 'Camera No', 'Server Address']
    missing_columns = [col for col in required_columns if col not in df.columns]
    
    if missing_columns:
        logger.error(f"Missing required columns: {missing_columns}")
        sys.exit(1)
    
    # Add Scene Classification and Status columns if not present
    if SCENE_COLUMN_TARGET not in df.columns:
        df[SCENE_COLUMN_TARGET] = pd.NA
    
    if "Status" not in df.columns:
        df["Status"] = ""
    else:
        df["Status"] = df["Status"].fillna("")
    
    # Add Status Detail column for uncertain/obstructed cameras
    if "Status Detail" not in df.columns:
        df["Status Detail"] = ""
    
    # Add a unique ID column for tracking
    if "Row_ID" not in df.columns:
        df["Row_ID"] = range(len(df))
    
    # Clean up Server Address column
    df['Server Address'] = df['Server Address'].fillna('').astype(str).str.strip()
    
    logger.info(f"Total rows in Excel: {len(df)}")
    logger.info(f"Unique servers found: {df['Server Address'].nunique()}")
    logger.info(f"Server distribution:\n{df['Server Address'].value_counts()}")
    logger.info(f"Optimized settings: {MAX_WORKERS_NETWORK} network workers, {MAX_WORKERS_VLM} VLM workers")
    logger.info(f"Timeouts: API={TIMEOUT_API}s, OpenCV={TIMEOUT_OPENCV}ms, FFmpeg={TIMEOUT_FFMPEG}s")
    
except Exception as e:
    logger.error(f"Failed to load Excel file: {e}", exc_info=True)
    sys.exit(1)

# ============================================================
# Authentication for Multiple Servers
# ============================================================
import hashlib

# Server tokens storage (thread-safe)
server_tokens = {}
server_token_timestamps = {}
token_lock = threading.Lock()

def generate_signature(userName, password, realm, randomKey):
    """Generate authentication signature for camview.io API"""
    temp = hashlib.md5(password.encode()).hexdigest()
    temp = hashlib.md5((userName + temp).encode()).hexdigest()
    temp = hashlib.md5(temp.encode()).hexdigest()
    temp = hashlib.md5((userName + ":" + realm + ":" + temp).encode()).hexdigest()
    return hashlib.md5((temp + ":" + randomKey).encode()).hexdigest()


def make_api_call(url, payload, headers, timeout=None):
    """Make API call to camview.io server (treats 200 and 401 as success)"""
    if timeout is None:
        timeout = TIMEOUT_API
    
    try:
        logger.debug(f"API call to: {url}")
        response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=timeout)
        logger.debug(f"Response status: {response.status_code}")
        
        # For authentication, both 200 and 401 contain valid JSON responses
        if response.status_code in (200, 401):
            logger.debug(f"Returning response (status {response.status_code} is valid)")
            return response
        else:
            logger.debug(f"Unexpected status code: {response.status_code}, returning None")
            return None
            
    except Exception as e:
        logger.debug(f"API request exception: {e}, returning None")
        return None


def parse_json_response(response, context):
    """Parse JSON response from API call (handles both 200 and 401 responses)"""
    if response is None:
        logger.debug(f"[{context}] No response to parse")
        return None
    
    try:
        parsed_data = json.loads(response.content.decode("utf-8"))
        logger.debug(f"[{context}] Parsed JSON successfully")
        return parsed_data
    except Exception as exc:
        logger.debug(f"Failed to parse JSON for {context}: {exc}")
        logger.debug(f"Raw response text: {response.text[:200] if hasattr(response, 'text') else 'N/A'}")
        return None


def authenticate_server(server_address, max_attempts=3):
    """
    Authenticate with a camview.io server and get access token
    
    Args:
        server_address: Server address (e.g., D15.camview.io)
        max_attempts: Maximum authentication attempts
    
    Returns:
        Authentication token or None if failed
    """
    auth_url = f"https://{server_address}/admin/API/accounts/authorize"
    logger.info(f"Authenticating with {server_address}...")
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "userName": AUTH_USERNAME,
        "ipAddress": "",
        "clientType": "WINPC_V1"
    }
    
    for attempt in range(1, max_attempts + 1):
        logger.debug(f"Auth attempt {attempt}/{max_attempts} for {server_address}")
        
        # Step 1: Get realm and randomKey
        res = make_api_call(auth_url, payload, headers)
        if res is None:
            logger.debug(f"No HTTP response from {server_address}")
            continue
        
        resp_json = parse_json_response(res, "initial authorization")
        if resp_json is None:
            logger.debug(f"Failed to parse initial auth JSON from {server_address}")
            logger.debug(f"Raw response: {res.text[:300] if hasattr(res, 'text') else 'N/A'}")
            continue
        
        if resp_json.get("code") not in (None, 1000):
            logger.debug(f"Auth error code {resp_json.get('code')} from {server_address}")
            continue
        
        # Extract auth parameters
        realm = resp_json.get("realm")
        randomKey = resp_json.get("randomKey")
        publicKey = resp_json.get("publickey")
        cookie = res.headers.get("Set-Cookie", "")
        
        if not all([realm, randomKey, publicKey]):
            logger.debug(f"Missing auth parameters from {server_address}")
            logger.debug(f"realm={realm}, randomKey={randomKey}, publicKey={publicKey[:30] if publicKey else None}...")
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
            "ipAddress": "",
            "clientType": "WINPC_V1",
            "userType": "0"
        }
        
        headers2 = {"Content-Type": "application/json", "Cookie": cookie}
        res2 = make_api_call(auth_url, payload2, headers2)
        
        if res2 is None:
            logger.debug(f"No HTTP response for token exchange from {server_address}")
            continue
        
        resp_json2 = parse_json_response(res2, "token exchange")
        if resp_json2 is None:
            logger.debug(f"Failed to parse token exchange JSON from {server_address}")
            logger.debug(f"Raw response: {res2.text[:300] if hasattr(res2, 'text') else 'N/A'}")
            continue
        
        if resp_json2.get("code") not in (None, 1000):
            logger.debug(f"Token exchange error code {resp_json2.get('code')} from {server_address}")
            continue
        
        token = resp_json2.get("token")
        if token:
            logger.info(f"✅ Authenticated with {server_address}")
            return token
        else:
            logger.debug(f"Token missing in response from {server_address}")
    
    logger.error(f"❌ Authentication failed for {server_address} after {max_attempts} attempts")
    return None


def get_server_token(server_address):
    """
    Get valid token for a server (with auto-refresh)
    Thread-safe and caches tokens
    """
    global server_tokens, server_token_timestamps
    
    with token_lock:
        # Check if we have a valid cached token
        if server_address in server_tokens:
            elapsed = time.time() - server_token_timestamps.get(server_address, 0)
            if elapsed < TOKEN_EXPIRY_SECONDS:
                logger.debug(f"Using cached token for {server_address}")
                return server_tokens[server_address]
            else:
                logger.debug(f"Token expired for {server_address}, refreshing...")
        
        # Authenticate and cache token
        token = authenticate_server(server_address)
        if token:
            server_tokens[server_address] = token
            server_token_timestamps[server_address] = time.time()
            start_keepalive(server_address)

        return token


def keepalive_loop(server_address, stop_event, interval=22):
    """Background thread: sends PUT keepalive every ~22s to prevent session expiry (30s timeout)."""
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
            logger.debug(f"Keepalive sent for {server_address}")
        except Exception as e:
            logger.debug(f"Keepalive failed for {server_address}: {e}")


keepalive_threads = {}

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
    import re
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


def add_token_to_url(rtsp_url, token):
    """Append session token as ?token= query parameter.
    DSS API docs show exact format: ...cameraid=X%24Y%26substream=1?token=NNN
    The URL must remain encoded (%24, %26) and token goes after a literal '?'.
    """
    return f"{rtsp_url}?token={token}"


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
    import re
    m = re.match(r'[a-z]+://([^/:]+)(?::(\d+))?', url)
    if not m:
        return None, None
    host = m.group(1)
    port = int(m.group(2)) if m.group(2) else (554 if url.startswith('rtsp') else 443)
    return host, port


def start_video_stream(server_address, device_id, camera_no):
    """
    Start video stream and get RTSP URL from camview.io API
    
    Args:
        server_address: Server address (e.g., D15.camview.io)
        device_id: Device ID
        camera_no: Camera number
    
    Returns:
        Tuple: (rtsp_url, stream_token, success)
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
            "dataType": "2",
            "enableRtsps": "0"
        }
    }
    
    logger.debug(f"Starting video: {server_address} - {device_id}_{camera_no}")
    res = make_api_call(url, payload, headers)
    
    if res is None:
        return None, False
    
    # Check if token expired (401 during StartVideo means token issue)
    if res.status_code == 401:
        logger.debug(f"Token expired for {server_address}, clearing cache")
        with token_lock:
            server_tokens.pop(server_address, None)
        return None, None, False
    
    # Check for successful response (200 OK)
    if res.status_code != 200:
        logger.debug(f"StartVideo failed with status {res.status_code}")
        return None, None, False
    
    try:
        resp_json = res.json()
        
        # Extract data field (matches original script flow)
        data = resp_json.get("data")
        if not data:
            logger.debug(f"Missing 'data' in StartVideo response for {device_id}_{camera_no}")
            logger.debug(f"Full response: {resp_json}")
            return None, None, False
        
        # Extract RTSP URLs from data field
        # IMPORTANT: keep URLs as-is (encoded). The DSS API returns %24 for $
        # and %26 for &. The token must be appended as ?token=N on the encoded URL.
        # Decoding breaks the RTSP server path matching (returns 404 instead of 200).
        urls = []
        for key in ("url", "url2"):
            raw = data.get(key)
            if not raw:
                continue
            parts = [part.strip() for part in raw.split('|') if part.strip()]
            urls.extend(parts)

        # Extract stream token (use from data or fall back to session token)
        stream_token = data.get("token") or token
        if not data.get("token"):
            logger.debug(f"Stream token missing, using session token")

        if not urls:
            logger.debug(f"No stream URLs in response for {device_id}_{camera_no}")
            logger.debug(f"Data received: {data}")
            return None, None, False

        # Log the port being used so firewall issues are obvious
        h, p = extract_host_port(urls[0])
        logger.debug(f"✅ Got {len(urls)} URL(s) for {device_id}_{camera_no} — target port: {p}")
        for i, u in enumerate(urls, 1):
            logger.debug(f"  URL {i}: {u[:70]}...")

        # Fast TCP probe: if port is unreachable, skip the 8s+ timeout entirely
        if h and p:
            if not probe_tcp_port(h, p, timeout=2.0):
                logger.warning(f"Port {p} on {h} is BLOCKED/unreachable — skipping RTSP for {device_id}_{camera_no}. Check firewall rules for port {p}.")
                return None, None, False

        return urls, stream_token, True
            
    except Exception as e:
        logger.debug(f"Failed to parse StartVideo response: {e}")
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

    logger.debug(f"HLS StartVideo: {server_address} - {device_id}_{camera_no}")
    res = make_api_call(url, payload, headers)
    if res is None or res.status_code != 200:
        return None, None, False

    try:
        data = res.json().get("data") or {}
        urls = []
        for key in ("url", "url2"):
            raw = data.get(key)
            if not raw:
                continue
            urls.extend([p.strip() for p in raw.split('|') if p.strip()])

        stream_token = data.get("token") or token
        if urls:
            logger.debug(f"HLS URLs for {device_id}_{camera_no}: {urls[0][:70]}...")
            return urls, stream_token, True
    except Exception as e:
        logger.debug(f"HLS response parse error: {e}")

    return None, None, False


# ============================================================
# RTSP URL Construction for Multi-Server (with API)
# ============================================================
def build_rtsp_url(row):
    """
    Build RTSP URL from Excel row data using API
    
    Args:
        row: Pandas Series with camera data
    
    Returns:
        Tuple: (rtsp_url, success)
    """
    try:
        server_address = row.get('Server Address', '').strip()
        device_id = row.get('Device ID', '')
        camera_no = row.get('Camera No', '')
        
        # Validate required fields
        if not server_address or pd.isna(server_address):
            logger.debug(f"Row {row.get('Row_ID')}: No server address")
            return None, False
        
        if pd.isna(device_id) or device_id == '':
            logger.debug(f"Row {row.get('Row_ID')}: No device ID")
            return None, False
        
        if pd.isna(camera_no):
            logger.debug(f"Row {row.get('Row_ID')}: No camera number")
            return None, False
        
        # Convert to appropriate types
        device_id = str(int(device_id)) if isinstance(device_id, (int, float)) else str(device_id)
        camera_no = str(int(camera_no)) if isinstance(camera_no, (int, float)) else str(camera_no)
        
        # Get RTSP URLs (list) and stream token via API
        rtsp_urls, stream_token, success = start_video_stream(server_address, device_id, camera_no)

        if success and rtsp_urls and stream_token:
            # Filter out private/LAN IPs that are unreachable from outside
            if SKIP_PRIVATE_IPS:
                public_urls = [u for u in rtsp_urls if not is_private_ip_url(u)]
                if public_urls:
                    rtsp_urls = public_urls
                    logger.debug(f"Filtered to {len(rtsp_urls)} public URL(s) for {device_id}_{camera_no}")
            # Add token to each RTSP URL for authentication
            rtsp_urls_with_token = [add_token_to_url(url, stream_token) for url in rtsp_urls]
            logger.debug(f"✅ Got {len(rtsp_urls_with_token)} URL(s) for {device_id}_{camera_no}")
            return rtsp_urls_with_token, True

        # RTSP failed (port blocked) — try HLS fallback (HTTP/443, always firewall-friendly)
        logger.info(f"RTSP unavailable for {device_id}_{camera_no}, trying HLS fallback...")
        hls_urls, hls_token, hls_success = start_video_stream_hls(server_address, device_id, camera_no)
        if hls_success and hls_urls and hls_token:
            hls_with_token = [add_token_to_url(u, hls_token) for u in hls_urls]
            logger.info(f"✅ HLS fallback succeeded for {device_id}_{camera_no}")
            return hls_with_token, True

        logger.debug(f"❌ Both RTSP and HLS failed for {device_id}_{camera_no}")
        return None, False
        
    except Exception as e:
        logger.debug(f"Failed to build URL for Row {row.get('Row_ID')}: {e}")
        return None, False


def test_stream_url(stream_url, timeout=None):
    """
    Test if the stream URL is accessible (supports both HTTP HLS and RTSP)
    
    Args:
        stream_url: The streaming URL (HTTP or RTSP)
        timeout: Timeout in seconds (uses global TIMEOUT_API if None)
    
    Returns:
        Tuple: (is_accessible, error_message)
    """
    if timeout is None:
        timeout = TIMEOUT_API
    
    logger.debug(f"Testing stream: {stream_url[:80]}...")
    
    try:
        # For HTTP URLs, use requests
        if stream_url.startswith('http'):
            response = requests.head(stream_url, timeout=timeout, allow_redirects=True)
            if response.status_code == 200:
                logger.debug("✅ HTTP stream accessible")
                return True, None
            else:
                logger.debug(f"HTTP stream returned {response.status_code}")
                return False, f"HTTP {response.status_code}"
        else:
            # For RTSP URLs, use OpenCV
            cap = cv2.VideoCapture(stream_url)
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout * 1000)
            
            if cap.isOpened():
                cap.release()
                logger.debug("✅ RTSP stream accessible")
                return True, None
            else:
                logger.debug("RTSP stream not accessible")
                return False, "Stream cannot be opened"
            
    except Exception as e:
        logger.debug(f"Stream test failed: {e}")
        return False, str(e)


# ============================================================
# Frame Capture from Stream - OPTIMIZED (supports HTTP HLS and RTSP)
# ============================================================
def capture_frame_from_stream(stream_url, row_id, timeout_ms=None):
    """
    Capture a frame from stream (OPTIMIZED with smart bailout)
    Supports both HTTP HLS (.m3u8) and RTSP streams
    
    Args:
        stream_url: The streaming URL (HTTP or RTSP)
        row_id: Unique identifier for this camera
        timeout_ms: Timeout in milliseconds (uses global TIMEOUT_OPENCV if None)
    
    Returns:
        Path to saved frame image, or None if capture fails
    """
    if timeout_ms is None:
        timeout_ms = TIMEOUT_OPENCV
    
    logger.debug(f"Capturing frame for Row ID {row_id}")
    
    # Try with OpenCV first
    img_path = capture_frame_with_opencv_stream(stream_url, row_id, timeout_ms)
    
    if img_path:
        return img_path
    
    # Fallback to FFmpeg
    logger.debug("OpenCV failed, trying FFmpeg...")
    img_path = capture_frame_with_ffmpeg_stream(stream_url, row_id)
    
    return img_path


def _opencv_capture_worker(stream_url, result_box):
    """Worker that runs in a daemon thread; stores result in result_box[0]."""
    try:
        cap = cv2.VideoCapture(stream_url)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, TIMEOUT_OPENCV)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, TIMEOUT_OPENCV)
        if not cap.isOpened():
            cap.release()
            return
        for _ in range(3):
            cap.read()
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None:
            result_box[0] = frame
    except Exception:
        pass


def capture_frame_with_opencv_stream(stream_url, row_id, timeout_ms=None):
    """Capture frame using OpenCV with a hard thread timeout to avoid 30s hangs."""
    result_box = [None]
    t = threading.Thread(target=_opencv_capture_worker, args=(stream_url, result_box), daemon=True)
    t.start()
    t.join(timeout=TIMEOUT_OPENCV_REAL)

    if result_box[0] is None:
        logger.debug(f"OpenCV failed to open stream for Row ID {row_id}")
        return None

    frame = result_box[0]
    save_dir = f"frames/multi_server/{row_id}"
    os.makedirs(save_dir, exist_ok=True)
    img_path = f"{save_dir}/frame_{uuid.uuid4().hex}.jpg"
    cv2.imwrite(img_path, frame)
    logger.debug(f"✅ Frame captured with OpenCV: {img_path}")
    return img_path


def capture_frame_with_ffmpeg_stream(stream_url, row_id):
    """Capture frame using FFmpeg (OPTIMIZED) - supports both HTTP HLS and RTSP"""
    if not shutil.which("ffmpeg"):
        logger.debug("ffmpeg not found")
        return None
    
    save_dir = f"frames/multi_server/{row_id}"
    os.makedirs(save_dir, exist_ok=True)
    img_path = f"{save_dir}/frame_{uuid.uuid4().hex}.jpg"
    
    # Build command based on URL type
    if stream_url.startswith('rtsp'):
        cmd = [
            "ffmpeg",
            "-y",
            "-rtsp_transport", "tcp",
            "-timeout", str(TIMEOUT_FFMPEG * 1000000),  # Convert to microseconds
            "-i", stream_url,
            "-frames:v", "1",
            "-loglevel", "error",
            img_path
        ]
    else:
        # HTTP/HLS streams
        cmd = [
            "ffmpeg",
            "-y",
            "-timeout", str(TIMEOUT_FFMPEG * 1000000),
            "-i", stream_url,
            "-frames:v", "1",
            "-loglevel", "error",
            img_path
        ]
    
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=TIMEOUT_FFMPEG)

        if result.returncode != 0 or not os.path.exists(img_path):
            stderr_out = result.stderr.decode('utf-8', errors='replace').strip()
            logger.warning(f"FFmpeg failed for Row ID {row_id} (exit={result.returncode}): {stderr_out}")
            return None

        logger.debug(f"✅ Frame captured with FFmpeg: {img_path}")
        return img_path

    except subprocess.TimeoutExpired as e:
        # Capture whatever stderr was written before timeout
        stderr_out = (e.stderr or b'').decode('utf-8', errors='replace').strip()
        logger.warning(f"FFmpeg timed out for Row ID {row_id}: {stderr_out or 'no stderr'}")
        return None
    except Exception as e:
        logger.warning(f"FFmpeg error for Row ID {row_id}: {e}")
        return None


# ============================================================
# VLM Classification - OPTIMIZED with Grey/Obstruction Detection
# ============================================================

def is_grey_or_obstructed(image_path):
    """
    Quick check if image is grey screen or heavily obstructed
    Returns: (is_problematic, reason)
    """
    try:
        img = cv2.imread(image_path)
        if img is None:
            return True, "Failed to read image"
        
        # Convert to grayscale
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        
        # Check 1: Nearly uniform color (grey screen)
        std_dev = gray.std()
        if std_dev < 5:  # Very low variance = uniform grey
            return True, "Uniform grey screen"
        
        # Check 2: Very dark image (camera blocked)
        mean_brightness = gray.mean()
        if mean_brightness < 10:
            return True, "Very dark/blocked camera"
        
        # Check 3: Very bright image (overexposed/white)
        if mean_brightness > 245:
            return True, "Overexposed/white screen"
        
        # Check 4: Low detail (blurry/obstructed)
        laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
        if laplacian_var < 10:
            return True, "Low detail/obstructed"
        
        return False, None
        
    except Exception as e:
        logger.debug(f"Grey check failed: {e}")
        return False, None


def classify_with_vlm(image_path):
    """
    Uses Ollama Vision Model for scene classification (OPTIMIZED)
    Includes detection for grey/obstructed cameras
    """
    logger.debug(f"VLM classification: {image_path}")
    
    # Quick pre-check for grey/obstructed images
    is_bad, reason = is_grey_or_obstructed(image_path)
    if is_bad:
        logger.debug(f"Image flagged as problematic: {reason}")
        return "Uncertain/Obstructed", reason

    # === CATEGORY DEFINITIONS ===
    CATEGORIES = [
        "Server Room",
        "Outdoor Scene",
        "Outside Area",
        "Parking Lot",
        "Entry",
        "Computer Lab"
    ]

    PROMPT = f"""You are an expert computer vision classifier.
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

    url = "http://localhost:11434/api/generate"
    
    # Read and encode the image to base64
    try:
        with open(image_path, 'rb') as image_file:
            image_data = base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
        logger.debug(f"Failed to encode image: {e}")
        return "Uncertain", "Failed to read image"
    
    payload = {
        "model": "qwen3-vl:8b",
        "prompt": PROMPT,
        "images": [image_data],
        "stream": False
    }

    try:
        response = requests.post(url, json=payload, timeout=TIMEOUT_VLM)
        
        if response.status_code != 200:
            logger.debug(f"VLM request failed: {response.status_code}")
            return "Uncertain", "VLM request failed"
        
        response_json = response.json()
        result = response_json.get("response", "").strip()
        
        if not result:
            return "Uncertain", "Empty VLM response"
        
        # Parse JSON response
        try:
            result_json = json.loads(result)
            category = result_json.get("category", "Uncertain")
            reason = result_json.get("reason", "")
            
            # Check if VLM also detected uncertainty
            if "uncertain" in category.lower() or "unclear" in category.lower():
                return "Uncertain/Obstructed", reason
            
            return category, reason
        except json.JSONDecodeError:
            logger.debug("Could not parse VLM JSON")
            return "Uncertain", "Invalid VLM response"
            
    except requests.exceptions.Timeout:
        logger.debug("VLM request timed out")
        return "Uncertain", "VLM timeout"
    except Exception as e:
        logger.debug(f"VLM error: {e}")
        return "Uncertain", str(e)


# ============================================================
# Excel Update & Checkpoint System (Thread-safe)
# ============================================================
excel_lock = threading.Lock()
checkpoint_counter = 0

def update_excel_in_memory(row_id, label, status, status_detail=""):
    """Update dataframe in memory (thread-safe) with status detail"""
    global df, checkpoint_counter
    with excel_lock:
        mask = df["Row_ID"] == row_id
        if mask.any():
            df.loc[mask, SCENE_COLUMN_TARGET] = label
            df.loc[mask, "Status"] = status
            df.loc[mask, "Status Detail"] = status_detail
            logger.debug(f"Updated Row {row_id}: {status} - {label}")
            
            # Increment checkpoint counter
            checkpoint_counter += 1
            if checkpoint_counter % CHECKPOINT_INTERVAL == 0:
                save_checkpoint()
        else:
            logger.warning(f"Row ID {row_id} not found")


def save_checkpoint():
    """Save current progress to checkpoint file"""
    try:
        checkpoint_data = {
            'timestamp': datetime.now().isoformat(),
            'processed_rows': df[df['Status'] != ''].shape[0],
            'total_rows': len(df),
            'dataframe': df.to_dict('records')
        }
        
        with open(CHECKPOINT_FILE, 'w') as f:
            json.dump(checkpoint_data, f)
        
        logger.info(f"✅ Checkpoint saved: {checkpoint_data['processed_rows']}/{checkpoint_data['total_rows']}")
    except Exception as e:
        logger.error(f"Failed to save checkpoint: {e}")


def load_checkpoint():
    """Load progress from checkpoint file"""
    global df
    
    if not os.path.exists(CHECKPOINT_FILE):
        return False
    
    try:
        with open(CHECKPOINT_FILE, 'r') as f:
            checkpoint_data = json.load(f)
        
        logger.info(f"Found checkpoint from {checkpoint_data['timestamp']}")
        logger.info(f"Previously processed: {checkpoint_data['processed_rows']}/{checkpoint_data['total_rows']}")
        
        # Restore dataframe
        df_checkpoint = pd.DataFrame(checkpoint_data['dataframe'])
        
        # Merge with current df (in case Excel changed)
        if len(df_checkpoint) == len(df):
            df = df_checkpoint
            logger.info("✅ Checkpoint loaded successfully")
            return True
        else:
            logger.warning("Checkpoint size mismatch, starting fresh")
            return False
            
    except Exception as e:
        logger.error(f"Failed to load checkpoint: {e}")
        return False


# ============================================================
# Camera Processing - OPTIMIZED TWO-PASS STRATEGY
# ============================================================

def quick_accessibility_check(row):
    """
    PASS 1: Quick accessibility check using API
    Tests if we can get RTSP URLs from the server
    
    Returns: (row_id, stream_urls, is_accessible, error_message)
    """
    row_id = row['Row_ID']
    
    # Build stream URLs using API
    stream_urls, success = build_rtsp_url(row)
    
    if success and stream_urls:
        return (row_id, stream_urls, True, None)
    else:
        return (row_id, None, False, 'Failed to get stream URLs from API')


def process_accessible_camera(row, stream_urls):
    """
    PASS 2: Full processing for accessible cameras (capture + classify)

    IMPORTANT: Stream tokens expire after 30 seconds of non-use (DSS API spec).
    Pass 1 tokens may be stale by the time Pass 2 runs for later cameras.
    We always re-call StartVideo here to get a fresh token immediately before
    connecting to RTSP, then fall back to the stored URLs if re-fetch fails.
    """
    row_id = row['Row_ID']

    result = {
        'row_id': row_id,
        'status': 'Accessible',
        'label': 'Uncertain',
        'status_detail': '',
        'error': None
    }

    try:
        # Re-fetch fresh RTSP URLs+token right before connecting (30s token window)
        fresh_urls, success = build_rtsp_url(row)
        if success and fresh_urls:
            active_urls = fresh_urls
        else:
            logger.debug(f"Row {row_id}: Re-fetch failed, using Pass 1 URLs")
            active_urls = stream_urls or []

        # Try each URL in sequence until one works
        img_path = None
        for url_idx, stream_url in enumerate(active_urls, 1):
            logger.debug(f"Trying RTSP option {url_idx}/{len(active_urls)} for Row ID {row_id}")
            img_path = capture_frame_from_stream(stream_url, row_id)
            if img_path:
                logger.debug(f"Successfully captured frame from URL {url_idx}")
                break
        
        if not img_path:
            result['status'] = 'Not Accessible'
            result['label'] = 'Failed to Classify'
            result['error'] = 'Frame capture failed (all URLs tried)'
            result['status_detail'] = 'Could not capture frame from any URL'
            return result
        
        # Classify the frame
        label, reason = classify_with_vlm(img_path)
        
        if "Uncertain" in label or "Obstructed" in label:
            # Mark as Accessible but Uncertain/Obstructed
            result['status'] = 'Accessible'
            result['label'] = 'Uncertain/Obstructed'
            result['status_detail'] = reason or 'Image unclear or obstructed'
        else:
            result['status'] = 'Accessible'
            result['label'] = label
            result['status_detail'] = 'Successfully classified'
        
        return result
        
    except Exception as e:
        logger.error(f"Error in Pass 2 for Row {row_id}: {e}")
        result['status'] = 'Accessible'
        result['label'] = 'Uncertain'
        result['status_detail'] = f'Error: {str(e)}'
        result['error'] = str(e)
        return result


# ============================================================
# Main Processing Loop - OPTIMIZED TWO-PASS STRATEGY
# ============================================================
def main():
    global df
    
    logger.info("="*80)
    logger.info("🚀 OPTIMIZED Multi-Server Camera Classification - TWO-PASS MODE")
    logger.info("="*80)
    logger.info(f"Total cameras: {len(df)}")
    logger.info(f"Network workers: {MAX_WORKERS_NETWORK}")
    logger.info(f"VLM workers: {MAX_WORKERS_VLM}")
    logger.info(f"Checkpoint interval: {CHECKPOINT_INTERVAL}")
    logger.info("="*80)
    
    # Load checkpoint if exists
    if RESUME_FROM_CHECKPOINT and load_checkpoint():
        # Filter to only unprocessed rows
        unprocessed = df[df['Status'] == '']
        if len(unprocessed) == 0:
            logger.info("✅ All cameras already processed!")
            save_results()
            return
        logger.info(f"Resuming: {len(unprocessed)} cameras remaining")
    
    start_time = time.time()
    
    if TWO_PASS_MODE:
        # ========== PASS 1: Quick Accessibility Check ==========
        logger.info("\n" + "="*80)
        logger.info("PASS 1: Quick Accessibility Check (No frame capture)")
        logger.info("="*80)
        
        accessible_cameras = []
        pass1_start = time.time()
        
        # Get rows to process
        rows_to_check = df[df['Status'] == ''].copy()
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS_NETWORK) as executor:
            futures = [executor.submit(quick_accessibility_check, row) 
                      for _, row in rows_to_check.iterrows()]
            
            with tqdm(total=len(futures), desc="Pass 1: Checking accessibility") as pbar:
                for future in as_completed(futures):
                    try:
                        row_id, stream_urls, is_accessible, error = future.result()
                        
                        if is_accessible and stream_urls:
                            # Store for Pass 2 (stream_urls is a list)
                            accessible_cameras.append((row_id, stream_urls))
                        else:
                            # Mark as not accessible
                            update_excel_in_memory(
                                row_id, 
                                'Failed to Classify', 
                                'Not Accessible',
                                error or 'Stream not accessible'
                            )
                        
                        pbar.update(1)
                        
                    except Exception as e:
                        logger.error(f"Pass 1 task failed: {e}")
                        pbar.update(1)
        
        pass1_time = time.time() - pass1_start
        
        logger.info(f"\n✅ Pass 1 Complete!")
        logger.info(f"   Time: {int(pass1_time//60)}m {int(pass1_time%60)}s")
        logger.info(f"   Accessible: {len(accessible_cameras)}")
        logger.info(f"   Not Accessible: {len(rows_to_check) - len(accessible_cameras)}")
        
        # Save checkpoint after Pass 1
        save_checkpoint()
        
        if len(accessible_cameras) == 0:
            logger.warning("No accessible cameras found!")
            save_results()
            return
        
        # ========== PASS 2: Frame Capture & Classification ==========
        logger.info("\n" + "="*80)
        logger.info(f"PASS 2: Frame Capture & Classification ({len(accessible_cameras)} cameras)")
        logger.info("="*80)
        
        pass2_start = time.time()
        classified_count = 0
        uncertain_count = 0
        
        # Process accessible cameras with VLM
        with ThreadPoolExecutor(max_workers=MAX_WORKERS_VLM) as executor:
            # Create tasks for accessible cameras
            tasks = []
            for row_id, stream_urls in accessible_cameras:
                row = df[df['Row_ID'] == row_id].iloc[0]
                tasks.append(executor.submit(process_accessible_camera, row, stream_urls))
            
            with tqdm(total=len(tasks), desc="Pass 2: Classifying") as pbar:
                for future in as_completed(tasks):
                    try:
                        result = future.result()
                        
                        # Update dataframe
                        update_excel_in_memory(
                            result['row_id'],
                            result['label'],
                            result['status'],
                            result['status_detail']
                        )
                        
                        if 'Uncertain' in result['label']:
                            uncertain_count += 1
                        else:
                            classified_count += 1
                        
                        pbar.update(1)
                        
                    except Exception as e:
                        logger.error(f"Pass 2 task failed: {e}")
                        pbar.update(1)
        
        pass2_time = time.time() - pass2_start
        
        logger.info(f"\n✅ Pass 2 Complete!")
        logger.info(f"   Time: {int(pass2_time//60)}m {int(pass2_time%60)}s")
        logger.info(f"   Classified: {classified_count}")
        logger.info(f"   Uncertain/Obstructed: {uncertain_count}")
    
    # ========== Final Summary ==========
    total_time = time.time() - start_time
    
    accessible_total = len(df[df['Status'] == 'Accessible'])
    not_accessible = len(df[df['Status'] == 'Not Accessible'])
    uncertain = len(df[df['Status Detail'].str.contains('Uncertain|Obstructed', na=False)])
    
    # Server-wise statistics
    server_stats = df.groupby('Server Address').agg({
        'Status': lambda x: (x == 'Accessible').sum(),
        'Row_ID': 'count'
    }).rename(columns={'Status': 'Accessible', 'Row_ID': 'Total'})
    server_stats['Success Rate'] = (server_stats['Accessible'] / server_stats['Total'] * 100).round(1)
    
    summary = f"""
{'='*80}
🎉 FINAL SUMMARY
{'='*80}
Total Cameras: {len(df)}
✅ Accessible (Classified): {accessible_total - uncertain}
⚠️  Accessible (Uncertain/Obstructed): {uncertain}
❌ Not Accessible: {not_accessible}
⏱️  Total Time: {int(total_time//60)}m {int(total_time%60)}s
📊 Success Rate: {(accessible_total/len(df)*100):.1f}%

Server-wise Statistics:
{server_stats.to_string()}
{'='*80}
"""
    
    print(summary)
    logger.info(summary)
    
    # Save final results
    save_results()

    # Stop keepalive threads
    stop_all_keepalives()

    # Clean up checkpoint
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        logger.info("✅ Checkpoint file cleaned up")


def save_results():
    """
    Save results to THREE separate Excel files:
    1. Accessible cameras (classified successfully)
    2. Accessible but Uncertain/Obstructed cameras
    3. Not Accessible cameras
    """
    logger.info("="*80)
    logger.info("Saving results to Excel files")
    logger.info("="*80)
    
    # Create output directory
    output_dir = "results/multi_server_classification"
    os.makedirs(output_dir, exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    try:
        # Split dataframe into three categories
        accessible_df = df[
            (df['Status'] == 'Accessible') & 
            (~df['Status Detail'].str.contains('Uncertain|Obstructed', na=False))
        ].copy()
        
        uncertain_df = df[
            (df['Status'] == 'Accessible') & 
            (df['Status Detail'].str.contains('Uncertain|Obstructed', na=False))
        ].copy()
        
        not_accessible_df = df[df['Status'] == 'Not Accessible'].copy()
        
        # Remove Row_ID column (internal use only)
        for dataframe in [accessible_df, uncertain_df, not_accessible_df]:
            if 'Row_ID' in dataframe.columns:
                dataframe.drop('Row_ID', axis=1, inplace=True)
        
        # Save accessible cameras
        if len(accessible_df) > 0:
            accessible_file = os.path.join(output_dir, f"accessible_classified_{timestamp}.xlsx")
            accessible_df.to_excel(accessible_file, index=False, sheet_name="Classified")
            logger.info(f"✅ Saved {len(accessible_df)} classified cameras to: {accessible_file}")
            print(f"   ✅ Classified: {accessible_file}")
        
        # Save uncertain/obstructed cameras
        if len(uncertain_df) > 0:
            uncertain_file = os.path.join(output_dir, f"accessible_uncertain_{timestamp}.xlsx")
            uncertain_df.to_excel(uncertain_file, index=False, sheet_name="Uncertain")
            logger.info(f"⚠️  Saved {len(uncertain_df)} uncertain/obstructed cameras to: {uncertain_file}")
            print(f"   ⚠️  Uncertain: {uncertain_file}")
        
        # Save non-accessible cameras
        if len(not_accessible_df) > 0:
            not_accessible_file = os.path.join(output_dir, f"not_accessible_{timestamp}.xlsx")
            not_accessible_df.to_excel(not_accessible_file, index=False, sheet_name="Not Accessible")
            logger.info(f"❌ Saved {len(not_accessible_df)} non-accessible cameras to: {not_accessible_file}")
            print(f"   ❌ Not Accessible: {not_accessible_file}")
        
        # Also save complete results in one file with multiple sheets
        complete_file = os.path.join(output_dir, f"complete_results_{timestamp}.xlsx")
        df_save = df.copy()
        if 'Row_ID' in df_save.columns:
            df_save.drop('Row_ID', axis=1, inplace=True)
        
        with pd.ExcelWriter(complete_file, engine='openpyxl') as writer:
            if len(accessible_df) > 0:
                accessible_df.to_excel(writer, sheet_name="Classified", index=False)
            if len(uncertain_df) > 0:
                uncertain_df.to_excel(writer, sheet_name="Uncertain", index=False)
            if len(not_accessible_df) > 0:
                not_accessible_df.to_excel(writer, sheet_name="Not Accessible", index=False)
            df_save.to_excel(writer, sheet_name="All Results", index=False)
        
        logger.info(f"📊 Saved complete results to: {complete_file}")
        print(f"\n📁 Complete Results: {complete_file}")
        
        # Save server-wise summary
        try:
            server_stats_data = []
            for server in df['Server Address'].unique():
                server_df = df[df['Server Address'] == server]
                total = len(server_df)
                accessible = (server_df['Status'] == 'Accessible').sum()
                not_accessible = (server_df['Status'] == 'Not Accessible').sum()
                success_rate = (accessible / total * 100) if total > 0 else 0
                
                server_stats_data.append({
                    'Server Address': server,
                    'Total': total,
                    'Accessible': accessible,
                    'Not Accessible': not_accessible,
                    'Success Rate %': round(success_rate, 2)
                })
            
            server_summary = pd.DataFrame(server_stats_data)
            
            summary_file = os.path.join(output_dir, f"server_summary_{timestamp}.xlsx")
            server_summary.to_excel(summary_file, sheet_name="Server Summary", index=False)
            logger.info(f"📈 Saved server summary to: {summary_file}")
            print(f"📈 Server Summary: {summary_file}")
        except Exception as e:
            logger.warning(f"Could not create server summary: {e}")
        
    except Exception as e:
        logger.error(f"Failed to save results: {e}", exc_info=True)


# ============================================================
# Entry Point
# ============================================================
if __name__ == "__main__":
    try:
        main()
        logger.info("="*80)
        logger.info("Processing completed successfully!")
        logger.info(f"Log file: {log_filename}")
        logger.info("="*80)
    except KeyboardInterrupt:
        logger.warning("\n\nProcessing interrupted by user")
        save_results()
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
