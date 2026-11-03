"""One availability check of one camera: frame captured -> UP. No classification of any kind."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from .camview import Camview
from .capture import Ffmpeg, blank_frame
from .hls import HlsClient, TokenBucket
from .outcomes import UP, Attempt, map_outcome
from .roster import Camera

log = logging.getLogger("uptime.checker")


@dataclass
class CheckResult:
    result: str
    reason: str
    stage: str
    http_status: int | None = None
    api_code: int | None = None
    frame_blank: bool | None = None
    startvideo_ms: int | None = None
    ffmpeg_ms: int | None = None
    total_ms: int = 0
    jpeg: bytes | None = None
    note: str = ""  # stderr tail / detail, for logs only


class Checker:
    def __init__(self, settings, camview: Camview, hls: HlsClient, ffmpeg: Ffmpeg, bucket: TokenBucket):
        self.s = settings
        self.camview, self.hls, self.ffmpeg, self.bucket = camview, hls, ffmpeg, bucket

    def check(self, cam: Camera, dvr_recent_up: bool = False, stop: threading.Event | None = None,
              repeat: bool = False) -> CheckResult:
        t0 = time.monotonic()
        res = self._rtsp(cam, dvr_recent_up, repeat) if cam.link_type == "RTSP" else self._hls(cam, stop)
        res.total_ms = int((time.monotonic() - t0) * 1000)
        return res

    def _done(self, attempt: Attempt, dvr_recent_up: bool = False, repeat: bool = False, **kw) -> CheckResult:
        result, reason = map_outcome(attempt, dvr_recent_up, repeat)
        return CheckResult(result=result, reason=reason, stage=attempt.stage,
                           http_status=attempt.http_status, api_code=attempt.api_code, **kw)

    def _frame(self, jpeg: bytes, **kw) -> CheckResult:
        blank = blank_frame(jpeg) if self.s.blank_check else None
        return CheckResult(result=UP, reason="ok", stage="ffmpeg", frame_blank=blank, jpeg=jpeg, **kw)

    def _rtsp(self, cam: Camera, dvr_recent_up: bool, repeat: bool = False) -> CheckResult:
        if cam.url.lower().startswith("rtsp://"):
            g = self.ffmpeg.grab(cam.url, rtsp=True, timeout_s=self.s.ffmpeg_timeout_s)
            if g.ok:
                return self._frame(g.jpeg, ffmpeg_ms=g.ms)
            return self._done(Attempt("ffmpeg", detail=g.detail), ffmpeg_ms=g.ms, note=g.stderr_tail)
        if not cam.server or not cam.device_id or not cam.camera_no:
            return self._done(Attempt("config", detail="missing_device_or_channel"))

        sv = self.camview.start_video(cam.server, cam.device_id, cam.camera_no)
        if not sv.ok:
            return self._done(Attempt(sv.stage, http_status=sv.http_status, api_code=sv.api_code, detail=sv.detail),
                              dvr_recent_up, repeat, startvideo_ms=sv.ms)
        sv_ms = sv.ms
        try:
            url = self.camview.pick_url(cam.server, sv.urls)
            if url is None:
                return self._done(Attempt("probe", detail="media_unreachable"), startvideo_ms=sv_ms)
            g = self.ffmpeg.grab(url, rtsp=True, timeout_s=self.s.ffmpeg_timeout_s)
            ff_ms = g.ms
            if not g.ok and g.detail == "unauthorized":
                # The stream token was refused: one fresh StartVideo, one more grab.
                sv2 = self.camview.start_video(cam.server, cam.device_id, cam.camera_no)
                sv_ms += sv2.ms
                if sv2.ok:
                    url2 = self.camview.pick_url(cam.server, sv2.urls)
                    if url2 is not None:
                        g = self.ffmpeg.grab(url2, rtsp=True, timeout_s=self.s.ffmpeg_timeout_s)
                        ff_ms += g.ms
            self.camview.note_result(cam.server, g.ok)
            if g.ok:
                return self._frame(g.jpeg, startvideo_ms=sv_ms, ffmpeg_ms=ff_ms)
            return self._done(Attempt("ffmpeg", detail=g.detail), startvideo_ms=sv_ms, ffmpeg_ms=ff_ms,
                              note=g.stderr_tail)
        finally:
            self.camview.stop_video(cam.server, cam.device_id, cam.camera_no)  # no-op unless STOP_VIDEO=1

    def _hls(self, cam: Camera, stop: threading.Event | None) -> CheckResult:
        if not cam.url:
            return self._done(Attempt("config", detail="missing_url"))
        r = self.hls.resolve(cam.url, stop)
        if not r.ok:
            return self._done(Attempt("portal", http_status=r.http_status, detail=r.detail), startvideo_ms=r.ms)
        # ffmpeg fetches the playlist and at least one segment: pay for two requests up front.
        if not self.bucket.take(2, stop):
            return self._done(Attempt("portal", detail="stopped"), startvideo_ms=r.ms)
        g = self.ffmpeg.grab(r.m3u8, rtsp=False, timeout_s=self.s.hls_ffmpeg_timeout_s)
        if g.ok:
            return self._frame(g.jpeg, startvideo_ms=r.ms, ffmpeg_ms=g.ms)
        return self._done(Attempt("ffmpeg", detail=g.detail), startvideo_ms=r.ms, ffmpeg_ms=g.ms, note=g.stderr_tail)
