"""Cameras of a live run, keyed by physical camera (one capture per physical camera).

Every camera of the exam is included whatever its classification status: the uptime check is
independent of the vision pipeline. The centre schedule (exam_centre_schedule /
exam_slot_schedule) is applied with the same rule as script-server/utils/db_client.py:719-784.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

import pymysql

from .db import ER_NO_SUCH_TABLE
from .schedule import Window

log = logging.getLogger("uptime.roster")

HLS_LANE = "hls"

SCHEDULE_CLAUSE = """
  AND (NOT EXISTS (SELECT 1 FROM exam_slot_schedule ss0 WHERE ss0.exam_id = c.exam_id)
       OR EXISTS (SELECT 1 FROM exam_slot_schedule ss WHERE ss.exam_id = c.exam_id
                  AND ss.exam_date = %s AND ss.shift_id = %s AND ss.is_running = 1))
  AND (NOT EXISTS (SELECT 1 FROM exam_centre_schedule cs0 WHERE cs0.exam_id = c.exam_id)
       OR EXISTS (SELECT 1 FROM exam_centre_schedule cs WHERE cs.exam_id = c.exam_id
                  AND cs.tc_code = c.tc_code AND cs.exam_date = %s AND cs.shift_id = %s
                  AND cs.is_running = 1))
"""

RTSP_SQL = (
    "SELECT c.id, c.device_id, c.camera_no, c.camera_name, c.centre_name, c.tc_code, c.city, "
    "c.state, c.cctv_url FROM camera_links_rtsp c WHERE c.exam_id = %s AND c.is_active = 1"
)
HLS_SQL = (
    "SELECT c.id, c.ch_code, c.camera_name, c.centre_name, c.tc_code, c.city, c.state, c.cctv_url, "
    "c.shift_id, c.exam_date FROM camera_links_hls c WHERE c.exam_id = %s AND c.is_active = 1 "
    "AND (c.shift_id = %s OR c.shift_id IS NULL) AND (c.exam_date = %s OR c.exam_date IS NULL)"
)


@dataclass
class Member:
    """One live run that contains the camera, with that run's camera-row details."""
    run_key: tuple
    camera_row_id: int | None
    camera_name: str | None
    centre_name: str | None
    tc_code: str | None
    city: str | None
    state: str | None


@dataclass
class Camera:
    key: str  # physical camera key (ASCII)
    link_type: str  # "RTSP" or "HLS"
    lane: str  # Camview host for RTSP, "hls" for HLS
    dvr_key: str  # host|device_id for RTSP, tcs|tc_code for HLS
    server: str  # Camview host or HLS portal host
    device_id: str = ""
    camera_no: str = ""
    url: str = ""  # HLS portal URL, or a plain rtsp:// URL
    members: list[Member] = field(default_factory=list)


def _ascii(v) -> str:
    return quote(str(v if v is not None else "").strip(), safe="._-:@")


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def url_host(url) -> str:
    raw = str(url or "").strip()
    if "://" not in raw:
        raw = "//" + raw
    try:
        return (urlparse(raw).hostname or "").lower()
    except ValueError:
        return ""


def _member(row: dict, run_key) -> Member:
    return Member(
        run_key=run_key,
        camera_row_id=int(row["id"]) if row.get("id") is not None else None,
        camera_name=_clean(row.get("camera_name")),
        centre_name=_clean(row.get("centre_name")),
        tc_code=_clean(row.get("tc_code")),
        city=_clean(row.get("city")),
        state=_clean(row.get("state")),
    )


def rtsp_camera(row: dict, run_key) -> Camera:
    url = str(row.get("cctv_url") or "").strip()
    host = url_host(url)
    dev = str(row.get("device_id") or "").strip()
    ch = str(row.get("camera_no") or "").strip()
    if url.lower().startswith("rtsp://"):
        # A plain stream URL (no Camview API): key on the URL itself.
        key = f"rtspurl|{_ascii(host)}|{hashlib.sha1(url.encode()).hexdigest()[:16]}"
        return Camera(key=key, link_type="RTSP", lane=host or "rtsp", dvr_key=host or "rtsp",
                      server=host, url=url, members=[_member(row, run_key)])
    key = f"rtsp|{_ascii(host)}|{_ascii(dev)}|{_ascii(ch)}"
    return Camera(key=key, link_type="RTSP", lane=host, dvr_key=f"{_ascii(host)}|{_ascii(dev)}",
                  server=host, device_id=dev, camera_no=ch, members=[_member(row, run_key)])


def hls_camera(row: dict, run_key) -> Camera:
    url = str(row.get("cctv_url") or "").strip()
    host = url_host(url)
    ch = str(row.get("ch_code") or "").strip()
    key = f"hls|{_ascii(host)}|{_ascii(ch)}" if ch else f"hls|row|{row.get('id')}"
    return Camera(key=key, link_type="HLS", lane=HLS_LANE, dvr_key=f"tcs|{_ascii(row.get('tc_code'))}",
                  server=host, url=url, members=[_member(row, run_key)])


def _hls_rank(row: dict) -> tuple:
    # Exact date/shift rows win over NULL ("all") rows; then the newest row.
    return (row.get("shift_id") is not None) + (row.get("exam_date") is not None), int(row.get("id") or 0)


def build_run_roster(rows: list[dict], w: Window) -> dict[str, Camera]:
    out: dict[str, Camera] = {}
    if w.link_type == "RTSP":
        for row in rows:
            cam = rtsp_camera(row, w.key)
            out.setdefault(cam.key, cam)
        return out
    best: dict[str, dict] = {}
    for row in rows:
        key = hls_camera(row, w.key).key
        if key not in best or _hls_rank(row) > _hls_rank(best[key]):
            best[key] = row
    nulls = sum(1 for r in rows if r.get("shift_id") is None or r.get("exam_date") is None)
    if 0 < nulls < len(rows):
        log.warning("Exam %s mixes per-shift/date HLS rows with NULL (all) rows (%d of %d NULL)",
                    w.exam_id, nulls, len(rows))
    for key, row in best.items():
        out[key] = hls_camera(row, w.key)
    return out


def load_run_roster(db, w: Window) -> dict[str, Camera] | None:
    """Cameras scheduled for this run, or None when the query failed (keep the previous roster)."""
    sched = (w.exam_date, w.shift_id, w.exam_date, w.shift_id)
    if w.link_type == "RTSP":
        base, params = RTSP_SQL, (w.exam_id,)
    else:
        base, params = HLS_SQL, (w.exam_id, w.shift_id, w.exam_date)
    try:
        try:
            rows = db.query(base + SCHEDULE_CLAUSE, params + sched)
        except pymysql.err.ProgrammingError as e:
            if not (e.args and e.args[0] == ER_NO_SUCH_TABLE):
                raise
            log.warning("Schedule tables missing — exam %s roster loaded without the centre schedule", w.exam_id)
            rows = db.query(base, params)
    except Exception as e:  # any other failure: never fall back to "every camera"
        log.error("Roster query failed for exam %s shift %s %s: %s — keeping previous roster",
                  w.exam_id, w.shift_id, w.exam_date, e)
        return None
    return build_run_roster(rows, w)


def shard_of(dvr_key: str, count: int) -> int:
    """Stable shard (0..count-1) of a DVR / centre; the same on every worker."""
    return int(hashlib.md5(dvr_key.encode("utf-8")).hexdigest()[:8], 16) % count


class Shard:
    """This instance's place in the fleet: fixed (UPTIME_SHARD_INDEX / _COUNT) or, with auto sharding, recomputed
    from the live instances in camera_uptime_instances. Mutable, unlike Settings."""

    def __init__(self, index: int = 0, count: int = 1):
        self.shard_index, self.shard_count = index, max(1, count)

    def set(self, index: int, count: int) -> bool:
        count = max(1, count)
        index = index if 0 <= index < count else 0
        changed = (index, count) != (self.shard_index, self.shard_count)
        self.shard_index, self.shard_count = index, count
        return changed


def owns_dvr(settings, dvr_key: str) -> bool:
    """This instance's share: with UPTIME_SHARD_COUNT > 1 only the DVRs that hash to UPTIME_SHARD_INDEX."""
    return settings.shard_count <= 1 or shard_of(dvr_key, settings.shard_count) == settings.shard_index


def in_scope(cam: Camera, settings, shard=None) -> bool:
    """Cameras this service is configured to check (ONLY_SERVERS pilot, INCLUDE_HLS, its shard).
    `shard` (a Shard) overrides the fixed shard in settings."""
    if not owns_dvr(shard or settings, cam.dvr_key):
        return False
    if cam.lane == HLS_LANE:
        return settings.include_hls
    return not settings.only_servers or cam.lane in settings.only_servers


def merge_rosters(rosters: dict[tuple, dict[str, Camera]]) -> dict[str, Camera]:
    """Union of all live runs; a camera in several runs is checked once, results go to each run."""
    merged: dict[str, Camera] = {}
    for run_key, cams in rosters.items():
        for key, cam in cams.items():
            if key in merged:
                merged[key].members.extend(m for m in cam.members if m.run_key == run_key)
            else:
                merged[key] = Camera(**{**cam.__dict__, "members": list(cam.members)})
    return merged


class RosterGuard:
    """Applies a new roster unless it jumps by more than `threshold` against the applied one.

    While a centre schedule is being re-uploaded the tables are briefly empty, and "no rows" means
    "no restriction" (the whole exam). A big jump is only applied when the next refresh repeats it.
    """

    def __init__(self, threshold: float = 0.2):
        self.threshold = threshold
        self.applied: dict[str, Camera] | None = None
        self._pending_size: int | None = None

    def offer(self, new: dict[str, Camera]) -> bool:
        old_n, new_n = len(self.applied or {}), len(new)
        if not self.applied or abs(new_n - old_n) <= self.threshold * old_n:
            self.applied, self._pending_size = new, None
            return True
        if self._pending_size is not None and abs(new_n - self._pending_size) <= max(1.0, 0.05 * self._pending_size):
            self.applied, self._pending_size = new, None
            return True
        self._pending_size = new_n
        log.warning("Roster jumped %d -> %d cameras; waiting for the next refresh to confirm", old_n, new_n)
        return False
