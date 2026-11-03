import io
import os
import stat
import time

from PIL import Image

from uptime.capture import Ffmpeg, blank_frame, frame_rel_path, save_frame, stderr_reason

FAKE = """#!/usr/bin/env python3
import os, sys, time
mode = os.environ.get("FAKE_FFMPEG_MODE", "jpeg")
open(os.environ["FAKE_FFMPEG_PIDFILE"], "w").write(str(os.getpid()))
if mode == "jpeg":
    sys.stdout.buffer.write(open(os.environ["FAKE_FFMPEG_JPEG"], "rb").read())
elif mode == "hang":
    time.sleep(60)
elif mode == "401":
    sys.stderr.write("[rtsp @ 0x1] method DESCRIBE failed: 401 Unauthorized\\n"); sys.exit(1)
elif mode == "ip401":
    sys.stderr.write("rtsp://10.4.0.1:9100/cameraid=401: Invalid data found\\n"); sys.exit(1)
"""


def jpeg_bytes(noise: bool) -> bytes:
    img = Image.effect_noise((64, 48), 80) if noise else Image.new("L", (64, 48), 128)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG")
    return buf.getvalue()


def setup_fake(tmp_path, monkeypatch, mode):
    exe = tmp_path / "ffmpeg"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    jpg = tmp_path / "f.jpg"
    jpg.write_bytes(jpeg_bytes(True))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_FFMPEG_MODE", mode)
    monkeypatch.setenv("FAKE_FFMPEG_JPEG", str(jpg))
    monkeypatch.setenv("FAKE_FFMPEG_PIDFILE", str(tmp_path / "pid"))
    return tmp_path / "pid"


def test_grab_returns_frame(tmp_path, monkeypatch):
    setup_fake(tmp_path, monkeypatch, "jpeg")
    g = Ffmpeg().grab("rtsp://x/y?token=t", rtsp=True, timeout_s=2)
    assert g.ok and g.jpeg.startswith(b"\xff\xd8")


def test_hung_ffmpeg_is_killed_and_reaped(tmp_path, monkeypatch):
    pidfile = setup_fake(tmp_path, monkeypatch, "hang")
    t0 = time.monotonic()
    g = Ffmpeg(wall_margin_s=0.5).grab("rtsp://x/y", rtsp=True, timeout_s=1)
    assert not g.ok and g.detail == "timeout" and time.monotonic() - t0 < 5
    pid = int(pidfile.read_text())
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive  # killed with its process group, and reaped (no zombie)


def test_only_real_401_counts_as_unauthorized(tmp_path, monkeypatch):
    setup_fake(tmp_path, monkeypatch, "401")
    assert Ffmpeg().grab("rtsp://x", rtsp=True, timeout_s=2).detail == "unauthorized"
    setup_fake(tmp_path, monkeypatch, "ip401")
    assert Ffmpeg().grab("rtsp://x", rtsp=True, timeout_s=2).detail == "no_frame"


def test_command_shape():
    rtsp = Ffmpeg(width=640).command("rtsp://h/p?token=t", True, 6)
    assert rtsp[:3] == ["nice", "-n", "10"] and "-rtsp_transport" in rtsp and "6000000" in rtsp
    assert rtsp[rtsp.index("-i") + 1] == "rtsp://h/p?token=t"  # passed as-is, never decoded
    hls = Ffmpeg().command("https://h/a.m3u8", False, 8)
    assert "-rw_timeout" in hls and "-rtsp_transport" not in hls


def test_stderr_reasons():
    assert stderr_reason("Connection to tcp://1.2.3.4:9100 failed: Connection refused") == "media_unreachable"
    assert stderr_reason("Server returned 404 Not Found") == "http_404"
    assert stderr_reason("rtsp: timeout") == "timeout"
    assert stderr_reason("Error opening input files: Invalid data found when processing input") == "no_stream"
    assert stderr_reason("weird") == "no_frame"


def test_blank_frame():
    assert blank_frame(jpeg_bytes(False))
    assert not blank_frame(jpeg_bytes(True))
    assert blank_frame(b"not a jpeg")


def test_save_frame_atomic(tmp_path):
    rel = frame_rel_path(46, "2026-11-02", 169, "rtsp|d14.camview.io|1000796|17")
    assert rel.startswith("46/2026-11-02/169/rtsp_d14.camview.io_1000796_17_") and rel.endswith(".jpg")
    assert save_frame(str(tmp_path), rel, b"abc") and save_frame(str(tmp_path), rel, b"xyz")
    assert (tmp_path / rel).read_bytes() == b"xyz"
    assert not [p for p in (tmp_path / rel).parent.iterdir() if p.name.endswith(".tmp")]


def test_rtsp_timeout_option_follows_ffmpeg_version(monkeypatch):
    import subprocess as sp

    from uptime import capture

    def fake(version):
        return lambda *a, **k: sp.CompletedProcess(a, 0, stdout=f"ffmpeg version {version} Copyright\n", stderr="")

    monkeypatch.setattr(capture.subprocess, "run", fake("4.4.2-0ubuntu0.22.04.1"))
    assert capture.rtsp_timeout_option() == "-stimeout"  # 4.x: -timeout would mean listen mode
    monkeypatch.setattr(capture.subprocess, "run", fake("6.1.1-3ubuntu5"))
    assert capture.rtsp_timeout_option() == "-timeout"
    monkeypatch.setattr(capture.subprocess, "run", fake("n7.0"))
    assert capture.rtsp_timeout_option() == "-timeout"
