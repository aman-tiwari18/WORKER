"""HLS (TCS iON) access: portal page -> .m3u8, paced per HTTP request.

TCS/Akamai blocked the shared egress IP after a burst probe, so every request this service makes
(portal GET, redirects, and ffmpeg's playlist + segment fetches) is paid for from one token bucket
that never exceeds 1 request/s. Extraction ported from script-server/ops/check_hls_links.py:92-101
and scripts/detection/tcs/tcs_hls_utils.py:427-478.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

import requests

_M3U8_BARE = re.compile(r'https?://[^\s<>"]+?\.m3u8[^\s<>"]*')
_M3U8_QUOTED = re.compile(r'(["\'])(https?://[^"\']+\.m3u8[^"\']*)(["\'])')
_JSON_KEYS = ("url", "stream_url", "hls_url", "playback_url", "streaming_url")
_BLOCK = re.compile(r"client ip|access denied|reference #|errors\.edgesuite", re.I)


class TokenBucket:
    """`rate` tokens per second, up to `capacity`. take() blocks; charge() pays after the fact."""

    def __init__(self, rate: float, capacity: float | None = None, clock=time.monotonic, sleep=time.sleep):
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else max(1.0, rate))
        self._tokens = self.capacity
        self._t = clock()
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._t) * self.rate)
        self._t = now

    def take(self, n: float = 1.0, stop: threading.Event | None = None) -> bool:
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= n or (n > self.capacity and self._tokens >= self.capacity):
                    self._tokens -= n
                    return True
                wait = (min(n, self.capacity) - self._tokens) / self.rate
            if stop is not None and stop.is_set():
                return False
            self._sleep(min(wait, 0.5))

    def charge(self, n: float) -> None:
        with self._lock:
            self._refill()
            self._tokens -= n


def extract_m3u8(text: str, json_obj=None) -> str | None:
    m = _M3U8_BARE.findall(text or "")
    if m:
        return m[0]
    if isinstance(json_obj, dict):
        for k in _JSON_KEYS:
            v = json_obj.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
    if ".m3u8" in (text or ""):
        q = _M3U8_QUOTED.findall(text)
        if q:
            return q[0][1]
    return None


def is_block(status: int | None, text: str) -> bool:
    return status in (400, 403) and bool(_BLOCK.search(text or ""))


@dataclass
class Resolve:
    ok: bool
    m3u8: str | None = None
    http_status: int | None = None
    detail: str | None = None
    ms: int = 0


class HlsClient:
    def __init__(self, bucket: TokenBucket, timeout: float = 8.0, session: requests.Session | None = None):
        self.bucket = bucket
        self.timeout = timeout
        self._http = session or requests.Session()

    def resolve(self, portal_url: str, stop: threading.Event | None = None) -> Resolve:
        t0 = time.monotonic()

        def ms() -> int:
            return int((time.monotonic() - t0) * 1000)

        if not self.bucket.take(1, stop):
            return Resolve(False, detail="stopped", ms=ms())
        try:
            r = self._http.get(portal_url, timeout=self.timeout, allow_redirects=True)
        except requests.Timeout:
            return Resolve(False, detail="timeout", ms=ms())
        except requests.RequestException:
            return Resolve(False, detail="conn_error", ms=ms())
        if r.history:
            self.bucket.charge(len(r.history))  # each redirect was another request
        text = r.text or ""
        if is_block(r.status_code, text):
            return Resolve(False, http_status=r.status_code, detail="blocked", ms=ms())
        if r.status_code != 200:
            return Resolve(False, http_status=r.status_code, detail=f"portal_http_{r.status_code}", ms=ms())
        try:
            obj = r.json() if "json" in (r.headers.get("Content-Type") or "") else None
        except ValueError:
            obj = None
        m3u8 = extract_m3u8(text, obj)
        if not m3u8:
            return Resolve(False, http_status=200, detail="hls_extract_failed", ms=ms())
        return Resolve(True, m3u8=m3u8, http_status=200, ms=ms())
