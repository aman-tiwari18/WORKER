# """
# TCS HLS Stream Utilities
# =========================
# Shared utilities for handling TCS HLS streaming links.
# This module provides functions to extract and access HLS streams from TCS URLs.
# """

# import requests
# import cv2
# import re
# import logging
# import shutil
# import subprocess
# import uuid
# import os

# logger = logging.getLogger(__name__)

# def extract_hls_streaming_url(initial_url, timeout=10):
#     """
#     Extract the actual HLS streaming URL from the initial TCS URL
    
#     When you access the initial URL in a browser, you get a page containing
#     the actual streaming URL. This function extracts that URL.
    
#     Args:
#         initial_url: The URL from the Excel file (CCTV URL column)
#         timeout: Request timeout in seconds
    
#     Returns:
#         The actual HLS streaming URL, or None if extraction fails
#     """
#     logger.info(f"Extracting HLS streaming URL from: {initial_url[:100]}...")
    
#     try:
#         # Make request to the initial URL
#         response = requests.get(initial_url, timeout=timeout, allow_redirects=True)
        
#         if response.status_code != 200:
#             logger.warning(f"Failed to access initial URL. Status code: {response.status_code}")
#             return None
        
#         # The response should contain the actual streaming URL
#         # Try different extraction methods:
        
#         # Method 1: Look for .m3u8 URL in the response text
#         m3u8_pattern = r'https?://[^\s<>"]+?\.m3u8[^\s<>"]*'
#         matches = re.findall(m3u8_pattern, response.text)
        
#         if matches:
#             # Return the first .m3u8 URL found
#             streaming_url = matches[0]
#             logger.info(f"✅ Found HLS streaming URL: {streaming_url[:100]}...")
#             return streaming_url
        
#         # Method 2: Look for URL in JSON response
#         try:
#             json_data = response.json()
#             if isinstance(json_data, dict):
#                 # Look for common keys that might contain the URL
#                 for key in ['url', 'stream_url', 'hls_url', 'playback_url', 'streaming_url']:
#                     if key in json_data:
#                         streaming_url = json_data[key]
#                         logger.info(f"✅ Found HLS streaming URL in JSON (key: {key}): {streaming_url[:100]}...")
#                         return streaming_url
#         except:
#             pass
        
#         # Method 3: Look for any .m3u8 reference in the entire response
#         if '.m3u8' in response.text:
#             logger.debug("Found .m3u8 reference in response, attempting to extract...")
#             # Try to extract URL-like pattern before .m3u8
#             pattern = r'(["\'])(https?://[^"\']+\.m3u8[^"\']*)(["\'])'
#             matches = re.findall(pattern, response.text)
#             if matches:
#                 streaming_url = matches[0][1]
#                 logger.info(f"✅ Found HLS streaming URL (method 3): {streaming_url[:100]}...")
#                 return streaming_url
        
#         logger.warning("Could not find HLS streaming URL in response")
#         logger.debug(f"Response preview: {response.text[:500]}...")
#         return None
        
#     except requests.exceptions.Timeout:
#         logger.warning(f"Timeout accessing initial URL after {timeout}s")
#         return None
#     except Exception as e:
#         logger.error(f"Failed to extract HLS URL: {e}", exc_info=True)
#         return None


# def test_hls_stream(hls_url, timeout=10):
#     """
#     Test if the HLS stream is accessible
    
#     Args:
#         hls_url: The HLS streaming URL (.m3u8)
#         timeout: Request timeout in seconds
    
#     Returns:
#         True if stream is accessible, False otherwise
#     """
#     logger.info(f"Testing HLS stream accessibility: {hls_url[:100]}...")
    
#     try:
#         response = requests.get(hls_url, timeout=timeout, stream=True)
        
#         if response.status_code == 200:
#             logger.info("✅ HLS stream is accessible")
#             return True
#         else:
#             logger.warning(f"HLS stream returned status code: {response.status_code}")
#             return False
            
#     except Exception as e:
#         logger.warning(f"Failed to access HLS stream: {e}")
#         return False


# def capture_frame_from_hls(hls_url, camera_id, save_dir=None, timeout_ms=10000):
#     """
#     Capture a frame from HLS stream using OpenCV or FFmpeg
    
#     Args:
#         hls_url: The HLS streaming URL (.m3u8)
#         camera_id: Unique identifier for this camera
#         save_dir: Directory to save the frame (optional)
#         timeout_ms: Timeout in milliseconds
    
#     Returns:
#         Tuple of (frame, image_path) where frame is the numpy array and image_path is the saved file path
#         Returns (None, None) if capture fails
#     """
#     logger.info(f"Attempting to capture frame from HLS stream for Camera ID {camera_id}")
    
#     # Try with OpenCV first
#     frame, img_path = capture_frame_with_opencv_hls(hls_url, camera_id, save_dir, timeout_ms)
    
#     if frame is not None:
#         return frame, img_path
    
#     # Fallback to FFmpeg if OpenCV fails
#     logger.info("OpenCV capture failed, trying FFmpeg...")
#     frame, img_path = capture_frame_with_ffmpeg_hls(hls_url, camera_id, save_dir)
    
#     return frame, img_path


# def capture_frame_with_opencv_hls(hls_url, camera_id, save_dir=None, timeout_ms=10000):
#     """Capture frame using OpenCV"""
#     logger.info(f"Capturing frame with OpenCV for Camera ID {camera_id}")
    
#     try:
#         cap = cv2.VideoCapture(hls_url)
        
#         # Set timeout
#         cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms)
#         cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms)
        
#         if not cap.isOpened():
#             logger.warning(f"Failed to open HLS stream with OpenCV for Camera ID {camera_id}")
#             return None, None
        
#         # Read frame
#         ret, frame = cap.read()
#         cap.release()
        
#         if not ret or frame is None:
#             logger.warning(f"Failed to read frame from HLS stream for Camera ID {camera_id}")
#             return None, None
        
#         # Save frame if save_dir is provided
#         img_path = None
#         if save_dir:
#             os.makedirs(save_dir, exist_ok=True)
#             img_path = os.path.join(save_dir, f"frame_{uuid.uuid4().hex}.jpg")
#             cv2.imwrite(img_path, frame)
#             logger.info(f"✅ Frame captured with OpenCV: {img_path}")
#         else:
#             logger.info(f"✅ Frame captured with OpenCV (not saved)")
        
#         return frame, img_path
        
#     except Exception as e:
#         logger.warning(f"OpenCV capture failed: {e}")
#         return None, None


# def capture_frame_with_ffmpeg_hls(hls_url, camera_id, save_dir=None):
#     """Capture frame using FFmpeg"""
#     logger.info(f"Attempting FFmpeg frame capture for Camera ID {camera_id}")
    
#     if not shutil.which("ffmpeg"):
#         logger.warning("ffmpeg executable not found on PATH")
#         return None, None
    
#     # Create temporary file for FFmpeg output
#     if save_dir:
#         os.makedirs(save_dir, exist_ok=True)
#         img_path = os.path.join(save_dir, f"frame_{uuid.uuid4().hex}.jpg")
#     else:
#         img_path = f"/tmp/tcs_frame_{uuid.uuid4().hex}.jpg"
    
#     cmd = [
#         "ffmpeg",
#         "-y",
#         "-timeout", "10000000",  # 10 seconds in microseconds
#         "-i", hls_url,
#         "-frames:v", "1",
#         "-loglevel", "error",
#         img_path
#     ]
    
#     try:
#         logger.debug(f"Running FFmpeg command...")
#         result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        
#         if result.returncode != 0:
#             logger.warning(f"FFmpeg exited with code {result.returncode}")
#             logger.debug(f"FFmpeg stderr: {result.stderr.decode(errors='ignore')[:500]}")
#             return None, None
        
#         if not os.path.exists(img_path):
#             logger.warning("FFmpeg did not create output frame")
#             return None, None
        
#         # Read the frame
#         frame = cv2.imread(img_path)
#         if frame is None:
#             logger.warning("Failed to read frame created by FFmpeg")
#             return None, None
        
#         logger.info(f"✅ Frame captured with FFmpeg: {img_path}")
        
#         # If save_dir was not provided, delete the temporary file and return frame only
#         if not save_dir:
#             os.remove(img_path)
#             img_path = None
        
#         return frame, img_path
        
#     except subprocess.TimeoutExpired:
#         logger.warning("FFmpeg timed out after 15 seconds")
#         return None, None
#     except Exception as e:
#         logger.error(f"FFmpeg failed: {e}")
#         return None, None


# def open_hls_stream(hls_url, timeout_ms=10000):
#     """
#     Open an HLS stream for continuous reading
    
#     Args:
#         hls_url: The HLS streaming URL (.m3u8)
#         timeout_ms: Timeout in milliseconds
    
#     Returns:
#         cv2.VideoCapture object if successful, None otherwise
#     """
#     logger.info(f"Opening HLS stream: {hls_url[:100]}...")
    
#     backends = [
#         (cv2.CAP_FFMPEG, "FFmpeg"),
#         (cv2.CAP_GSTREAMER, "GStreamer"),
#         (cv2.CAP_ANY, "Any available")
#     ]
    
#     for backend, backend_name in backends:
#         try:
#             logger.debug(f"Trying {backend_name} backend...")
#             cap = cv2.VideoCapture(hls_url, backend)
#             cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
#             cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms)
#             cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
            
#             if cap.isOpened():
#                 ret, test_frame = cap.read()
#                 if ret and test_frame is not None:
#                     logger.info(f"✅ Successfully opened HLS stream with {backend_name} backend")
#                     return cap
#                 else:
#                     logger.debug(f"{backend_name} backend opened but cannot read frames")
#                     cap.release()
#             else:
#                 logger.debug(f"Failed to open with {backend_name} backend")
#                 if cap:
#                     cap.release()
#         except Exception as e:
#             logger.debug(f"Error with {backend_name} backend: {e}")
#             if 'cap' in locals() and cap:
#                 cap.release()
    
#     logger.error("Failed to open HLS stream with any backend")
#     return None


# def get_hls_url_from_initial_url(initial_url, timeout=10):
#     """
#     Convenience function that combines extraction and testing
    
#     Args:
#         initial_url: The initial TCS URL
#         timeout: Request timeout in seconds
    
#     Returns:
#         The HLS URL if accessible, None otherwise
#     """
#     hls_url = extract_hls_streaming_url(initial_url, timeout)
#     if hls_url and test_hls_stream(hls_url, timeout):
#         return hls_url
#     return None










"""
TCS HLS Stream Utilities
=========================
Shared utilities for handling TCS HLS streaming links.
This module provides functions to extract and access HLS streams from TCS URLs.
"""

import requests
import cv2
import re
import logging
import shutil
import subprocess
import select
import time
import uuid
import os
import numpy as np

logger = logging.getLogger(__name__)


# ── FFmpeg open/read timeout for OpenCV's FFmpeg backend ────────────────────
# OpenCV IGNORES cap.set(CAP_PROP_OPEN_TIMEOUT_MSEC) for the FFmpeg HLS demuxer,
# so a stale segment or a slow CDN response makes VideoCapture(url) block for the
# FFmpeg default (~30s) — under round-robin fan-out that stalls capture slots and
# starves the GPU. The only knob FFmpeg honours here is the `timeout` demuxer
# option (microseconds), passed via OPENCV_FFMPEG_CAPTURE_OPTIONS BEFORE the
# VideoCapture is constructed. Merge it in without clobbering an existing value
# (master_pipeline sets `rtsp_transport;tcp` for the RTSP path).
_HLS_OPEN_TIMEOUT_US = str(int(os.environ.get("HLS_FFMPEG_TIMEOUT_US", "12000000")))  # 12s
def _ensure_ffmpeg_capture_timeout():
    cur = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS", "")
    parts = [p for p in cur.split("|") if p]
    if not any(p.split(";")[0] == "timeout" for p in parts):
        parts.append(f"timeout;{_HLS_OPEN_TIMEOUT_US}")
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "|".join(parts)
_ensure_ffmpeg_capture_timeout()

# ── HLS capture strategy ───────────────────────────────────────────────────
# cv2.VideoCapture's FFmpeg HLS demuxer ignores read timeouts, so a stalling
# segment wedges the reader ~30s and cannot be interrupted from Python. The
# subprocess-pipe path (HLSPipeCapture) runs our own ffmpeg with -timeout/
# -rw_timeout (which it DOES honour) and a select()-guarded reader, so a dead
# stream returns control in a few seconds and the process is hard-killable.
#
# BUT: this worker process holds a live CUDA context (the YOLO model), and
# fork()-ing a subprocess out of a CUDA-loaded, multi-threaded process is slow
# and gets slower under concurrent forks (measured: cv2 opens in ~1s p50, but
# with the pipe as the PRIMARY path under round-robin fan-out, open latency hit
# p90=11s / max=46s — worse than the 30s reads it was meant to avoid). So: cv2
# stays primary (fast, no fork); the pipe is only the FALLBACK for cameras cv2
# genuinely can't open, where paying the fork cost occasionally is cheap.
# Set HLS_USE_FFMPEG_PIPE=1 to make the pipe primary instead (fine for a
# capture-only process with no CUDA context loaded).
HLS_USE_FFMPEG_PIPE   = os.environ.get("HLS_USE_FFMPEG_PIPE", "0").strip().lower() not in ("0", "false", "no", "off", "")
HLS_PIPE_W           = int(os.environ.get("HLS_PIPE_WIDTH",  "1280"))
HLS_PIPE_H           = int(os.environ.get("HLS_PIPE_HEIGHT", "720"))
HLS_PIPE_READ_TIMEOUT = float(os.environ.get("HLS_PIPE_READ_TIMEOUT", "10"))  # seconds per frame


# ── FFmpeg binary resolution ────────────────────────────────────────────────
# mp.Process(spawn) does NOT inherit PATH reliably — so we search common
# locations explicitly instead of relying only on shutil.which.
_FFMPEG_SEARCH_PATHS = [
    "/usr/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
    "/snap/bin/ffmpeg",
    "/bin/ffmpeg",
]

def _find_ffmpeg():
    """Return absolute path to ffmpeg binary, or None if not found."""
    # Try PATH first
    found = shutil.which("ffmpeg")
    if found:
        return found
    # Fallback: common install locations
    for path in _FFMPEG_SEARCH_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None

def _find_ffprobe():
    """Return absolute path to ffprobe binary, or None if not found."""
    found = shutil.which("ffprobe")
    if found:
        return found
    for path in _FFMPEG_SEARCH_PATHS:
        probe = path.replace("ffmpeg", "ffprobe")
        if os.path.isfile(probe) and os.access(probe, os.X_OK):
            return probe
    return None

FFMPEG_BIN  = _find_ffmpeg()
FFPROBE_BIN = _find_ffprobe()

if FFMPEG_BIN:
    logger.debug(f"ffmpeg found at: {FFMPEG_BIN}")
else:
    logger.warning("⚠️ ffmpeg binary not found — HLS pipe fallback will be unavailable")


def extract_hls_streaming_url(initial_url, timeout=10):
    """
    Extract the actual HLS streaming URL from the initial TCS URL.
    """
    logger.info(f"Extracting HLS streaming URL from: {initial_url[:100]}...")

    try:
        response = requests.get(initial_url, timeout=timeout, allow_redirects=True)

        if response.status_code != 200:
            logger.warning(f"Failed to access initial URL. Status code: {response.status_code}")
            return None

        # Method 1: Look for .m3u8 URL in the response text
        m3u8_pattern = r'https?://[^\s<>"]+?\.m3u8[^\s<>"]*'
        matches = re.findall(m3u8_pattern, response.text)
        if matches:
            streaming_url = matches[0]
            logger.info(f"✅ Found HLS streaming URL: {streaming_url[:100]}...")
            return streaming_url

        # Method 2: Look for URL in JSON response
        try:
            json_data = response.json()
            if isinstance(json_data, dict):
                for key in ['url', 'stream_url', 'hls_url', 'playback_url', 'streaming_url']:
                    if key in json_data:
                        streaming_url = json_data[key]
                        logger.info(f"✅ Found HLS streaming URL in JSON (key: {key}): {streaming_url[:100]}...")
                        return streaming_url
        except Exception:
            pass

        # Method 3: Look for any .m3u8 reference in the entire response
        if '.m3u8' in response.text:
            pattern = r'(["\'])(https?://[^"\']+\.m3u8[^"\']*)(["\'])'
            matches = re.findall(pattern, response.text)
            if matches:
                streaming_url = matches[0][1]
                logger.info(f"✅ Found HLS streaming URL (method 3): {streaming_url[:100]}...")
                return streaming_url

        logger.warning("Could not find HLS streaming URL in response")
        logger.debug(f"Response preview: {response.text[:500]}...")
        return None

    except requests.exceptions.Timeout:
        logger.warning(f"Timeout accessing initial URL after {timeout}s")
        return None
    except Exception as e:
        logger.error(f"Failed to extract HLS URL: {e}", exc_info=True)
        return None


def test_hls_stream(hls_url, timeout=10):
    """
    Test if the HLS stream is accessible.
    """
    logger.info(f"Testing HLS stream accessibility: {hls_url[:100]}...")
    try:
        response = requests.get(hls_url, timeout=timeout, stream=True)
        if response.status_code == 200:
            logger.info("✅ HLS stream is accessible")
            return True
        else:
            logger.warning(f"HLS stream returned status code: {response.status_code}")
            return False
    except Exception as e:
        logger.warning(f"Failed to access HLS stream: {e}")
        return False


def capture_frame_from_hls(hls_url, camera_id, save_dir=None, timeout_ms=10000):
    """
    Capture a frame from HLS stream using OpenCV or FFmpeg.
    """
    logger.info(f"Attempting to capture frame from HLS stream for Camera ID {camera_id}")

    frame, img_path = capture_frame_with_opencv_hls(hls_url, camera_id, save_dir, timeout_ms)
    if frame is not None:
        return frame, img_path

    logger.info("OpenCV capture failed, trying FFmpeg...")
    frame, img_path = capture_frame_with_ffmpeg_hls(hls_url, camera_id, save_dir)
    return frame, img_path


def capture_frame_with_opencv_hls(hls_url, camera_id, save_dir=None, timeout_ms=10000):
    """Capture frame using OpenCV."""
    logger.info(f"Capturing frame with OpenCV for Camera ID {camera_id}")
    try:
        cap = cv2.VideoCapture(hls_url)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms)

        if not cap.isOpened():
            logger.warning(f"Failed to open HLS stream with OpenCV for Camera ID {camera_id}")
            return None, None

        ret, frame = cap.read()
        cap.release()

        if not ret or frame is None:
            logger.warning(f"Failed to read frame from HLS stream for Camera ID {camera_id}")
            return None, None

        img_path = None
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)
            img_path = os.path.join(save_dir, f"frame_{uuid.uuid4().hex}.jpg")
            cv2.imwrite(img_path, frame)
            logger.info(f"✅ Frame captured with OpenCV: {img_path}")
        else:
            logger.info("✅ Frame captured with OpenCV (not saved)")

        return frame, img_path

    except Exception as e:
        logger.warning(f"OpenCV capture failed: {e}")
        return None, None


def capture_frame_with_ffmpeg_hls(hls_url, camera_id, save_dir=None):
    """Capture frame using FFmpeg."""
    logger.info(f"Attempting FFmpeg frame capture for Camera ID {camera_id}")

    if not FFMPEG_BIN:
        logger.warning("ffmpeg binary not found on system")
        return None, None

    if save_dir:
        os.makedirs(save_dir, exist_ok=True)
        img_path = os.path.join(save_dir, f"frame_{uuid.uuid4().hex}.jpg")
    else:
        img_path = f"/tmp/tcs_frame_{uuid.uuid4().hex}.jpg"

    cmd = [
        FFMPEG_BIN, "-y",
        "-allowed_extensions", "ALL",
        "-timeout", "10000000",
        "-i", hls_url,
        "-frames:v", "1",
        "-loglevel", "error",
        img_path
    ]

    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)

        if result.returncode != 0:
            logger.warning(f"FFmpeg exited with code {result.returncode}")
            logger.debug(f"FFmpeg stderr: {result.stderr.decode(errors='ignore')[:500]}")
            return None, None

        if not os.path.exists(img_path):
            logger.warning("FFmpeg did not create output frame")
            return None, None

        frame = cv2.imread(img_path)
        if frame is None:
            logger.warning("Failed to read frame created by FFmpeg")
            return None, None

        logger.info(f"✅ Frame captured with FFmpeg: {img_path}")

        if not save_dir:
            os.remove(img_path)
            img_path = None

        return frame, img_path

    except subprocess.TimeoutExpired:
        logger.warning("FFmpeg timed out after 15 seconds")
        return None, None
    except Exception as e:
        logger.error(f"FFmpeg failed: {e}")
        return None, None


# ============================================================
# 🔧 FFmpeg pipe helpers (used by open_hls_stream fallback)
# ============================================================

def _get_stream_resolution(hls_url):
    """Use ffprobe to detect stream resolution. Returns (width, height) or safe default."""
    if not FFPROBE_BIN:
        logger.warning("ffprobe not found — using default resolution 1280x720")
        return 1280, 720
    try:
        cmd = [
            FFPROBE_BIN, "-v", "error",
            "-allowed_extensions", "ALL",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            hls_url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split(",")
            if len(parts) == 2:
                w, h = int(parts[0]), int(parts[1])
                logger.info(f"✅ ffprobe resolution: {w}x{h}")
                return w, h
    except Exception as e:
        logger.warning(f"ffprobe failed: {e}")
    return 1280, 720


class HLSPipeCapture:
    """
    Drop-in replacement for cv2.VideoCapture for HLS.
    Wraps an FFmpeg subprocess pipe and exposes .read() / .release().

    read() accumulates one raw BGR frame with a select()-guarded loop and a hard
    per-frame deadline, so a stalled stream returns (False, None) in
    HLS_PIPE_READ_TIMEOUT seconds instead of blocking forever. release() kills the
    ffmpeg process outright.
    """

    def __init__(self, pipe, width, height, read_timeout=HLS_PIPE_READ_TIMEOUT):
        self._pipe   = pipe
        self._width  = width
        self._height = height
        self._frame_size = width * height * 3
        self._read_timeout = read_timeout
        self._fd = pipe.stdout.fileno()
        self._opened = True

    def isOpened(self):
        return self._opened and self._pipe.poll() is None

    def read(self):
        need = self._frame_size
        buf = bytearray()
        deadline = time.time() + self._read_timeout
        try:
            while len(buf) < need:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self._opened = False
                    return False, None
                r, _, _ = select.select([self._fd], [], [], min(remaining, 2.0))
                if not r:
                    if self._pipe.poll() is not None:      # ffmpeg exited/timed out
                        self._opened = False
                        return False, None
                    continue
                chunk = os.read(self._fd, need - len(buf))
                if not chunk:                              # EOF
                    self._opened = False
                    return False, None
                buf.extend(chunk)
            frame = np.frombuffer(bytes(buf), np.uint8).reshape((self._height, self._width, 3))
            return True, frame.copy()
        except Exception:
            self._opened = False
            return False, None

    def release(self):
        self._opened = False
        try:
            self._pipe.kill()
            self._pipe.wait(timeout=3)
        except Exception:
            pass
        try:
            self._pipe.stdout.close()
        except Exception:
            pass

    def get(self, prop_id):
        """Minimal cv2.VideoCapture.get() compatibility."""
        if prop_id == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self._width)
        if prop_id == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self._height)
        return 0.0

    def set(self, prop_id, value):
        return False  # Not supported for pipe


def _open_hls_pipe(hls_url):
    """Open the stream via our own ffmpeg subprocess → HLSPipeCapture.

    ffmpeg is given -timeout/-rw_timeout (honoured, unlike cv2) and scales to a
    fixed size so no ffprobe round-trip is needed and a wrong-resolution guess
    can't corrupt frames. Returns HLSPipeCapture or None.
    """
    if not FFMPEG_BIN:
        return None
    w, h = HLS_PIPE_W, HLS_PIPE_H
    cmd = [
        FFMPEG_BIN, "-nostdin", "-loglevel", "error",
        "-allowed_extensions", "ALL",
        "-timeout", _HLS_OPEN_TIMEOUT_US,       # socket I/O timeout (µs) — dead CDN fails fast
        "-rw_timeout", _HLS_OPEN_TIMEOUT_US,
        "-i", hls_url,
        "-an", "-sn",
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
               f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-"
    ]
    try:
        pipe = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)
    except Exception as e:
        logger.error(f"FFmpeg pipe spawn failed: {e}")
        return None

    cap = HLSPipeCapture(pipe, w, h)
    ret, frame = cap.read()          # prove it actually produces a frame
    if ret and frame is not None:
        logger.info(f"✅ HLS stream opened via ffmpeg pipe ({w}x{h})")
        return cap
    cap.release()
    return None


def open_hls_stream(hls_url, timeout_ms=10000):
    """
    Open an HLS stream for continuous reading. Returns an object exposing
    .read()/.release() (HLSPipeCapture or cv2.VideoCapture), or None.

    Order is controlled by HLS_USE_FFMPEG_PIPE (default: pipe first, cv2 fallback).
    The cv2 FFmpeg backend ignores read timeouts and wedges ~30s on a stalled
    segment; the pipe path fails fast and is killable.
    """
    logger.info(f"Opening HLS stream: {hls_url[:100]}...")
    _ensure_ffmpeg_capture_timeout()

    def _try_cv2(url):
        try:
            c = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            c.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            c.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms)
            c.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
            if c.isOpened():
                ret, fr = c.read()
                if ret and fr is not None:
                    logger.info("✅ Successfully opened HLS stream with cv2 FFmpeg backend")
                    return c
            c.release()
        except Exception as e:
            logger.debug(f"cv2 FFmpeg backend error: {e}")
            try:
                c.release()
            except Exception:
                pass
        return None

    order = (_open_hls_pipe, _try_cv2) if HLS_USE_FFMPEG_PIPE else (_try_cv2, _open_hls_pipe)
    for opener in order:
        cap = opener(hls_url)
        if cap is not None:
            return cap
    logger.error("Could not open HLS stream (pipe + cv2 both failed)")
    return None


def get_hls_url_from_initial_url(initial_url, timeout=10):
    """
    Convenience function that combines extraction and testing.
    """
    hls_url = extract_hls_streaming_url(initial_url, timeout)
    if hls_url and test_hls_stream(hls_url, timeout):
        return hls_url
    return None