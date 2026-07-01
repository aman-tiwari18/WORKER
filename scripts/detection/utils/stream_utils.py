# utils/stream_utils.py
"""
Thin wrapper around tcs_hls_utils.py — no duplication.
Adds only stream-type detection logic.
"""

import logging

logger = logging.getLogger(__name__)

# Re-export everything from tcs_hls_utils so the rest of the
# codebase only needs to import from here
from tcs_hls_utils import (
    extract_hls_streaming_url,
    test_hls_stream,
    open_hls_stream,
    capture_frame_from_hls,
    capture_frame_with_opencv_hls,
    capture_frame_with_ffmpeg_hls,
    get_hls_url_from_initial_url,
)


# ============================================================
# 🔍 STREAM TYPE DETECTION  (only new logic added here)
# ============================================================

def detect_stream_type(url: str) -> str:
    """
    Look at the URL string and return 'rtsp', 'hls', or 'unknown'.

    Rules (in order):
      1. Starts with rtsp:// or rtsps://  → 'rtsp'
      2. Contains .m3u8                   → 'hls'
      3. Starts with http/https           → 'hls'  (TCS-style page URLs)
      4. Anything else                    → 'unknown'
    """
    if not url or str(url).lower().strip() in ("nan", "none", ""):
        return "unknown"

    lower = url.lower().strip()

    if lower.startswith("rtsp://") or lower.startswith("rtsps://"):
        return "rtsp"

    if ".m3u8" in lower:
        return "hls"

    if lower.startswith("http://") or lower.startswith("https://"):
        return "hls"

    return "unknown"