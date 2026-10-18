# ---------------------------------------------------------------------------
# stdlib
# ---------------------------------------------------------------------------
import asyncio
import contextlib
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
from urllib.parse import unquote
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
import time
import urllib3
from tqdm.asyncio import tqdm as atqdm

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
        update_rtsp_status_and_frame_path,
        insert_hls_classification_history,
        insert_rtsp_classification_history,
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
        update_rtsp_status_and_frame_path,
        insert_hls_classification_history,
        insert_rtsp_classification_history,
    )

# ===========================================================================
# Logging
# ===========================================================================
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
LOG_DIR      = os.path.join(PROJECT_ROOT, "scripts/logs/vision")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILENAME = os.path.join(
    LOG_DIR, f"vision_db_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILENAME), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ===========================================================================
# Configuration
# ===========================================================================
_CPU_CORES = multiprocessing.cpu_count()

MAX_WORKERS_NETWORK:      int = int(os.getenv("VISION_MAX_WORKERS_NETWORK",      "16"))  # 16 for RTSP (was 64 — DVR gets overwhelmed)
MAX_WORKERS_VLM:          int = int(os.getenv("VISION_MAX_WORKERS_VLM",          "32"))
MAX_WORKERS_RTSP_CAPTURE: int = int(os.getenv("VISION_MAX_WORKERS_RTSP_CAPTURE", "6"))   # concurrent FFmpeg RTSP streams per worker; DVR stream limit
THREAD_POOL_SIZE:         int = int(os.getenv("VISION_THREAD_POOL",              str(min(_CPU_CORES * 2, 128))))

TIMEOUT_EXTRACT_URL: int = 3
TIMEOUT_TEST_STREAM: int = 3
TIMEOUT_OPENCV_MS:   int = 3_000
TIMEOUT_OPENCV_REAL: int = 6        # hard wall-clock thread join for OpenCV
TIMEOUT_FFMPEG:      int = int(os.getenv("VISION_TIMEOUT_FFMPEG", "10"))  # 10s: enough for RTSP grab, short enough to release DVR slots quickly
TIMEOUT_VLM:         int = int(os.getenv("VISION_TIMEOUT_VLM", "120"))
TIMEOUT_API:         int = int(os.getenv("VISION_TIMEOUT_API",  "10"))

VLLM_URL:       str = os.getenv("VISION_VLLM_URL",             "http://localhost:8001/v1/chat/completions")
VLLM_MODEL:     str = os.getenv("VISION_VLLM_MODEL",           "Qwen/Qwen3-VL-30B-A3B-Instruct")
STOP_VLLM_ON_EXIT: bool = os.getenv("VISION_STOP_VLLM_ON_EXIT", "1") == "1"
# RTSP: classify only cameras whose label is empty or "Uncertain…", and never one a person has
# verified. CLASSIFY_ONLY_UNLABELED=0 classifies every reachable camera again (e.g. after the
# cameras were re-installed) without touching code.
CLASSIFY_ONLY_UNLABELED: bool = os.getenv("CLASSIFY_ONLY_UNLABELED", "1").strip().lower() not in ("0", "false", "no")

DEFAULT_EXAM_ID:   int = 2
DEFAULT_SHIFT_ID:  int = 7
DEFAULT_EXAM_DATE: str = datetime.now().strftime("%Y-%m-%d")

VISION_ASSIGNMENT_PAYLOAD: Optional[str] = os.getenv("VISION_ASSIGNMENT_PAYLOAD")

SAVE_FOLDER         = os.path.join(PROJECT_ROOT, "results/tcs_classification")
# Frames must land on NFS so they persist on the main server AND so Pass-2
# (same worker, same run) can read them back without re-capturing from the DVR.
# The detection results dir (/app/scripts/detection/results) is a symlink to
# /data/nfs/cctv_images on all workers, so basing FRAMES_FOLDER here routes
# every saved JPEG through NFS to the main server automatically.
# Pass-2 reads frame_path with os.path.exists() — NFS sync export guarantees
# Pass-1 writes are durable before Pass-2 reads them.
_SCRIPT_DIR         = os.path.dirname(os.path.abspath(__file__))
FRAMES_FOLDER       = os.path.join(_SCRIPT_DIR, "results", "classification_frames")
CHECKPOINT_FILE     = "checkpoint_progress.json"
CHECKPOINT_INTERVAL = 500
RESUME_FROM_CHECKPOINT = True
SCENE_COLUMN_TARGET = "Scene Classification"

os.makedirs(SAVE_FOLDER, exist_ok=True)
os.makedirs(FRAMES_FOLDER, exist_ok=True)


def _frames_dir_for_assignment(assignment_id=None) -> str:
    """Return per-assignment frame directory, creating it on first call."""
    subdir = f"assignment_{assignment_id}" if assignment_id else "default"
    path = os.path.join(FRAMES_FOLDER, subdir)
    os.makedirs(path, exist_ok=True)
    return path

# One shared thread-pool for all blocking I/O (OpenCV, FFmpeg, HLS extract, Camview auth)
_THREAD_POOL = ThreadPoolExecutor(max_workers=THREAD_POOL_SIZE)

# Initialized per pipeline run (needs a running event loop) — limits concurrent DVR RTSP streams
_RTSP_CAP_SEM: Optional[asyncio.Semaphore] = None

# ===========================================================================
# Camview DVR Authentication (for RTSP cameras)
# ===========================================================================
import hashlib as _hashlib
import socket as _socket

_CAMVIEW_AUTH_USER   = os.getenv("CAMVIEW_AUTH_USER",  "system")
_CAMVIEW_AUTH_PASS   = os.getenv("CAMVIEW_AUTH_PASS",  "Sasd@12345")

# Token coordination server — eliminates cross-VM session collision.
# Camview DVRs allow only ONE active session per username. With 4 workers all
# authenticating as "system" simultaneously, each new auth invalidates the
# previous worker's token. The token server on worker-0 holds a single session
# per DVR server and serves the same token to all workers.
#
# Auto-detected: non-worker-0 workers default to the main server (192.168.19.30).
# Override via env var CAMVIEW_TOKEN_SERVER= (set to "" to disable and auth directly).
_WORKER_ID = os.getenv("WORKER_ID", "worker-0")
# All workers (including worker-0) use the token coordination server to avoid
# cross-VM DVR session collision. Worker-0 uses localhost (it hosts the server);
# workers 1-3 reach it at the main server IP.
_DEFAULT_TOKEN_SERVER = (
    "http://192.168.19.30:8090"
    if _WORKER_ID not in ("worker-0", "")
    else "http://localhost:8090"
)
_CAMVIEW_TOKEN_SERVER = os.getenv("CAMVIEW_TOKEN_SERVER", _DEFAULT_TOKEN_SERVER).rstrip("/")
# Per-server password overrides: CAMVIEW_AUTH_PASS_d15=<password> etc.
_CAMVIEW_PER_SERVER_PASS: dict = {
    k.replace("CAMVIEW_AUTH_PASS_", "").lower() + ".camview.io": v
    for k, v in os.environ.items()
    if k.startswith("CAMVIEW_AUTH_PASS_")
}
_CAMVIEW_TOKEN_TTL  = int(os.getenv("CAMVIEW_TOKEN_TTL", "3600"))
_CAMVIEW_SKIP_PRIV  = os.getenv("CAMVIEW_SKIP_PRIVATE_IPS", "1") == "1"

_camview_tokens:      dict = {}
_camview_timestamps:  dict = {}
# Per-server locks: d14/d15/d16 authenticate in parallel instead of queueing
# behind one global lock (the old single lock was the thundering-herd root cause).
_camview_server_locks:      dict = {}          # server → Lock
_camview_server_locks_meta  = threading.Lock() # guards the locks-dict itself

# ── Disk-based token cache (cross-process safe, ported from master_pipeline.py) ──
import fcntl as _fcntl
_TOKEN_CACHE_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server_tokens.json")
_TOKEN_LOCK_FILE    = _TOKEN_CACHE_FILE + ".lock"
_CAMVIEW_AUTH_BACKOFF    = int(os.getenv("CAMVIEW_AUTH_BACKOFF", "65"))  # seconds before retrying failed auth (>= DVR lockout window ~47s)
_CAMVIEW_KEEPALIVE_SECS  = 22   # send keepalive every 22s (session timeout is ~30s)
_camview_keepalive_threads: dict = {}  # server → (Thread, Event)
_local_cache_lock   = threading.Lock()


def _token_cache_load() -> None:
    """Read token cache from disk. Caller must hold the file lock."""
    global _camview_tokens, _camview_timestamps
    if not os.path.exists(_TOKEN_CACHE_FILE):
        return
    try:
        with open(_TOKEN_CACHE_FILE) as f:
            data = json.load(f)
        _camview_tokens     = data.get("tokens", {})
        _camview_timestamps = {k: float(v) for k, v in data.get("timestamps", {}).items()}
    except Exception as e:
        logger.debug(f"[Camview] Token cache load error: {e}")


def _token_cache_save() -> None:
    """Write token cache to disk. Caller must hold the file lock."""
    try:
        with open(_TOKEN_CACHE_FILE, "w") as f:
            json.dump({"tokens": _camview_tokens, "timestamps": _camview_timestamps}, f)
    except Exception as e:
        logger.debug(f"[Camview] Token cache save error: {e}")


def _get_camview_server_lock(server: str) -> threading.Lock:
    """Return (creating if necessary) the per-server auth lock."""
    with _camview_server_locks_meta:
        if server not in _camview_server_locks:
            _camview_server_locks[server] = threading.Lock()
        return _camview_server_locks[server]


def _camview_signature(user: str, password: str, realm: str, random_key: str) -> str:
    t = _hashlib.md5(password.encode()).hexdigest()
    t = _hashlib.md5((user + t).encode()).hexdigest()
    t = _hashlib.md5(t.encode()).hexdigest()
    t = _hashlib.md5((user + ":" + realm + ":" + t).encode()).hexdigest()
    return _hashlib.md5((t + ":" + random_key).encode()).hexdigest()


def _is_private_ip_url(url: str) -> bool:
    m = re.search(r'://(\d+\.\d+\.\d+\.\d+)', url)
    if not m:
        return False
    p = list(map(int, m.group(1).split('.')))
    return (p[0] == 10 or
            (p[0] == 172 and 16 <= p[1] <= 31) or
            (p[0] == 192 and p[1] == 168))


def _probe_tcp_port(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with _socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _sync_camview_authenticate(server: str) -> Optional[str]:
    """Blocking — 2-step Camview challenge/response auth. Returns token or None.

    Some DVR firmware versions (e.g. d15) reject encryptType=MD5 with code 1101
    ('nonce is illegal') but accept the same MD5-chain signature with encryptType=RSA.
    On 1101 the function gets a fresh challenge and retries with encryptType=RSA.
    """
    import requests as _req
    auth_url = f"https://{server}/admin/API/accounts/authorize"
    headers  = {"Content-Type": "application/json"}
    payload  = {"userName": _CAMVIEW_AUTH_USER, "ipAddress": "", "clientType": "WINPC_V1"}
    # Use per-server password if configured, else global default
    password = _CAMVIEW_PER_SERVER_PASS.get(server.lower(), _CAMVIEW_AUTH_PASS)

    for attempt in range(1, 3):
        try:
            r1 = _req.post(auth_url, headers=headers, data=json.dumps(payload),
                           timeout=TIMEOUT_API, verify=False)
            if r1.status_code not in (200, 401):
                continue
            j1 = r1.json()
            realm      = j1.get("realm")
            random_key = j1.get("randomKey")
            public_key = j1.get("publickey") or j1.get("publicKey")
            cookie     = r1.headers.get("Set-Cookie", "")
            if not all([realm, random_key, public_key]):
                continue

            sig = _camview_signature(_CAMVIEW_AUTH_USER, password, realm, random_key)
            for enc_type in ("MD5", "RSA"):
                p2 = {"mac": "", "signature": sig, "userName": _CAMVIEW_AUTH_USER,
                      "randomKey": random_key, "publicKey": public_key,
                      "encryptType": enc_type, "ipAddress": "", "clientType": "WINPC_V1",
                      "userType": "0"}
                r2 = _req.post(auth_url, headers={"Content-Type": "application/json", "Cookie": cookie},
                               data=json.dumps(p2), timeout=TIMEOUT_API, verify=False)
                j2 = r2.json()
                token = j2.get("token")
                if token:
                    logger.info(f"[Camview] Authenticated: {server}")
                    return token
                # code 1101 = DVR rejects this encryptType — try the other one with a fresh challenge
                if j2.get("code") == 1101 and enc_type == "MD5":
                    logger.debug(f"[Camview] {server} rejected encryptType=MD5 (code 1101), retrying with RSA")
                    # Get a fresh challenge for the RSA retry (session is consumed)
                    try:
                        r1b = _req.post(auth_url, headers=headers, data=json.dumps(payload),
                                        timeout=TIMEOUT_API, verify=False)
                        j1b = r1b.json()
                        if j1b.get("randomKey") and j1b.get("realm"):
                            realm      = j1b["realm"]
                            random_key = j1b["randomKey"]
                            public_key = j1b.get("publickey") or j1b.get("publicKey") or public_key
                            cookie     = r1b.headers.get("Set-Cookie", cookie)
                            sig        = _camview_signature(_CAMVIEW_AUTH_USER, password, realm, random_key)
                    except Exception:
                        pass
                    continue
                break  # non-1101 failure — no point trying RSA

        except Exception as e:
            logger.debug(f"[Camview] Auth attempt {attempt} failed for {server}: {e}")
        time.sleep(attempt)

    logger.warning(f"[Camview] Auth failed: {server}")
    return None


def _get_token_from_server(server: str) -> Optional[str]:
    """Call the token coordination server instead of authenticating directly.
    All workers (including worker-0 at localhost:8090) go through here so that
    only ONE session per DVR server exists at any time.

    No direct-auth fallback: if the coordinator fails, we return None so the
    camera is marked not-accessible. A direct-auth fallback would re-introduce
    cross-session collision (worker's direct auth invalidates the coordinator's
    token and vice versa).
    """
    import requests as _req
    try:
        resp = _req.get(
            f"{_CAMVIEW_TOKEN_SERVER}/token",
            params={"server": server},
            timeout=90,
        )
        if resp.status_code == 200:
            token = resp.json().get("token")
            if token:
                # Cache locally so repeated calls within same batch skip the round-trip
                with _local_cache_lock:
                    _camview_tokens[server]     = token
                    _camview_timestamps[server] = time.time()
                return token
        logger.warning(f"[Camview] Token server returned {resp.status_code} for {server}")
        # 503 = DVR auth failed on coordinator side; signal so callers skip DB overwrite
        return "DVR_UNAVAILABLE" if resp.status_code == 503 else None
    except Exception as e:
        logger.warning(f"[Camview] Token server unreachable for {server}: {e}")
        return None


def _evict_token_on_server(server: str) -> None:
    """Tell the coordination server to evict a stale token (called after 401)."""
    import requests as _req
    if not _CAMVIEW_TOKEN_SERVER:
        return
    try:
        _req.post(
            f"{_CAMVIEW_TOKEN_SERVER}/evict",
            json={"server": server},
            timeout=5,
        )
    except Exception:
        pass


def _sync_camview_get_token(server: str) -> Optional[str]:
    """Returns cached token or authenticates fresh.
    Uses a per-server lock so d14/d15/d16 can authenticate concurrently.
    Also consults the shared disk cache so parallel worker processes share tokens
    instead of hammering the DVR with simultaneous auth requests.

    If CAMVIEW_TOKEN_SERVER is set, delegates to the coordination server on
    worker-0 instead of authenticating directly (fixes cross-VM session collision).
    """
    # ── Token coordination server path (workers 1-3) ─────────────────────────
    if _CAMVIEW_TOKEN_SERVER:
        return _get_token_from_server(server)

    # ── Fast path: check in-memory cache first (no I/O) ──────────────────────
    lock = _get_camview_server_lock(server)
    with _local_cache_lock:
        tok = _camview_tokens.get(server)
        ts  = _camview_timestamps.get(server, 0)
        if tok and tok != "FAILED" and time.time() - ts < _CAMVIEW_TOKEN_TTL:
            return tok
        if tok == "FAILED" and time.time() - ts < _CAMVIEW_AUTH_BACKOFF:
            return None   # negative-cache: don't hammer server

    # ── Slow path: acquire per-server lock, check disk cache, then auth ───────
    with lock:
        # Re-check after acquiring lock (another thread may have authed already)
        with _local_cache_lock:
            tok = _camview_tokens.get(server)
            ts  = _camview_timestamps.get(server, 0)
            if tok and tok != "FAILED" and time.time() - ts < _CAMVIEW_TOKEN_TTL:
                return tok

        # Check disk (written by other worker processes)
        f_lock = None
        try:
            f_lock = open(_TOKEN_LOCK_FILE, "w")
            _fcntl.flock(f_lock, _fcntl.LOCK_EX)
            _token_cache_load()
            tok = _camview_tokens.get(server)
            ts  = _camview_timestamps.get(server, 0)
            # If no keepalive thread is running for this server the DVR session
            # has likely expired (session timeout ~30s after last keepalive).
            # Use a short TTL so we don't hand out a stale token that will 401.
            effective_ttl = (
                _CAMVIEW_TOKEN_TTL
                if server in _camview_keepalive_threads
                else _CAMVIEW_KEEPALIVE_SECS * 2
            )
            if tok and tok != "FAILED" and time.time() - ts < effective_ttl:
                return tok
            # Authenticate — we hold both the per-server lock and the file lock
            token = _sync_camview_authenticate(server)
            now   = time.time()
            with _local_cache_lock:
                _camview_tokens[server]     = token if token else "FAILED"
                _camview_timestamps[server] = now
            _token_cache_save()
            if token:
                _start_camview_keepalive(server)
            return token
        except Exception as e:
            logger.warning(f"[Camview] get_token error for {server}: {e}")
            return None
        finally:
            if f_lock:
                try:
                    _fcntl.flock(f_lock, _fcntl.LOCK_UN)
                    f_lock.close()
                except Exception:
                    pass


def _camview_keepalive_loop(server: str, stop: threading.Event) -> None:
    """Background thread: sends PUT keepalive every ~22s to keep session alive."""
    import requests as _req
    while not stop.wait(_CAMVIEW_KEEPALIVE_SECS):
        with _local_cache_lock:
            tok = _camview_tokens.get(server)
        if not tok or tok == "FAILED":
            continue
        try:
            _req.put(
                f"https://{server}/admin/API/accounts/keepalive",
                headers={"Content-Type": "application/json", "X-Subject-Token": tok},
                json={}, timeout=5, verify=False,
            )
            logger.debug(f"[Camview] Keepalive OK: {server}")
        except Exception:
            pass


def _start_camview_keepalive(server: str) -> None:
    """Start per-server keepalive thread if not already running."""
    if server not in _camview_keepalive_threads:
        stop = threading.Event()
        t = threading.Thread(target=_camview_keepalive_loop, args=(server, stop), daemon=True)
        t.start()
        _camview_keepalive_threads[server] = (t, stop)
        logger.info(f"[Camview] Keepalive thread started: {server}")


def _preauth_camview_servers(cameras: list, force: bool = False) -> None:
    """Pre-authenticate every unique Camview server before Pass-1/Pass-2 begins.
    This warms up all token caches so the 64 concurrent Pass-1 workers get
    cache hits instead of triggering a thundering-herd of simultaneous auths.
    Runs authentications in parallel (one thread per unique server).

    force=True (Pass-2): clears FAILED markers so servers that failed during
    Pass-1 get a fresh auth attempt (lockout window is ~65s, Pass-1 takes
    several minutes, so the lockout should have cleared by Pass-2 time).
    """
    servers = sorted({
        re.sub(r'^https?://', '', str(c.get('cctv_url') or '')).rstrip('/').lower()
        for c in cameras
        if c.get('cctv_url')
        and not str(c.get('cctv_url', '')).lower().startswith('rtsp://')
    })
    if not servers:
        return
    logger.info(f"[Camview] Pre-authenticating {len(servers)} server(s) (force={force}): {servers}")

    if force:
        # Clear FAILED markers so the negative-cache backoff doesn't block Pass-2.
        # By the time Pass-2 runs, the DVR lockout window (≤65s) has elapsed.
        with _local_cache_lock:
            for srv in servers:
                if _camview_tokens.get(srv) == "FAILED":
                    _camview_tokens.pop(srv, None)
                    _camview_timestamps.pop(srv, None)
                    logger.info(f"[Camview] Cleared FAILED cache for {srv} ahead of Pass-2")

    def _auth_one(srv):
        token = _sync_camview_get_token(srv)
        if token:
            logger.info(f"[Camview] Pre-auth OK: {srv}")
        else:
            logger.warning(f"[Camview] Pre-auth FAILED: {srv}")

    threads = [threading.Thread(target=_auth_one, args=(s,), daemon=True) for s in servers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=TIMEOUT_API * 3 + 10)


def _sync_camview_start_video(server: str, device_id: str, camera_no: str) -> Tuple[Optional[List[str]], Optional[str], bool]:
    """
    Blocking — calls Camview StartVideo API, returns (url_list, stream_token, success).
    url_list already filtered (no private IPs if _CAMVIEW_SKIP_PRIV).
    Retries once on 401 (refreshes token) and once on transient network errors.
    """
    import requests as _req
    cam_label = f"{device_id}_{camera_no}@{server}"

    for attempt in range(1, 3):   # up to 2 attempts
        token = _sync_camview_get_token(server)
        if not token or token == "DVR_UNAVAILABLE":
            logger.warning(f"[Camview] StartVideo skipped — no auth token ({cam_label}, attempt {attempt})")
            sentinel = "DVR_UNAVAILABLE" if token == "DVR_UNAVAILABLE" else None
            return None, sentinel, False

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
            res = _req.post(sv_url, headers=headers, data=json.dumps(payload),
                            timeout=TIMEOUT_API, verify=False)

            if res.status_code == 401:
                # Some Camview firmware versions return HTTP 401 but still include
                # the stream data in the body (same pattern as the auth challenge
                # which also returns 401 + data). Check the body before discarding.
                try:
                    body_401 = res.json()
                    data_401 = body_401.get("data") or {}
                    urls_401: List[str] = []
                    for key in ("url", "url2"):
                        raw = data_401.get(key)
                        if raw:
                            urls_401.extend(p.strip() for p in raw.split("|") if p.strip())
                    if urls_401:
                        logger.info(
                            f"[Camview] StartVideo returned 401 but body contains URLs "
                            f"for {cam_label} — using them directly"
                        )
                        stream_token_401 = data_401.get("token") or token
                        if _CAMVIEW_SKIP_PRIV:
                            public_401 = [u for u in urls_401 if not _is_private_ip_url(u)]
                            urls_401 = public_401 if public_401 else urls_401
                        final_401 = [f"{u}?token={stream_token_401}" for u in urls_401]
                        return final_401, stream_token_401, True
                    logger.debug(
                        f"[Camview] StartVideo 401 body (no URLs): {json.dumps(body_401)[:200]}"
                    )
                except Exception:
                    pass
                logger.warning(f"[Camview] StartVideo 401 — refreshing token and retrying ({cam_label}, attempt {attempt})")
                # Tell coordination server the token is stale (if using one)
                _evict_token_on_server(server)
                # Evict the stale stream token and force a fresh auth.
                # Do NOT poison with "FAILED" — that triggers the 5s negative-cache
                # backoff in _sync_camview_get_token, which would block both this
                # retry AND every other concurrent Pass-1 worker on the same server.
                # Hold the per-server lock so concurrent callers wait for the new
                # token instead of stampeding the auth endpoint.
                lock = _get_camview_server_lock(server)
                with lock:
                    with _local_cache_lock:
                        cur_tok = _camview_tokens.get(server)
                        # Only evict if the stale token is still the one cached.
                        # Another thread may have already refreshed it.
                        needs_refresh = (cur_tok == token)
                        if needs_refresh:
                            _camview_tokens.pop(server, None)
                            _camview_timestamps.pop(server, None)
                    
                    if needs_refresh:
                        # When using the coordinator, ask it for a fresh token
                        # (it already had its token evicted via _evict_token_on_server).
                        # Direct auth here would create a competing DVR session.
                        if _CAMVIEW_TOKEN_SERVER:
                            fresh = _get_token_from_server(server)
                        else:
                            fresh = _sync_camview_authenticate(server)
                        now = time.time()
                        if fresh:
                            with _local_cache_lock:
                                _camview_tokens[server]     = fresh
                                _camview_timestamps[server] = now
                            if not _CAMVIEW_TOKEN_SERVER:
                                try:
                                    f_lock = open(_TOKEN_LOCK_FILE, "w")
                                    _fcntl.flock(f_lock, _fcntl.LOCK_EX)
                                    _token_cache_save()
                                    _fcntl.flock(f_lock, _fcntl.LOCK_UN)
                                    f_lock.close()
                                except Exception:
                                    pass
                                _start_camview_keepalive(server)
                            logger.info(f"[Camview] Token refreshed successfully for {server}")
                        else:
                            # Auth itself failed — fall back to negative cache
                            # so we don't hammer a genuinely-down server.
                            with _local_cache_lock:
                                _camview_tokens[server]     = "FAILED"
                                _camview_timestamps[server] = now
                            logger.warning(f"[Camview] Token refresh failed for {server} — giving up on {cam_label}")
                            if attempt >= 2:
                                return None, None, False
                    else:
                        # Another thread refreshed it, just reuse it
                        logger.info(f"[Camview] Reusing token refreshed by another thread for {server}")
                        token = cur_tok
                # Retry the StartVideo call with the fresh token (or fail if auth failed)
                if attempt < 2:
                    time.sleep(1)
                continue

            if res.status_code != 200:
                logger.warning(f"[Camview] StartVideo HTTP {res.status_code} for {cam_label} (attempt {attempt})")
                # 503 = DVR overloaded/down; signal with sentinel so callers skip DB update
                sentinel = "DVR_UNAVAILABLE" if res.status_code == 503 else None
                return None, sentinel, False

            body = res.json()
            data = body.get("data") or {}
            urls: List[str] = []
            for key in ("url", "url2"):
                raw = data.get(key)
                if not raw:
                    continue
                urls.extend(p.strip() for p in raw.split("|") if p.strip())

            stream_token = data.get("token") or token

            if not urls:
                err_code = body.get("code")
                if err_code == 2084:
                    # 2084 = "no authorization" — either camera-level permission OR DVR
                    # concurrent stream capacity exceeded.  Return a sentinel so the
                    # async caller can retry with backoff without blocking the thread pool.
                    logger.debug(
                        f"[Camview] StartVideo 2084 for {cam_label} (attempt {attempt})"
                    )
                    return None, "DVR_CAPACITY", False
                # Log the full response so we can diagnose server-side rejections
                logger.warning(
                    f"[Camview] StartVideo returned no URLs for {cam_label} "
                    f"(attempt {attempt}) — response: {json.dumps(body)[:300]}"
                )
                return None, None, False

            if _CAMVIEW_SKIP_PRIV:
                public = [u for u in urls if not _is_private_ip_url(u)]
                urls = public if public else urls

            # TCP port probe on first public URL
            first = urls[0]
            m = re.match(r'[a-z]+://([^/:]+)(?::(\d+))?', first)
            if m:
                host = m.group(1)
                port = int(m.group(2)) if m.group(2) else 554
                if not _probe_tcp_port(host, port):
                    logger.warning(f"[Camview] Port {port} on {host} unreachable for {cam_label}")
                    return None, None, False

            final = [f"{u}?token={stream_token}" for u in urls]
            return final, stream_token, True

        except Exception as e:
            logger.warning(f"[Camview] StartVideo exception for {cam_label} (attempt {attempt}): {e}")
            if attempt < 2:
                time.sleep(1)
            else:
                return None, None, False

    return None, None, False


def _sync_camview_stop_video(server: str, device_id: str, camera_no: str) -> None:
    """Best-effort StopVideo — explicitly release the DVR RTSP stream slot.

    Called after every FFmpeg capture (success or failure) so the slot is freed
    immediately instead of waiting for the DVR's own idle timeout (can be 60-300 s).
    Errors are silently ignored; this is fire-and-forget.
    """
    import requests as _req
    try:
        token = _sync_camview_get_token(server)
        if not token or token in ("DVR_UNAVAILABLE", "FAILED", "DVR_CAPACITY"):
            return
        url = f"https://{server}/admin/API/MTS/Video/StopVideo?token={token}"
        payload = {
            "clientType": "WINPC_V1", "project": "PSDK",
            "method": "MTS.Video.StopVideo",
            "data": {"channelId": f"{device_id}$1$0${camera_no}", "streamType": "1"},
        }
        _req.post(url,
                  headers={"Content-Type": "application/json", "X-Subject-Token": token},
                  data=json.dumps(payload), timeout=3, verify=False)
    except Exception:
        pass


def _detect_ffmpeg_rtsp_timeout_opt() -> str:
    """Return -timeout (FFmpeg 6.x) or -stimeout (FFmpeg 4.x) for RTSP connections."""
    try:
        out = subprocess.check_output(["ffmpeg", "-version"],
                                      stderr=subprocess.DEVNULL).decode(errors="replace")
        m = re.search(r"version (\d+)\.", out)
        if m and int(m.group(1)) >= 6:
            return "-timeout"
    except Exception:
        pass
    return "-stimeout"

_FFMPEG_RTSP_TIMEOUT_OPT: str = _detect_ffmpeg_rtsp_timeout_opt()

logger.info("=" * 80)
logger.info("TCS Camera Classification — DB Mode | H200 Optimized")
logger.info(f"  CPU cores        : {_CPU_CORES}")
logger.info(f"  Network workers  : {MAX_WORKERS_NETWORK}")
logger.info(f"  RTSP cap workers : {MAX_WORKERS_RTSP_CAPTURE}  (concurrent FFmpeg RTSP streams)")
logger.info(f"  FFmpeg RTSP opt  : {_FFMPEG_RTSP_TIMEOUT_OPT} (auto-detected for installed FFmpeg)")
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
    "Biometric Area",
]

VLM_PROMPT = f"""You are an expert CCTV scene classifier for an Indian exam-center
deployment. You MUST classify every image into EXACTLY ONE of these seven
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
  hub room, store room, observer/control/office room)
- "Biometric Area": the candidate registration / verification point — a
  table or counter with fingerprint scanners, iris or face-capture devices,
  webcams, a laptop or tablet used for attendance, or candidates queuing or
  standing one by one at such a desk. Choose this over "Entry" or
  "Server Room" whenever a registration/biometric desk is the main subject,
  even if it sits near a doorway or in a small room.
- "Entry": a building entrance — main doors, gates, reception desk,
  signboards, security check facing the doorway (no biometric desk).
- "Outside Area": building surroundings or interior circulation — corridors,
  hallways, washroom pathways, lobbies, staircases, boundary walls,
  external building view.
- "Parking Lot": open area with cars, bikes or painted parking lines.
- "Outdoor Scene": open natural scene dominated by sky, trees, gardens, or
  roads with no building context.

Decision policy — read carefully:
1. You MUST commit to one of the seven categories above whenever any scene
   structure is visible. Pick the single closest match even if the image is
   dim, blurry, low-contrast, has a timestamp/date overlay, or shows an
   unfamiliar angle. These are NOT reasons to say Uncertain.
2. Return "Uncertain" ONLY if the image is completely black, completely
   white, completely uniform grey, or so scrambled/corrupted that no scene
   structure can be identified at all. If you can name even one object
   (chair, door, computer, wall, sky), you MUST pick a real category.
3. Do not invent categories. Do not return Markdown, prose, or code fences.

Output format — return ONLY this JSON object on a single line:
{{"category": "<one of the seven categories above, or Uncertain>", "reason": "<one short sentence citing the strongest visual evidence>"}}"""


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
    """Always returns a unique string key for the camera.
    For RTSP: combines device_id + camera_no to avoid collisions across cameras on same device.
    For HLS: uses ch_code.
    """
    device_id = str(camera.get("device_id") or "").strip()
    if device_id:
        camera_no = str(camera.get("camera_no") or "").strip()
        return f"{device_id}_{camera_no}" if camera_no else device_id
    return str(camera.get("ch_code") or camera.get("camera_id") or "").strip()


def _add_timeout_config():
    """Return TIMEOUT_API — deferred so it can reference the config constant."""
    return TIMEOUT_API


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
    """
    Blocking — runs in thread-pool.
    Uses a daemon thread with hard wall-clock timeout so a hung RTSP stream
    doesn't stall the thread-pool indefinitely.
    """
    result_box: List[Optional[bytes]] = [None]

    def _worker():
        try:
            cap = cv2.VideoCapture(url)
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, TIMEOUT_OPENCV_MS)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC,  TIMEOUT_OPENCV_MS)
            if not cap.isOpened():
                return
            for _ in range(2):      # skip first frames (often incomplete)
                cap.read()
            ret, frame = cap.read()
            cap.release()
            if not ret or frame is None:
                return
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if ok:
                result_box[0] = buf.tobytes()
        except Exception as e:
            logger.debug(f"OpenCV worker error: {e}")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=TIMEOUT_OPENCV_REAL)
    return result_box[0]


def _sync_ffmpeg_capture(url: str, is_rtsp: bool = False) -> Optional[bytes]:
    """Blocking — runs in thread-pool. FFmpeg writes to stdout pipe, not disk."""
    if not shutil.which("ffmpeg"):
        return None
    cmd = ["ffmpeg", "-y"]
    if is_rtsp:
        # FFmpeg 6.x uses -timeout; FFmpeg 4.x uses -stimeout (detected at startup).
        cmd += ["-rtsp_transport", "tcp",
                _FFMPEG_RTSP_TIMEOUT_OPT, str(TIMEOUT_FFMPEG * 1_000_000)]
    else:
        cmd += ["-timeout", str(TIMEOUT_FFMPEG * 1_000_000)]
    cmd += [
        "-i", url,
        "-frames:v", "1",
        "-f", "image2",
        "-vcodec", "mjpeg",
        "-loglevel", "error",
        "pipe:1",
    ]
    try:
        res = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=TIMEOUT_FFMPEG + 3,
        )
        if res.returncode == 0 and res.stdout:
            stderr_txt = res.stderr.decode(errors="replace")
            if "401" in stderr_txt or "Unauthorized" in stderr_txt:
                logger.debug(f"FFmpeg 401 on {url[:60]}")
                return b"_FFmpeg401_"  # sentinel — caller retries StartVideo for fresh token
            return res.stdout
        stderr_txt = res.stderr.decode(errors="replace")
        if "401" in stderr_txt or "Unauthorized" in stderr_txt:
            return b"_FFmpeg401_"
        return None
    except Exception as e:
        logger.debug(f"FFmpeg error: {e}")
        return None


async def _capture_frame_bytes(url: str, is_rtsp: bool = False) -> Optional[bytes]:
    """Async wrapper — tries FFmpeg first for RTSP (OpenCV silently ignores
    CAP_PROP_OPEN/READ_TIMEOUT_MSEC for RTSP, causing 30-second FFmpeg-internal
    hangs that bleed through the daemon thread). Falls back to OpenCV for HLS.
    """
    loop = asyncio.get_event_loop()
    # DO NOT unquote RTSP URLs! DSS servers require %24 for $ and %26 for & in the token
    process_url = url if is_rtsp else unquote(url)

    if is_rtsp:
        # Acquire per-worker RTSP capture semaphore to prevent DVR concurrent stream overflow.
        # _RTSP_CAP_SEM is initialized per pipeline run; default 6 concurrent FFmpeg streams.
        _cap_ctx = _RTSP_CAP_SEM if _RTSP_CAP_SEM is not None else contextlib.nullcontext()
        async with _cap_ctx:
            # Go straight to FFmpeg for RTSP — it respects -stimeout properly
            jpeg = await loop.run_in_executor(_THREAD_POOL, _sync_ffmpeg_capture, process_url, True)
            if jpeg and jpeg != b"_FFmpeg401_":
                return jpeg
            return None  # 401 or capture failure — caller handles retry if needed

    # HLS: OpenCV first (faster), then FFmpeg fallback
    jpeg = await loop.run_in_executor(_THREAD_POOL, _sync_opencv_capture, process_url)
    if jpeg:
        return jpeg
    logger.debug("OpenCV failed → FFmpeg fallback")
    result = await loop.run_in_executor(_THREAD_POOL, _sync_ffmpeg_capture, process_url, False)
    return None if result == b"_FFmpeg401_" else result


async def _capture_frame_with_save(
    url: str,
    cam_id: str,
    is_rtsp: bool = False,
    rtsp_url_list: Optional[List[str]] = None,
) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Capture a JPEG frame and persist it to FRAMES_FOLDER.
    For RTSP, tries each URL in rtsp_url_list (all have tokens).
    Returns (jpeg_bytes, saved_file_path) — path is None if save failed.
    """
    urls_to_try = (rtsp_url_list or []) if is_rtsp else [url]
    if not urls_to_try:
        urls_to_try = [url]

    jpeg: Optional[bytes] = None
    for try_url in urls_to_try:
        jpeg = await _capture_frame_bytes(try_url, is_rtsp=is_rtsp)
        if jpeg:
            break

    if not jpeg:
        return None, None

    # Save to disk
    safe_id  = re.sub(r'[^\w\-]', '_', cam_id)[:60]
    fname    = f"{safe_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    fpath    = os.path.join(FRAMES_FOLDER, fname)
    try:
        with open(fpath, "wb") as fh:
            fh.write(jpeg)
    except Exception as e:
        logger.debug(f"Frame save failed for {cam_id}: {e}")
        fpath = None

    return jpeg, fpath


async def _camview_rtsp_capture_and_save(
    camera: dict, cam_id: str, frames_dir: str
) -> Tuple[Optional[bytes], Optional[str]]:
    """Capture a frame from a Camview DVR camera and save it to frames_dir.

    MUST be called while already holding _RTSP_CAP_SEM so that StartVideo
    is called immediately before FFmpeg — the token never waits in a queue.

    Retries up to 3 times on DVR_CAPACITY (code 2084) with async backoff.
    Calls StopVideo after every attempt to explicitly release the DVR slot.
    """
    device_id   = str(camera.get("device_id") or "").strip()
    camera_no   = str(camera.get("camera_no")  or "").strip()
    camera_name = str(camera.get("camera_name") or "")
    server      = re.sub(r'^https?://', '', str(camera.get("cctv_url", ""))).rstrip('/').lower()
    loop        = asyncio.get_event_loop()
    sv_timeout  = (TIMEOUT_API + 3) * 2 + 5

    rtsp_urls: Optional[List[str]] = None
    ok = False
    for sv_try in range(4):   # attempt 0 + up to 3 retries on DVR_CAPACITY
        try:
            rtsp_urls, sentinel, ok = await asyncio.wait_for(
                loop.run_in_executor(
                    _THREAD_POOL, _sync_camview_start_video, server, device_id, camera_no
                ),
                timeout=sv_timeout,
            )
        except asyncio.TimeoutError:
            logger.debug(f"[Pass-1] StartVideo timed out for {cam_id}")
            return None, None
        except Exception as e:
            logger.debug(f"[Pass-1] StartVideo error for {cam_id}: {e}")
            return None, None

        if ok:
            break
        if sentinel == "DVR_CAPACITY" and sv_try < 3:
            backoff = (sv_try + 1) * 3  # 3, 6, 9 seconds — StopVideo frees slots fast now
            logger.debug(
                f"[Pass-1] DVR capacity (2084) for {cam_id} — sleeping {backoff}s (retry {sv_try+1}/3)"
            )
            await asyncio.sleep(backoff)
            continue
        return None, None   # permanent failure or retries exhausted

    if not ok or not rtsp_urls:
        return None, None

    # Capture — token is fresh, used immediately.
    # Outer loop: retry StartVideo to get a fresh token if FFmpeg gets 401 (DVR preemption).
    # Some DVRs return 401 on RTSP OPTIONS but the stream is actually fine — one retry usually works.
    jpeg: Optional[bytes] = None
    _FFmpeg401 = b"_FFmpeg401_"
    for ffmpeg_try in range(3):  # 0 = first attempt; 1,2 = retries on 401
        if ffmpeg_try > 0:
            # Release old slot, get a fresh token
            await loop.run_in_executor(
                _THREAD_POOL, _sync_camview_stop_video, server, device_id, camera_no
            )
            await asyncio.sleep(2)  # brief wait before re-auth
            try:
                rtsp_urls, sentinel, ok = await asyncio.wait_for(
                    loop.run_in_executor(
                        _THREAD_POOL, _sync_camview_start_video, server, device_id, camera_no
                    ),
                    timeout=sv_timeout,
                )
            except Exception:
                break
            if not ok or not rtsp_urls:
                break
            logger.debug(f"[Pass-1] Retry {ffmpeg_try}/2 after 401 for {cam_id}")

        for url in rtsp_urls:
            jpeg = await loop.run_in_executor(_THREAD_POOL, _sync_ffmpeg_capture, url, True)
            if jpeg and jpeg != _FFmpeg401:
                break  # got a real frame
            if jpeg == _FFmpeg401:
                jpeg = _FFmpeg401  # keep sentinel, will trigger outer retry
        if jpeg and jpeg != _FFmpeg401:
            break  # success — exit outer retry loop
        if jpeg != _FFmpeg401:
            break  # genuine failure (None), not a 401 — no point retrying
        # jpeg == _FFmpeg401 → retry outer loop for fresh token

    if jpeg == _FFmpeg401:
        jpeg = None  # exhausted retries

    # Explicitly release the DVR stream slot so it's available to other workers immediately.
    await loop.run_in_executor(
        _THREAD_POOL, _sync_camview_stop_video, server, device_id, camera_no
    )

    if not jpeg:
        return None, None

    safe_name = re.sub(r'[^\w\-]', '_', camera_name)[:40]
    fname  = f"{device_id}_{camera_no}_{safe_name}.jpg"
    fpath  = os.path.join(frames_dir, fname)
    try:
        with open(fpath, "wb") as fh:
            fh.write(jpeg)
    except Exception as e:
        logger.debug(f"[Pass-1] Frame save failed for {cam_id}: {e}")
        fpath = None

    return jpeg, fpath


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
    num_ctx=2048 per-request caps KV cache: 46GB → ~8GB VRAM.
    Model weights and output quality unchanged.
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

                # Treat any Uncertain-ish answer as the obstructed bucket so the
                # UI can distinguish "model unsure" from "model classified".
                if (not category
                        or "uncertain" in category.lower()
                        or "unclear" in category.lower()
                        or "unknown" in category.lower()):
                    # Always log when the model bails — this is what we want to
                    # see disappear after the prompt tightening.
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
    frames_dir: str = "",
) -> dict:
    """
    RTSP Pass-1: capture a frame from each camera and save it to frames_dir.

    For Camview cameras (cctv_url is a DVR hostname, not an rtsp:// URL):
      - Acquires sem (sem_net, 16 concurrent) as the outer gate, then
        acquires _RTSP_CAP_SEM (6 concurrent) as the inner gate.
      - StartVideo is called INSIDE _RTSP_CAP_SEM so the fresh token is used
        by FFmpeg immediately — no queue-wait token expiry.
      - Returns accessible=True + frame_path if a frame is captured.

    For direct rtsp:// URLs: marks accessible=True, frame_path=None.
      Frame capture is deferred to Pass-2 (no Camview token needed).
    """
    cam_id    = _camera_id(camera)
    device_id = str(camera.get("device_id") or "").strip()
    _cn_raw   = camera.get("camera_no")
    camera_no = str(_cn_raw).strip() if _cn_raw is not None else ""
    raw_url   = str(camera.get("cctv_url") or "").strip()

    _fail = lambda reason: {"camera_id": cam_id, "cam": camera,
                            "accessible": False, "resolved_url": None,
                            "rtsp_urls": None, "frame_path": None, "reason": reason}

    if not raw_url:
        return _fail("missing_cctv_url")

    # Direct RTSP URL — no Camview auth, defer frame capture to Pass-2
    if raw_url.lower().startswith("rtsp://"):
        return {"camera_id": cam_id, "cam": camera,
                "accessible": True, "resolved_url": raw_url,
                "rtsp_urls": [raw_url], "frame_path": None, "reason": "direct_rtsp"}

    if not device_id or not camera_no:
        return _fail("missing_device_id_or_camera_no")

    # Camview camera: capture frame atomically (StartVideo + FFmpeg inside RTSP semaphore)
    async with sem:
        _cap_ctx = _RTSP_CAP_SEM if _RTSP_CAP_SEM is not None else contextlib.nullcontext()
        async with _cap_ctx:
            jpeg, frame_path = await _camview_rtsp_capture_and_save(
                camera, cam_id, frames_dir or FRAMES_FOLDER
            )

    if not jpeg:
        return _fail("frame_capture_failed")

    return {"camera_id": cam_id, "cam": camera,
            "accessible": True, "resolved_url": None,
            "rtsp_urls": None, "frame_path": frame_path, "reason": "ok"}


# ===========================================================================
# Pass-2: VLM classification from saved frames
# ===========================================================================

async def _classify_frame_from_disk(
    access_result: dict,
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
) -> dict:
    """
    Pass-2: classify a camera using its already-saved frame.

    Reads JPEG bytes from access_result["frame_path"] (written by Pass-1).
    No DVR access, no StartVideo, no RTSP — pure disk read + VLM.

    Fallback: if frame_path is None (direct rtsp:// cameras), captures the
    frame now via _capture_frame_with_save (which uses _RTSP_CAP_SEM internally).
    """
    cam_id     = access_result["camera_id"]
    cam        = access_result["cam"]
    frame_path = access_result.get("frame_path")
    loop       = asyncio.get_event_loop()

    base = {
        "camera_id":   cam_id,
        "camera_pk":   cam.get("id"),    # camera_links_{hls,rtsp}.id — for history insert
        "device_id":   str(cam.get("device_id") or ""),
        "camera_no":   str(cam.get("camera_no")  or ""),
        "camera_name": cam.get("camera_name"),
        "frame_path":  frame_path,
    }

    try:
        if frame_path and os.path.exists(frame_path):
            # Happy path: read frame saved by Pass-1
            jpeg = await loop.run_in_executor(
                _THREAD_POOL,
                lambda: open(frame_path, "rb").read(),  # noqa: WPS111
            )
        else:
            # Fallback for direct rtsp:// cameras (frame_path is None from Pass-1)
            resolved = access_result.get("resolved_url") or ""
            rtsp_urls = access_result.get("rtsp_urls")
            jpeg, new_path = await _capture_frame_with_save(
                resolved, cam_id, is_rtsp=True,
                rtsp_url_list=rtsp_urls,
            )
            base["frame_path"] = new_path
            if not jpeg:
                logger.warning(f"[Pass-2] Frame capture failed for {cam_id}")
                return {**base, "classification": "Uncertain",
                        "status": "Not Accessible", "detail": "frame_capture_failed"}

        async with sem:
            label, reason = await _classify_with_vlm(jpeg, session)

        return {**base, "classification": label,
                "status": "Accessible", "detail": reason or "classified"}

    except Exception as e:
        logger.error(f"[Pass-2] Error for {cam_id}: {e}", exc_info=True)
        return {**base, "classification": "Uncertain",
                "status": "Not Accessible", "detail": f"processing_error: {e}"}


# ===========================================================================
# Bulk DB persist — one call after all tasks, not one per camera
# ===========================================================================

def _needs_classification(cam: dict) -> bool:
    """
    RTSP: does this reachable camera still need a label from vLLM?
      - manually verified            -> never (a person's label wins)
      - label empty / null           -> yes
      - label starts with "uncertain" ("Uncertain", "Uncertain/Obstructed") -> yes
      - any other label              -> no (already known; Pass-1 refreshed its status + frame)
    CLASSIFY_ONLY_UNLABELED=0 -> every camera.
    """
    if not CLASSIFY_ONLY_UNLABELED:
        return True
    if str(cam.get("manually_verified") or "0").strip().lower() in ("1", "true"):
        return False
    label = str(cam.get("classification") or "").strip().lower()
    return (not label) or label.startswith("uncertain")


def _persist_history_for_kept(runtime: dict, kept: List[dict]) -> int:
    """
    Cameras that were reachable but NOT sent to vLLM keep their label; still add a
    camera_classification_rtsp row (existing label + this shift + date + frame) so the
    per-shift report and the Cameras-tab run picker see them as classified in this run.
    Without these rows every such camera would read "Not Classified / Offline" for the shift.
    """
    inserted = 0
    for r in kept:
        cam = r.get("cam") or {}
        label = str(cam.get("classification") or "").strip()
        if not cam.get("id") or not label:
            continue
        try:
            inserted += insert_rtsp_classification_history(
                cam.get("id"), runtime.get("shift_id"), runtime.get("assignment_date"),
                r.get("frame_path"), label,
            )
        except Exception as e:
            logger.error(f"History insert failed for kept camera {r.get('camera_id')}: {e}")
    return inserted


def _bulk_persist(runtime: dict, classifications: List[dict]) -> int:
    updated = 0
    skipped = 0
    history_inserted = 0
    for item in classifications:
        cam_id     = str(item.get("camera_id") or "").strip()
        label      = str(item.get("classification") or "").strip()
        status     = str(item.get("status") or "").strip() or None
        frame_path = str(item.get("frame_path") or "").strip() or None
        camera_pk  = item.get("camera_pk")
        if not cam_id or not label:
            continue
        # Only persist when the camera classified successfully. Transient
        # failures (token refresh, frame capture, processing errors) keep
        # status == "Not Accessible" — Pass-1 already wrote that, no overwrite.
        if status != "Accessible":
            skipped += 1
            continue
        try:
            if runtime["link_type"] == "HLS":
                updated += update_hls_classification(
                    runtime["exam_id"], runtime["shift_id"],
                    runtime["assignment_date"], cam_id, label, status
                )
                history_inserted += insert_hls_classification_history(
                    camera_pk, runtime.get("shift_id"),
                    runtime.get("assignment_date"), frame_path, label,
                )
            else:
                # For RTSP use device_id + camera_no from item, fall back to splitting cam_id
                device_id = str(item.get("device_id") or "").strip()
                camera_no = str(item.get("camera_no") or "").strip()
                if not device_id:
                    parts     = cam_id.split("_", 1)
                    device_id = parts[0]
                    camera_no = parts[1] if len(parts) > 1 else ""
                updated += update_rtsp_classification(
                    runtime["exam_id"], device_id, camera_no, label, status, frame_path,
                    camera_pk=camera_pk,    # one row: a device id + camera no can repeat on another server
                )
                history_inserted += insert_rtsp_classification_history(
                    camera_pk, runtime.get("shift_id"),
                    runtime.get("assignment_date"), frame_path, label,
                )
        except Exception as e:
            logger.error(f"DB persist failed for {cam_id}: {e}")
    if skipped:
        logger.info(
            f"Bulk DB persist: {updated} rows updated, {history_inserted} history rows inserted, "
            f"{skipped} frame_capture_failed skipped"
        )
    else:
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
                device_id  = str(item.get("device_id") or "").strip()
                camera_no  = str(item.get("camera_no") or "").strip()
                frame_path = str(item.get("frame_path") or "").strip() or None
                if not device_id:
                    parts     = cam_id.split("_", 1)
                    device_id = parts[0]
                    camera_no = parts[1] if len(parts) > 1 else ""
                camera_pk  = item.get("camera_pk")
                if frame_path:
                    updated += update_rtsp_status_and_frame_path(
                        runtime["exam_id"], device_id, camera_no, status, frame_path, camera_pk=camera_pk
                    )
                else:
                    updated += update_rtsp_status(
                        runtime["exam_id"], device_id, camera_no, status, camera_pk=camera_pk
                    )
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


def _update_df(row_id: int, label: str, status: str, detail: str,
               frame_path: Optional[str] = None) -> None:
    global _checkpoint_count
    with _df_lock:
        mask = _df["Row_ID"] == row_id
        if mask.any():
            _df.loc[mask, SCENE_COLUMN_TARGET] = label
            _df.loc[mask, "Status"]            = status
            _df.loc[mask, "Status Detail"]     = detail
            if frame_path:
                _df.loc[mask, "Frame Path"]    = frame_path
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

    global _RTSP_CAP_SEM
    _RTSP_CAP_SEM = asyncio.Semaphore(MAX_WORKERS_RTSP_CAPTURE)

    sem_net = asyncio.Semaphore(MAX_WORKERS_NETWORK)
    sem_vlm = asyncio.Semaphore(MAX_WORKERS_VLM)

    accessible: List[dict] = []
    failed:     List[dict] = []

    async with aiohttp.ClientSession(connector=_make_connector()) as session:

        # ── Pre-auth: warm up Camview tokens before the 64-worker storm ──
        if is_rtsp:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(_THREAD_POOL, _preauth_camview_servers, cameras)

        # ── Pass-1 ────────────────────────────────────────────────────────
        frames_dir = _frames_dir_for_assignment(runtime.get("assignment_id")) if is_rtsp else ""
        logger.info(
            f"Pass-1 | {len(cameras)} cameras | {MAX_WORKERS_NETWORK} concurrent"
            + (f" | frames → {frames_dir}" if frames_dir else "")
        )
        p1_coros = (
            [_check_rtsp_camera(c, sem_net, frames_dir) for c in cameras]
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
                failed.append({"camera_id": res["camera_id"], "cam": res.get("cam", {}), "reason": res["reason"]})

        logger.info(
            f"Pass-1 done {time.time()-p1_start:.1f}s | "
            f"accessible={len(accessible)} failed={len(failed)}"
        )
        
        statuses_to_persist = []
        for x in accessible:
            statuses_to_persist.append({
                "camera_id": x["camera_id"],
                "device_id": str(x["cam"].get("device_id") or ""),
                "camera_no": str(x["cam"].get("camera_no") or ""),
                "camera_pk": x["cam"].get("id"),
                "status":    "Accessible",
                "frame_path": x.get("frame_path"),  # written to DB in Pass-1 persist
            })
        for x in failed:
            # Skip DB update when the failure was a transient DVR-side 503 —
            # writing "Not Accessible" here would destroy valid status from prior runs.
            if x.get("reason") == "dvr_service_unavailable":
                continue
            cam = x.get("cam", {})
            statuses_to_persist.append({
                "camera_id": x["camera_id"],
                "device_id": str(cam.get("device_id") or ""),
                "camera_no": str(cam.get("camera_no") or ""),
                "camera_pk": cam.get("id"),
                "status": "Not Accessible",
            })
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

        # ── Which reachable cameras need a label? (RTSP only; HLS classifies all) ──
        if is_rtsp:
            to_classify = [r for r in accessible if _needs_classification(r.get("cam") or {})]
            kept        = [r for r in accessible if r not in to_classify]
            logger.info(
                f"Classification needed for {len(to_classify)} of {len(accessible)} reachable cameras "
                f"({len(kept)} keep their label — verified, or already labelled)"
                + ("" if CLASSIFY_ONLY_UNLABELED else " [CLASSIFY_ONLY_UNLABELED=0: all]")
            )
        else:
            to_classify, kept = accessible, []

        # vLLM is started only now, and only if something needs it: a worker with nothing
        # to label never loads it (saves ~4 min and ~79 GB of GPU).
        if is_rtsp and to_classify:
            _ensure_vllm_running()

        # ── Pass-2 ────────────────────────────────────────────────────────
        # No DVR access in Pass-2 — frames were saved to disk in Pass-1.
        logger.info(
            f"Pass-2 | {len(to_classify)} cameras | "
            f"{MAX_WORKERS_VLM} concurrent vLLM"
        )
        p2_coros = [
            _classify_frame_from_disk(r, session, sem_vlm)
            for r in to_classify
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
                "device_id":      res.get("device_id", ""),
                "camera_no":      res.get("camera_no", ""),
                "camera_name":    res.get("camera_name"),
                "classification": res["classification"],
                "status":         res.get("status"),
                "frame_path":     res.get("frame_path"),
            })
            if res.get("status") != "Accessible":
                failed.append({"camera_id": res["camera_id"], "reason": res.get("detail")})

        logger.info(f"Pass-2 done {time.time()-p2_start:.1f}s")

    # Re-persist DB status for cameras that passed Pass-1 but failed Pass-2.
    # Without this, the DB ends up with status="Accessible" (from the Pass-1
    # status persist above) but classification stays null because _bulk_persist
    # skips non-Accessible items — leaving rows in an inconsistent state.
    pass2_failed_statuses = [
        {
            "camera_id": c["camera_id"],
            "device_id": str(c.get("device_id") or ""),
            "camera_no": str(c.get("camera_no") or ""),
            "camera_pk": c.get("camera_pk"),
            "status":    "Not Accessible",
        }
        for c in classifications
        if c.get("status") != "Accessible"
    ]
    if pass2_failed_statuses:
        logger.info(
            f"Pass-2: {len(pass2_failed_statuses)} cameras failed after Pass-1 success — "
            f"reverting DB status to Not Accessible"
        )
        _bulk_persist_status(runtime, pass2_failed_statuses)

    # Bulk persist — one round-trip at the end. Only Pass-2 results are saved; an empty
    # label never overwrites anything (see _bulk_persist).
    updated = _bulk_persist(runtime, classifications)
    kept_history = _persist_history_for_kept(runtime, kept) if is_rtsp else 0
    if is_rtsp:
        logger.info(f"Kept existing labels for {len(kept)} cameras ({kept_history} history rows written)")

    return {
        "assignment_id":                    runtime["assignment_id"],
        "link_type":                        runtime["link_type"],
        "processed":                        len(classifications),
        "kept_existing_labels":             len(kept),
        "failed":                           failed,
        "classifications":                  classifications,
        "persisted_classification_updates": updated,
    }


# ===========================================================================
# Default mode  (no VISION_ASSIGNMENT_PAYLOAD)
# ===========================================================================

async def _default_pipeline(cameras: List[dict], runtime: dict) -> None:
    global _df, _RTSP_CAP_SEM
    is_rtsp = runtime["link_type"] == "RTSP"
    _RTSP_CAP_SEM = asyncio.Semaphore(MAX_WORKERS_RTSP_CAPTURE)
    sem_net = asyncio.Semaphore(MAX_WORKERS_NETWORK)
    sem_vlm = asyncio.Semaphore(MAX_WORKERS_VLM)

    accessible: List[dict] = []

    async with aiohttp.ClientSession(connector=_make_connector()) as session:

        # Pass-1
        frames_dir = _frames_dir_for_assignment() if is_rtsp else ""
        logger.info(
            f"Pass-1 | {len(cameras)} cameras | {MAX_WORKERS_NETWORK} concurrent"
            + (f" | frames → {frames_dir}" if frames_dir else "")
        )
        p1_coros = (
            [_check_rtsp_camera(c, sem_net, frames_dir) for c in cameras]
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

        # Pass-2 — frames already on disk, pure VLM
        logger.info(
            f"Pass-2 | {len(accessible)} cameras | "
            f"{MAX_WORKERS_VLM} vLLM workers"
        )
        p2_coros = [
            _classify_frame_from_disk(r, session, sem_vlm)
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
            _update_df(row_id, res["classification"], res["status"], res["detail"],
                       frame_path=res.get("frame_path"))

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
    _df["Frame Path"]    = ""
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

    # Bulk DB persist — include device_id / camera_no / frame_path for RTSP
    classifications = []
    for _, row in _df[_df["Status"] == "Accessible"].iterrows():
        try:
            cam_pk = int(row.get("id")) if row.get("id") not in (None, "") else None
        except (TypeError, ValueError):
            cam_pk = None
        classifications.append({
            "camera_id":      row["cam_id_ref"],
            "camera_pk":      cam_pk,
            "device_id":      str(row.get("device_id") or ""),
            "camera_no":      str(row.get("camera_no") or ""),
            "classification": row[SCENE_COLUMN_TARGET],
            "status":         "Accessible",
            "frame_path":     str(row.get("frame_path") or "") or None,
        })
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
# Entry point
# ===========================================================================
_VLLM_PROC: Optional[subprocess.Popen] = None
_VLLM_CLEANUP_DONE = False
# True once this process has started — or adopted — a vLLM server. A run that never needed one
# (nothing to classify) must not kill a vLLM another process is using when it exits.
_VLLM_TOUCHED = False


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
    if not _VLLM_TOUCHED and _VLLM_PROC is None:
        logger.info(f"vLLM was never started by this run ({reason}) — nothing to stop")
        return

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
    # vLLM's tensor-parallel workers are Python multiprocessing.spawn children.
    # Even after SIGKILL on the parent, the spawn workers sometimes survive
    # (they catch CUDA mid-call and ignore the kill). Each leftover process
    # holds ~3 GB container RAM + ~1-3 GB GPU. Across many runs they pile up
    # and on workers eventually trip the 32 GB cgroup OOMKiller. The same
    # pkill is also run at vLLM startup, but if no next job runs, orphans
    # stay forever — reap them here at shutdown instead.
    try:
        _reap_orphan_spawn_workers()
        logger.info("orphan reaper: dispatched SIGKILL to orphaned spawn_main subprocesses")
    except Exception as e:
        logger.debug(f"orphan reaper failed: {e}")


def _reap_orphan_spawn_workers():
    """SIGKILL leftover multiprocessing spawn workers whose parent has died
    (reparented to PID 1) — e.g. vLLM workers that survived their server.
    A running pipeline's own spawn children (the unified detector, producers)
    still have a live parent, so they are left alone. A plain
    `pkill -f spawn_main.*--multiprocessing-fork` killed those too, stopping
    every detection that happened to be running when classification ended."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], check=False, timeout=5,
                             stdout=subprocess.PIPE, text=True).stdout
    except Exception as e:
        logger.debug(f"orphan reaper ps failed: {e}")
        return
    for line in out.splitlines():
        parts = line.split(None, 2)
        if (len(parts) == 3 and parts[1] == "1"
                and "spawn_main" in parts[2] and "--multiprocessing-fork" in parts[2]):
            try:
                os.kill(int(parts[0]), signal.SIGKILL)
            except Exception:
                pass


def _handle_shutdown_signal(signum, frame):
    logger.warning(f"Received signal {signum}; cleaning up classification vLLM before exit")
    _shutdown_vllm(f"signal {signum}")
    raise SystemExit(128 + signum)


def _gpu_memory_gb() -> Optional[Tuple[float, float]]:
    """(free_gb, total_gb) of GPU 0, or None when nvidia-smi is unavailable."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10).stdout
        free_mb, total_mb = [float(x) for x in out.strip().splitlines()[0].split(",")]
        return free_mb / 1024.0, total_mb / 1024.0
    except Exception:
        return None


def _wait_for_free_gpu_memory() -> None:
    """Block (up to VLLM_WAIT_FREE_MEM_S, default 15 min) until vLLM's memory request fits on the GPU."""
    try:
        util = float(os.getenv("VLLM_GPU_MEMORY_UTILIZATION", "0.55"))
    except ValueError:
        util = 0.55
    limit = float(os.getenv("VLLM_WAIT_FREE_MEM_S", "900"))
    deadline = time.time() + limit
    announced = False
    while True:
        mem = _gpu_memory_gb()
        if mem is None:
            return
        free, total = mem
        need = util * total + 1.0
        if free >= need:
            return
        if time.time() >= deadline:
            logger.error(f"GPU memory still short after {limit:.0f}s (free {free:.0f} GB, vLLM needs {need:.0f} GB); starting anyway.")
            return
        if not announced:
            logger.warning(f"Waiting for GPU memory before starting vLLM: free {free:.0f} GB, needs {need:.0f} GB "
                           f"(another job is using the card); will wait up to {limit:.0f}s.")
            announced = True
        time.sleep(20)


def _ensure_vllm_running():
    global _VLLM_PROC, _VLLM_TOUCHED
    if _VLLM_PROC is not None and _VLLM_PROC.poll() is None:
        return _VLLM_PROC                      # already started by this run
    _VLLM_TOUCHED = True
    try:
        if requests.get("http://localhost:8001/health", timeout=2).status_code == 200:
            if STOP_VLLM_ON_EXIT:
                logger.info("vLLM server is already running; it will be stopped when classification exits.")
            else:
                logger.info("vLLM server is already running in background and will be left running.")
            return None
    except:
        pass

    # Same guard as the HLS script: vLLM refuses to start unless gpu-memory-utilization x total is
    # free, and the other exam's detectors may still hold the card. Wait for it instead of crashing.
    _wait_for_free_gpu_memory()

    logger.info("Starting temporary vLLM server process. Please wait (~60-180 seconds)...")
    _vllm_script = os.getenv("VLLM_START_SCRIPT", os.path.join(PROJECT_ROOT, "start_vllm.sh"))
    logger.info(f"Using vLLM start script: {_vllm_script}")

    # Log vLLM output to a file for debugging instead of /dev/null
    _vllm_log_path = os.path.join(LOG_DIR, f"vllm_startup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    os.makedirs(LOG_DIR, exist_ok=True)
    _vllm_log_file = open(_vllm_log_path, "w")
    logger.info(f"vLLM startup log → {_vllm_log_path}")

    proc = subprocess.Popen(
        ["/bin/bash", _vllm_script],
        stdout=_vllm_log_file,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid
    )
    _VLLM_PROC = proc

    # Poll for up to VLLM_STARTUP_TIMEOUT_S (default 600 s = 10 min), 2 s per poll.
    # The 30B MoE model loads in ~1-2 min when its weights stream from disk, but a
    # cold start that reads them on demand took ~8 min on worker-0 (38 s/shard), and
    # the old 3-minute limit killed healthy loads as if they were deadlocked. A real
    # hang (RPC timeout, CUDA driver contention) still gets killed, just later.
    _MAX_STARTUP_POLLS = max(1, int(os.getenv("VLLM_STARTUP_TIMEOUT_S", "600")) // 2)
    for i in range(_MAX_STARTUP_POLLS):
        try:
            if requests.get("http://localhost:8001/health", timeout=2).status_code == 200:
                logger.info(f"Temporary vLLM server successfully started! (took ~{i*2}s)")
                _vllm_log_file.close()
                return proc
        except:
            pass
        time.sleep(2)
        if proc.poll() is not None:
            logger.error(f"vLLM server crashed during startup (exit code {proc.returncode}).")
            logger.error(f"Check vLLM log at: {_vllm_log_path}")
            _vllm_log_file.close()
            # Fatal: vLLM crashed, no point proceeding with classification
            _shutdown_vllm("startup crash")
            sys.exit(1)

    # vLLM timed out — still running but not serving. This usually means
    # the RPC handshake between vLLM API server and EngineCore is deadlocked
    # (common when multiple vLLM instances compete for CUDA driver init).
    logger.error(
        f"vLLM server failed to start within {_MAX_STARTUP_POLLS * 2}s. "
        f"Killing zombie vLLM process and aborting classification."
    )
    logger.error(f"Check vLLM log at: {_vllm_log_path}")
    _vllm_log_file.close()
    _shutdown_vllm("startup timeout")
    sys.exit(1)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    logger.info("=" * 80)
    logger.info("TCS Camera Classification — starting")
    logger.info("=" * 80)
    
    # RTSP assignment runs start vLLM lazily (run_assignment_mode, after Pass-1, only if some
    # camera needs a label). HLS and the standalone mode still start it up front, as before.
    _lazy_vllm = bool(VISION_ASSIGNMENT_PAYLOAD) and str(_parse_payload().get("link_type") or "").upper() == "RTSP"
    vllm_proc = None if _lazy_vllm else _ensure_vllm_running()
    
    try:
        if VISION_ASSIGNMENT_PAYLOAD:
            result = asyncio.run(run_assignment_mode())
            print(json.dumps(result, indent=2))
        else:
            main()
        logger.info(f"Done. Log: {LOG_FILENAME}")
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        _shutdown_vllm("classification exit")
