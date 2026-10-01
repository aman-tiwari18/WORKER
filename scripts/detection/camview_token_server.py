#!/usr/bin/env python3
"""
Camview DVR Token Coordination Server
======================================
Runs on worker-0 (192.168.19.30) and serves as the single authenticator for
all Camview DVR servers. Workers 1–3 call this server to get tokens instead
of authenticating directly, eliminating cross-VM session collision.

Camview DVRs (d14/d15/d16) allow only ONE active session per username.
When 4 workers all authenticate simultaneously each new auth invalidates the
previous worker's token. This server ensures only one auth happens per DVR
at a time and all workers share the same session token.

Usage (on worker-0):
    python3 camview_token_server.py [--port 8090]

Clients (all workers, worker-0 included via localhost): vision_classification_rtsp.py
and pipeline/master_pipeline.py (detection) — both fetch GET /token and report dead
tokens via POST /evict {"server", "token"}. Override with CAMVIEW_TOKEN_SERVER.

Supervised by pm2 (process "camview-token-server") so it restarts if it dies.
"""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Credentials live in script-server/.env; load it here so the server runs the same
# however it is launched (pm2, shell). Already-set env vars win.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env"))
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [TokenServer] %(message)s",
)
logger = logging.getLogger("camview_token_server")

# ---------------------------------------------------------------------------
# Configuration (mirrors vision_classification_rtsp.py)
# ---------------------------------------------------------------------------
_AUTH_USER    = os.getenv("CAMVIEW_AUTH_USER", "system")
_AUTH_PASS    = os.getenv("CAMVIEW_AUTH_PASS", "Sasd@12345")
_TIMEOUT_API  = int(os.getenv("VISION_TIMEOUT_API", "6"))
_KEEPALIVE_S  = 22   # keepalive interval in seconds
# A token is handed out while its session is provably alive: fresh from login, or
# accepted by a keepalive within this window. No fixed max age — re-logging in on a
# healthy session would kill the token every worker is streaming with right now.
_ALIVE_WINDOW_S = int(os.getenv("CAMVIEW_ALIVE_WINDOW", str(_KEEPALIVE_S * 4)))
_AUTH_BACKOFF = int(os.getenv("CAMVIEW_AUTH_BACKOFF", "400"))  # seconds before retrying a failed auth (>300s DVR lockout window)

# Per-server password overrides: CAMVIEW_AUTH_PASS_d15=<password> → d15.camview.io
_PER_SERVER_PASS: dict = {
    k.replace("CAMVIEW_AUTH_PASS_", "").lower() + ".camview.io": v
    for k, v in os.environ.items()
    if k.startswith("CAMVIEW_AUTH_PASS_")
}

_tokens:        dict = {}   # server → token string
_timestamps:    dict = {}   # server → float (epoch)
_alive_ts:      dict = {}   # server → float (epoch of last keepalive the DVR accepted)
_fail_times:    dict = {}   # server → float (epoch of last auth failure, negative cache)
_server_locks:  dict = {}   # server → Lock
_meta_lock      = threading.Lock()
_keepalive_threads: dict = {}  # server → (Thread, Event)


def _get_lock(server: str) -> threading.Lock:
    with _meta_lock:
        if server not in _server_locks:
            _server_locks[server] = threading.Lock()
        return _server_locks[server]


def _camview_signature(user: str, password: str, realm: str, random_key: str) -> str:
    t = hashlib.md5(password.encode()).hexdigest()
    t = hashlib.md5((user + t).encode()).hexdigest()
    t = hashlib.md5(t.encode()).hexdigest()
    t = hashlib.md5((user + ":" + realm + ":" + t).encode()).hexdigest()
    return hashlib.md5((t + ":" + random_key).encode()).hexdigest()


def _do_auth(server: str) -> Optional[str]:
    """2-step Camview challenge/response. Tries MD5 then RSA (for d15).
    Uses per-server password if CAMVIEW_AUTH_PASS_<shortname> env var is set.
    """
    auth_url = f"https://{server}/admin/API/accounts/authorize"
    headers  = {"Content-Type": "application/json"}
    payload  = {"userName": _AUTH_USER, "ipAddress": "", "clientType": "WINPC_V1"}
    password = _PER_SERVER_PASS.get(server.lower(), _AUTH_PASS)

    for attempt in range(1, 3):
        try:
            r1 = requests.post(auth_url, headers=headers, json=payload,
                               timeout=_TIMEOUT_API, verify=False)
            if r1.status_code not in (200, 401):
                continue
            j1       = r1.json()
            realm    = j1.get("realm")
            rnd_key  = j1.get("randomKey")
            pub_key  = j1.get("publickey") or j1.get("publicKey")
            cookie   = r1.headers.get("Set-Cookie", "")
            if not all([realm, rnd_key, pub_key]):
                continue

            sig = _camview_signature(_AUTH_USER, password, realm, rnd_key)

            for enc_type in ("MD5", "RSA"):
                p2 = {
                    "mac": "", "signature": sig, "userName": _AUTH_USER,
                    "randomKey": rnd_key, "publicKey": pub_key,
                    "encryptType": enc_type, "ipAddress": "",
                    "clientType": "WINPC_V1", "userType": "0",
                }
                r2  = requests.post(
                    auth_url,
                    headers={"Content-Type": "application/json", "Cookie": cookie},
                    json=p2, timeout=_TIMEOUT_API, verify=False,
                )
                j2    = r2.json()
                token = j2.get("token")
                if token:
                    logger.info(f"Authenticated {server} (encryptType={enc_type})")
                    return token
                code = j2.get("code")
                if code == 2002:
                    unlock = j2.get("data", {}).get("unlockRemainTime", "?")
                    logger.warning(f"{server} locked (code 2002), unlockRemainTime={unlock}s")
                    return None
                if code == 1101 and enc_type == "MD5":
                    logger.debug(f"{server} rejected MD5 (code 1101), getting fresh challenge for RSA")
                    try:
                        r1b = requests.post(auth_url, headers=headers, json=payload,
                                            timeout=_TIMEOUT_API, verify=False)
                        j1b = r1b.json()
                        if j1b.get("randomKey") and j1b.get("realm"):
                            realm   = j1b["realm"]
                            rnd_key = j1b["randomKey"]
                            pub_key = j1b.get("publickey") or j1b.get("publicKey") or pub_key
                            cookie  = r1b.headers.get("Set-Cookie", cookie)
                            sig     = _camview_signature(_AUTH_USER, password, realm, rnd_key)
                    except Exception:
                        pass
                    continue
                break
        except Exception as e:
            logger.debug(f"Auth attempt {attempt} for {server}: {e}")
        time.sleep(attempt)

    logger.warning(f"Auth failed for {server}")
    return None


def _start_keepalive(server: str, token: str) -> None:
    """Start a background keepalive thread for server (stops any existing one first)."""
    if server in _keepalive_threads:
        _, stop_ev = _keepalive_threads[server]
        stop_ev.set()

    stop_ev = threading.Event()

    def _loop():
        while not stop_ev.wait(_KEEPALIVE_S):
            with _meta_lock:
                current = _tokens.get(server)
            if not current or current != token:
                return  # token was replaced or evicted
            try:
                r = requests.put(
                    f"https://{server}/admin/API/accounts/keepalive",
                    headers={"Content-Type": "application/json", "X-Subject-Token": token},
                    json={}, timeout=5, verify=False,
                )
            except Exception as e:
                logger.debug(f"Keepalive error for {server}: {e}")
                continue
            if r.status_code == 200:
                with _meta_lock:
                    if _tokens.get(server) == token:
                        _alive_ts[server] = time.time()
                logger.debug(f"Keepalive OK: {server}")
            elif r.status_code == 401:
                # Session is gone on the DVR (replaced / expired). Drop it so the next
                # /token request logs in again instead of handing out a dead token.
                logger.warning(f"Keepalive rejected by {server} (401) — session lost, token dropped")
                evict_token(server, token)
                return
            else:
                logger.warning(f"Keepalive for {server} returned HTTP {r.status_code}")

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    _keepalive_threads[server] = (t, stop_ev)
    logger.info(f"Keepalive thread started for {server}")


def get_token(server: str, force: bool = False) -> Optional[str]:
    """Return a valid token for server. Thread-safe. Authenticates if needed.
    Negative-caches failures for _AUTH_BACKOFF seconds so a locked DVR is not
    hammered by queued concurrent requests (which would extend the lockout).
    """
    lock = _get_lock(server)
    with lock:
        now = time.time()
        if not force:
            with _meta_lock:
                tok      = _tokens.get(server)
                ts       = _timestamps.get(server, 0)
                fail_ts  = _fail_times.get(server, 0)

            # Return the cached token while its session is known alive
            with _meta_lock:
                alive = max(ts, _alive_ts.get(server, 0))
            if tok and now - alive < _ALIVE_WINDOW_S:
                return tok

            # Negative cache: don't retry a recently-failed server
            if not tok and now - fail_ts < _AUTH_BACKOFF:
                logger.debug(f"Skipping auth for {server} — in backoff ({_AUTH_BACKOFF - (now - fail_ts):.0f}s left)")
                return None

        token = _do_auth(server)
        now   = time.time()
        with _meta_lock:
            if token:
                _tokens[server]     = token
                _timestamps[server] = now
                _alive_ts[server]   = now
                _fail_times.pop(server, None)   # clear any prior failure
                _start_keepalive(server, token)
            else:
                _tokens.pop(server, None)
                _timestamps.pop(server, None)
                _alive_ts.pop(server, None)
                _fail_times[server] = now       # record failure time for backoff
        return token


def evict_token(server: str, token: Optional[str] = None) -> bool:
    """Evict a stale token (a worker got a 401 from StartVideo, or keepalive was rejected).

    With `token`, evict only if it is still the current one: many workers report the
    same dead token at once, and each report must not throw away the fresh session
    the first report already caused (that login storm is what locks DVR accounts).
    Without `token` (older callers) the current token is evicted unconditionally.
    Does not take the per-server auth lock, so it never waits behind a slow login."""
    with _meta_lock:
        current = _tokens.get(server)
        if not current or (token is not None and token != current):
            return False
        _tokens.pop(server, None)
        _timestamps.pop(server, None)
        _alive_ts.pop(server, None)
    logger.info(f"Token evicted for {server} (session reported dead)")
    return True


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    request_queue_size = 128


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.debug(f"{self.address_string()} - {fmt % args}")

    def _send_json(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)

        if parsed.path == "/health":
            now = time.time()
            with _meta_lock:
                in_backoff = {
                    s: round(_AUTH_BACKOFF - (now - ts), 1)
                    for s, ts in _fail_times.items()
                    if now - ts < _AUTH_BACKOFF
                }
                sessions = {
                    s: {"age_s": round(now - _timestamps.get(s, now)),
                        "last_alive_s": round(now - max(_timestamps.get(s, 0), _alive_ts.get(s, 0)))}
                    for s in _tokens
                }
            self._send_json(200, {
                "status":     "ok",
                "servers":    list(_tokens.keys()),
                "sessions":   sessions,
                "in_backoff": in_backoff,
            })
            return

        if parsed.path == "/token":
            server = (qs.get("server") or [""])[0].strip()
            force  = (qs.get("force")  or ["0"])[0] == "1"
            if not server:
                self._send_json(400, {"error": "missing ?server="})
                return
            token = get_token(server, force=force)
            if token:
                self._send_json(200, {"token": token})
            else:
                self._send_json(503, {"error": "auth failed"})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed  = urlparse(self.path)
        length  = int(self.headers.get("Content-Length", 0))
        body    = json.loads(self.rfile.read(length)) if length else {}

        if parsed.path == "/evict":
            server = body.get("server", "").strip()
            if not server:
                self._send_json(400, {"error": "missing server in body"})
                return
            evicted = evict_token(server, body.get("token") or None)
            self._send_json(200, {"ok": True, "evicted": evicted})
            return

        self._send_json(404, {"error": "not found"})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Camview Token Coordination Server")
    parser.add_argument("--port", type=int, default=int(os.getenv("TOKEN_SERVER_PORT", "8090")))
    parser.add_argument("--host", default=os.getenv("TOKEN_SERVER_HOST", "0.0.0.0"))
    args = parser.parse_args()

    server = _ThreadedHTTPServer((args.host, args.port), _Handler)
    logger.info(f"Camview Token Server listening on {args.host}:{args.port}")
    logger.info(f"  Auth user : {_AUTH_USER}")
    logger.info(f"  Endpoints : GET /token?server=<host>  GET /health  POST /evict")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")


if __name__ == "__main__":
    main()
