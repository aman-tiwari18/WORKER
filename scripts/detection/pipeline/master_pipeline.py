
"""
Master Detection Pipeline
=========================
Supports both RTSP (DSS-auth + OpenCV capture) and HLS (GPU-batch workers).
Camera configs loaded from DB via utils.camera_loader (env-var driven).

FIXED: 
- Removed broken HLS fallback from RTSP producer (protocol mixing caused frame failures)
- Fixed tuple unpacking bug when RTSP returns 2-tuple vs HLS 3-tuple
- Fixed order_urls() to properly detect ALL private IPs (10.x, 172.16-31.x, 192.168.x)
- Switched from raw ffmpeg pipe to OpenCV VideoCapture (robust resolution negotiation)
- Reduced MAX_CONCURRENT from 64 → 24 to prevent system overload
- Reduced producer FPS from 25 → 5 (0.2s sleep) to prevent GPU queue overflow
- Added responsive shutdown in producer threads (join(timeout=1.0) loop)
- Added pre-connection stream token refresh to avoid stale-token hangs
- Added frame validation before queuing
"""


import multiprocessing as mp
import os
import sys
import time
import argparse
import logging
import signal
import requests
import hashlib
import urllib.parse
import subprocess
import numpy as np
import threading
import importlib
import json
import re
import fcntl
import queue as queue_module  # FIXED: imported for queue.Full exception
import cv2  # FIXED: added for robust VideoCapture-based streaming


# ================= PATH SETUP =================
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
WORKERS_DIR  = os.path.join(PROJECT_ROOT, "workers")
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, WORKERS_DIR)

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Force RTSP over TCP for OpenCV — prevents UDP data loss behind QEMU/KVM NAT
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

# Silence the OpenCV/FFmpeg capture-error flood. Round-robin probing of
# capacity-limited DVRs produces thousands of "open VIDEOIO(FFMPEG)" / "method
# OPTIONS failed: 401" lines per minute on stderr; that flood bloats the per-run
# logs to 60+ MB and starves the poller's event loop (which drains subprocess
# output), contributing to its instability. These are expected DVR rejections,
# not actionable errors — quiet them at the libav + OpenCV layers.
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")  # AV_LOG_QUIET
try:
    cv2.setLogLevel(0)  # 0 = LOG_LEVEL_SILENT
except Exception:
    pass


def _worker_process_entry(module_name, fn_name, queue):
    """
    Trampoline for mp.Process — pickle-safe with spawn mode.
    Imports the worker module inside the child process and calls the worker function.
    """
    import importlib
    # Re-add paths in child process (spawn resets sys.path)
    _base     = os.path.dirname(os.path.abspath(__file__))
    _root     = os.path.abspath(os.path.join(_base, ".."))
    _workers  = os.path.join(_root, "workers")
    for p in (_root, _workers):
        if p not in sys.path:
            sys.path.insert(0, p)

    try:
        module = importlib.import_module(module_name)
    except (ImportError, ModuleNotFoundError):
        module = importlib.import_module(f"workers.{module_name}")

    fn = getattr(module, fn_name)
    fn(queue)


def get_worker(analytic_name, mode="rtsp"):
    """
    Lazily import the requested worker to prevent loading unused models.
    Returns the worker function for the given analytic and mode (rtsp/hls).
    NOTE: For mp.Process with spawn mode, use get_worker_info() + _worker_process_entry instead.
    """
    mapping = {
        "server":           ("server_room_worker",         "server_room_worker",         "server_room_worker_hls"),
        "crowd":            ("crowd_worker",               "crowd_worker",               "crowd_worker_hls"),
        "crowd_biometric":  ("crowd_det_biometric_worker", "crowd_det_biometric_worker", "crowd_det_biometric_worker_hls"),
        "intruder":         ("intruder_worker",            "intruder_worker",            "intruder_worker_hls"),
        "entry_exit":       ("entry_exit_worker",          "entry_exit_worker",          "entry_exit_worker_hls"),
        "proximity":        ("close_proximity_worker",     "close_proximity_worker",     "close_proximity_worker_hls"),
        "phone":            ("phone_worker",               "phone_worker",               "phone_worker_hls"),
        "invigilator":      ("invigilator_worker",         "invigilator_worker",         "invigilator_worker_hls"),
        "tamper":           ("camera_tampering_worker",    "camera_tampering_worker",    "camera_tampering_worker_hls"),
        "inversion":        ("camera_inversion_worker",    "camera_inversion_worker",    "camera_inversion_worker_hls"),
        "crowd_zero":       ("crowd_zero_det_worker",      "crowd_zero_det_worker",      "crowd_zero_det_worker_hls"),
        "crowd_five":       ("crowd_five_det_worker",      "crowd_five_det_worker",      "crowd_five_det_worker_hls"),
        "blind_spot":       ("blind_spot_worker",          "blind_spot_worker",          "blind_spot_worker_hls"),
        "strongroom":       ("strongroom_worker",          "strongroom_worker",          "strongroom_worker_hls"),
        "qp_box":           ("qp_box_det_worker",          "qp_box_det_worker",          "qp_box_det_worker_hls"),
    }
    
    if analytic_name not in mapping:
        logger.error(f"Unknown analytic requested: {analytic_name}")
        return None
        
    module_name, rtsp_fn, hls_fn = mapping[analytic_name]
    try:
        try:
            module = importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError):
            module = importlib.import_module(f"workers.{module_name}")

        fn_name = rtsp_fn if mode.lower() == "rtsp" else hls_fn
        return getattr(module, fn_name)
    except (ImportError, AttributeError, ModuleNotFoundError) as e:
        logger.error(f"Failed to load worker {analytic_name} ({mode}): {e}")
        return None


def get_worker_info(analytic_name, mode="rtsp"):
    """
    Returns (module_name, fn_name) strings for use with _worker_process_entry.
    This is pickle-safe for mp.Process with spawn start method.
    """
    mapping = {
        "server":           ("server_room_worker",         "server_room_worker",         "server_room_worker_hls"),
        "crowd":            ("crowd_worker",               "crowd_worker",               "crowd_worker_hls"),
        "crowd_biometric":  ("crowd_det_biometric_worker", "crowd_det_biometric_worker", "crowd_det_biometric_worker_hls"),
        "intruder":         ("intruder_worker",            "intruder_worker",            "intruder_worker_hls"),
        "entry_exit":       ("entry_exit_worker",          "entry_exit_worker",          "entry_exit_worker_hls"),
        "proximity":        ("close_proximity_worker",     "close_proximity_worker",     "close_proximity_worker_hls"),
        "phone":            ("phone_worker",               "phone_worker",               "phone_worker_hls"),
        "invigilator":      ("invigilator_worker",         "invigilator_worker",         "invigilator_worker_hls"),
        "tamper":           ("camera_tampering_worker",    "camera_tampering_worker",    "camera_tampering_worker_hls"),
        "inversion":        ("camera_inversion_worker",    "camera_inversion_worker",    "camera_inversion_worker_hls"),
        "crowd_zero":       ("crowd_zero_det_worker",      "crowd_zero_det_worker",      "crowd_zero_det_worker_hls"),
        "crowd_five":       ("crowd_five_det_worker",      "crowd_five_det_worker",      "crowd_five_det_worker_hls"),
        "blind_spot":       ("blind_spot_worker",          "blind_spot_worker",          "blind_spot_worker_hls"),
        "strongroom":       ("strongroom_worker",          "strongroom_worker",          "strongroom_worker_hls"),
        "qp_box":           ("qp_box_det_worker",          "qp_box_det_worker",          "qp_box_det_worker_hls"),
    }

    if analytic_name not in mapping:
        logger.error(f"Unknown analytic requested: {analytic_name}")
        return None, None

    module_name, rtsp_fn, hls_fn = mapping[analytic_name]
    fn_name = rtsp_fn if mode.lower() == "rtsp" else hls_fn
    return module_name, fn_name


# ================= CAMERA LOADER (DB-driven) =================
from utils.camera_loader import load_all_pending_cameras, load_cameras_for_execution


# ================= CONFIG =================
QUEUE_SIZE     = 5
STOP           = False
# Max cameras that stream CONCURRENTLY per analytic process. This is NOT a
# startup rate-limit — start_producer() holds the semaphore for the producer's
# WHOLE lifetime, so any camera beyond this cap blocks forever and is NEVER
# processed (silent drop). The old hard-coded 24 was tuned for a small box; on
# the current 8×H200 / 224-core workers it throttled the GPU to ~30% and left
# most of each worker's camera shard unprocessed. Now env-overridable (per host
# size) with a default large enough to cover a typical per-worker shard.
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "256"))

# UNIFIED DETECTOR (Scaling Path A): run ONE shared YOLO pass per frame and dispatch the
# boxes to each camera's enabled analytics, instead of one model-per-analytic worker.
# Removes the ~10x redundant inference. Default ON (validated on worker-3 covering the full
# shard) — set UNIFIED_DETECTOR=0 per-worker for an instant rollback to the legacy path.
UNIFIED_DETECTOR = os.environ.get("UNIFIED_DETECTOR", "1") == "1"

# NVDEC hardware decode via GStreamer (Scaling Path A). Default OFF until the worker's
# OpenCV build is confirmed to have GStreamer + nvv4l2decoder (set USE_NVDEC=1 to enable).
# Falls back to the FFMPEG path automatically if the GStreamer pipeline fails to open.
USE_NVDEC = os.environ.get("USE_NVDEC", "0") == "1"
# Overridable pipeline (use {url}/{latency}); default is H.264. For H.265 swap
# rtph264depay/h264parse -> rtph265depay/h265parse.
GST_PIPELINE_TEMPLATE = os.environ.get(
    "GST_PIPELINE_TEMPLATE",
    'rtspsrc location="{url}" protocols=tcp latency={latency} ! '
    'rtph264depay ! h264parse ! nvv4l2decoder ! '
    'nvvidconv ! video/x-raw,format=BGRx ! '
    'videoconvert ! video/x-raw,format=BGR ! '
    'appsink drop=true max-buffers=1 sync=false'
)

# Sampling rate per camera. Default 5 fps = current behavior (0.2s). Scaling Path A
# lowers this to ~3 fps via SAMPLE_FPS=3 (cooldowns are 60-360s so 3 fps is plenty).
SAMPLE_FPS      = float(os.environ.get("SAMPLE_FPS", "5"))
_FRAME_INTERVAL = (1.0 / SAMPLE_FPS) if SAMPLE_FPS > 0 else 0.2

# ROUND-ROBIN (Scaling Path A): cover ALL assigned cameras (not just a 256-cap live pool) by
# rotating a bounded pool of capture slots. Default ON with UNIFIED_DETECTOR. The per-DVR
# concurrency cap (MAX_STREAMS_PER_DVR) — not RR_POOL_SIZE — is the real throttle, so the pool
# can be sized to cover the whole shard without overwhelming any single DVR.
ROUND_ROBIN          = os.environ.get("ROUND_ROBIN", "1") == "1"
RR_POOL_SIZE         = int(os.environ.get("RR_POOL_SIZE", "256"))    # max concurrent capture threads
RR_BURST_FRAMES      = int(os.environ.get("RR_BURST_FRAMES", "2"))   # frames grabbed per visit
# Max simultaneous RTSP streams pulled from one DVR/server. Matches the proven classification
# path (VISION_MAX_WORKERS_RTSP_CAPTURE=6). A large RR pool fans out across DVRs but never
# exceeds this per DVR — prevents the 2084/DVR_CAPACITY storms that zeroed the GPU before.
MAX_STREAMS_PER_DVR  = int(os.environ.get("MAX_STREAMS_PER_DVR", "6"))
# Cache the per-camera StartVideo (RTSP URL + stream token) for this many seconds to avoid a
# DSS API call on every RR revisit. Default 180s (DSS tokens outlive this); 0 disables.
STARTVIDEO_CACHE_TTL = float(os.environ.get("STARTVIDEO_CACHE_TTL", "180"))
_ACTIVE_CHILD_PROCESSES = set()
_ACTIVE_CHILD_LOCK = threading.Lock()

DSS_USER = "system"
DSS_PASS = "Sasd@12345"
# DSS_PASS = "Sasd@12345"

# ================= CRITICAL FIX: Shared disk-based token cache =================
TOKEN_CACHE_FILE = os.path.join(PROJECT_ROOT, "server_tokens.json")
TOKEN_LOCK_FILE = f"{TOKEN_CACHE_FILE}.lock"
TOKEN_EXPIRY_SECONDS = 300
LOCKOUT_WAIT_SECONDS = 300

_server_tokens_local = {}
_server_token_timestamps_local = {}
_local_lock = threading.Lock()


def _load_token_cache():
    """Load tokens from disk. Thread-safe but NOT process-safe — caller must hold file lock."""
    global _server_tokens_local, _server_token_timestamps_local
    if not os.path.exists(TOKEN_CACHE_FILE):
        return
    try:
        with open(TOKEN_CACHE_FILE, 'r') as f:
            data = json.load(f)
            _server_tokens_local = data.get("tokens", {})
            _server_token_timestamps_local = data.get("timestamps", {})
    except Exception as e:
        logger.error(f"Failed to load token cache: {e}")


def _save_token_cache():
    """Save tokens to disk. Thread-safe but NOT process-safe — caller must hold file lock."""
    try:
        with open(TOKEN_CACHE_FILE, 'w') as f:
            json.dump({
                "tokens": _server_tokens_local,
                "timestamps": _server_token_timestamps_local
            }, f)
    except Exception as e:
        logger.error(f"Failed to save token cache: {e}")


def _register_child_process(proc):
    if not proc:
        return
    with _ACTIVE_CHILD_LOCK:
        _ACTIVE_CHILD_PROCESSES.add(proc)


def _unregister_child_process(proc):
    if not proc:
        return
    with _ACTIVE_CHILD_LOCK:
        _ACTIVE_CHILD_PROCESSES.discard(proc)


def _shutdown_child_processes(timeout=3):
    with _ACTIVE_CHILD_LOCK:
        processes = list(_ACTIVE_CHILD_PROCESSES)

    if not processes:
        return

    logger.warning(f"Stopping {len(processes)} child process(es)...")

    for proc in processes:
        try:
            if proc.is_alive():
                logger.warning(f"Sending SIGTERM to child {proc.name} (pid={proc.pid})")
                proc.terminate()
        except Exception:
            pass

    deadline = time.time() + max(timeout, 0)
    for proc in processes:
        remaining = max(0, deadline - time.time())
        try:
            proc.join(timeout=remaining)
        except Exception:
            pass

    for proc in processes:
        try:
            if proc.is_alive():
                logger.warning(f"Sending SIGKILL to child {proc.name} (pid={proc.pid})")
                proc.kill()
                proc.join(timeout=1)
        except Exception:
            pass
        finally:
            _unregister_child_process(proc)


# ================= GRACEFUL SHUTDOWN =================
def handle_sigterm(sig, frame):
    global STOP
    STOP = True
    logger.warning("SIGTERM / SIGINT received — stopping pipeline...")
    
    stop_all_keepalives()
    
    for mod_name in [
        "workers.intruder_worker",
        "workers.close_proximity_worker",
        "workers.entry_exit_worker",
        "workers.invigilator_worker",
        "workers.server_room_worker",
        "workers.phone_worker",
        "workers.crowd_worker",
        "workers.crowd_det_biometric_worker",
        "workers.crowd_zero_det_worker",
        "workers.crowd_five_det_worker",
        "workers.blind_spot_worker",
        "workers.camera_tampering_worker",
        "workers.camera_inversion_worker",
        "workers.strongroom_worker",
        "workers.qp_box_det_worker",
    ]:
        try:
            mod = sys.modules.get(mod_name)
            if mod and hasattr(mod, "_STOP_EVENT"):
                mod._STOP_EVENT.set()
        except Exception:
            pass

    _shutdown_child_processes()

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT,  handle_sigterm)


# ============================================================
# DSS AUTH — FIXED: Pure file-lock based, no mp.Manager()
# ============================================================

def make_api_call(url, payload, headers, timeout=5):
    try:
        response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=timeout)
        if response.status_code in (200, 401):
            return response
        return None
    except Exception:
        return None


def parse_json_response(response, context):
    if response is None:
        return None
    try:
        return json.loads(response.content.decode("utf-8"))
    except Exception:
        return None


def generate_signature(userName, password, realm, randomKey):
    temp = hashlib.md5(password.encode()).hexdigest()
    temp = hashlib.md5((userName + temp).encode()).hexdigest()
    temp = hashlib.md5(temp.encode()).hexdigest()
    temp = hashlib.md5((userName + ":" + realm + ":" + temp).encode()).hexdigest()
    return hashlib.md5((temp + ":" + randomKey).encode()).hexdigest()


def authenticate_server(server_address, max_attempts=2):
    """
    Authenticate with DSS server. 
    CRITICAL: Only call this while holding the file lock!
    """
    auth_url = f"https://{server_address}/admin/API/accounts/authorize"
    logger.info(f"Authenticating with {server_address}...")
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "userName": DSS_USER,
        "ipAddress": "",
        "clientType": "WINPC_V1"
    }
    
    for attempt in range(1, max_attempts + 1):
        # Step 1: Get realm and randomKey
        res = make_api_call(auth_url, payload, headers)
        if res is None:
            time.sleep(0.5)
            continue
        
        resp_json = parse_json_response(res, "initial authorization")
        if resp_json is None:
            time.sleep(0.5)
            continue
        
        code = resp_json.get("code")
        if code not in (None, 1000, 0):
            logger.debug(f"Step 1 failed for {server_address}: code={code}")
            time.sleep(1)
            continue
        
        realm = resp_json.get("realm")
        randomKey = resp_json.get("randomKey")
        publicKey = resp_json.get("publickey")
        cookie = res.headers.get("Set-Cookie", "")
        
        if not all([realm, randomKey, publicKey]):
            time.sleep(0.5)
            continue
        
        # Step 2: Generate signature and get token
        sig = generate_signature(DSS_USER, DSS_PASS, realm, randomKey)
        
        payload2 = {
            "mac": "",
            "signature": sig,
            "userName": DSS_USER,
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
            time.sleep(0.5)
            continue
        
        resp_json2 = parse_json_response(res2, "token exchange")
        if resp_json2 is None:
            time.sleep(0.5)
            continue
        
        code2 = resp_json2.get("code")
        if code2 not in (None, 1000, 0):
            err_desc = resp_json2.get("desc", "Unknown error")
            logger.error(f"DSS auth Step 2 failed for {server_address}: {err_desc} (code: {code2})")

            if code2 == 2002:  # User locked — bail; caller negative-caches as FAILED.
                logger.warning(f"User '{DSS_USER}' is locked on {server_address}.")
                return None

            time.sleep(1)
            continue
        
        token = resp_json2.get("token")
        if token:
            logger.info(f"Authenticated with {server_address}")
            return token
            
    logger.error(f"Authentication failed for {server_address} after {max_attempts} attempts")
    return None


# Sentinel tokens — anything in this set is a negative-cache marker, not a real token.
_NEGATIVE_TOKENS = ("LOCKED", "FAILED", "AUTH_IN_PROGRESS")
# Max time we trust an AUTH_IN_PROGRESS marker before treating it as stale and re-claiming.
AUTH_IN_PROGRESS_TIMEOUT = 45


def _with_file_lock(fn):
    """Acquire TOKEN_LOCK_FILE exclusively, call fn(), release. Returns fn()'s value."""
    os.makedirs(os.path.dirname(TOKEN_LOCK_FILE) or ".", exist_ok=True)
    f_lock = open(TOKEN_LOCK_FILE, "w")
    try:
        fcntl.flock(f_lock, fcntl.LOCK_EX)
        return fn()
    finally:
        try:
            fcntl.flock(f_lock, fcntl.LOCK_UN)
            f_lock.close()
        except Exception:
            pass


def _claim_auth_slot(server_address):
    """
    Under the file lock, decide what to do next for this server:
      ("token", <token>) — cached valid token, use it
      ("skip",  None)    — recent negative or auth-in-progress; back off
      ("auth",  None)    — caller should authenticate (we just claimed the slot)
    Holding time: just a cache read + optional write. No network I/O here.
    """
    def _do():
        _load_token_cache()
        now = time.time()
        token = _server_tokens_local.get(server_address)
        ts    = _server_token_timestamps_local.get(server_address, 0)
        elapsed = now - ts

        if token in _NEGATIVE_TOKENS:
            ttl = AUTH_IN_PROGRESS_TIMEOUT if token == "AUTH_IN_PROGRESS" else LOCKOUT_WAIT_SECONDS
            if elapsed < ttl:
                return ("skip", None)
        elif token and elapsed < TOKEN_EXPIRY_SECONDS:
            return ("token", token)

        # Claim the slot so other workers see AUTH_IN_PROGRESS and skip.
        _server_tokens_local[server_address] = "AUTH_IN_PROGRESS"
        _server_token_timestamps_local[server_address] = now
        _save_token_cache()
        return ("auth", None)

    return _with_file_lock(_do)


def _record_auth_result(server_address, token):
    """Under the file lock, persist the auth outcome (token or FAILED sentinel)."""
    def _do():
        _load_token_cache()
        now = time.time()
        if token:
            _server_tokens_local[server_address] = token
        else:
            # Any non-success is negative-cached as FAILED for LOCKOUT_WAIT_SECONDS,
            # so the storm of workers behind us doesn't each pay the auth-timeout cost.
            _server_tokens_local[server_address] = "FAILED"
        _server_token_timestamps_local[server_address] = now
        _save_token_cache()

    _with_file_lock(_do)


def get_server_token(server_address):
    """
    Token management with claim-and-release file locking.

    The file lock is now held only for cache reads/writes (microseconds);
    the actual auth network round-trip happens OUTSIDE the lock. The first
    worker to find an expired/missing token writes AUTH_IN_PROGRESS into
    the cache, releases the lock, does the auth, and writes the result.
    Other workers seeing AUTH_IN_PROGRESS skip immediately rather than
    serializing behind the auth call.

    Auth timeouts (e.g. DVR offline) are negative-cached as FAILED, so
    subsequent workers skip the server fast instead of each paying the
    full request timeout.
    """
    global _server_tokens_local, _server_token_timestamps_local

    # Fast path: in-memory cache, no lock at all.
    with _local_lock:
        token = _server_tokens_local.get(server_address)
        ts    = _server_token_timestamps_local.get(server_address, 0)
        elapsed = time.time() - ts
        if token in _NEGATIVE_TOKENS:
            ttl = AUTH_IN_PROGRESS_TIMEOUT if token == "AUTH_IN_PROGRESS" else LOCKOUT_WAIT_SECONDS
            if elapsed < ttl:
                return None
        elif token and elapsed < TOKEN_EXPIRY_SECONDS:
            return token

    # Slow path: consult shared cache, possibly claim the auth slot.
    try:
        decision, token = _claim_auth_slot(server_address)
    except Exception as e:
        logger.error(f"Error claiming auth slot for {server_address}: {e}")
        return None

    if decision == "token":
        with _local_lock:
            _server_tokens_local[server_address] = token
            _server_token_timestamps_local[server_address] = time.time()
        return token

    if decision == "skip":
        return None

    # decision == "auth": we own the auth slot. Do the auth OUTSIDE the file lock.
    new_token = None
    try:
        new_token = authenticate_server(server_address)
    except Exception as e:
        logger.error(f"authenticate_server raised for {server_address}: {e}")

    try:
        _record_auth_result(server_address, new_token)
    except Exception as e:
        logger.error(f"Failed to record auth result for {server_address}: {e}")

    if new_token:
        with _local_lock:
            _server_tokens_local[server_address] = new_token
            _server_token_timestamps_local[server_address] = time.time()
        start_keepalive(server_address)

    return new_token


def keepalive_loop(server_address, stop_event, interval=22):
    """Background thread: sends PUT keepalive every ~22s."""
    while not stop_event.wait(interval):
        token = None
        with _local_lock:
            token = _server_tokens_local.get(server_address)
        
        if not token or token == "LOCKED":
            continue
        
        try:
            requests.put(
                f"https://{server_address}/admin/API/accounts/keepalive",
                headers={"Content-Type": "application/json", "X-Subject-Token": token},
                json={},
                timeout=5
            )
            logger.debug(f"Keepalive sent for {server_address}")
        except Exception:
            pass


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
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def extract_host_port(url):
    m = re.match(r'[a-z]+://([^/:]+)(?::(\d+))?', url)
    if not m:
        return None, None
    host = m.group(1)
    port = int(m.group(2)) if m.group(2) else (554 if url.startswith('rtsp') else 443)
    return host, port


def authenticate_and_get_token(server):
    """Backwards compatibility wrapper."""
    return get_server_token(server)


# ============================================================
# RTSP HELPERS
# ============================================================
def add_token_to_url(rtsp_url, token):
    return f"{rtsp_url}?token={token}"


def extract_stream_urls(api_data):
    """
    FIXED: Unified URL extraction for both RTSP and HLS responses.
    Returns list of URLs from 'url' and 'url2' fields.
    """
    urls = []
    for key in ("url", "url2"):
        raw = api_data.get(key)
        if not raw:
            continue
        urls.extend(p.strip() for p in raw.split("|") if p.strip())
    return urls


def get_rtsp_from_dss(device_id, camera_no, token, server):
    """Returns (urls_list, stream_token) or None."""
    try:
        url     = f"https://{server}/admin/API/MTS/Video/StartVideo?token={token}"
        headers = {
            "Content-Type":    "application/json;charset=UTF-8",
            "X-Subject-Token": token
        }
        payload = {
            "clientType": "WINPC_V1",
            "project":    "PSDK",
            "method":     "MTS.Video.StartVideo",
            "data": {
                "streamType":  "1",
                "optional":    f"/admin/API/MTS/Video/StartVideo?token={token}",
                "channelId":   f"{device_id}$1$0${camera_no}",
                "dataType":    "2",
                "enableRtsps": "0"
            }
        }
        
        logger.debug(f"Starting video: {server} - {device_id}_{camera_no}")
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
        
        if r.status_code == 401:
            logger.debug(f"Token expired for {server}, clearing cache")
            with _local_lock:
                _server_tokens_local.pop(server, None)
            return None

        if r.status_code != 200:
            logger.debug(f"StartVideo failed with status {r.status_code}")
            return None

        data = r.json()
        if "data" not in data:
            logger.warning(f"No 'data' in DSS response for {device_id}_{camera_no}: {data}")
            return None

        api_data = data["data"]
        urls = extract_stream_urls(api_data)
        if not urls:
            logger.warning(f"No RTSP URLs for {device_id}_{camera_no}")
            return None

        stream_token = api_data.get("token") or token

        # FIXED: Probe each URL's port and filter unreachable ones immediately
        reachable_urls = []
        for u in urls:
            h, p = extract_host_port(u)
            if h and p:
                if not probe_tcp_port(h, p, timeout=2.0):
                    logger.warning(f"Port {p} on {h} is BLOCKED — filtering out for {device_id}_{camera_no}")
                    continue
            reachable_urls.append(u)
        
        if not reachable_urls:
            logger.warning(f"No reachable URLs for {device_id}_{camera_no}")
            return None

        return reachable_urls, stream_token

    except Exception as e:
        logger.error(f"RTSP fetch error for {device_id}_{camera_no}: {e}")
        return None


def start_video_stream_hls(server_address, device_id, camera_no):
    """
    Request HLS (HTTP) stream URL. 
    Returns (urls_list, stream_token, success_bool).
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
            "dataType": "1",   # 1 = HLS/HTTP
            "enableRtsps": "0"
        }
    }

    try:
        res = make_api_call(url, payload, headers)
        if res is None or res.status_code != 200:
            return None, None, False

        data = res.json().get("data") or {}
        urls = extract_stream_urls(data)

        stream_token = data.get("token") or token
        if urls:
            return urls, stream_token, True
    except Exception as e:
        logger.debug(f"HLS response parse error: {e}")

    return None, None, False


def order_urls(urls):
    """
    FIXED: Properly sort public IPs first, then private IPs.
    Uses is_private_ip_url() to detect ALL private ranges (10.x, 172.16-31.x, 192.168.x).
    """
    public  = [u for u in urls if not is_private_ip_url(u)]
    private = [u for u in urls if is_private_ip_url(u)]
    return public + private


# ============================================================
# FIXED: OpenCV-based Stream Capture (replaces fragile raw ffmpeg pipe)
# ============================================================

# def open_video_capture(stream_url, timeout_ms=10000):
#     """
#     Open a video capture with robust settings.
#     Forces TCP for RTSP and sets reasonable timeouts.
#     """
#     # Ensure TCP transport is set for RTSP
#     if stream_url.startswith("rtsp"):
#         os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
    
#     cap = cv2.VideoCapture(stream_url)
#     cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms)
#     cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, min(timeout_ms, 5000))
    
#     return cap

def open_video_capture(stream_url, timeout_ms=10000):
    """
    Open a video capture with robust settings.
    CRITICAL: Keep URL encoded (%24, %26) as-is. DSS RTSP servers expect encoded format.
    Decoding causes 404 Not Found errors.
    
    To prevent OpenCV from misrouting %24/%26 URLs to CAP_IMAGES backend, we explicitly
    specify CAP_FFMPEG backend (which handles encoded URLs correctly).
    """
    # NVDEC path (Scaling Path A): hardware-decode on the GPU via GStreamer.
    if USE_NVDEC and str(stream_url).startswith("rtsp"):
        try:
            pipeline = GST_PIPELINE_TEMPLATE.format(url=stream_url, latency=200)
            gcap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
            if gcap.isOpened():
                return gcap
            gcap.release()
            logger.warning(f"NVDEC/GStreamer open failed → FFMPEG fallback for {stream_url[:50]}...")
        except Exception as e:
            logger.warning(f"NVDEC/GStreamer error ({e}) → FFMPEG fallback")

    # CRITICAL: Use CAP_FFMPEG explicitly to prevent CAP_IMAGES misrouting
    # Keep URL encoded - DSS servers require %24 for $ and %26 for &
    cap = cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)

    # TCP transport for RTSP (prevents UDP packet loss behind NAT/firewall)
    if stream_url.startswith("rtsp"):
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, min(timeout_ms, 5000))

    return cap


def read_frame_from_capture(cap, label):
    """
    Read a frame with validation.
    Returns (frame, is_valid).
    """
    ret, frame = cap.read()
    if not ret or frame is None:
        return None, False
    
    # Validate frame properties
    if frame.size == 0 or frame.ndim != 3 or frame.shape[2] != 3:
        logger.warning(f"Invalid frame shape from {label}: {frame.shape if frame is not None else 'None'}")
        return None, False
    
    return frame, True


# ============================================================
# RTSP PRODUCER — FIXED: No HLS fallback, OpenCV capture, proper token handling
# ============================================================
def rtsp_producer(cam_configs, queues, pre_fetched_token=None, enabled_scripts=None):
    """
    Reads frames from a DSS/RTSP camera and puts packets into worker queues.
    
    FIXED:
    - Removed broken HLS fallback (HLS cameras should use HLS pipeline)
    - Uses OpenCV VideoCapture instead of raw ffmpeg pipe
    - Handles 2-tuple return from get_rtsp_from_dss correctly
    - Validates frames before queuing
    - 5 FPS instead of 25 FPS to prevent GPU queue overflow
    - Refreshes stream info on each reconnection attempt
    """
    if isinstance(cam_configs, dict):
        cam_configs = [cam_configs]

    first         = cam_configs[0]
    device_id     = first.get("camera_id") or first.get("device_id", "")
    camera_no     = first.get("ch_no")     or first.get("camera_no", "1")
    camera_id_pk  = first.get("camera_id_pk", "")
    exam_id       = first.get("exam_id", "0")
    shift_id      = first.get("shift_id") or os.environ.get("SHIFT_ID", "0")
    server        = first.get("server_ip") or first.get("cctv_url", "")
    
    if "://" in str(server):
        from urllib.parse import urlparse
        server = urlparse(str(server)).netloc

    direct_url    = first.get("cctv_url")  or first.get("stream_url", "")
    label         = f"{device_id}_{camera_no}"
    use_dss       = bool(server and not str(direct_url).startswith("rtsp://"))

    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 3

    while not STOP and consecutive_failures < MAX_CONSECUTIVE_FAILURES:
        stream_urls = []
        stream_token = None
        
        if use_dss:
            # FIXED: Always get a fresh token before connecting. 
            # Pre-fetched token is used as fallback, but may be stale.
            token = pre_fetched_token
            if token:
                logger.debug(f"Using pre-fetched token for {label}")
            else:
                token = get_server_token(server)
            
            if not token:
                logger.error(f"Auth failed (server might be locked) → {label}, retry in 30s")
                time.sleep(30)
                consecutive_failures += 1
                continue

            result = get_rtsp_from_dss(device_id, camera_no, token, server)
            if result is None:
                logger.warning(f"RTSP fetch failed for {label}")
                consecutive_failures += 1
                if consecutive_failures < MAX_CONSECUTIVE_FAILURES:
                    time.sleep(10)
                continue

            # FIXED: get_rtsp_from_dss returns exactly 2 items: (urls, stream_token)
            urls, stream_token = result
            
            ordered = order_urls(urls)
            if not ordered:
                logger.warning(f"Empty URL list after ordering → {label}")
                consecutive_failures += 1
                if consecutive_failures < MAX_CONSECUTIVE_FAILURES:
                    time.sleep(10)
                continue

            stream_urls = [add_token_to_url(url, stream_token) for url in ordered]
        else:
            if not direct_url:
                logger.warning(f"No server_ip or cctv_url for {label}. Skipping permanently.")
                break
            stream_urls = [direct_url]

        # Try each URL until one works
        connected = False
        for url_index, stream_url in enumerate(stream_urls):
            if is_private_ip_url(stream_url):
                logger.debug(f"Skipping private IP URL: {stream_url[:60]}...")
                continue

            # Port already probed in get_rtsp_from_dss, but double-check for direct URLs
            h, p = extract_host_port(stream_url)
            if h and p and not str(stream_url).startswith("rtsp://"):
                if not probe_tcp_port(h, p, timeout=2.0):
                    logger.warning(f"Port {p} on {h} is BLOCKED — skipping for {label}")
                    continue

            logger.info(f"Connecting → {label}  URL #{url_index+1}/{len(stream_urls)}")

            cap = open_video_capture(stream_url)
            if not cap.isOpened():
                logger.warning(f"Failed to open capture for {label} at {stream_url[:60]}...")
                cap.release()
                continue

            # fail_count = 0
            # MAX_FRAME_FAILS = 50
            # last_good_frame_time = time.time()
            # STREAM_DEAD_THRESHOLD = 15 

            # while not STOP:
            #     frame, is_valid = read_frame_from_capture(cap, label)

            #     if not is_valid:
            #         fail_count += 1
            #         if fail_count <= 3 or fail_count == MAX_FRAME_FAILS:
            #             logger.warning(f"Frame fail #{fail_count} → {label}")
            #         if fail_count >= MAX_FRAME_FAILS:
            #             break
            #         time.sleep(0.5)
            #         continue

            #     if not connected:
            #         logger.info(f"Connected successfully → {label}")
            #         connected = True
            #         consecutive_failures = 0  # Reset on successful connection
            fail_count = 0
            MAX_FRAME_FAILS = 50
            last_good_frame_time = time.time()
            STREAM_DEAD_THRESHOLD = 15  # seconds without any good frame

            while not STOP:
                frame, is_valid = read_frame_from_capture(cap, label)

                if not is_valid:
                    fail_count += 1
                    if fail_count <= 3 or fail_count % 10 == 0:
                        logger.warning(f"Frame fail #{fail_count} → {label}")
                    
                    # Only disconnect if we've gone too long without ANY good frame
                    if time.time() - last_good_frame_time > STREAM_DEAD_THRESHOLD:
                        logger.warning(f"Stream dead for {STREAM_DEAD_THRESHOLD}s → {label}")
                        break
                    
                    time.sleep(0.5)
                    continue

                # Reset on every good frame
                last_good_frame_time = time.time()
                fail_count = 0
                
                if not connected:
                    logger.info(f"Connected successfully → {label}")
                    connected = True
                    consecutive_failures = 0
                
                # ... queue frame ...
                
                fail_count = 0
                frame_snapshot = frame.copy()  # Avoid issues if OpenCV reuses buffers
                if enabled_scripts is not None:
                    # UNIFIED path: one queue, dict packet carrying the camera's analytics.
                    packet = {
                        "frame":          frame_snapshot,
                        "device_id":      device_id,
                        "ch_no":          camera_no,
                        "camera_no":      camera_no,
                        "camera_id_pk":   camera_id_pk,
                        "exam_id":        exam_id,
                        "shift_id":       shift_id,
                        "source_type":    "RTSP",   # rtsp_producer only feeds RTSP cameras
                        "enabled_scripts": enabled_scripts,
                    }
                else:
                    # Legacy path: per-script queues, tuple packet.
                    packet = (frame_snapshot, device_id, camera_no, camera_id_pk, exam_id)

                for q in queues:
                    try:
                        q.put(packet, timeout=0.1)
                    except queue_module.Full:
                        # FIXED: Queue full is expected if GPU is behind; drop frame gracefully
                        pass
                del frame_snapshot  # Free memory immediately after queuing
                
                # Sampling rate (Scaling Path A): SAMPLE_FPS, default 3 fps. Cooldowns are
                # 60-360s so higher fps adds no reportable value, only GPU/decode load.
                time.sleep(_FRAME_INTERVAL)

            cap.release()
            
            if connected or STOP:
                break
            
            if url_index < len(stream_urls) - 1:
                logger.info(f"Trying next URL for {label}")

        # If no URL connected, increment failure counter
        if not connected and not STOP:
            consecutive_failures += 1
            logger.warning(
                f"All URLs failed → {label} "
                f"(failure {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})"
            )
            if consecutive_failures < MAX_CONSECUTIVE_FAILURES:
                time.sleep(10)

    # Do not signal shared workers from an individual camera producer.
    # The RTSP pipeline owns the shared worker lifecycle and sends the
    # final sentinel only after every producer has finished.
    logger.info(f"RTSP Producer stopped → {label}")


# ============================================================
# RTSP PIPELINE RUNNER — FIXED: Better concurrency and lifecycle management
# ============================================================
def run_rtsp_pipeline(camera_group, max_concurrent=MAX_CONCURRENT):
    """
    FIXED: 
    - Pre-authenticates per unique server ONCE in the parent process
    - Uses responsive join() loop for clean shutdown
    - Limits concurrent producers to prevent system overload
    - No longer mixes HLS into RTSP pipeline
    """
    
    # 1. Identify unique servers and pre-auth for each
    unique_servers = set()
    for c in camera_group:
        server = c.get("server_ip") or c.get("cctv_url", "")
        if "://" in str(server):
            from urllib.parse import urlparse
            server = urlparse(str(server)).netloc
        if server and not str(c.get("cctv_url", "")).startswith("rtsp://"):
            unique_servers.add(server)
    
    server_tokens_map = {}
    for server in unique_servers:
        if STOP:
            break
        token = get_server_token(server)
        if token:
            server_tokens_map[server] = token
            logger.info(f"Pre-authenticated for server {server}")
        else:
            logger.error(f"Failed to pre-authenticate for server {server} — its cameras may fail")

    # 2. Identify all unique analytics needed
    unique_scripts = set(c.get("script_code") for c in camera_group if c.get("script_code"))

    # 3. Start worker processes and create shared queues
    worker_processes = []
    script_queues = {}      # legacy: per-script queue
    unified_queue  = None   # unified: single shared queue feeding the unified worker

    if UNIFIED_DETECTOR:
        # Scaling Path A: ONE shared YOLO worker for ALL analytics.
        # Big feed buffer so a large RR pool bursting frames doesn't drop them before the GPU.
        unified_queue = mp.Queue(maxsize=int(os.environ.get("UNIFIED_QUEUE_SIZE", "256")))
        p = mp.Process(
            target=_worker_process_entry,
            args=("unified_detector", "unified_worker", unified_queue),
            name="Worker-unified",
            daemon=True
        )
        p.start()
        _register_child_process(p)
        worker_processes.append(p)
        logger.info(f"Started UNIFIED detector worker (pid={p.pid}) for analytics: {sorted(unique_scripts)}")
    else:
        # Legacy: one model-per-analytic worker.
        for script in unique_scripts:
            module_name, fn_name = get_worker_info(script, mode="rtsp")
            if not module_name:
                logger.error(f"Could not find worker info for {script}")
                continue

            q = mp.Queue(maxsize=QUEUE_SIZE * 2)
            script_queues[script] = q

            p = mp.Process(
                target=_worker_process_entry,
                args=(module_name, fn_name, q),
                name=f"Worker-{script}",
                daemon=True
            )
            p.start()
            _register_child_process(p)
            worker_processes.append(p)
            logger.info(f"Started shared worker for: {script} (pid={p.pid})")

        if not script_queues:
            logger.error("No workers could be started — aborting RTSP pipeline. Check worker imports.")
            return

    # 4. Group cameras by label for producers
    grouped = {}
    for c in camera_group:
        label = f"{c['camera_id']}_{c.get('ch_no') or c.get('camera_no', '1')}"
        grouped.setdefault(label, []).append(c)

    logger.info(f"Launching producers for {len(grouped)} camera groups (max_concurrent={max_concurrent})")

    # 5a. ROUND-ROBIN mode (Scaling Path A): rotate all cameras over a bounded pool.
    rr_mode = ROUND_ROBIN and UNIFIED_DETECTOR and unified_queue is not None
    if rr_mode:
        logger.info(f"ROUND-ROBIN: {len(grouped)} cameras over pool of {RR_POOL_SIZE} slots")
        _run_round_robin(grouped, server_tokens_map, unified_queue)

    # 5b. Continuous-producer mode (default): one persistent producer per camera.
    sem = threading.Semaphore(max_concurrent)
    active_producers = []

    def start_producer(label, configs):
        with sem:
            if STOP:
                return
            
            # Get the pre-fetched token for this camera's server
            first = configs[0]
            server = first.get("server_ip") or first.get("cctv_url", "")
            if "://" in str(server):
                from urllib.parse import urlparse
                server = urlparse(str(server)).netloc
            pre_token = server_tokens_map.get(server) if server else None

            if UNIFIED_DETECTOR:
                # One queue; tell the producer which analytics this camera runs.
                enabled_scripts = sorted({cfg.get("script_code") for cfg in configs if cfg.get("script_code")})
                if not enabled_scripts:
                    logger.warning(f"No script_code for camera {label} — skipping")
                    return
                producer_queues = [unified_queue]
                producer_args = (configs, producer_queues, pre_token, enabled_scripts)
            else:
                producer_queues = []
                for cfg in configs:
                    script = cfg.get("script_code")
                    if script in script_queues:
                        producer_queues.append(script_queues[script])

                if not producer_queues:
                    logger.warning(f"No matching script queue for camera {label} — skipping")
                    return
                producer_args = (configs, producer_queues, pre_token)

            proc = mp.Process(
                target=rtsp_producer,
                args=producer_args,
                name=f"Producer-{label}",
                daemon=True
            )
            proc.start()
            _register_child_process(proc)
            active_producers.append(proc)
            
            # FIXED: Responsive join loop — checks STOP every second instead of blocking forever
            while proc.is_alive() and not STOP:
                proc.join(timeout=1.0)
            _unregister_child_process(proc)

    threads = []
    for label, configs in grouped.items():
        if STOP or rr_mode:   # rr_mode already drove all cameras above; skip continuous producers
            break
        t = threading.Thread(target=start_producer, args=(label, configs), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.05)  # Small stagger to avoid thundering herd

    for t in threads:
        t.join()

    # 6. Clean up workers
    logger.info("All producers finished. Stopping workers...")
    if UNIFIED_DETECTOR:
        if unified_queue is not None:
            try:
                unified_queue.put(None)
            except Exception:
                pass
    else:
        for script, q in script_queues.items():
            try:
                q.put(None)
            except Exception:
                pass

    for p in worker_processes:
        if p.is_alive():
            p.join(timeout=10)
            if p.is_alive():
                logger.warning(f"Force terminating worker {p.name}")
                p.terminate()
                p.join(timeout=2)
        _unregister_child_process(p)


# ============================================================
# StartVideo cache + ROUND-ROBIN driver (Scaling Path A)
# ============================================================
_startvideo_cache = {}            # label -> (urls, stream_token, ts)
_startvideo_cache_lock = threading.Lock()


def get_rtsp_cached(label, device_id, camera_no, token, server):
    """get_rtsp_from_dss with an optional in-process TTL cache to avoid a DSS call
    on every round-robin revisit. TTL=0 disables caching (always fetch fresh)."""
    if STARTVIDEO_CACHE_TTL > 0:
        with _startvideo_cache_lock:
            hit = _startvideo_cache.get(label)
        if hit and (time.time() - hit[2]) < STARTVIDEO_CACHE_TTL:
            return hit[0], hit[1]
    result = get_rtsp_from_dss(device_id, camera_no, token, server)
    if result and STARTVIDEO_CACHE_TTL > 0:
        urls, stream_token = result
        with _startvideo_cache_lock:
            _startvideo_cache[label] = (urls, stream_token, time.time())
    return result


def _rr_resolve(first):
    server = first.get("server_ip") or first.get("cctv_url", "")
    if "://" in str(server):
        from urllib.parse import urlparse
        server = urlparse(str(server)).netloc
    return server


# Per-DVR concurrency throttle for round-robin capture. A large RR pool fans out across many
# DVRs, but no single DVR/server may have more than MAX_STREAMS_PER_DVR streams pulled at once
# (mirrors the per-server limit proven in vision_classification_rtsp.py). Keyed by server.
_rr_server_sems: dict = {}
_rr_server_sems_meta = threading.Lock()


def _get_server_sem(server: str) -> threading.Semaphore:
    with _rr_server_sems_meta:
        sem = _rr_server_sems.get(server)
        if sem is None:
            sem = threading.Semaphore(MAX_STREAMS_PER_DVR)
            _rr_server_sems[server] = sem
        return sem


def _rr_capture_once(label, configs, enabled_scripts, server_tokens_map, unified_queue):
    """One round-robin visit: connect, grab a short burst, push packets, disconnect.

    Returns True if the visit actually pulled frames, False if it skipped (DVR slot busy or
    no usable stream) — the caller backs off briefly on False to avoid a hot spin.
    """
    first      = configs[0]
    device_id  = first.get("camera_id") or first.get("device_id", "")
    camera_no  = first.get("ch_no") or first.get("camera_no", "1")
    cam_pk     = first.get("camera_id_pk", "")
    exam_id    = first.get("exam_id", "0")
    shift_id   = first.get("shift_id") or os.environ.get("SHIFT_ID", "0")
    direct_url = first.get("cctv_url") or first.get("stream_url", "")
    server     = _rr_resolve(first)
    use_dss    = bool(server and not str(direct_url).startswith("rtsp://"))

    # Cap concurrent streams per DVR: if this server is already at capacity, skip this visit
    # (the slot rotates to a camera on a less-busy DVR instead of blocking).
    sem_key = server or device_id
    sem = _get_server_sem(sem_key)
    if not sem.acquire(blocking=False):
        return False
    try:
        stream_urls = []
        if use_dss:
            token = server_tokens_map.get(server) or get_server_token(server)
            if not token:
                return False
            result = get_rtsp_cached(label, device_id, camera_no, token, server)
            if not result:
                return False
            urls, stream_token = result
            stream_urls = [add_token_to_url(u, stream_token) for u in order_urls(urls)]
        elif direct_url:
            stream_urls = [direct_url]

        for stream_url in stream_urls:
            if is_private_ip_url(stream_url):
                continue
            cap = open_video_capture(stream_url)
            if not cap.isOpened():
                cap.release()
                continue
            grabbed = 0
            for _ in range(RR_BURST_FRAMES):
                frame, ok = read_frame_from_capture(cap, label)
                if not ok:
                    break
                packet = {
                    "frame":           frame.copy(),
                    "device_id":       device_id,
                    "ch_no":           camera_no,
                    "camera_no":       camera_no,
                    "camera_id_pk":    cam_pk,
                    "exam_id":         exam_id,
                    "shift_id":        shift_id,
                    "enabled_scripts": enabled_scripts,
                }
                try:
                    unified_queue.put(packet, timeout=0.1)
                except queue_module.Full:
                    pass
                grabbed += 1
            cap.release()
            if grabbed:
                return True  # success on this URL; done for this visit
        return False
    finally:
        sem.release()


def _run_round_robin(grouped, server_tokens_map, unified_queue):
    """Bounded pool of capture slots; cameras rotate through a shared queue (fair, no starvation)."""
    import collections
    work = collections.deque(
        (label, configs, sorted({c.get("script_code") for c in configs if c.get("script_code")}))
        for label, configs in grouped.items()
    )
    work_lock = threading.Lock()

    def slot_loop():
        while not STOP:
            with work_lock:
                if not work:
                    return
                item = work[0]
                work.rotate(-1)   # move head to tail → fair rotation
            label, configs, enabled = item
            if not enabled:
                continue
            try:
                got = _rr_capture_once(label, configs, enabled, server_tokens_map, unified_queue)
            except Exception as e:
                logger.error(f"[RR] capture error {label}: {e}")
                got = False
            if not got:
                # DVR slot busy or no usable stream — back off briefly so an over-provisioned
                # pool doesn't hot-spin rotating the deque.
                time.sleep(0.02)

    n_slots = min(RR_POOL_SIZE, max(1, len(work)))
    logger.info(f"[RR] starting {n_slots} capture slots for {len(work)} cameras")
    slots = [threading.Thread(target=slot_loop, daemon=True, name=f"RR-slot-{i}") for i in range(n_slots)]
    for t in slots:
        t.start()
    # Run until STOP; rotation continues for the whole shift.
    while not STOP and any(t.is_alive() for t in slots):
        for t in slots:
            t.join(timeout=1.0)
        with work_lock:
            if not work:
                break


# ============================================================
# HLS PIPELINE RUNNER — unchanged, handles HLS cameras separately
# ============================================================
def run_hls_pipeline(camera_group):
    grouped = {}
    for c in camera_group:
        grouped.setdefault(c.get("script_code", ""), []).append(c)

    threads = []
    for script_code, configs in grouped.items():
        worker_fn = get_worker(script_code, mode="hls")
        if not worker_fn: 
            continue
        t = threading.Thread(target=worker_fn, args=(configs,), daemon=True)
        t.start()
        threads.append(t)

    for t in threads: 
        t.join()


# ============================================================
# MAIN
# ============================================================
def main():
    exam_id   = os.environ.get("EXAM_ID",  "").strip()
    shift_id  = os.environ.get("SHIFT_ID", "").strip()
    link_type = os.environ.get("LINK_TYPE", "").strip().upper()

    if exam_id and shift_id and link_type:
        parser = argparse.ArgumentParser()
        parser.add_argument("--analytics", nargs="+", default=[])
        args, _ = parser.parse_known_args()
        script_codes = args.analytics

        if not script_codes:
            logger.error("--analytics flag missing")
            os._exit(1)

        results = []
        for script_code in script_codes:
            execution = {
                "exam_id":     exam_id,
                "shift_id":    shift_id,
                "link_type":   link_type,
                "script_code": script_code,
            }
            cameras = load_cameras_for_execution(execution)
            if cameras: 
                results.append((execution, cameras))
    else:
        results = load_all_pending_cameras()

    if not results:
        logger.info("No cameras to process.")
        os._exit(0)

    all_rtsp_cameras = []
    all_hls_cameras  = []

    for ex, cams in results:
        lt = ex.get("link_type", "").upper()
        if lt == "RTSP": 
            all_rtsp_cameras.extend(cams)
        elif lt == "HLS": 
            all_hls_cameras.extend(cams)

    pipeline_threads = []
    if all_rtsp_cameras:
        t = threading.Thread(
            target=run_rtsp_pipeline, 
            args=(all_rtsp_cameras, MAX_CONCURRENT), 
            name="RTSP-pipeline"
        )
        t.start()
        pipeline_threads.append(t)

    if all_hls_cameras:
        t = threading.Thread(target=run_hls_pipeline, args=(all_hls_cameras,), name="HLS-pipeline")
        t.start()
        pipeline_threads.append(t)

    for t in pipeline_threads: 
        t.join()
    
    logger.info("MASTER PIPELINE COMPLETED")
    os._exit(0)


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
