"""The one place that turns what happened during a check into UP / DOWN / UNKNOWN + reason.

UP      a frame was captured
DOWN    the camera side failed (no frame, camera/channel error, feed not live)
UNKNOWN shared infrastructure failed (token server, DVR/DSS overload, CDN block), so the check
        says nothing about the camera. Excluded from uptime %.
The raw stage / http_status / api_code are stored with every check so this can be re-mapped.
"""
from __future__ import annotations

from dataclasses import dataclass

UP, DOWN, UNKNOWN = "UP", "DOWN", "UNKNOWN"

# Reasons that mean "try this camera again later in the same pass".
RETRYABLE = frozenset({
    "token_unavailable", "token_rejected", "dvr_unavailable", "dvr_capacity", "dss_timeout",
    "dss_unreachable", "dss_error", "media_unreachable", "stream_token_rejected", "tcs_blocked",
    "portal_timeout", "portal_unreachable", "portal_error",
})


@dataclass
class Attempt:
    stage: str  # config | token | startvideo | probe | ffmpeg | portal
    ok: bool = False
    http_status: int | None = None
    api_code: int | None = None
    detail: str | None = None


def map_outcome(a: Attempt, dvr_recent_up: bool = False, repeat: bool = False) -> tuple[str, str]:
    """`repeat`: this camera already had a retryable failure earlier in the same pass."""
    if a.ok:
        return UP, "ok"
    s, h, c, d = a.stage, a.http_status, a.api_code, a.detail or ""

    if d == "stopped":  # interrupted by shutdown: never recorded
        return UNKNOWN, "stopped"
    if s == "config":
        return UNKNOWN, d or "bad_config"
    if s == "token":
        return UNKNOWN, "token_unavailable"
    if s == "startvideo":
        if h == 401:
            return UNKNOWN, "token_rejected"
        if h == 503:
            return UNKNOWN, "dvr_unavailable"
        if c == 2084:
            # DSS answers 2084 both when the DVR is at capacity and when the camera/DVR cannot be
            # streamed (offline device, no permission). If the same DVR served another camera
            # minutes ago, the channel itself is refused; a refusal that repeats on the retry is
            # not a passing capacity limit either. Only a first, unexplained 2084 is UNKNOWN.
            if dvr_recent_up:
                return DOWN, "no_permission"
            return (DOWN, "refused_2084") if repeat else (UNKNOWN, "dvr_capacity")
        if d == "timeout":
            return UNKNOWN, "dss_timeout"
        if d == "conn_error":
            return UNKNOWN, "dss_unreachable"
        if h is not None and h >= 500:
            return UNKNOWN, "dss_error"
        if h is not None and h != 200:
            return DOWN, f"startvideo_http_{h}"
        return DOWN, f"startvideo_code_{c}" if c is not None else "startvideo_no_urls"
    if s == "probe":
        return UNKNOWN, "media_unreachable"
    if s == "ffmpeg":
        if d == "unauthorized":
            return UNKNOWN, "stream_token_rejected"
        if d == "media_unreachable":
            return UNKNOWN, "media_unreachable"
        if d == "timeout":
            return DOWN, "ffmpeg_timeout"
        if d == "no_stream":
            return DOWN, "no_stream"
        if d.startswith("http_"):
            return DOWN, d
        return DOWN, "no_frame"
    if s == "portal":
        if d == "blocked":
            return UNKNOWN, "tcs_blocked"
        if d == "timeout":
            return UNKNOWN, "portal_timeout"
        if d == "conn_error":
            return UNKNOWN, "portal_unreachable"
        if h is not None and h >= 500:
            return UNKNOWN, "portal_error"
        if d == "hls_extract_failed":
            return DOWN, "hls_extract_failed"
        if h is not None and h != 200:
            return DOWN, f"portal_http_{h}"
        return DOWN, d or "portal_failed"
    return UNKNOWN, d or "unexpected"
