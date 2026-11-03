"""One-frame grabs with the system ffmpeg, the blank-frame flag and the latest-frame file.

The ffmpeg command follows script-server/scripts/detection/vision_classification_rtsp.py:1065-1101,
with two fixes: only "401 Unauthorized" counts as a token rejection (a bare "401" can appear in a
URL or IP), and a frame on stdout always wins. ffmpeg runs under `nice` in its own process group
so a hung grab is killed with its children; every live child is tracked for shutdown.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("uptime.capture")

_UNAUTHORIZED = re.compile(r"401 Unauthorized", re.I)
_UNREACHABLE = re.compile(r"Connection refused|No route to host|Network is unreachable", re.I)
_NO_STREAM = re.compile(r"Invalid data found when processing input", re.I)  # DSS had no stream for it
_HTTP_CODE = re.compile(r"Server returned (\d{3})|HTTP error (\d{3})", re.I)
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class Grab:
    ok: bool
    jpeg: bytes | None = None
    detail: str | None = None  # unauthorized | media_unreachable | no_stream | http_NNN | timeout | no_frame
    ms: int = 0
    stderr_tail: str = ""


def stderr_reason(text: str) -> str:
    if _UNAUTHORIZED.search(text):
        return "unauthorized"
    if _UNREACHABLE.search(text):
        return "media_unreachable"
    if _NO_STREAM.search(text):
        return "no_stream"
    m = _HTTP_CODE.search(text)
    if m:
        return f"http_{m.group(1) or m.group(2)}"
    if re.search(r"timed out|timeout", text, re.I):
        return "timeout"
    return "no_frame"


def ffmpeg_major(binary: str = "ffmpeg") -> int | None:
    try:
        out = subprocess.run([binary, "-version"], capture_output=True, text=True, errors="replace",
                             timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"ffmpeg version n?(\d+)\.", out)
    return int(m.group(1)) if m else None


def rtsp_timeout_option(binary: str = "ffmpeg") -> str:
    """Socket timeout option of the RTSP demuxer. ffmpeg < 5 calls it -stimeout; there -timeout means
    "wait for an incoming connection" and puts ffmpeg in listen mode (every grab fails instantly with
    "Unable to open RTSP for listening"). ffmpeg 5+ renamed -stimeout to -timeout."""
    major = ffmpeg_major(binary)
    return "-stimeout" if major is not None and major < 5 else "-timeout"


class Ffmpeg:
    def __init__(self, binary: str = "ffmpeg", nice: int = 10, width: int = 640, wall_margin_s: float = 4.0):
        self.binary, self.nice, self.width, self.wall_margin_s = binary, nice, width, wall_margin_s
        self._children: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self.rtsp_timeout_opt = rtsp_timeout_option(binary)

    def command(self, url: str, rtsp: bool, timeout_s: int) -> list[str]:
        cmd = ["nice", "-n", str(self.nice), self.binary, "-nostdin", "-hide_banner", "-loglevel", "error"]
        if rtsp:
            cmd += ["-rtsp_transport", "tcp", "-allowed_media_types", "video", self.rtsp_timeout_opt,
                    str(timeout_s * 1_000_000)]
        else:
            cmd += ["-rw_timeout", str(timeout_s * 1_000_000)]
        return cmd + ["-i", url, "-frames:v", "1", "-vf", f"scale='min({self.width},iw)':-2",
                      "-q:v", "5", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]

    def grab(self, url: str, rtsp: bool, timeout_s: int) -> Grab:
        t0 = time.monotonic()
        try:
            p = subprocess.Popen(self.command(url, rtsp, timeout_s), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except OSError as e:
            return Grab(False, detail="no_frame", stderr_tail=f"spawn failed: {e}")
        with self._lock:
            self._children.add(p)
        try:
            try:
                out, err = p.communicate(timeout=timeout_s + self.wall_margin_s)
                timed_out = False
            except subprocess.TimeoutExpired:
                self._kill(p)
                out, err = p.communicate()
                timed_out = True
        finally:
            with self._lock:
                self._children.discard(p)
        ms = int((time.monotonic() - t0) * 1000)
        text = (err or b"").decode(errors="replace")
        tail = text.strip()[-300:]
        if out and not timed_out and p.returncode == 0:
            return Grab(True, jpeg=out, ms=ms, stderr_tail=tail)
        if timed_out:
            return Grab(False, detail="timeout", ms=ms, stderr_tail=tail)
        return Grab(False, detail=stderr_reason(text), ms=ms, stderr_tail=tail)

    @staticmethod
    def _kill(p: subprocess.Popen) -> None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                p.kill()
            except ProcessLookupError:
                pass

    def kill_all(self) -> int:
        with self._lock:
            live = list(self._children)
        for p in live:
            self._kill(p)
        return len(live)


def blank_frame(jpeg: bytes) -> bool:
    """Uniform / black / white frame (typically a DVR "no video" screen). Pixel statistics only:
    no model, no label. Undecodable data counts as blank."""
    try:
        from PIL import Image, ImageStat

        with Image.open(io.BytesIO(jpeg)) as img:
            st = ImageStat.Stat(img.convert("L"))
        mean, std = st.mean[0], st.stddev[0]
    except Exception:
        return True
    return std < 5 or mean < 10 or mean > 245


def frame_rel_path(exam_id: int, exam_date, shift_id: int, camera_key: str) -> str:
    slug = _SAFE.sub("_", camera_key)[:80].strip("_")
    digest = hashlib.sha1(camera_key.encode()).hexdigest()[:8]
    return f"{exam_id}/{exam_date}/{shift_id}/{slug}_{digest}.jpg"


def save_frame(root: str, rel: str, jpeg: bytes) -> bool:
    """Write atomically (temp + rename) so the admin page never reads a half-written file."""
    path = Path(root) / rel
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(jpeg)
        os.replace(tmp, path)
        return True
    except OSError as e:
        log.warning("Frame save failed (%s): %s", rel, e)
        return False
