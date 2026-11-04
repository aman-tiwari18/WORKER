"""Settings, read once from the environment and the .env next to the package.

Values already present in the process environment (pm2) win over the .env file.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

from dotenv import load_dotenv

SERVICE_DIR = Path(__file__).resolve().parent.parent


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _first(*names: str, default: str = "") -> str:
    """First non-empty of several env names (UPTIME_DB_* beats the worker's own DB_* in the shared .env)."""
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return default


def _hosts(name: str) -> tuple[str, ...]:
    return tuple(p.strip().lower() for p in os.getenv(name, "").split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    # MySQL
    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = ""
    db_password: str = ""
    db_name: str = "cctv_monitoring"

    # On/off from the Fleet page (UPTIME_ENABLED). Off = the process stays up but checks nothing.
    enabled: bool = True

    # Camview through the shared token server
    token_server: str = "http://localhost:8090"
    camview_api_timeout_s: float = 10.0
    rtsp_stream_type: str = "1"
    stop_video: bool = False

    # Pacing and backoffs
    max_inflight_per_server: int = 1
    dvr_min_gap_s: float = 60.0
    max_inflight_per_dvr: int = 1
    frame_save_threads: int = 8  # frames are saved off the check threads (NFS can be slow)
    max_new_streams_per_min: int = 20
    hls_req_per_s: float = 0.5  # THIS instance's rate (the fleet-wide HLS_REQ_PER_S divided by shard_count)
    hls_req_per_s_fleet: float = 0.5  # the fleet-wide rate; auto sharding divides it by the live instance count
    only_servers: tuple[str, ...] = ()
    include_hls: bool = True
    dvr_unavailable_backoff_s: float = 120.0
    dvr_capacity_backoff_s: float = 60.0
    token_backoff_s: float = 400.0
    tcs_block_pause_s: float = 1800.0
    # A lane whose pass ran this long may start the next one while only refusing DVRs still have cameras
    # left; those carry over (unchecked first) instead of holding back every other DVR of the lane.
    carry_over_after_s: float = 600.0

    # Sharding: one instance per worker, each checks only the DVRs whose hash falls on its index
    # (a DVR is always handled by exactly one instance, so the per-DVR pacing stays valid fleet-wide).
    shard_index: int = 0
    shard_count: int = 1
    worker_id: str = ""
    # Auto sharding (UPTIME_SHARD_COUNT=0, the default in the worker image): every instance heartbeats into
    # camera_uptime_instances and takes its place in the sorted list of live instances, so adding or removing a
    # server rebalances the DVRs within a minute or two, with no settings to change.
    shard_auto: bool = False
    instance_ttl_s: int = 180

    # Windows and refresh
    start_hold_min: int = 5
    max_window_hours: int = 16
    schedule_refresh_s: int = 60
    roster_refresh_s: int = 300
    pause_during_vision: bool = True

    # Capture
    ffmpeg_timeout_s: int = 6
    hls_ffmpeg_timeout_s: int = 8
    hls_portal_timeout_s: float = 8.0
    frame_width: int = 640
    blank_check: bool = True
    save_frames: bool = True
    frames_root: str = "/data/nfs/cctv_images/uptime_frames"
    # UP + already labelled RTSP camera -> Accessible + newest frame in the Cameras tab (uptime/promote.py)
    promote_to_cameras: bool = True
    classification_frames_root: str = "/data/nfs/cctv_images/classification_frames"

    # Ops
    http_host: str = "127.0.0.1"
    http_port: int = 8095
    pause_file: str = ""
    min_mem_available_gb: float = 8.0
    spool_dir: str = str(SERVICE_DIR / "spool")
    lock_file: str = str(SERVICE_DIR / ".camera-uptime.lock")
    log_level: str = "INFO"

    def public(self) -> dict:
        """Settings without secrets (stored in each run row, shown by /status)."""
        d = asdict(self)
        d.pop("db_password", None)
        d["only_servers"] = list(self.only_servers)
        return d


def load(env_file: str | os.PathLike | None = None) -> Settings:
    load_dotenv(env_file or SERVICE_DIR / ".env", override=False)
    raw_count = _str("UPTIME_SHARD_COUNT", "1").lower()
    shard_auto = raw_count in ("0", "auto")
    shard_count = 1 if shard_auto else max(1, int(raw_count))
    shard_index = 0 if shard_auto else _int("UPTIME_SHARD_INDEX", 0)
    hls_fleet = min(1.0, max(0.05, _float("HLS_REQ_PER_S", 0.5)))
    if not 0 <= shard_index < shard_count:
        raise ValueError(f"UPTIME_SHARD_INDEX={shard_index} must be in 0..{shard_count - 1}")
    return Settings(
        db_host=_first("UPTIME_DB_HOST", "DB_HOST", default="localhost"),
        db_port=int(_first("UPTIME_DB_PORT", "DB_PORT", default="3306")),
        db_user=_first("UPTIME_DB_USER", "DB_USER"),
        db_password=os.getenv("UPTIME_DB_PASSWORD") or os.getenv("DB_PASSWORD", ""),
        db_name=_first("UPTIME_DB_NAME", "DB_NAME", default="cctv_monitoring"),
        enabled=_bool("UPTIME_ENABLED", True),
        token_server=_str("CAMVIEW_TOKEN_SERVER", "http://localhost:8090").rstrip("/"),
        camview_api_timeout_s=_float("CAMVIEW_API_TIMEOUT_S", 10.0),
        rtsp_stream_type=_str("RTSP_STREAM_TYPE", "1") or "1",
        stop_video=_bool("STOP_VIDEO", False),
        max_inflight_per_server=max(1, _int("MAX_INFLIGHT_PER_SERVER", 1)),
        dvr_min_gap_s=_float("DVR_MIN_GAP_S", 60.0),
        max_inflight_per_dvr=max(1, _int("MAX_INFLIGHT_PER_DVR", 1)),
        frame_save_threads=max(1, _int("FRAME_SAVE_THREADS", 8)),
        max_new_streams_per_min=max(1, _int("MAX_NEW_STREAMS_PER_MIN", 20)),
        # HLS_REQ_PER_S is the fleet-wide rate (TCS: never more than 1/s from the shared egress IP);
        # every shard takes its equal part.
        hls_req_per_s=hls_fleet / shard_count,
        hls_req_per_s_fleet=hls_fleet,
        only_servers=_hosts("ONLY_SERVERS"),
        include_hls=_bool("INCLUDE_HLS", True),
        dvr_unavailable_backoff_s=_float("DVR_UNAVAILABLE_BACKOFF_S", 120.0),
        dvr_capacity_backoff_s=_float("DVR_CAPACITY_BACKOFF_S", 60.0),
        token_backoff_s=_float("TOKEN_BACKOFF_S", 400.0),
        tcs_block_pause_s=_float("TCS_BLOCK_PAUSE_S", 1800.0),
        carry_over_after_s=max(60.0, _float("CARRY_OVER_AFTER_S", 600.0)),
        start_hold_min=_int("START_HOLD_MIN", 5),
        max_window_hours=_int("MAX_WINDOW_HOURS", 16),
        schedule_refresh_s=max(10, _int("SCHEDULE_REFRESH_S", 60)),
        roster_refresh_s=max(30, _int("ROSTER_REFRESH_S", 300)),
        pause_during_vision=_bool("PAUSE_DURING_VISION", True),
        ffmpeg_timeout_s=max(2, _int("FFMPEG_TIMEOUT_S", 6)),
        hls_ffmpeg_timeout_s=max(2, _int("HLS_FFMPEG_TIMEOUT_S", 8)),
        hls_portal_timeout_s=_float("HLS_PORTAL_TIMEOUT_S", 8.0),
        frame_width=max(160, _int("FRAME_WIDTH", 640)),
        blank_check=_bool("BLANK_CHECK", True),
        save_frames=_bool("SAVE_FRAMES", True),
        frames_root=_str("FRAMES_ROOT", "/data/nfs/cctv_images/uptime_frames"),
        promote_to_cameras=_bool("PROMOTE_TO_CAMERAS", True),
        classification_frames_root=_str("CLASSIFICATION_FRAMES_ROOT", "/data/nfs/cctv_images/classification_frames"),
        shard_index=shard_index,
        shard_count=shard_count,
        worker_id=_first("UPTIME_WORKER_ID", "WORKER_NAME", "WORKER_ID"),
        shard_auto=shard_auto,
        instance_ttl_s=max(60, _int("UPTIME_INSTANCE_TTL_S", 180)),
        http_host=_str("HTTP_HOST", "127.0.0.1"),
        http_port=_int("HTTP_PORT", 8095),
        pause_file=_str("PAUSE_FILE"),
        min_mem_available_gb=_float("MIN_MEM_AVAILABLE_GB", 8.0),
        spool_dir=_str("SPOOL_DIR", str(SERVICE_DIR / "spool")),
        lock_file=_str("LOCK_FILE", str(SERVICE_DIR / ".camera-uptime.lock")),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
    )
