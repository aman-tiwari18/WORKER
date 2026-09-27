"""Turn a camera config into stream URIs, and name its server/device for budgets.

RTSP goes through DSS exactly like the round-robin path (login token, StartVideo,
cached for STARTVIDEO_CACHE_TTL). The DSS helpers are passed in from
master_pipeline rather than imported, so this never re-imports the running
__main__ module (which would duplicate its signal handlers and STOP flag).
"""
import logging
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def camera_key(cfg, link_type):
    if link_type == "HLS":
        return "hls_" + str(cfg.get("ch_no") or cfg.get("camera_no") or cfg.get("camera_id"))
    dev = cfg.get("camera_id") or cfg.get("device_id") or ""
    return f"{dev}_{cfg.get('ch_no') or cfg.get('camera_no') or '1'}"


class Resolver:
    """helpers (RTSP): get_server_token, get_rtsp_cached, order_urls, add_token_to_url,
    is_private_ip_url, rr_resolve, startvideo_cache, startvideo_cache_lock."""

    def __init__(self, link_type, helpers=None):
        self.link_type = link_type
        self.h = helpers or {}

    def server(self, cfg):
        if str(cfg.get("cctv_url") or "").startswith("file://"):
            return "file"
        if self.link_type == "HLS":
            url = cfg.get("cctv_url") or cfg.get("stream_url") or cfg.get("url", "")
            return urlparse(str(url)).netloc or "hls"
        return self.h["rr_resolve"](cfg) or cfg.get("camera_id") or cfg.get("device_id", "")

    def device(self, cfg):
        if self.link_type == "HLS":
            return None  # one HLS origin serves every camera; no per-recorder limit
        return str(cfg.get("camera_id") or cfg.get("device_id") or "")

    def resolve(self, cfg):
        """Candidate URIs, best first. [] if the camera can't be reached now."""
        direct = str(cfg.get("cctv_url") or cfg.get("stream_url") or "")
        if direct.startswith("file://"):          # recorded video (testing)
            return [direct]
        return self._resolve_hls(cfg) if self.link_type == "HLS" else self._resolve_rtsp(cfg)

    def invalidate(self, cfg):
        """Forget the cached StartVideo result so the next resolve gets a fresh token."""
        cache = self.h.get("startvideo_cache")
        if cache is None:
            return
        with self.h["startvideo_cache_lock"]:
            cache.pop(camera_key(cfg, "RTSP"), None)

    def _resolve_rtsp(self, cfg):
        h = self.h
        direct = str(cfg.get("cctv_url") or cfg.get("stream_url") or "")
        if direct.startswith("rtsp://"):
            return [direct]
        server = self.server(cfg)
        token = h["get_server_token"](server)
        if not token:
            return []
        device = cfg.get("camera_id") or cfg.get("device_id", "")
        cam_no = cfg.get("ch_no") or cfg.get("camera_no", "1")
        result = h["get_rtsp_cached"](camera_key(cfg, "RTSP"), device, cam_no, token, server)
        if not result:
            return []
        urls, stream_token = result
        return [h["add_token_to_url"](u, stream_token) for u in h["order_urls"](urls)
                if not h["is_private_ip_url"](u)]

    def _resolve_hls(self, cfg):
        from tcs_hls_utils import extract_hls_streaming_url, test_hls_stream
        initial = cfg.get("cctv_url") or cfg.get("stream_url") or cfg.get("url", "")
        hls = extract_hls_streaming_url(initial) if initial else None
        return [hls] if hls and test_hls_stream(hls) else []
