"""
tcs_hls_utils — HLS stream helpers (re-export shim).

The implementation lives in ``tcs/tcs_hls_utils.py``. It used to be duplicated
here and the two copies drifted: the round-robin work hardened the ``tcs/`` copy
while every worker kept importing THIS path, so none of that hardening ran.

The difference that mattered, measured 2026-09-09:

  The old copy set its open timeout with
  ``cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, ...)``, which OpenCV silently
  IGNORES for the FFmpeg HLS demuxer. A slow open therefore blocked for
  FFmpeg's ~30s default. Under round-robin that outlives the slot's
  slice+grace budget, so the visit is abandoned — but the capture thread is
  still wedged inside cap.read() and only releases its origin-limiter slot in
  its ``finally``. Above ~8 concurrent slots those threads accumulate faster
  than they drain, the limiter is fully consumed and the pool deadlocks:

      8 slots   -> ok 213/224 (95%),  stuck 6
    128 slots   -> ok  14/196  (9%),  stuck 146, live pinned at 128, no progress

  The ``tcs/`` implementation passes ``timeout`` (microseconds) through
  OPENCV_FFMPEG_CAPTURE_OPTIONS before the capture is constructed, which is the
  only knob FFmpeg honours here, so a bad open fails fast and frees its slot.

Re-exporting rather than copying keeps exactly one implementation, so the two
cannot drift again. ``tcs`` resolves as a namespace package because the workers
put this directory (DETECTION_ROOT) on sys.path before importing.

Public API is unchanged — same three names, same signatures — so no caller
needs touching.
"""

from tcs.tcs_hls_utils import (  # noqa: F401
    extract_hls_streaming_url,
    test_hls_stream,
    open_hls_stream,
)

__all__ = ["extract_hls_streaming_url", "test_hls_stream", "open_hls_stream"]
