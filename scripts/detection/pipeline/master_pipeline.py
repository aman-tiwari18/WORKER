
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

# RF-DETR's deformable attention makes torch JIT-compile a kernel with NVRTC, which
# dlopen()s libnvrtc-builtins.so.13.0 by bare name. The pip wheel ships it in
# site-packages/nvidia/cu13/lib, which is not on the loader path, so the monitor_tamper
# model failed with "failed to open libnvrtc-builtins.so.13.0". The loader reads
# LD_LIBRARY_PATH only at exec, so set it here: the worker processes spawned below
# inherit it.
try:
    import nvidia as _nvidia_pkg
    _cu13_lib = os.path.join(list(_nvidia_pkg.__path__)[0], "cu13", "lib")
    if os.path.isdir(_cu13_lib) and _cu13_lib not in os.environ.get("LD_LIBRARY_PATH", "").split(":"):
        os.environ["LD_LIBRARY_PATH"] = ":".join(filter(None, [_cu13_lib, os.environ.get("LD_LIBRARY_PATH", "")]))
except Exception:
    pass

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


def _worker_process_entry(module_name, fn_name, queue, *extra):
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
    fn(queue, *extra)


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
        "furniture":        ("improper_furniture_worker",  "improper_furniture_worker",  "improper_furniture_worker_hls"),
        "crowd_five_invigilator": ("crowd_five_invigilator_worker", "crowd_five_invigilator_worker", "crowd_five_invigilator_worker_hls"),
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
        "furniture":        ("improper_furniture_worker",  "improper_furniture_worker",  "improper_furniture_worker_hls"),
        "crowd_five_invigilator": ("crowd_five_invigilator_worker", "crowd_five_invigilator_worker", "crowd_five_invigilator_worker_hls"),
    }

    if analytic_name not in mapping:
        logger.error(f"Unknown analytic requested: {analytic_name}")
        return None, None

    module_name, rtsp_fn, hls_fn = mapping[analytic_name]
    fn_name = rtsp_fn if mode.lower() == "rtsp" else hls_fn
    return module_name, fn_name


# ================= CAMERA LOADER (DB-driven) =================
from utils.camera_loader import load_all_pending_cameras, load_cameras_for_execution, _camera_matches_script
from utils.db_client import rtsp_server_tag


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

# Analytics that need a CONTINUOUS feed, not round-robin bursts: monitor_tamper follows a
# monitor being carried and counts empty frames over seconds, which a 2-frame visit every
# minute or two can never satisfy. Their cameras get a stream held open for the whole run
# (SAMPLE_FPS frames/s); every other camera stays on round-robin.
CONTINUOUS_SCRIPTS = {s.strip() for s in os.environ.get("CONTINUOUS_SCRIPTS", "monitor_tamper").split(",") if s.strip()}

# ── Shift session (branch new_architectured_stream, docs/SHARED_STREAM_SESSION_PLAN.md) ──
# SHIFT_SESSION=1: one run per (exam, shift, day) holds ONE kept-open stream per camera for the
# whole shift; every analytic reads the frames it needs from it, inside its own time window and
# at its own pace. The analytics, their windows, frequencies and camera types come from
# SESSION_FILE (written by the poller, re-read every SESSION_RELOAD_S, so analytics that start
# later in the shift join the running session without a restart). SHIFT_SESSION=0 = old path.
SHIFT_SESSION          = os.environ.get("SHIFT_SESSION", "0") == "1"

# 13 Oct: every kept-open camera is its own process and the parent holds pipes / sockets for each;
# the container's soft open-files limit is 1024 (hard 524288), so raise it for ~800 cameras.
try:
    import resource
    _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(max(_soft, 65536), _hard), _hard))
except Exception:
    pass
SESSION_FILE           = os.environ.get("SESSION_FILE", "").strip()
SESSION_MAX_STREAMS    = int(os.environ.get("SESSION_MAX_STREAMS", "800"))
SESSION_RELOAD_S       = float(os.environ.get("SESSION_RELOAD_S", "10"))
# How often an analytic looks at one camera: see _session_interval. Cameras are spread evenly over
# each interval, so at any moment only a slice of them hands a frame on.
SESSION_MIN_INTERVAL_S = float(os.environ.get("SESSION_MIN_INTERVAL_S", "10"))
SESSION_MAX_INTERVAL_S = float(os.environ.get("SESSION_MAX_INTERVAL_S", "600"))
SESSION_MONITOR_INTERVAL_S = float(os.environ.get("SESSION_MONITOR_INTERVAL_S", "10"))
SESSION_TRACK_INTERVAL_S   = float(os.environ.get("SESSION_TRACK_INTERVAL_S", "60"))
SESSION_TRACKING = {"invigilator", "crowd_five_invigilator"}   # follow a camera for minutes; never held
# A positive frame asks for the confirming frame this soon (the detector alerts on 2 hits in a row).
SESSION_CONFIRM_S = float(os.environ.get("SESSION_CONFIRM_S", "10"))
# Zero Person: after an alert a camera is not looked at again for this long (30 min).
SESSION_ZERO_COOLDOWN_S = int(os.environ.get("SESSION_ZERO_COOLDOWN_S", "1800"))
# Detector processes: person analytics split over this many (by camera), Phone and Monitor one each.
SESSION_PERSON_PROCS = int(os.environ.get("SESSION_PERSON_PROCS", "2"))
SESSION_MONITOR_PROCS = int(os.environ.get("SESSION_MONITOR_PROCS", "2"))
SESSION_QUEUE_SIZE = int(os.environ.get("SESSION_QUEUE_SIZE", "96"))
SESSION_MAX_FRAME_AGE_S = float(os.environ.get("SESSION_MAX_FRAME_AGE_S", "20"))
# Frames narrower than this are enlarged (aspect kept) in the detector, before the models: every
# analytic's pixel limits (Invigilator/Crowd standing height >= 100 px, Zero Person/Proximity box
# area >= 1500 px², ...) were tuned on 1280 px main-stream frames, and substreams are often 352 px.
SESSION_ZOOM_W = int(os.environ.get("SESSION_ZOOM_W", "1280") or 0)
# A camera's stream opens this long before its first window and closes this long after its last.
SESSION_OPEN_LEAD_S  = float(os.environ.get("SESSION_OPEN_LEAD_S", "180"))
SESSION_IDLE_CLOSE_S = float(os.environ.get("SESSION_IDLE_CLOSE_S", "120"))
SESSION_CAMERA_REFRESH_S = float(os.environ.get("SESSION_CAMERA_REFRESH_S", "600"))
# Non-session jobs (HLS): re-read the accessible/classified cameras this often while the job runs, so
# cameras that become accessible after launch are picked up; also how often a job that started with no
# cameras re-checks. 0 = off (the old behaviour: cameras are read once at launch, no cameras = exit).
CAMERA_REFRESH_S = float(os.environ.get("CAMERA_REFRESH_S", "300"))
# Stop opening more streams when the host has less free memory than this (GB).
SESSION_MIN_FREE_MEM_GB = float(os.environ.get("SESSION_MIN_FREE_MEM_GB", "6"))
SESSION_START_STAGGER_S = float(os.environ.get("SESSION_START_STAGGER_S", "0.05"))
# Reconnect after a dropped stream: random delay, so a DVR dropping all its cameras at once does
# not reconnect them all in the same second; repeated failures back off up to the cap.
SESSION_RECONNECT_MIN_S = float(os.environ.get("SESSION_RECONNECT_MIN_S", "5"))
SESSION_RECONNECT_MAX_S = float(os.environ.get("SESSION_RECONNECT_MAX_S", "30"))
SESSION_BACKOFF_CAP_S   = float(os.environ.get("SESSION_BACKOFF_CAP_S", "300"))
# DVR stream: "1" main, "2" substream. Session mode uses the substream for every analytic.
DSS_STREAM_TYPE = os.environ.get("DSS_STREAM_TYPE", "2" if SHIFT_SESSION else "1").strip() or "1"
# Classical-CV analytics with their own worker (fed from the same stream); every other analytic
# goes to the unified detector.
SESSION_SIDE_SCRIPTS = ("tamper", "inversion")
CONTINUOUS_RETRY_SECONDS = float(os.environ.get("CONTINUOUS_RETRY_SECONDS", "15"))

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

# Camview/DSS login, shared with camview_token_server.py; set in script-server/.env.
DSS_USER = os.environ.get("CAMVIEW_AUTH_USER", "system")
DSS_PASS = os.environ.get("CAMVIEW_AUTH_PASS", "")

# Camview DVRs allow ONE active session per username: every new login kills the
# previous token. So all tokens come from the token coordination server on
# worker-0 (camview_token_server.py) — one session per DVR for the whole fleet,
# kept alive there — exactly like vision_classification_rtsp.py. Detection used
# to log in directly from every worker/process (and re-login every 5 min), so
# each login killed the session the others were streaming with.
# Set CAMVIEW_TOKEN_SERVER="" to fall back to direct login (single-host only).
_WORKER_ID = os.environ.get("WORKER_ID", "worker-0")
CAMVIEW_TOKEN_SERVER = os.environ.get(
    "CAMVIEW_TOKEN_SERVER",
    "http://localhost:8090" if _WORKER_ID in ("worker-0", "") else "http://192.168.19.30:8090",
).rstrip("/")
# How long a process reuses a token from the server before asking again. Short, so a
# re-login on the server reaches every worker within seconds (a 401 refreshes at once).
TOKEN_SERVER_LOCAL_TTL = float(os.environ.get("TOKEN_SERVER_LOCAL_TTL", "20"))
# After the server reports a DVR as unavailable (auth failed / locked), don't ask again for this long.
TOKEN_SERVER_FAIL_TTL = float(os.environ.get("TOKEN_SERVER_FAIL_TTL", "30"))

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
        "workers.improper_furniture_worker",
        "workers.crowd_five_invigilator_worker",
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


_ts_tokens = {}        # server -> (token or None, fetched_at)  — token-server mode cache
_ts_locks = {}         # server -> Lock (one in-flight fetch per server per process)
_ts_locks_meta = threading.Lock()
_warn_last = {}        # key -> last warning time (rate-limited logging)


def _warn_rl(key, msg, every=60):
    """logger.warning at most once per `every` seconds per key (the RR loop retries constantly)."""
    now = time.time()
    if now - _warn_last.get(key, 0) >= every:
        _warn_last[key] = now
        logger.warning(msg)


def _ts_lock(server_address):
    with _ts_locks_meta:
        lock = _ts_locks.get(server_address)
        if lock is None:
            lock = _ts_locks[server_address] = threading.Lock()
        return lock


def _get_token_from_server(server_address, force=False):
    """Token for a DVR from the coordination server (cached here TOKEN_SERVER_LOCAL_TTL s).
    None = no usable token right now (DVR locked/unreachable, or server down)."""
    with _ts_lock(server_address):
        tok, ts = _ts_tokens.get(server_address, (None, 0))
        age = time.time() - ts
        if not force:
            if tok and age < TOKEN_SERVER_LOCAL_TTL:
                return tok
            if not tok and ts and age < TOKEN_SERVER_FAIL_TTL:
                return None
        try:
            r = requests.get(f"{CAMVIEW_TOKEN_SERVER}/token",
                             params={"server": server_address, **({"force": "1"} if force else {})},
                             timeout=90)
            tok = r.json().get("token") if r.status_code == 200 else None
            if not tok:
                _warn_rl(f"ts:{server_address}",
                         f"Token server has no token for {server_address} (HTTP {r.status_code}) — DVR auth failed or locked")
        except Exception as e:
            tok = None
            _warn_rl(f"ts:{server_address}", f"Token server {CAMVIEW_TOKEN_SERVER} unreachable for {server_address}: {e}")
        _ts_tokens[server_address] = (tok, time.time())
        return tok


def invalidate_server_token(server_address, token):
    """The DVR rejected `token` (HTTP 401). Drop it everywhere it could be reused, so the
    very next get_server_token() returns the current/new session instead of the dead one.
    Only drops it if it is still the cached token — a newer one is left alone."""
    if not token:
        return
    if CAMVIEW_TOKEN_SERVER:
        with _ts_lock(server_address):
            if _ts_tokens.get(server_address, (None, 0))[0] == token:
                _ts_tokens.pop(server_address, None)
        try:
            # The server evicts only if `token` is still its current one (no re-login storm).
            requests.post(f"{CAMVIEW_TOKEN_SERVER}/evict",
                          json={"server": server_address, "token": token}, timeout=5)
        except Exception:
            pass
        return
    with _local_lock:
        if _server_tokens_local.get(server_address) == token:
            _server_tokens_local.pop(server_address, None)
            _server_token_timestamps_local.pop(server_address, None)

    def _do():  # also the shared disk cache, or other processes keep handing it out
        _load_token_cache()
        if _server_tokens_local.get(server_address) == token:
            _server_tokens_local.pop(server_address, None)
            _server_token_timestamps_local.pop(server_address, None)
            _save_token_cache()
    try:
        _with_file_lock(_do)
    except Exception as e:
        logger.error(f"Failed to invalidate token cache for {server_address}: {e}")


def get_server_token(server_address):
    """Current DSS session token for a DVR server — call it every time a token is needed
    (it is cached; never hold on to a token across visits: sessions get replaced)."""
    if CAMVIEW_TOKEN_SERVER:
        return _get_token_from_server(server_address)
    return _get_server_token_direct(server_address)


def _get_server_token_direct(server_address):
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


def _camera_classes_from_env(script_code):
    """Camera types chosen on this analytic's assignment: JSON list in
    CAMERA_CLASSIFICATIONS_<ANALYTIC> (see script_runner.py). [] = use defaults."""
    raw = os.environ.get(f"CAMERA_CLASSIFICATIONS_{str(script_code).upper()}", "").strip()
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except ValueError:
        val = [raw]
    return [str(v).strip() for v in val if str(v).strip()] if isinstance(val, list) else []


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
                "streamType":  DSS_STREAM_TYPE,
                "optional":    f"/admin/API/MTS/Video/StartVideo?token={token}",
                "channelId":   f"{device_id}$1$0${camera_no}",
                "dataType":    "2",
                "enableRtsps": "0"
            }
        }
        
        logger.debug(f"Starting video: {server} - {device_id}_{camera_no}")
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
        
        if r.status_code == 401:
            _warn_rl(f"401:{server}", f"DSS session token rejected by {server} (401) — refreshing token")
            invalidate_server_token(server, token)
            return None

        if r.status_code != 200:
            _warn_rl(f"sv:{server}:{r.status_code}", f"StartVideo on {server} failed: HTTP {r.status_code}")
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
    # Continuous analytics (CONTINUOUS_SCRIPTS): keep reconnecting for the whole run instead
    # of dropping the camera after 3 failures, and keep the stream drained (see below).
    continuous = bool(set(enabled_scripts or ()) & CONTINUOUS_SCRIPTS)
    if continuous:
        MAX_CONSECUTIVE_FAILURES = 10 ** 9

    while not STOP and consecutive_failures < MAX_CONSECUTIVE_FAILURES:
        stream_urls = []
        stream_token = None
        
        if use_dss:
            # Always the CURRENT session token (cached in get_server_token). Never the
            # pre-fetched one: once the DVR session is replaced it stays dead, and a
            # producer reconnecting with it failed for the rest of the shift.
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

            last_push = 0.0
            while not STOP:
                if continuous:
                    # grab() every frame so we always sample the LIVE picture: reading one
                    # frame then sleeping lets the stream's buffer back up and the feed
                    # falls behind real time. retrieve() only the frames we hand on.
                    if not cap.grab():
                        frame, is_valid = None, False
                    elif time.time() - last_push < _FRAME_INTERVAL:
                        continue
                    else:
                        ok, frame = cap.retrieve()
                        is_valid = bool(ok) and frame is not None and frame.size > 0 and frame.ndim == 3
                        if is_valid:
                            last_push = time.time()
                else:
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
                        "captured_at":     time.time(),
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
                # (Continuous mode paces itself by grabbing, see above.)
                if not continuous:
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
                time.sleep(CONTINUOUS_RETRY_SECONDS if continuous else 10)

    # Do not signal shared workers from an individual camera producer.
    # The RTSP pipeline owns the shared worker lifecycle and sends the
    # final sentinel only after every producer has finished.
    logger.info(f"RTSP Producer stopped → {label}")


# ============================================================
# RTSP PIPELINE RUNNER — FIXED: Better concurrency and lifecycle management
# ============================================================
class _SessionConfig:
    """The session's analytics, re-read from SESSION_FILE when it changes (checked every
    SESSION_RELOAD_S). {script: {"windows": [[start_epoch, end_epoch], ...], "freq": s, "cls": [...]}}"""

    def __init__(self, path):
        self.path, self.mtime, self.data, self.next_check = path, None, {}, 0.0

    def get(self):
        now = time.time()
        if now >= self.next_check:
            self.next_check = now + SESSION_RELOAD_S
            try:
                m = os.path.getmtime(self.path)
                if m != self.mtime:
                    with open(self.path) as f:
                        self.data = (json.load(f) or {}).get("analytics") or {}
                    self.mtime = m
            except Exception as e:
                _warn_rl(f"sessionfile:{self.path}", f"[session] cannot read {self.path}: {e}")
        return self.data


def _session_group(script):
    """Which detector process an analytic's frames go to (one model per process kind)."""
    if script in SESSION_SIDE_SCRIPTS:
        return "side"
    if script == "phone":
        return "phone"
    if script == "monitor_tamper":
        return "monitor"
    return "person"


def _session_script_index():
    """Fixed slot of each analytic in the shared feedback arrays (same order in every process)."""
    return {s: i for i, s in enumerate(sorted(set(UNIFIED_SCRIPTS) | set(SESSION_SIDE_SCRIPTS)))}


def _session_interval(script, freq):
    """How often an analytic looks at one camera. The cameras are spread evenly over this
    interval (see _session_phase), so the load is flat instead of every camera at once.
      - monitor_tamper: SESSION_MONITOR_INTERVAL_S (10 s). Its alerts need the monitor seen in
        several frames while it is carried (held-monitor fallback, carry evidence); the old
        continuous feed gave each camera a frame every ~1.5-10 s (exam 42, 7 Oct).
      - invigilator / crowd_five_invigilator: follow a person for 7 min with a checkpoint at least
        every 180 s (invigilator_tracking.MAX_CHECK_GAP_SEC): freq/3, at most SESSION_TRACK_INTERVAL_S
        (60 s: the script's own checkpoint rate, so one lost frame does not reset the 7-min count;
        every other look shares the frame of the 120 s analytics).
      - every other analytic: once per its alert frequency; a positive frame asks for the
        confirming frame SESSION_CONFIRM_S later (the detector needs 2 hits in a row), so an
        alert still comes once per frequency."""
    if script == "monitor_tamper":
        return max(SESSION_MONITOR_INTERVAL_S, 1.0)
    try:
        f = float(freq) if freq else 120.0
    except (TypeError, ValueError):
        f = 120.0
    if script in SESSION_TRACKING:
        f = min(f / 3.0, SESSION_TRACK_INTERVAL_S)
    return min(max(f, SESSION_MIN_INTERVAL_S), SESSION_MAX_INTERVAL_S)


def _session_cooldown(script, freq):
    """Seconds between saved alerts per camera that the detector applies in a session."""
    try:
        f = int(float(freq))
    except (TypeError, ValueError):
        return None
    if script == "crowd_zero":
        return max(f, SESSION_ZERO_COOLDOWN_S)
    return f


def _session_phase(label):
    """This camera's fixed place (0..1) inside every interval: cameras are spread evenly over
    the interval, and all analytics of one camera land on the same frame when their intervals
    match (one inference serves them all). Stable across restarts."""
    import zlib
    return zlib.crc32(label.encode()) / 2 ** 32


def _grid_next(now, interval, phase, strictly_after):
    """Next time on this camera's grid (phase * interval + k * interval)."""
    import math
    off = phase * interval
    k = (now - off) / interval
    k = math.floor(k) + 1 if strictly_after else math.ceil(k)
    return off + k * interval


def _window_parts(sc, w):
    if isinstance(w, dict):
        return w.get("start"), w.get("end"), w.get("freq"), w.get("cls")
    if isinstance(w, (list, tuple)) and len(w) == 2:     # older format: [start, end] + analytic-wide freq/cls
        return w[0], w[1], sc.get("freq"), sc.get("cls")
    return None, None, None, None


def _session_active(classification, conf, now, lead=0.0):
    """{script: freq} of the analytics with a window open now (or opening within `lead` s)
    whose camera types match this camera. Two windows of one analytic: the shorter freq."""
    active = {}
    for script, sc in conf.items():
        for w in sc.get("windows") or []:
            s0, e0, wf, wc = _window_parts(sc, w)
            if s0 is None or e0 is None or not (s0 - lead <= now <= e0):
                continue
            if not _camera_matches_script(classification, script, wc or None):
                continue
            try:
                f = float(wf) if wf else None
            except (TypeError, ValueError):
                f = None
            if script not in active or (f and (active[script] is None or f < active[script])):
                active[script] = f
    return active


class _CameraSchedule:
    """When each active analytic next looks at one camera (see _session_interval), adjusted by
    the detectors' feedback (confirm / hold) and load factor."""

    def __init__(self, label, cls, conf_src, fb, slot, shard):
        self.label, self.cls, self.conf_src, self.fb, self.shard = label, cls, conf_src, fb, shard
        self.idx = fb["idx"]
        self.base = slot * len(self.idx)
        self.next_due = {}
        self.phase = _session_phase(label)

    def load_factor(self, group):
        li = self.fb["load_i"].get(group)     # {"person": [i...], "phone": [i], "monitor": [i...]}
        return self.fb["load"][li[self.shard % len(li)]] if li else 1.0

    def plan(self, now):
        """({detector group: [scripts]}, {script: cooldown}) to serve with a frame taken now, or None."""
        fb, base, next_due = self.fb, self.base, self.next_due
        active = _session_active(self.cls, self.conf_src.get(), now)
        for s in list(next_due):
            if s not in active:
                del next_due[s]                  # window closed: re-phased if it opens again
        due = []
        for script, freq in active.items():
            interval = _session_interval(script, freq) * self.load_factor(_session_group(script))
            nd = next_due.get(script)
            if nd is None:
                nd = _grid_next(now, interval, self.phase, strictly_after=False)
            i = self.idx.get(script)
            if i is not None:
                c = fb["confirm"][base + i]
                if c and c < nd:
                    nd = c                       # first positive frame: confirm it soon
                    fb["confirm"][base + i] = 0.0
                h = fb["hold"][base + i]
                if h > nd:
                    nd = h                       # alert saved: nothing new before its cooldown ends
            next_due[script] = nd
            if nd <= now:
                due.append((script, freq, interval))
        if not due:
            return None
        out, cooldowns = {}, {}
        for script, freq, interval in due:
            next_due[script] = _grid_next(now, interval, self.phase, strictly_after=True)
            out.setdefault(_session_group(script), []).append(script)
            cd = _session_cooldown(script, freq)
            if cd is not None and script != "monitor_tamper":   # monitor_tamper groups its own incidents
                cooldowns[script] = cd
        return out, cooldowns


def session_producer(cfg, queues, session_file, slot, fb):
    """One camera of a shift session: keep its (sub)stream open while any analytic needs it,
    drain it continuously, and hand a frame on only when an analytic is due for this camera.

    queues = {"person": [q, ...], "phone": [q], "monitor": [q, ...], "side": {script: q}};
    fb = shared arrays the detectors write: "confirm" (take a frame for this analytic at t),
    "hold" (alert saved: nothing before t) and "load" (per detector queue: stretch intervals
    by this factor while the detector is behind)."""
    import random
    device_id    = cfg.get("camera_id") or cfg.get("device_id", "")
    camera_no    = cfg.get("ch_no") or cfg.get("camera_no", "1")
    camera_id_pk = cfg.get("camera_id_pk", "")
    exam_id      = cfg.get("exam_id", "0")
    shift_id     = cfg.get("shift_id") or os.environ.get("SHIFT_ID", "0")
    tc_code      = cfg.get("tc_code") or ""
    cls          = cfg.get("classification") or ""
    pkt_dev      = cfg.get("pkt_dev") or device_id    # device id the detectors see (server-tagged when shared)
    server       = cfg.get("server_ip") or cfg.get("cctv_url", "")
    if "://" in str(server):
        server = urllib.parse.urlparse(str(server)).netloc
    direct_url   = cfg.get("cctv_url") or cfg.get("stream_url", "")
    use_dss      = bool(server and not str(direct_url).startswith("rtsp://"))
    label        = f"{pkt_dev}_{camera_no}"
    conf_src     = _SessionConfig(session_file)
    shard        = slot                    # each detector group takes slot % its number of processes
    failures     = 0

    sched        = _CameraSchedule(label, cls, conf_src, fb, slot, shard)

    def backoff():
        nonlocal failures
        failures += 1
        delay = min(SESSION_RECONNECT_MAX_S * (2 ** min(failures - 1, 6)), SESSION_BACKOFF_CAP_S)
        time.sleep(random.uniform(SESSION_RECONNECT_MIN_S, max(SESSION_RECONNECT_MIN_S, delay)))

    while not STOP:
        if use_dss:
            token = get_server_token(server)
            if not token:
                backoff(); continue
            result = get_rtsp_from_dss(device_id, camera_no, token, server)   # fresh stream link every (re)connect
            if result is None:
                backoff(); continue
            urls, stream_token = result
            stream_urls = [add_token_to_url(u, stream_token) for u in order_urls(urls)]
        else:
            stream_urls = [direct_url] if direct_url else []
        stream_urls = [u for u in stream_urls if not is_private_ip_url(u)]
        if not stream_urls:
            backoff(); continue

        connected = False
        for stream_url in stream_urls:
            cap = open_video_capture(stream_url)
            if not cap.isOpened():
                cap.release()
                continue
            last_good, next_plan = time.time(), 0.0
            while not STOP:
                if not cap.grab():
                    if time.time() - last_good > 15:          # no frame for 15 s: stream is dead
                        break
                    time.sleep(0.2)
                    continue
                last_good = time.time()
                if not connected:
                    connected = True
                    failures = 0
                    logger.info(f"[session] stream open → {label}")
                if last_good < next_plan:
                    continue                                    # keep draining; plan 4x a second
                next_plan = last_good + 0.25
                todo = sched.plan(last_good)
                if not todo:
                    continue
                by_group, cooldowns = todo
                ok, frame = cap.retrieve()
                if not ok or frame is None or frame.ndim != 3 or frame.size == 0:
                    continue
                for group, scripts in by_group.items():
                    if group == "side":
                        for s_ in scripts:
                            q = (queues.get("side") or {}).get(s_)
                            if q is not None:
                                try:
                                    q.put((frame, pkt_dev, camera_no, camera_id_pk, exam_id), timeout=0.05)
                                except queue_module.Full:
                                    pass
                        continue
                    gq = queues.get(group) or []
                    if not gq:
                        continue
                    q = gq[shard % len(gq)]               # a camera always goes to the same process
                    pkt = {"frame": frame, "device_id": pkt_dev, "ch_no": camera_no, "camera_no": camera_no,
                           "camera_id_pk": camera_id_pk, "exam_id": exam_id, "shift_id": shift_id,
                           "tc_code": tc_code, "source_type": "RTSP", "enabled_scripts": scripts,
                           "captured_at": last_good, "slot": slot, "classification": cls}
                    if SESSION_ZOOM_W:
                        pkt["zoom"] = SESSION_ZOOM_W            # enlarged in the detector, not on the queue
                    cds = {s_: cooldowns[s_] for s_ in scripts if s_ in cooldowns}
                    if cds:
                        pkt["cooldowns"] = cds
                    try:
                        q.put(pkt, timeout=0.05)
                    except queue_module.Full:
                        pass                                    # detector busy: next interval
            cap.release()
            if connected or STOP:
                break
        if STOP:
            break
        if connected:
            logger.warning(f"[session] stream dropped → {label}, reconnecting")
            time.sleep(random.uniform(SESSION_RECONNECT_MIN_S, SESSION_RECONNECT_MAX_S))
        else:
            backoff()
    logger.info(f"[session] producer stopped → {label}")


def _host_free_mem_gb():
    try:
        import psutil
        return psutil.virtual_memory().available / 1e9
    except Exception:
        return float("inf")


def run_rtsp_session(exam_id, shift_id):
    """Shift session: a camera's stream is opened shortly before the first analytic window that
    needs it and kept open while any window does; a script that starts later reuses the streams
    already open and only opens the cameras still missing. Closed again SESSION_IDLE_CLOSE_S after
    its last window. Frames go to one detector process per model kind (person: several, split by
    camera; phone; monitor) so a slow model never holds up the others."""
    conf_watch = _SessionConfig(SESSION_FILE)
    pool = _session_camera_pool(exam_id, shift_id)
    if not pool:
        logger.info("[session] no accessible cameras for this exam — nothing to stream")
        return
    pool_at = time.time()
    servers = set()
    for c in pool.values():
        srv = c.get("cctv_url", "")
        if "://" in str(srv):
            srv = urllib.parse.urlparse(str(srv)).netloc
        if srv and not str(c.get("cctv_url", "")).startswith("rtsp://"):
            servers.add(srv)
    for srv in servers:
        if STOP:
            return
        logger.info(f"[session] pre-auth {srv}: {'ok' if get_server_token(srv) else 'FAILED'}")

    # ---- detector processes (started when their first analytic appears) and shared feedback ----
    idx = _session_script_index()
    n_person, n_monitor = max(SESSION_PERSON_PROCS, 1), max(SESSION_MONITOR_PROCS, 1)
    q_names = ([f"person-{k + 1}" for k in range(n_person)] + ["phone"]
               + [f"monitor-{k + 1}" for k in range(n_monitor)])
    qs = {name: mp.Queue(maxsize=SESSION_QUEUE_SIZE) for name in q_names}
    fb = {
        "idx": idx,
        "confirm": mp.Array("d", SESSION_MAX_STREAMS * len(idx), lock=False),
        "hold": mp.Array("d", SESSION_MAX_STREAMS * len(idx), lock=False),
        "load": mp.Array("d", [1.0] * len(q_names), lock=False),
        "load_i": {g: [i for i, n in enumerate(q_names) if n.startswith(g)] for g in ("person", "phone", "monitor")},
    }
    queues = {g: [qs[n] for n in q_names if n.startswith(g)] for g in ("person", "phone", "monitor")}
    queues.update({
              # every side queue exists from the start: producers keep the queues they were started with
              "side": {s: mp.Queue(maxsize=QUEUE_SIZE * 2) for s in SESSION_SIDE_SCRIPTS}})
    detectors, side_workers = {}, {}

    def scripts_of(conf, group):
        return sorted(s for s in conf if _session_group(s) == group and s in UNIFIED_SCRIPTS)

    def ensure_workers(conf):
        for group in ("person", "phone", "monitor"):
            scripts = scripts_of(conf, group)
            if not scripts:
                continue
            names = [n for n in q_names if n.startswith(group)]
            for name in names:
                if name in detectors and detectors[name].is_alive():
                    continue
                if name in detectors:
                    logger.error(f"[session] detector {name} died — restarting")
                    _unregister_child_process(detectors[name])
                opts = {"name": name, "confirm": fb["confirm"], "hold": fb["hold"], "idx": idx,
                        "load": fb["load"], "load_i": q_names.index(name), "qmax": SESSION_QUEUE_SIZE,
                        "confirm_s": SESSION_CONFIRM_S, "max_age_s": SESSION_MAX_FRAME_AGE_S,
                        "no_hold": sorted(SESSION_TRACKING | {"monitor_tamper"}),
                        "no_feedback": ["monitor_tamper"]}
                p = mp.Process(target=_worker_process_entry,
                               args=("unified_detector", "unified_worker", qs[name], scripts, opts),
                               name=f"Worker-{name}", daemon=True)
                p.start(); _register_child_process(p); detectors[name] = p
                logger.info(f"[session] detector {name} started for {scripts}")
        for script in SESSION_SIDE_SCRIPTS:
            if script not in conf or script in side_workers:
                continue
            module_name, fn_name = get_worker_info(script, mode="rtsp")
            if not module_name:
                continue
            q = queues["side"][script]
            sp = mp.Process(target=_worker_process_entry, args=(module_name, fn_name, q),
                            name=f"Worker-{script}", daemon=True)
            sp.start(); _register_child_process(sp); side_workers[script] = (sp, q)

    logger.info(f"[session] exam {exam_id} shift {shift_id}: {len(pool)} accessible cameras; "
                f"stream type {DSS_STREAM_TYPE} ({'substream' if DSS_STREAM_TYPE == '2' else 'main'}), "
                f"zoom {SESSION_ZOOM_W or 'off'} px, {n_person} person + {n_monitor} monitor detector(s), "
                f"monitor every {SESSION_MONITOR_INTERVAL_S:g} s, invigilator every <= {SESSION_TRACK_INTERVAL_S:g} s, "
                f"others once per their frequency (+confirm after {SESSION_CONFIRM_S:g} s), "
                f"open {SESSION_OPEN_LEAD_S:g} s early, close {SESSION_IDLE_CLOSE_S:g} s after the last window")

    producers = {}                     # camera key -> (process, slot)
    free_slots = list(range(SESSION_MAX_STREAMS - 1, -1, -1))
    last_needed = {}
    opened = closed = 0
    capped_warned = mem_warned = False
    last_stats = last_plan = 0.0
    grace = float(os.environ.get("SESSION_END_GRACE_S", "60"))

    closing = []                       # (process, terminated at): reaped without blocking the loop

    def stop_producer(key):
        p, slot = producers.pop(key)
        if p.is_alive():
            p.terminate()
        closing.append((p, time.time()))
        base = slot * len(idx)
        for i in range(len(idx)):
            fb["confirm"][base + i] = 0.0
            fb["hold"][base + i] = 0.0
        free_slots.append(slot)

    while not STOP:
        now = time.time()
        if now - last_plan >= SESSION_RELOAD_S:
            last_plan = now
            conf = conf_watch.get()
            ends = [e for sc in conf.values() for w in (sc.get("windows") or [])
                    for e in [_window_parts(sc, w)[1]] if e]
            if ends and now > max(ends) + grace:
                logger.info("[session] every analytic window has ended — closing the session")
                break
            ensure_workers(conf)
            if now - pool_at >= SESSION_CAMERA_REFRESH_S:
                fresh = _session_camera_pool(exam_id, shift_id)
                if fresh:
                    pool, pool_at = fresh, now
            # cameras some analytic needs now (or within the open lead), most-needed first
            wanted = []
            for key, cam in pool.items():
                n = len(_session_active(cam.get("classification") or "", conf, now, SESSION_OPEN_LEAD_S))
                if n:
                    wanted.append((n, key))
                    last_needed[key] = now
            wanted.sort(reverse=True)
            for key in [k for k, (p, _s) in producers.items() if not p.is_alive()]:
                stop_producer(key)                         # died: reopened below if still wanted
            for key in [k for k in producers if now - last_needed.get(k, 0) > SESSION_IDLE_CLOSE_S]:
                stop_producer(key); closed += 1
            started = 0
            for _n, key in wanted:
                if STOP or key in producers:
                    continue
                if not free_slots:
                    if not capped_warned:
                        logger.warning(f"[session] {len(wanted)} cameras wanted > SESSION_MAX_STREAMS="
                                       f"{SESSION_MAX_STREAMS}: the cameras fewest analytics need wait for a free stream")
                        capped_warned = True
                    break
                if _host_free_mem_gb() < SESSION_MIN_FREE_MEM_GB:
                    if not mem_warned:
                        logger.error(f"[session] free memory below {SESSION_MIN_FREE_MEM_GB:g} GB — "
                                     f"not opening more streams ({len(producers)} open)")
                        mem_warned = True
                    break
                slot = free_slots.pop()
                pp = mp.Process(target=session_producer, args=(pool[key], queues, SESSION_FILE, slot, fb),
                                name=f"Session-{key}", daemon=True)
                pp.start(); _register_child_process(pp); producers[key] = (pp, slot)
                opened += 1; started += 1
                if started % 100 == 0:
                    logger.info(f"[session] {started} new streams started ({len(producers)} open)")
                time.sleep(SESSION_START_STAGGER_S)
            if started:
                logger.info(f"[session] opened {started} new streams, {len(producers)} open")
                _publish_camera_count(len(producers), sorted(conf.keys()))
        for item in list(closing):
            p, t0 = item
            p.join(timeout=0)
            if p.is_alive() and time.time() - t0 > 10:
                p.kill(); p.join(timeout=1)
            if not p.is_alive():
                _unregister_child_process(p); closing.remove(item)
        if time.time() - last_stats >= 60:      # `now` is stale after a long round of stream starts
            last_stats = time.time()
            sizes = []
            for k, name in enumerate(q_names):
                try:
                    qn = qs[name].qsize()
                except Exception:
                    qn = "?"
                sizes.append(f"{name} {qn} (x{fb['load'][k]:.2f})")
            logger.info(f"[session] streams open {len(producers)} (opened {opened}, closed {closed} so far), "
                        f"detector queues: {', '.join(sizes)}, free mem {_host_free_mem_gb():.0f} GB, "
                        f"analytics now {sorted(conf_watch.get().keys())}")
        time.sleep(1.0)

    for key in list(producers):                 # close every stream
        p, _slot = producers[key]
        if p.is_alive():
            p.terminate()
    for key in list(producers):
        stop_producer(key)
    deadline = time.time() + 10
    for p, _t0 in closing:
        p.join(timeout=max(0.1, deadline - time.time()))
        if p.is_alive():
            p.kill()
        _unregister_child_process(p)
    for q in list(qs.values()) + list(queues["side"].values()):
        try:
            q.put(None)
        except Exception:
            pass
    for w in list(detectors.values()) + [p for p, _q in side_workers.values()]:
        w.join(timeout=10)
        if w.is_alive():
            w.terminate()
        _unregister_child_process(w)


def _session_camera_pool(exam_id, shift_id):
    """{camera key: cfg} of every scheduled, accessible RTSP camera of this worker (schedule and
    worker split already applied by the loader). Which of them stream is decided per window."""
    try:
        from utils.db_client import get_rtsp_cameras_accessible
    except ImportError:
        from db_client import get_rtsp_cameras_accessible
    try:
        rows = get_rtsp_cameras_accessible(exam_id) or []
    except Exception as e:
        logger.error(f"[session] camera list failed: {e}")
        return {}
    pool = {}
    # 16 Oct 2026: one device id + camera no can be two different cameras on two camview servers
    # (e.g. 1000103/0 on d15 and on d16). The oldest row (lowest id) keeps the plain key; any other
    # gets the server tag in its id (1000103d16), which every detector, cooldown, monitor-tamper
    # memory, proximity count and image name then uses. The stream still uses the real device id,
    # and report_to_db_rtsp reads the tag to find that camera's own row.
    rows = sorted(rows, key=lambda r: int(r.get("id") or 0))
    for r in rows:
        dev = str(r.get("device_id") or "").strip()
        if not dev or not r.get("cctv_url"):
            continue
        cam_no = str(r.get("camera_no") or "").strip()
        cam_no = cam_no if cam_no.lower() not in ("", "none", "null") else "1"
        pkt_dev = dev
        key = f"{dev}_{cam_no}"
        tag = rtsp_server_tag(r["cctv_url"])
        if key in pool:
            if tag == pool[key]["server_tag"]:
                continue                     # same camera listed twice on one server: a true duplicate
            pkt_dev = dev + tag
            key = f"{pkt_dev}_{cam_no}"
            if not tag or key in pool:
                continue
        pool[key] = {
            "server_tag": tag,
            "pkt_dev": pkt_dev,
            "cctv_url": r["cctv_url"], "camera_id": dev, "camera_id_pk": str(r.get("id") or ""),
            "camera_no": cam_no, "ch_no": cam_no, "classification": str(r.get("classification") or ""),
            "link_type": "RTSP", "camera_name": r.get("camera_name"), "centre_name": r.get("centre_name"),
            "tc_code": r.get("tc_code"), "exam_id": exam_id, "shift_id": shift_id,
        }
    return pool


def run_rtsp_pipeline(camera_group, max_concurrent=MAX_CONCURRENT, unified=None):
    """
    FIXED: 
    - Pre-authenticates per unique server ONCE in the parent process
    - Uses responsive join() loop for clean shutdown
    - Limits concurrent producers to prevent system overload
    - No longer mixes HLS into RTSP pipeline

    `unified` defaults to UNIFIED_DETECTOR. In unified mode the analytics the shared
    detector serves (UNIFIED_SCRIPTS) go to the unified worker; the rest (tamper /
    inversion — classical CV, no model boxes) get their own per-script worker, fed
    from the SAME capture (round-robin visit or producer), so they add no extra DVR
    streams. Before this they were sent to the unified worker, which has no
    evaluator for them, so they silently never ran on RTSP.
    """
    if unified is None:
        unified = UNIFIED_DETECTOR
    
    # 1. Identify unique servers and pre-auth for each
    unique_servers = set()
    for c in camera_group:
        server = c.get("server_ip") or c.get("cctv_url", "")
        if "://" in str(server):
            from urllib.parse import urlparse
            server = urlparse(str(server)).netloc
        if server and not str(c.get("cctv_url", "")).startswith("rtsp://"):
            unique_servers.add(server)
    
    # Warm-up / reachability check only: producers and round-robin visits call
    # get_server_token() themselves each time, so a session replaced mid-shift is
    # picked up — tokens in this map are NOT reused for streaming.
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

    unified_scripts = (unique_scripts & UNIFIED_SCRIPTS) if unified else set()
    per_script = unique_scripts - unified_scripts   # all of them in legacy mode

    if unified_scripts:
        # Scaling Path A: ONE shared worker for every model-based analytic.
        # Big feed buffer so a large RR pool bursting frames doesn't drop them before the GPU.
        unified_queue = mp.Queue(maxsize=int(os.environ.get("UNIFIED_QUEUE_SIZE", "256")))
        p = mp.Process(
            target=_worker_process_entry,
            args=("unified_detector", "unified_worker", unified_queue, sorted(unified_scripts)),
            name="Worker-unified",
            daemon=True
        )
        p.start()
        _register_child_process(p)
        worker_processes.append(p)
        logger.info(f"Started UNIFIED detector worker (pid={p.pid}) for analytics: {sorted(unified_scripts)}")

    if per_script:
        # One worker per analytic: every analytic in legacy mode; in unified mode only the
        # classical-CV ones (tamper / inversion), fed from the shared capture below.
        for script in per_script:
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

    if unified_queue is None and not script_queues:
        logger.error("No workers could be started — aborting RTSP pipeline. Check worker imports.")
        return

    # 4. Group cameras by label for producers
    grouped = {}
    for c in camera_group:
        label = f"{c['camera_id']}_{c.get('ch_no') or c.get('camera_no', '1')}"
        grouped.setdefault(label, []).append(c)

    logger.info(f"Launching producers for {len(grouped)} camera groups (max_concurrent={max_concurrent})")
    _per_r = {}
    for _c in camera_group:
        _per_r.setdefault(_c.get("script_code"), set()).add(f"{_c['camera_id']}_{_c.get('ch_no') or _c.get('camera_no', '1')}")
    _publish_camera_count(len(grouped), {k for k in _per_r if k}, per_analytic={k: len(v) for k, v in _per_r.items() if k})

    # 5a. ROUND-ROBIN mode (Scaling Path A): rotate cameras over a bounded pool — except
    # cameras running a CONTINUOUS_SCRIPTS analytic (monitor_tamper), which get a
    # persistent producer (5b) so that analytic sees a steady feed.
    rr_mode = ROUND_ROBIN and unified
    rr_grouped, continuous_grouped = dict(grouped), {}
    if rr_mode:
        for label, configs in grouped.items():
            if {c.get("script_code") for c in configs} & CONTINUOUS_SCRIPTS:
                continuous_grouped[label] = rr_grouped.pop(label)
    rr_thread = None
    if rr_mode and rr_grouped:
        logger.info(f"ROUND-ROBIN: {len(rr_grouped)} cameras over pool of {RR_POOL_SIZE} slots")
        rr_thread = threading.Thread(target=_run_round_robin,
                                     args=(rr_grouped, server_tokens_map, unified_queue, script_queues),
                                     name="RR-driver", daemon=True)
        rr_thread.start()
    if continuous_grouped:
        logger.info(f"CONTINUOUS: {len(continuous_grouped)} cameras get a persistent stream "
                    f"({sorted(CONTINUOUS_SCRIPTS)}; {SAMPLE_FPS:g} frames/s each)")

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

            if unified:
                # Unified queue (with the camera's model analytics) + the per-script queues
                # of its classical-CV analytics; all fed from this one capture.
                cam_scripts = {cfg.get("script_code") for cfg in configs if cfg.get("script_code")}
                enabled_scripts = sorted(cam_scripts & UNIFIED_SCRIPTS)
                producer_queues = ([unified_queue] if enabled_scripts else []) + [
                    script_queues[s] for s in sorted(cam_scripts) if s in script_queues]
                if not producer_queues:
                    logger.warning(f"No worker for camera {label} ({sorted(cam_scripts)}) — skipping")
                    return
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
    for label, configs in (continuous_grouped if rr_mode else grouped).items():
        if STOP:
            break
        t = threading.Thread(target=start_producer, args=(label, configs), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.05)  # Small stagger to avoid thundering herd

    for t in threads:
        t.join()
    # Round-robin runs until STOP; wait for it too before stopping the workers.
    if rr_thread is not None:
        while rr_thread.is_alive() and not STOP:
            rr_thread.join(timeout=1.0)

    # 6. Clean up workers
    logger.info("All producers finished. Stopping workers...")
    for q in ([unified_queue] if unified_queue is not None else []) + list(script_queues.values()):
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
        # Only reuse a StartVideo made with the current session token — its stream
        # token dies with the session it came from.
        if hit and hit[3] == token and (time.time() - hit[2]) < STARTVIDEO_CACHE_TTL:
            return hit[0], hit[1]
    result = get_rtsp_from_dss(device_id, camera_no, token, server)
    if result and STARTVIDEO_CACHE_TTL > 0:
        urls, stream_token = result
        with _startvideo_cache_lock:
            _startvideo_cache[label] = (urls, stream_token, time.time(), token)
    return result


def drop_rtsp_cache(label):
    with _startvideo_cache_lock:
        _startvideo_cache.pop(label, None)


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


def _rr_capture_once(label, configs, enabled_scripts, server_tokens_map, unified_queue, side_queues=()):
    """One round-robin visit: connect, grab a short burst, push packets, disconnect.

    Each frame goes to the unified queue (if the camera has model analytics —
    `enabled_scripts`) and to every queue in `side_queues` (the camera's per-script
    classical-CV workers), so one stream open serves all of the camera's analytics.

    Returns True if the visit actually pulled frames, False if it skipped (DVR slot busy or
    no usable stream) — the caller backs off briefly on False to avoid a hot spin.
    """
    first      = configs[0]
    device_id  = first.get("camera_id") or first.get("device_id", "")
    camera_no  = first.get("ch_no") or first.get("camera_no", "1")
    cam_pk     = first.get("camera_id_pk", "")
    exam_id    = first.get("exam_id", "0")
    shift_id   = first.get("shift_id") or os.environ.get("SHIFT_ID", "0")
    tc_code    = str(first.get("tc_code") or first.get("TC_CODE") or "")
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
            # Current token on every visit (was: the token pre-fetched at pipeline
            # start, reused for the whole shift → every visit failed once it died).
            token = get_server_token(server)
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
                    "tc_code":         tc_code,
                    "enabled_scripts": enabled_scripts,
                    "captured_at":     time.time(),
                }
                for q in ([unified_queue] if enabled_scripts else []) + list(side_queues):
                    try:
                        q.put(packet, timeout=0.1)
                    except queue_module.Full:
                        pass
                grabbed += 1
            cap.release()
            if grabbed:
                return True  # success on this URL; done for this visit
        if use_dss:
            drop_rtsp_cache(label)  # cached stream URLs didn't open — StartVideo afresh next visit
        return False
    finally:
        sem.release()


def _run_round_robin(grouped, server_tokens_map, unified_queue, script_queues=None):
    """Bounded pool of capture slots; cameras rotate through a shared queue (fair, no starvation).
    `script_queues` = per-script worker queues (tamper / inversion) fed from the same visits."""
    import collections
    script_queues = script_queues or {}
    work = collections.deque()
    for label, configs in grouped.items():
        cam_scripts = {c.get("script_code") for c in configs if c.get("script_code")}
        enabled = sorted(cam_scripts & UNIFIED_SCRIPTS) if unified_queue is not None else []
        side = [script_queues[s] for s in sorted(cam_scripts) if s in script_queues]
        work.append((label, configs, enabled, side))
    work_lock = threading.Lock()

    def slot_loop():
        while not STOP:
            with work_lock:
                if not work:
                    return
                item = work[0]
                work.rotate(-1)   # move head to tail → fair rotation
            label, configs, enabled, side = item
            if not enabled and not side:
                continue
            try:
                got = _rr_capture_once(label, configs, enabled, server_tokens_map, unified_queue, side)
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
def _publish_camera_count(n_cameras, analytics, per_analytic=None, unified=None):
    """Record how many cameras this job runs on, for the GPU Monitor page.

    One JSON file per pid under /tmp/detection_camera_counts/ that the worker
    API (app/gpu_monitor.py) reads; entries are only looked up for live pids.
    `per_analytic` = {script: cameras}; `unified` = shared-capture stats
    {pairs, distinct}. Repeated calls merge (later fields win).
    """
    import json as _json
    # One file per pid: jobs start simultaneously, and a shared read-modify-write file lost entries.
    d = "/tmp/detection_camera_counts"
    try:
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{os.getpid()}.json")
        try:
            with open(path) as f:
                rec = _json.load(f)
        except Exception:
            rec = {}
        rec.update({"cameras": int(n_cameras), "analytics": sorted(analytics)})
        if per_analytic is not None:
            rec["per_analytic"] = {k: int(v) for k, v in per_analytic.items()}
        if unified is not None:
            rec["unified"] = unified
        with open(path + ".tmp", "w") as f:
            _json.dump(rec, f)
        os.replace(path + ".tmp", path)
    except Exception as exc:
        logger.debug(f"camera-count publish failed: {exc}")
    logger.info("[GPU-MONITOR] %d cameras running analytics: %s", n_cameras, ", ".join(sorted(analytics)))


# HLS PIPELINE RUNNER — unchanged, handles HLS cameras separately
# ============================================================
# ============================================================
# HLS UNIFIED CAPTURE (Scaling Path A for HLS)
# ============================================================
# The per-script HLS model opened the SAME camera once per analytic and each
# worker ran its own YOLO pass: with 7 unified-capable analytics over exam 35
# shift 2 that was 6917 stream opens for 1592 distinct cameras. This mirrors
# what the RTSP path already does — one capture, one shared inference, fanned
# out to every analytic enabled for that camera (see unified_detector.py, whose
# _save_and_report already speaks HLS).
#
# Set UNIFIED_HLS=0 for an instant rollback to the per-script path below.
# UNIFIED_SCRIPTS (below) is also what run_rtsp_pipeline uses to split RTSP.
UNIFIED_HLS = os.environ.get("UNIFIED_HLS", "1") == "1"

# Analytics the shared detector can serve. MUST stay in sync with
# unified_detector.ANALYTIC_SPEC — listed literally rather than imported so the
# parent process doesn't pull in torch/ultralytics just to read the keys.
# 'tamper' and 'inversion' are classical-CV checks that consume no YOLO boxes,
# so they deliberately stay on their own per-script capture path.
# Classical-CV analytics (no model boxes) that, on HLS, run on their queue worker
# fed from the unified capture — one stream open per camera for everything.
HLS_SIDE_SCRIPTS = {"tamper", "inversion"}

UNIFIED_SCRIPTS = {
    "crowd", "crowd_zero", "crowd_five", "proximity", "intruder", "entry_exit",
    "server", "phone", "qp_box", "strongroom", "invigilator", "blind_spot",
    "crowd_biometric", "furniture", "crowd_five_invigilator",
    # Unified-only: no per-script worker, so it doesn't run with
    # UNIFIED_DETECTOR=0 / UNIFIED_HLS=0.
    "monitor_tamper",
}

# A visit opens the stream, grabs a short burst and releases. RTSP bursts 2
# frames in ~1s; an HLS open costs 3-10s, so the burst is bigger to amortise it
# — but it is still a burst, not the 12-30s dwell the per-script workers used.
# >=2 frames matters: unified_detector._passed_gate needs CONSEC_THRESHOLD=2
# consecutive hits, and a burst supplies them inside a single visit.
HLS_UNIFIED_BURST_FRAMES = int(os.environ.get("HLS_UNIFIED_BURST_FRAMES", "4"))
HLS_UNIFIED_SAMPLE_EVERY = int(os.environ.get("HLS_UNIFIED_SAMPLE_EVERY", "5"))
# Resolving the .m3u8 costs a ViewCameraFeedServlet round-trip; cache it per
# camera across visits (0 disables). Mirrors the RTSP StartVideo cache.
HLS_URL_CACHE_TTL = float(os.environ.get("HLS_URL_CACHE_TTL", "180"))

# Frames dropped because the unified queue was full. A full queue means the GPU
# is behind the producers and detections are being silently lost, so it is
# counted and surfaced rather than swallowed by the `break` below.
_hls_unified_stats = {"pushed": 0, "dropped": 0, "streams_live": 0, "reconnects": 0}
_hls_unified_stats_lock = threading.Lock()


# PERSISTENT mode: hold the stream open for the whole shift instead of
# re-opening it every visit. An HLS open costs 3-10s while the frame grab costs
# ~1s, so round-robin spends most of its time re-paying the expensive part —
# 1592 opens per sweep, against an origin that collapses when opens arrive
# together. The legacy path already held streams open and the origin was happy
# with ~128 of them all morning; its 16-camera limit was HLS_RR_POOL_SIZE's
# default, not an origin limit. Holding streams removes the revisit interval
# entirely: every camera is watched continuously.
#
# The cost moves to CPU. cap.read() decodes every frame, and at ~25fps a few
# hundred streams would bury 24 cores, so we grab() (demux, no colour convert /
# copy) and only retrieve() the frames we actually sample.
HLS_PERSIST_PUSH_INTERVAL = float(os.environ.get("HLS_PERSIST_PUSH_INTERVAL", "3.0"))
HLS_PERSIST_RECONNECT_BACKOFF = float(os.environ.get("HLS_PERSIST_RECONNECT_BACKOFF", "10.0"))


def _hls_push(packet, cfg, unified_queue, side_queues, timeout):
    """Send one captured HLS frame to the unified worker (if the camera has model
    analytics) and to the per-script worker of each of its classical-CV analytics
    (tamper / inversion). Returns True if any worker took it."""
    delivered = False
    if cfg.get("enabled_scripts") and unified_queue is not None:
        try:
            unified_queue.put(packet, timeout=timeout)
            delivered = True
        except queue_module.Full:
            with _hls_unified_stats_lock:
                _hls_unified_stats["dropped"] += 1
    for script in cfg.get("side_scripts") or ():
        q = (side_queues or {}).get(script)
        if q is None:
            continue
        try:
            q.put(packet, timeout=timeout)
            delivered = True
        except queue_module.Full:
            pass
    return delivered


def _hls_unified_persistent(cfg, stop_event, unified_queue, side_queues=None, state=None):
    """Hold one HLS stream open for the whole shift, pushing a sampled frame
    every HLS_PERSIST_PUSH_INTERVAL seconds into the unified queue (and the
    camera's tamper / inversion queues)."""
    from tcs_hls_utils import extract_hls_streaming_url, test_hls_stream, open_hls_stream

    ch_no    = cfg.get("ch_no") or cfg.get("camera_no") or cfg.get("camera_id")
    exam_id  = cfg.get("exam_id", "0")
    shift_id = cfg.get("shift_id") or os.environ.get("SHIFT_ID", "0")
    tc_code  = str(cfg.get("tc_code") or cfg.get("TC_CODE") or "")
    scripts  = cfg.get("enabled_scripts") or []
    initial  = cfg.get("cctv_url") or cfg.get("stream_url") or cfg.get("url", "")
    if not initial or not (scripts or cfg.get("side_scripts")):
        return

    while not stop_event.is_set():
        cap = None
        try:
            hls = extract_hls_streaming_url(initial)
            if not hls or not test_hls_stream(hls):
                stop_event.wait(HLS_PERSIST_RECONNECT_BACKOFF)
                continue
            cap = open_hls_stream(hls)
            if cap is None or not cap.isOpened():
                stop_event.wait(HLS_PERSIST_RECONNECT_BACKOFF)
                continue

            with _hls_unified_stats_lock:
                _hls_unified_stats["streams_live"] += 1
            last_push = 0.0
            try:
                while not stop_event.is_set():
                    # grab() advances the stream without paying for the decode
                    # -> colour convert -> copy that retrieve() does.
                    if not cap.grab():
                        break
                    now = time.time()
                    if now - last_push < HLS_PERSIST_PUSH_INTERVAL:
                        continue
                    ok, frame = cap.retrieve()
                    if not ok or frame is None:
                        break
                    last_push = now
                    if _hls_push({
                        "frame":           frame,
                        "device_id":       cfg.get("camera_id", ""),
                        "ch_no":           ch_no,
                        "camera_no":       ch_no,
                        "exam_id":         exam_id,
                        "shift_id":        shift_id,
                        "tc_code":         tc_code,
                        "source_type":     "HLS",
                        "enabled_scripts": scripts,
                        "captured_at":     now,
                    }, cfg, unified_queue, side_queues, timeout=0.5):
                        with _hls_unified_stats_lock:
                            _hls_unified_stats["pushed"] += 1
            finally:
                with _hls_unified_stats_lock:
                    _hls_unified_stats["streams_live"] -= 1
        except Exception as exc:
            logger.debug("[UnifiedHLS] persistent capture error ch=%s: %s", ch_no, exc)
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
        if not stop_event.is_set():
            with _hls_unified_stats_lock:
                _hls_unified_stats["reconnects"] += 1
            stop_event.wait(HLS_PERSIST_RECONNECT_BACKOFF)


def _hls_unified_capture(cfg, stop_event, unified_queue, side_queues=None, state=None):
    """One HLS round-robin visit: resolve, open, burst frames into the unified
    queue, release. Signature matches hls_scheduler's camera_fn contract
    (``state`` is optional so the legacy non-RR branch can call it too).

    Returns True if frames were actually pushed, False otherwise. The scheduler
    uses that instead of its took<UNPRODUCTIVE_UNDER_S heuristic, which assumes
    a dwelling visit and would score a fast, successful burst as dead."""
    from tcs_hls_utils import extract_hls_streaming_url, test_hls_stream, open_hls_stream

    if state is None:
        state = {}

    ch_no    = cfg.get("ch_no") or cfg.get("camera_no") or cfg.get("camera_id")
    exam_id  = cfg.get("exam_id", "0")
    shift_id = cfg.get("shift_id") or os.environ.get("SHIFT_ID", "0")
    tc_code  = str(cfg.get("tc_code") or cfg.get("TC_CODE") or "")
    scripts  = cfg.get("enabled_scripts") or []
    initial  = cfg.get("cctv_url") or cfg.get("stream_url") or cfg.get("url", "")
    if not initial or not (scripts or cfg.get("side_scripts")):
        return False

    # Reuse the resolved .m3u8 while it is still fresh; re-resolve on a miss or
    # after a failed open (a stale token shows up exactly that way).
    now = time.time()
    hls = state.get("hls_url")
    if not hls or (HLS_URL_CACHE_TTL and now - state.get("hls_ts", 0) > HLS_URL_CACHE_TTL):
        hls = extract_hls_streaming_url(initial)
        if not hls or not test_hls_stream(hls):
            state.pop("hls_url", None)
            return False
        state["hls_url"] = hls
        state["hls_ts"] = now

    cap = open_hls_stream(hls)
    if cap is None or not cap.isOpened():
        if cap is not None:
            cap.release()
        state.pop("hls_url", None)   # force a fresh resolve next visit
        return False

    try:
        pushed = 0
        frames_read = 0
        while pushed < HLS_UNIFIED_BURST_FRAMES and not stop_event.is_set():
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            frames_read += 1
            # Consecutive decoded frames are near-identical; space the burst out
            # so the analytics see genuinely different moments.
            if frames_read % max(1, HLS_UNIFIED_SAMPLE_EVERY) != 0:
                continue
            if not _hls_push({
                "frame":           frame.copy(),
                "device_id":       cfg.get("camera_id", ""),
                "ch_no":           ch_no,
                "camera_no":       ch_no,      # HLS: camera_no IS the ch_code
                "exam_id":         exam_id,
                "shift_id":        shift_id,
                "tc_code":         tc_code,
                "source_type":     "HLS",      # routes to report_to_db_hls
                "enabled_scripts": scripts,
                "captured_at":     time.time(),
            }, cfg, unified_queue, side_queues, timeout=0.5):
                break
            pushed += 1
        if pushed:
            with _hls_unified_stats_lock:
                _hls_unified_stats["pushed"] += pushed
        return pushed > 0
    finally:
        cap.release()


def _cam_key(c):
    return str(c.get("ch_no") or c.get("camera_no") or c.get("camera_id"))


def _hls_collapse(camera_group, served_unified=None, served_side=None):
    """One config per camera carrying every analytic enabled for it: model analytics in
    enabled_scripts, classical-CV ones in side_scripts. served_* limit the analytics to the ones this
    job's workers actually run (a refresh can find a camera newly eligible for an analytic that has no
    worker, and must not hand it frames)."""
    by_camera, order, others = {}, [], []
    for c in camera_group:
        sc = c.get("script_code")
        if sc in UNIFIED_SCRIPTS and (served_unified is None or sc in served_unified):
            bucket_name = "enabled_scripts"
        elif sc in HLS_SIDE_SCRIPTS and (served_side is None or sc in served_side):
            bucket_name = "side_scripts"
        else:
            others.append(c)
            continue
        key = _cam_key(c)
        entry = by_camera.get(key)
        if entry is None:
            entry = dict(c)
            entry["enabled_scripts"], entry["side_scripts"] = [], []
            by_camera[key] = entry
            order.append(key)
        if sc and sc not in entry[bucket_name]:
            entry[bucket_name].append(sc)
    return [by_camera[k] for k in order], others


def _hls_capture_supervisor(configs, reload_fn, stop_event, unified_queue, side_queues,
                            served_unified, served_side, label="UnifiedHLS", interval=None):
    """Runs the HLS capture and, every `interval` seconds, re-reads the job's cameras. A camera that
    is new, or newly eligible for another analytic, restarts ONLY the capture layer with the merged
    camera list; the detector workers, their queues and per-camera state are untouched. Cameras that
    stop being accessible are kept (their stream just fails) so a flap does not churn the streams."""
    from workers.hls_scheduler import run_hls_cameras, HLS_RR_POOL_SIZE, HLS_RR_ENABLED
    interval = CAMERA_REFRESH_S if interval is None else interval

    def sig(cfgs):
        return {_cam_key(c): (tuple(sorted(c["enabled_scripts"])), tuple(sorted(c["side_scripts"]))) for c in cfgs}

    def start(cfgs):
        persistent = (not HLS_RR_ENABLED) or len(cfgs) <= HLS_RR_POOL_SIZE
        fn = _hls_unified_persistent if persistent else _hls_unified_capture
        run_stop = threading.Event()
        t = threading.Thread(target=run_hls_cameras, args=(cfgs, run_stop, fn),
                             kwargs={"label": label, "extra_args": (unified_queue, side_queues)}, daemon=True)
        t.start()
        return run_stop, t

    current = {_cam_key(c): c for c in configs}
    run_stop, t = start(list(current.values())) if current else (None, None)
    while not stop_event.wait(interval):
        try:
            fresh, _ = _hls_collapse(reload_fn(), served_unified, served_side)
        except Exception as e:
            logger.warning("[%s refresh] camera re-read failed: %s", label, e)
            continue
        have, new = sig(list(current.values())), sig(fresh)
        added = [k for k in new if k not in have]
        widened = [k for k in new if k in have
                   and (set(new[k][0]) - set(have[k][0]) or set(new[k][1]) - set(have[k][1]))]
        if not added and not widened:
            continue
        for c in fresh:
            k = _cam_key(c)
            if k in current:                     # union of what it runs and what it is now eligible for
                c["enabled_scripts"] = sorted(set(c["enabled_scripts"]) | set(current[k]["enabled_scripts"]))
                c["side_scripts"] = sorted(set(c["side_scripts"]) | set(current[k]["side_scripts"]))
            current[k] = c
        logger.info("[%s refresh] %d new camera(s), %d with more analytics -> restarting capture on %d cameras",
                    label, len(added), len(widened), len(current))
        if run_stop is not None:
            run_stop.set()
            t.join(timeout=60)
        try:
            _per = {sc: sum(1 for c in current.values() if sc in c["enabled_scripts"]) for sc in sorted(served_unified)}
            _publish_camera_count(len(current), sorted(served_unified), per_analytic=_per)
        except Exception:
            pass
        run_stop, t = start(list(current.values()))
    if run_stop is not None:
        run_stop.set()


def run_hls_pipeline(camera_group, reload_fn=None, job_scripts=None):
    if not (UNIFIED_DETECTOR and UNIFIED_HLS):
        return _run_hls_pipeline_per_script(camera_group)

    # Split: analytics the shared detector serves; classical-CV ones (tamper /
    # inversion) that run on their own queue worker but share the SAME capture;
    # and anything else, which keeps its own per-script HLS path.
    unified_cams, side_cams, other_cams = [], [], []
    for c in camera_group:
        sc = c.get("script_code")
        if sc in UNIFIED_SCRIPTS:
            unified_cams.append(c)
        elif sc in HLS_SIDE_SCRIPTS:
            side_cams.append(c)
        else:
            other_cams.append(c)

    # Collapse to ONE config per camera carrying every analytic enabled for it —
    # model analytics in enabled_scripts, classical-CV ones in side_scripts. One
    # stream open per visit then serves all of them.
    by_camera, order = {}, []
    for c in unified_cams + side_cams:
        key = str(c.get("ch_no") or c.get("camera_no") or c.get("camera_id"))
        entry = by_camera.get(key)
        if entry is None:
            entry = dict(c)
            entry["enabled_scripts"], entry["side_scripts"] = [], []
            by_camera[key] = entry
            order.append(key)
        sc = c.get("script_code")
        bucket = entry["enabled_scripts"] if sc in UNIFIED_SCRIPTS else entry["side_scripts"]
        if sc and sc not in bucket:
            bucket.append(sc)
    unified_configs = [by_camera[k] for k in order]

    threads = []
    worker_queues = []

    if unified_configs:
        all_scripts = sorted({s for c in unified_configs for s in c["enabled_scripts"]})
        side_scripts = sorted({s for c in unified_configs for s in c["side_scripts"]})
        if reload_fn is not None and job_scripts:
            # a refresh can make a camera eligible for an analytic that had none at launch: have its worker up
            all_scripts = sorted(set(all_scripts) | (set(job_scripts) & UNIFIED_SCRIPTS))
            side_scripts = sorted(set(side_scripts) | (set(job_scripts) & HLS_SIDE_SCRIPTS))
        logger.info(
            "HLS UNIFIED: %d analytic-camera pairs -> %d distinct cameras (%.1fx fewer opens); "
            "model analytics: %s; classical-CV on the same capture: %s",
            len(unified_cams) + len(side_cams), len(unified_configs),
            (len(unified_cams) + len(side_cams)) / max(len(unified_configs), 1), all_scripts, side_scripts,
        )

        _per_u = {sc: sum(1 for c in unified_configs if sc in c["enabled_scripts"]) for sc in all_scripts}
        _publish_camera_count(len(unified_configs), all_scripts, per_analytic=_per_u,
                              unified={"pairs": len(unified_cams), "distinct": len(unified_configs)})
        unified_queue = None
        if all_scripts:
            unified_queue = mp.Queue(maxsize=int(os.environ.get("UNIFIED_QUEUE_SIZE", "256")))
            p = mp.Process(
                target=_worker_process_entry,
                args=("unified_detector", "unified_worker", unified_queue, all_scripts),
                name="Worker-unified-hls",
                daemon=True,
            )
            p.start()
            _register_child_process(p)
            worker_queues.append(unified_queue)
            logger.info("Started UNIFIED detector worker (pid=%s) for HLS analytics: %s", p.pid, all_scripts)

        # Tamper / inversion: their queue-consuming worker (the same one RTSP uses),
        # fed from the unified capture above instead of opening their own streams.
        side_queues = {}
        for script in side_scripts:
            module_name, fn_name = get_worker_info(script, mode="rtsp")
            if not module_name:
                logger.error(f"Could not find worker info for {script}")
                continue
            q = mp.Queue(maxsize=int(os.environ.get("UNIFIED_QUEUE_SIZE", "256")))
            p = mp.Process(target=_worker_process_entry, args=(module_name, fn_name, q),
                           name=f"Worker-{script}-hls", daemon=True)
            p.start()
            _register_child_process(p)
            side_queues[script] = q
            worker_queues.append(q)
            logger.info("Started %s worker (pid=%s) on the unified HLS capture", script, p.pid)

        from workers.hls_scheduler import run_hls_cameras, HLS_RR_POOL_SIZE, HLS_RR_ENABLED
        # The scheduler holds streams open when every camera fits the pool. Use
        # the persistent capture there and the burst capture when it rotates.
        persistent = (not HLS_RR_ENABLED) or len(unified_configs) <= HLS_RR_POOL_SIZE
        capture_fn = _hls_unified_persistent if persistent else _hls_unified_capture
        logger.info("HLS UNIFIED capture mode: %s", "PERSISTENT (streams held open)"
                    if persistent else "ROUND-ROBIN (open/burst/release)")
        stop_event = threading.Event()
        if reload_fn is not None and CAMERA_REFRESH_S > 0:
            logger.info("HLS camera refresh: re-reading accessible cameras every %.0fs", CAMERA_REFRESH_S)
            t = threading.Thread(
                target=_hls_capture_supervisor,
                args=(unified_configs, reload_fn, stop_event, unified_queue, side_queues,
                      set(all_scripts), set(side_scripts)),
                daemon=True,
            )
        else:
            t = threading.Thread(
                target=run_hls_cameras,
                args=(unified_configs, stop_event, capture_fn),
                kwargs={"label": "UnifiedHLS", "extra_args": (unified_queue, side_queues)},
                daemon=True,
            )
        t.start()
        threads.append(t)

    # Anything the unified capture doesn't serve keeps its own per-script path.
    grouped = {}
    for c in other_cams:
        grouped.setdefault(c.get("script_code", ""), []).append(c)
    for script_code, configs in grouped.items():
        worker_fn = get_worker(script_code, mode="hls")
        if not worker_fn:
            continue
        t = threading.Thread(target=worker_fn, args=(configs,), daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()
    for q in worker_queues:
        try:
            q.put(None)
        except Exception:
            pass


def _run_hls_pipeline_per_script(camera_group):
    """Legacy HLS path: one worker per analytic, each opening its own streams
    and running its own model. Kept for UNIFIED_HLS=0 rollback."""
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
def _load_group_results(exam_id, shift_id, link_type, script_codes, quiet=False):
    """[(execution, cameras)] for every analytic of this job that has cameras right now."""
    results = []
    for script_code in script_codes:
        execution = {
            "exam_id":     exam_id,
            "shift_id":    shift_id,
            "link_type":   link_type,
            "script_code": script_code,
        }
        # The assignment's "Camera Type" selection for THIS analytic (set by
        # script_runner.py as CAMERA_CLASSIFICATIONS_<ANALYTIC>); none = the
        # analytic's built-in default camera types.
        custom_cls = _camera_classes_from_env(script_code)
        if custom_cls:
            execution["camera_classifications"] = custom_cls
            if not quiet:
                logger.info(f"[CameraLoader] {script_code}: camera types from the assignment = {custom_cls}")
        cameras = load_cameras_for_execution(execution)
        if cameras:
            results.append((execution, cameras))
    return results


def main():
    exam_id   = os.environ.get("EXAM_ID",  "").strip()
    shift_id  = os.environ.get("SHIFT_ID", "").strip()
    link_type = os.environ.get("LINK_TYPE", "").strip().upper()

    if SHIFT_SESSION and SESSION_FILE and exam_id and shift_id and link_type == "RTSP":
        logger.info(f"[session] exam {exam_id} shift {shift_id} (session file {SESSION_FILE})")
        run_rtsp_session(exam_id, shift_id)
        logger.info("MASTER PIPELINE (shift session) COMPLETED")
        os._exit(0)

    if exam_id and shift_id and link_type:
        parser = argparse.ArgumentParser()
        parser.add_argument("--analytics", nargs="+", default=[])
        args, _ = parser.parse_known_args()
        script_codes = args.analytics

        if not script_codes:
            logger.error("--analytics flag missing")
            os._exit(1)

        results = _load_group_results(exam_id, shift_id, link_type, script_codes)
        if not results and CAMERA_REFRESH_S > 0:
            # Nothing eligible yet (classification may not have run, or the streams are not up): stay
            # alive and look again instead of exiting — an exit marks the assignment COMPLETED and the
            # poller never relaunches it. The runner ends the process at the window's end.
            wait_s = min(CAMERA_REFRESH_S, 120.0)
            logger.info(f"No cameras to process yet — re-checking every {wait_s:.0f}s until the window ends")
            while not results and not STOP:
                for _ in range(int(wait_s)):          # 1 s steps so a stop signal is honoured at once
                    if STOP:
                        break
                    time.sleep(1)
                if not STOP:
                    results = _load_group_results(exam_id, shift_id, link_type, script_codes, quiet=True)
            if STOP:
                logger.info("Stopped while waiting for cameras")
                os._exit(0)
            logger.info("Cameras are now available — starting")

        # Tell the GPU Monitor how many cameras this job runs on, whatever capture path it takes.
        _key = lambda c: (c.get("camera_id"), c.get("ch_no") or c.get("camera_no"))
        _distinct = {_key(c) for _, cams in results for c in cams}
        _per = {ex["script_code"]: len({_key(c) for c in cams}) for ex, cams in results}
        if _distinct:
            _publish_camera_count(len(_distinct), script_codes, per_analytic=_per)
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
        _reload = None
        if exam_id and shift_id and link_type and script_codes:
            def _reload():
                cams = []
                for ex, cs in _load_group_results(exam_id, shift_id, link_type, script_codes, quiet=True):
                    if ex.get("link_type", "").upper() == "HLS":
                        cams.extend(cs)
                return cams
        t = threading.Thread(target=run_hls_pipeline, args=(all_hls_cameras,),
                             kwargs={"reload_fn": _reload, "job_scripts": script_codes}, name="HLS-pipeline")
        t.start()
        pipeline_threads.append(t)

    for t in pipeline_threads: 
        t.join()
    
    logger.info("MASTER PIPELINE COMPLETED")
    os._exit(0)


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
