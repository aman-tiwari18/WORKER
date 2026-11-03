"""Camview (Dahua DSS) access through the shared token server.

Read-only use of the fleet session: this service never logs in to a DVR, never calls /evict and
never asks for force=1. Each DVR allows one session per username, and the token server's
keepalive owns that session's health. A rejected token is only dropped from our local cache.

StartVideo request/response handling ported from
script-server/scripts/detection/vision_classification_rtsp.py:559-765 (minus evict/login).
"""
from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger("uptime.camview")

_IPV4_IN_URL = re.compile(r"://(\d+)\.(\d+)\.(\d+)\.(\d+)")
_HOST_PORT = re.compile(r"^[a-z][a-z0-9+.-]*://(?:[^@/]*@)?([^/:?#]+)(?::(\d+))?", re.I)


def is_private_ip_url(url: str) -> bool:
    m = _IPV4_IN_URL.search(url)
    if not m:
        return False
    a, b = int(m.group(1)), int(m.group(2))
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)


def host_port(url: str, default_port: int = 554) -> tuple[str | None, int]:
    m = _HOST_PORT.match(url)
    if not m:
        return None, default_port
    return m.group(1), int(m.group(2)) if m.group(2) else default_port


def tcp_probe(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class TokenClient:
    """Session tokens from camview-token-server, cached locally for a few seconds."""

    def __init__(self, base_url: str, local_ttl: float = 20.0, fail_ttl: float = 30.0,
                 timeout: float = 60.0, session: requests.Session | None = None, clock=time.monotonic):
        self.base = base_url.rstrip("/")
        self.local_ttl, self.fail_ttl, self.timeout = local_ttl, fail_ttl, timeout
        self._http = session or requests.Session()
        self._clock = clock
        self._cache: dict[str, tuple[str | None, float]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._meta = threading.Lock()
        self._last_warn: dict[str, float] = {}

    def _lock(self, server: str) -> threading.Lock:
        with self._meta:
            return self._locks.setdefault(server, threading.Lock())

    def get(self, server: str, refresh: bool = False) -> str | None:
        """Current token for a Camview host, or None. `refresh` skips our local cache only."""
        with self._lock(server):
            tok, ts = self._cache.get(server, (None, 0.0))
            age = self._clock() - ts
            if not refresh:
                if tok and age < self.local_ttl:
                    return tok
                if not tok and ts and age < self.fail_ttl:
                    return None
            try:
                # /token can block 30-40 s while the token server logs in (it holds a lock).
                r = self._http.get(f"{self.base}/token", params={"server": server}, timeout=self.timeout)
                tok = (r.json() or {}).get("token") if r.status_code == 200 else None
                if not tok:
                    self._warn(server, f"token server has no token for {server} (HTTP {r.status_code})")
            except Exception as e:
                tok = None
                self._warn(server, f"token server unreachable for {server}: {e}")
            self._cache[server] = (tok, self._clock())
            return tok

    def drop_local(self, server: str, token: str) -> None:
        with self._lock(server):
            if self._cache.get(server, (None, 0.0))[0] == token:
                self._cache.pop(server, None)

    def health(self) -> dict | None:
        try:
            r = self._http.get(f"{self.base}/health", timeout=5)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    def _warn(self, server: str, msg: str) -> None:
        now = self._clock()
        if now - self._last_warn.get(server, -1e9) >= 60:
            self._last_warn[server] = now
            log.warning(msg)


@dataclass
class StartResult:
    ok: bool
    urls: list[str] = field(default_factory=list)  # stream URLs with ?token= appended, never decoded
    stage: str = "startvideo"  # "token" when no session token was available
    http_status: int | None = None
    api_code: int | None = None
    detail: str | None = None
    ms: int = 0


def _body(res) -> dict:
    try:
        b = res.json()
        return b if isinstance(b, dict) else {}
    except ValueError:
        return {}


def _urls_from(body: dict) -> tuple[list[str], str | None]:
    data = body.get("data") or {}
    if not isinstance(data, dict):
        return [], None
    urls: list[str] = []
    for key in ("url", "url2"):
        raw = data.get(key)
        if raw:
            urls.extend(p.strip() for p in str(raw).split("|") if p.strip())
    return urls, data.get("token")


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class Camview:
    def __init__(self, tokens: TokenClient, stream_type: str = "1", api_timeout: float = 10.0,
                 stop_video: bool = False, session: requests.Session | None = None,
                 probe=tcp_probe, sleep=time.sleep):
        self.tokens = tokens
        self.stream_type = str(stream_type)
        self.api_timeout = api_timeout
        self.stop_enabled = stop_video
        self._http = session or requests.Session()
        self._probe = probe
        self._sleep = sleep
        self._ports: dict[str, int] = {}  # Camview host -> stream port that answered last
        self._streak: dict[str, int] = {}
        self._lock = threading.Lock()

    def _payload(self, method: str, token: str, device_id: str, camera_no: str) -> dict:
        channel = f"{device_id}$1$0${camera_no}"
        if method == "StopVideo":
            return {"clientType": "WINPC_V1", "project": "PSDK", "method": "MTS.Video.StopVideo",
                    "data": {"channelId": channel, "streamType": self.stream_type}}
        return {"clientType": "WINPC_V1", "project": "PSDK", "method": "MTS.Video.StartVideo",
                "data": {"streamType": self.stream_type,
                         "optional": f"/admin/API/MTS/Video/StartVideo?token={token}",
                         "channelId": channel, "dataType": "2", "enableRtsps": "0"}}

    def start_video(self, server: str, device_id: str, camera_no: str) -> StartResult:
        t0 = time.monotonic()

        def ms() -> int:
            return int((time.monotonic() - t0) * 1000)

        token = self.tokens.get(server)
        if not token:
            return StartResult(False, stage="token", detail="token_unavailable", ms=ms())
        for attempt in (1, 2):
            url = f"https://{server}/admin/API/MTS/Video/StartVideo?token={token}"
            headers = {"Content-Type": "application/json", "X-Subject-Token": token}
            try:
                res = self._http.post(url, headers=headers,
                                      data=json.dumps(self._payload("StartVideo", token, device_id, camera_no)),
                                      timeout=self.api_timeout)
            except requests.Timeout:
                return StartResult(False, detail="timeout", ms=ms())
            except requests.RequestException:
                return StartResult(False, detail="conn_error", ms=ms())
            body = _body(res)
            if res.status_code == 401:
                urls, stream_token = _urls_from(body)  # some firmware answers 401 with the stream data
                if urls:
                    return self._finish(urls, stream_token or token, ms())
                if attempt == 1:
                    # Our copy may be stale: re-read the token server's current one (no evict, no force).
                    self.tokens.drop_local(server, token)
                    self._sleep(2)
                    fresh = self.tokens.get(server, refresh=True)
                    if fresh and fresh != token:
                        token = fresh
                        continue
                return StartResult(False, http_status=401, detail="token_rejected", ms=ms())
            if res.status_code != 200:
                return StartResult(False, http_status=res.status_code, detail=f"http_{res.status_code}", ms=ms())
            urls, stream_token = _urls_from(body)
            if not urls:
                return StartResult(False, http_status=200, api_code=_as_int(body.get("code")),
                                   detail="no_urls", ms=ms())
            return self._finish(urls, stream_token or token, ms())
        return StartResult(False, http_status=401, detail="token_rejected", ms=ms())

    @staticmethod
    def _finish(urls: list[str], stream_token: str, elapsed_ms: int) -> StartResult:
        public = [u for u in urls if not is_private_ip_url(u)]
        chosen = public or urls
        # Never URL-decode: the DSS server needs %24 / %26 exactly as sent.
        return StartResult(True, urls=[f"{u}?token={stream_token}" for u in chosen],
                           http_status=200, ms=elapsed_ms)

    def pick_url(self, server: str, urls: list[str]) -> str | None:
        """The URL on the port that answered last for this server, else the first that answers a TCP probe."""
        parsed = [(u, *host_port(u)) for u in urls]
        with self._lock:
            cached = self._ports.get(server)
        if cached is not None:
            for u, _h, p in parsed:
                if p == cached:
                    return u
        for u, h, p in parsed:
            if h and self._probe(h, p):
                with self._lock:
                    self._ports[server] = p
                return u
        return None

    def note_result(self, server: str, ok: bool) -> None:
        """After 10 failed grabs in a row on a server, forget its cached port (re-probe next time)."""
        with self._lock:
            if ok:
                self._streak[server] = 0
                return
            n = self._streak.get(server, 0) + 1
            self._streak[server] = n
            if n >= 10 and server in self._ports:
                log.info("10 failed grabs in a row on %s — re-probing its stream port", server)
                self._ports.pop(server, None)
                self._streak[server] = 0

    def stop_video(self, server: str, device_id: str, camera_no: str) -> None:
        """Only when STOP_VIDEO=1. It targets (shared session, channel, stream type), not our stream."""
        if not self.stop_enabled:
            return
        token = self.tokens.get(server)
        if not token:
            return
        try:
            self._http.post(f"https://{server}/admin/API/MTS/Video/StopVideo?token={token}",
                            headers={"Content-Type": "application/json", "X-Subject-Token": token},
                            data=json.dumps(self._payload("StopVideo", token, device_id, camera_no)),
                            timeout=3)
        except requests.RequestException:
            pass

    def ports(self) -> dict[str, int]:
        with self._lock:
            return dict(self._ports)
