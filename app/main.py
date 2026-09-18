#main.py
import asyncio
import re
import os
import time
import shutil
import openpyxl
from openpyxl.styles import Font
from pydantic import BaseModel
from datetime import datetime, timedelta, time as dt_time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote
from typing import Optional, List
import socketio
import requests
import json
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import httpx

from app.config import HTTP_PORT, MAIN_BACKEND_URL, POLL_INTERVAL_MS, SCRIPTS_DIR, WORKER_ID, WORKER_NAME
from app.logger import logger
from app.log_manager import log_manager
from app.poller import poller_service, IS_MAIN_WORKER
from app.gpu_monitor import get_local_gpu_stats_data
from scripts.detection.utils.db_client import get_hls_camera_by_ch_code, get_rtsp_camera_by_id, get_hls_cameras, get_rtsp_cameras, get_rtsp_camera_by_device_and_ch, _get_connection

# Import legacy pipeline functionality from the old backend
from scripts.detection.backend.schemas import (
    StartRequest, StopRequest, StatusResponse as AnalyticsStatusResponse, MessageResponse
)
from scripts.detection.backend.pipeline_manager import (
    start_analytics, stop_analytics, status_analytics,
    cleanup_old_pipelines, processes, ALL_ANALYTICS
)

# How often the watch loop wakes. Each wake does a cheap INCREMENTAL scan
# (only files newer than the last scan, via directory-mtime pruning), so this
# can be short without re-walking the whole ~200k-file history every time.
DETECTION_SCAN_INTERVAL_SECONDS = 3
# Periodically fall back to a FULL rescan so deletions / anything the
# incremental pass missed are reconciled into the snapshot + date index.
DETECTION_FULL_RESCAN_SECONDS = 300
# Look-back overlap (s) applied to the incremental mtime cutoff so we never miss
# a file because of clock skew or NFS attribute-cache lag. The snapshot diff
# (new = recent − known) prevents the overlap from re-emitting anything.
DETECTION_SCAN_MTIME_OVERLAP = 30
known_detection_files = {}
# Secondary index: type_key → date_str → Set[tagged_path]
# Allows O(k) date-filtered lookups instead of O(N) full scans.
known_detection_by_date: dict = {}

# ==========================================
# GPU Monitoring Configuration
# ==========================================
# Load GPU workers from environment or use defaults
def _load_gpu_workers():
    """Load GPU worker configuration from environment variables or defaults."""
    import json
    
    # Try to load from JSON environment variable first
    gpu_workers_json = os.getenv("GPU_WORKERS_JSON")
    if gpu_workers_json:
        try:
            return json.loads(gpu_workers_json)
        except json.JSONDecodeError:
            logger.warning("Failed to parse GPU_WORKERS_JSON, using defaults")
    
    # Build from individual environment variables
    workers = []
    
    # Master/Main worker (always localhost) - uses main HTTP_PORT, not separate port
    workers.append({
        "id": WORKER_ID,
        "name": WORKER_NAME,
        "ip": "localhost",
        "port": HTTP_PORT  # Use main server port
    })
    
    # Additional workers from environment
    total_workers = int(os.getenv("TOTAL_WORKERS", 4))
    for i in range(1, total_workers):
        worker_ip = os.getenv(f"WORKER_{i}_IP")
        if worker_ip:
            workers.append({
                "id": f"worker-{i}",
                "name": os.getenv(f"WORKER_{i}_NAME", f"Worker {i}"),
                "ip": worker_ip,
                "port": int(os.getenv(f"WORKER_{i}_PORT", HTTP_PORT))  # Default to HTTP_PORT
            })
    
    # If no additional workers configured, use hardcoded defaults for backward compatibility
    if len(workers) == 1:
        workers.extend([
            {"id": "worker-1", "name": "Worker 1", "ip": "192.168.19.43", "port": HTTP_PORT},
            {"id": "worker-2", "name": "Worker 2", "ip": "192.168.19.42", "port": HTTP_PORT},
            {"id": "worker-3", "name": "Worker 3", "ip": "192.168.19.41", "port": HTTP_PORT},
        ])
    
    return workers

GPU_WORKERS = _load_gpu_workers()

# Per-worker state for flap suppression: track consecutive failures and last good data
_worker_fail_count: dict[str, int] = {}
_worker_last_good: dict[str, dict] = {}
_WORKER_OFFLINE_THRESHOLD = 2  # mark offline only after this many consecutive failures


def _epoch_ms(value) -> Optional[int]:
    if not value:
        return None
    if hasattr(value, "timestamp"):
        return int(value.timestamp() * 1000)
    try:
        numeric = float(value)
        return int(numeric * 1000) if numeric < 10_000_000_000 else int(numeric)
    except Exception:
        try:
            return int(datetime.fromisoformat(str(value)).timestamp() * 1000)
        except Exception:
            return None


# In-memory camera index — bulk-loaded at startup by _init_camera_index().
# Keys: (device_id, camera_no) for RTSP; ch_no str for HLS.
# This replaces per-request DB queries (50 queries × 27 ms = 1.4 s → 50 × 0 ms = ~0 ms).
_rtsp_cam_index: dict = {}
_hls_cam_index: dict = {}


def _init_camera_index():
    """Bulk-load all active cameras into memory in a single query per table."""
    global _rtsp_cam_index, _hls_cam_index
    try:
        conn = _get_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT device_id, camera_no, camera_name, centre_name, city, state, "
                "tc_code, cctv_url, classification FROM camera_links_rtsp WHERE is_active=1"
            )
            rtsp_rows = cur.fetchall()
            for row in rtsp_rows:
                dev = str(row.get("device_id") or "")
                cam = str(row.get("camera_no") or "")
                if dev and cam:
                    _rtsp_cam_index[(dev, cam)] = row
                if cam:
                    _rtsp_cam_index.setdefault(("", cam), row)

            cur.execute(
                "SELECT exam_id, shift_id, ch_code, camera_name, centre_name, city, state, "
                "tc_code, cctv_url, camera_category FROM camera_links_hls WHERE is_active=1"
            )
            for row in cur.fetchall():
                ch = str(row.get("ch_code") or "")
                if ch:
                    _hls_cam_index[ch] = row
        conn.close()
        logger.info(f"Camera index: {len(rtsp_rows)} RTSP rows → {len(_rtsp_cam_index)} keys")
    except Exception as e:
        logger.error(f"Camera index init failed: {e}")


def _camera_meta_for_ch(ch_no: str, exam_date_iso: str = "") -> dict:
    if not ch_no:
        return {}
    hit = _hls_cam_index.get(ch_no)
    if hit is not None:
        return hit
    # Fallback to DB if index empty (e.g. init failed)
    return get_hls_camera_by_ch_code(ch_no, exam_date_iso) or {}


@lru_cache(maxsize=4096)
def _camera_meta_for_rtsp_pk(camera_id_pk: int) -> dict:
    if not camera_id_pk:
        return {}
    return get_rtsp_camera_by_id(camera_id_pk) or {}


def _camera_meta_for_rtsp_device_ch(device_id: str, camera_no: str) -> dict:
    if not camera_no:
        return {}
    if device_id:
        hit = _rtsp_cam_index.get((device_id, camera_no))
        if hit is not None:
            return hit
    # Try camera_no-only key (covers the fallback path)
    hit = _rtsp_cam_index.get(("", camera_no))
    if hit is not None:
        return hit
    # Last resort: DB (handles cameras added after startup)
    if device_id:
        result = get_rtsp_camera_by_device_and_ch(device_id, camera_no)
        if result:
            return result
    try:
        conn = _get_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM camera_links_rtsp WHERE camera_no = %s AND is_active=1 LIMIT 1",
                (camera_no,)
            )
            row = cur.fetchone()
        conn.close()
        return row or {}
    except Exception as e:
        logger.error(f"RTSP camera lookup by camera_no failed: {e}")
        return {}


def _shift_names_for_ids(shift_ids) -> dict:
    ids = []
    for shift_id in shift_ids:
        if shift_id in (None, ""):
            continue
        try:
            ids.append(int(shift_id))
        except (TypeError, ValueError):
            continue

    ids = sorted(set(ids))
    if not ids:
        return {}

    conn = None
    try:
        conn = _get_connection()
        with conn.cursor() as cur:
            fmt = ",".join(["%s"] * len(ids))
            cur.execute(
                f"SELECT id, shift_name FROM exam_shifts WHERE id IN ({fmt})",
                tuple(ids),
            )
            return {row.get("id"): row.get("shift_name") for row in cur.fetchall()}
    except Exception as e:
        logger.error(f"Shift name lookup failed: {e}")
        return {}
    finally:
        if conn:
            conn.close()


def _user_names_for_ids(user_ids) -> dict:
    """Resolve a set of user ids to display names in one query.

    Returns {id: full_name or username}. Used to show "Pushed by" on client
    detections without a per-row DB lookup."""
    ids = []
    for user_id in user_ids:
        if user_id in (None, ""):
            continue
        try:
            ids.append(int(user_id))
        except (TypeError, ValueError):
            continue

    ids = sorted(set(ids))
    if not ids:
        return {}

    conn = None
    try:
        conn = _get_connection()
        with conn.cursor() as cur:
            fmt = ",".join(["%s"] * len(ids))
            cur.execute(
                f"SELECT id, full_name, username FROM users WHERE id IN ({fmt})",
                tuple(ids),
            )
            return {
                row.get("id"): (row.get("full_name") or row.get("username"))
                for row in cur.fetchall()
            }
    except Exception as e:
        logger.error(f"User name lookup failed: {e}")
        return {}
    finally:
        if conn:
            conn.close()


def _to_hm(value):
    """Normalise an exam_shifts time column to zero-padded 'HH:MM'.

    MySQL TIME comes back from PyMySQL as a datetime.timedelta, whose str() is
    NOT zero-padded ('8:30:00' for 08:30). Slicing that to 5 chars yielded
    '8:30:', which compares lexicographically ABOVE every real 'HH:MM' stamp, so
    a morning shift's window rejected all of its own detections."""
    if value is None:
        return None
    if isinstance(value, timedelta):
        total = int(value.total_seconds()) % 86400
        return f"{total // 3600:02d}:{(total % 3600) // 60:02d}"
    if isinstance(value, (dt_time, datetime)):
        return f"{value.hour:02d}:{value.minute:02d}"
    text = str(value).strip()
    match = re.match(r'^(\d{1,2}):(\d{2})', text)
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


@lru_cache(maxsize=2048)
def _resolve_shift_window(shift_id):
    """Return (start_hm, end_hm) as 'HH:MM' for an exam_shifts row, or (None,None).

    Detections are filtered by shift via this TIME WINDOW rather than a stored
    shift tag — every detection has a timestamp, but historical rows often lack a
    reliable shift_id (NULL / per-execution value), so time is the robust key."""
    if not shift_id:
        return (None, None)
    conn = None
    try:
        conn = _get_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT start_time, end_time FROM exam_shifts WHERE id = %s LIMIT 1",
                (int(shift_id),),
            )
            row = cur.fetchone()
        if not row:
            return (None, None)

        return (_to_hm(row.get("start_time")), _to_hm(row.get("end_time")))
    except Exception as e:
        logger.error(f"Shift window lookup failed for shift_id={shift_id}: {e}")
        return (None, None)
    finally:
        if conn:
            conn.close()


def _apply_shift_window(shift_id, start_time, end_time):
    """Fold a shift's time window into existing start/end time bounds (intersection)."""
    if not shift_id:
        return start_time, end_time
    sw_start, sw_end = _resolve_shift_window(shift_id)
    if sw_start:
        start_time = max(start_time, sw_start) if start_time else sw_start
    if sw_end:
        end_time = min(end_time, sw_end) if end_time else sw_end
    return start_time, end_time


def _attach_shift_names(items) -> None:
    """In-place: ensure every item carrying a shift_id also has a human-readable
    shiftName. Resolves exam_shifts in ONE query for the whole page, so detection
    cards (grid + list) reliably show the shift even when the camera_report row's
    shift_id was NULL but the filename embedded it."""
    needing = [it.get("shift_id") for it in items if it.get("shift_id") and not it.get("shiftName")]
    if not needing:
        return
    name_map = _shift_names_for_ids(needing)
    if not name_map:
        return
    for it in items:
        sid = it.get("shift_id")
        if not sid or it.get("shiftName"):
            continue
        try:
            nm = name_map.get(int(sid))
        except (TypeError, ValueError):
            nm = None
        if nm:
            it["shiftName"] = nm
            it["shift_name"] = nm


def _enrich_detection_item_from_report(item: dict, conn=None) -> dict:
    uid = item.get("uid")
    if not uid:
        return item

    source = item.get("source")
    if source == "rtsp":
        tables = ["camera_report_rtsp"]
    elif source == "hls":
        tables = ["camera_report_hls"]
    else:
        tables = ["camera_report_hls", "camera_report_rtsp"]

    # Reuse a caller-provided connection (the watch loop opens ONE per cycle and
    # passes it for every new file) instead of opening+closing one per detection.
    own_conn = conn is None
    try:
        if own_conn:
            conn = _get_connection()
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(
                    f"""
                    SELECT cr.shift_id, cr.tc_code, cr.gpu_id, cr.pushed_to_client,
                           cr.pushed_by, es.shift_name
                    FROM {table} AS cr
                    LEFT JOIN exam_shifts AS es ON es.id = cr.shift_id
                    WHERE cr.unique_id = %s
                    LIMIT 1
                    """,
                    (uid,),
                )
                row = cur.fetchone()
                if not row:
                    continue

                if row.get("tc_code"):
                    item["tcCode"] = row.get("tc_code")
                    item["tc_code"] = row.get("tc_code")
                if row.get("shift_id") is not None:
                    item["shift_id"] = row.get("shift_id")
                if row.get("shift_name"):
                    item["shiftName"] = row.get("shift_name")
                    item["shift_name"] = row.get("shift_name")
                # Filename gpu_id is primary; only fall back to DB value when the
                # filename had none (older files saved before gpu_id was embedded).
                if row.get("gpu_id") is not None and item.get("gpu_id") is None:
                    item["gpu_id"] = row.get("gpu_id")
                if row.get("pushed_to_client") is not None:
                    item["pushed_to_client"] = row.get("pushed_to_client")
                if row.get("pushed_by") is not None:
                    item["pushedBy"] = row.get("pushed_by")
                    item["pushed_by"] = row.get("pushed_by")
                break
    except Exception as e:
        logger.error(f"Detection report metadata lookup failed: {e}")
    finally:
        if own_conn and conn:
            conn.close()

    return item


# RTSP filenames follow: {script}_{camera_id_pk}_{camera_no}_{date}_{time}_{extra}.jpg
# The prefix (script name like server, phone, crowd, etc.) comes before the pk.
_RTSP_SCRIPT_PREFIXES = {
    "server", "crowd", "intruder", "entry", "phone", "proximity",
    "invigilator", "tamper", "inversion", "blind", "zero", "five",
    "tampering", "violation", "entry_exit", "server_room", "blind_spot",
    "zero_person", "detection", "crowd_zero", "crowd_five", "blindspot",
    "crowd_biometric", "strongroom", "qp_box", "improper_furniture",
}


def _split_rtsp_stem(stem: str):
    """Return (prefix, device_id, camera_no) for RTSP filenames, or None.

    RTSP files are named: {prefix}_{device_id}_{camera_no}_{date}_{time}_{extra}.jpg
    The prefix may contain underscores (qp_box, crowd_biometric, entry_exit),
    so we find the first all-digit segment instead of assuming a fixed word count.
    """
    parts = stem.split("_")
    for i in range(1, len(parts) - 1):
        if parts[i].isdigit() and parts[i + 1].isdigit():
            prefix = "_".join(parts[:i]).lower()
            if prefix in _RTSP_SCRIPT_PREFIXES:
                return prefix, parts[i], parts[i + 1]
            return None
    return None


def _is_rtsp_filename(stem: str) -> bool:
    return _split_rtsp_stem(stem) is not None


def _extract_rtsp_pk(stem: str) -> int:
    """Extract the camera_id_pk (second segment) from an RTSP filename."""
    parts = stem.split("_")
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    return 0


def _extract_ch_no(camera_id: str) -> str:
    if not camera_id:
        return ""
    # Supports invigilator ids like 11365_54703_* where second segment is ch code.
    m = re.search(r"^\d+_(\d+)", camera_id)
    if m:
        return m.group(1)

    # Prefer pattern like phone_ch11365_54703 -> use 54703 as channel code.
    m = re.search(r"ch\d+_(\d+)", camera_id, flags=re.IGNORECASE)
    if m:
        return m.group(1)

    # Supports ids like blind_ch22760, ch22760, 22760
    m = re.search(r"ch(\d+)", camera_id, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"(\d+)", camera_id)
    return m.group(1) if m else ""


@lru_cache(maxsize=65536)
def _basic_detection_meta(filename: str) -> dict:
    """Extract cheap filter fields from filename without DB metadata lookups."""
    stem = Path(filename).stem
    dt_match = re.search(r'(\d{8})_(\d{6})', stem)
    date_iso = ""
    time_str = ""
    if dt_match:
        date_str = dt_match.group(1)
        time_str = dt_match.group(2)
        date_iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

    exam_id = None
    exam_match = re.search(r'_exam(\d+)', stem, flags=re.IGNORECASE)
    if exam_match:
        exam_id = int(exam_match.group(1))

    shift_id = None
    shift_match = re.search(r'_shift(\d+)', stem, flags=re.IGNORECASE)
    if shift_match:
        _sid = int(shift_match.group(1))
        shift_id = _sid if _sid > 0 else None

    return {"date": date_iso, "time": time_str, "exam_id": exam_id, "shift_id": shift_id}


def _basic_detection_matches(
    relative_path: str,
    date: Optional[str] = None,
    exam_id: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> bool:
    basic = _basic_detection_meta(relative_path)

    if date and not basic["date"].startswith(date):
        return False

    if exam_id and str(basic.get("exam_id")) != str(exam_id):
        return False

    if start_time or end_time:
        time_str = basic.get("time")
        if time_str and len(time_str) >= 4:
            h_m = f"{time_str[:2]}:{time_str[2:4]}"
            if start_time and h_m < start_time:
                return False
            if end_time and h_m > end_time:
                return False

    return True


def _iter_detection_files(type_key: str, folder: Path):
    """Yield image files for one detection type.

    Uses recursive discovery so nested folders (for example invigilator camera
    subfolders) are included in API listing and live Socket.IO notifications.
    """
    if not folder.exists():
        return

    for f in folder.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue

        # Invigilator keeps periodic snapshots in /annotated; show event images only.
        if type_key == "invigilator" and "annotated" in {p.lower() for p in f.parts}:
            continue

        yield f


def list_detection_files() -> dict:
    """Scan both HLS (tcs_detections/) and RTSP (results/) directories."""
    snapshot = {}
    for type_key, folder_name in DETECTION_FOLDERS.items():
        files = set()

        # HLS directory
        hls_folder = RESULTS_DIR / folder_name
        if hls_folder.exists():
            for f in _iter_detection_files(type_key, hls_folder):
                # prefix with hls: so we know the origin
                files.add("hls:" + str(f.relative_to(hls_folder).as_posix()))

        # RTSP directory
        rtsp_folder = RTSP_RESULTS_DIR / folder_name
        if rtsp_folder.exists():
            for f in _iter_detection_files(type_key, rtsp_folder):
                files.add("rtsp:" + str(f.relative_to(rtsp_folder).as_posix()))

        snapshot[type_key] = files
    return snapshot


def _iter_recent_detection_files(type_key: str, folder: Path, since_ts: float):
    """Yield image files under `folder` modified at/after `since_ts`.

    Incremental analog of _iter_detection_files. We still descend every
    subdirectory (a write deep in the tree only bumps its immediate parent's
    mtime, not its ancestors'), but we only stat the FILES inside a directory
    whose OWN mtime is >= since_ts. Unchanged history is therefore skipped
    instead of re-statting all ~200k files every few seconds — the per-cycle
    cost becomes O(directories + new files) rather than O(all files).
    """
    if not folder.exists():
        return
    stack = [str(folder)]
    while stack:
        d = stack.pop()
        try:
            dir_recent = os.stat(d).st_mtime >= since_ts
        except OSError:
            dir_recent = True  # can't tell → don't skip its files
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        if not dir_recent:
                            continue
                        name = entry.name.lower()
                        if not name.endswith((".jpg", ".jpeg", ".png")):
                            continue
                        # Invigilator: skip periodic /annotated snapshots (event images only).
                        if type_key == "invigilator" and "annotated" in entry.path.lower().split(os.sep):
                            continue
                        if entry.stat().st_mtime >= since_ts:
                            yield Path(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue


def scan_recent_detection_files(since_ts: float) -> dict:
    """Incremental scan: like list_detection_files but only files newer than
    since_ts (per type, both HLS + RTSP roots). Returns the same tagged-path
    shape so the watch loop can diff it against the running snapshot."""
    snapshot = {}
    for type_key, folder_name in DETECTION_FOLDERS.items():
        files = set()
        hls_folder = RESULTS_DIR / folder_name
        if hls_folder.exists():
            for f in _iter_recent_detection_files(type_key, hls_folder, since_ts):
                files.add("hls:" + str(f.relative_to(hls_folder).as_posix()))
        rtsp_folder = RTSP_RESULTS_DIR / folder_name
        if rtsp_folder.exists():
            for f in _iter_recent_detection_files(type_key, rtsp_folder, since_ts):
                files.add("rtsp:" + str(f.relative_to(rtsp_folder).as_posix()))
        snapshot[type_key] = files
    return snapshot


def _build_date_index(snapshot: dict) -> dict:
    """Build a type_key → date_str → set[tagged_path] index from a snapshot."""
    by_date: dict = {}
    for type_key, tagged_paths in snapshot.items():
        date_map: dict = {}
        for tagged_path in tagged_paths:
            try:
                _, relative_path = tagged_path.split(":", 1)
            except ValueError:
                continue
            d = _basic_detection_meta(relative_path).get("date")
            if d:
                date_map.setdefault(d, set()).add(tagged_path)
        by_date[type_key] = date_map
    return by_date


def _tagged_detection_matches(tagged_path: str, date: Optional[str] = None) -> bool:
    """Return whether a tagged detection path matches a cheap date filter."""
    try:
        _, relative_path = tagged_path.split(":", 1)
    except ValueError:
        relative_path = tagged_path
    return _basic_detection_matches(relative_path, date=date)


async def detection_watch_loop():
    global known_detection_files, known_detection_by_date

    # Wall-clock cutoff for the next incremental scan, and a monotonic marker for
    # when the last FULL reconcile ran. The startup snapshot (built in lifespan)
    # is already complete, so the first incremental pass only needs to look back
    # a little before "now".
    last_scan_wall = time.time()
    last_full_rescan = time.monotonic()

    while True:
        await asyncio.sleep(DETECTION_SCAN_INTERVAL_SECONDS)
        try:
            do_full = (time.monotonic() - last_full_rescan) >= DETECTION_FULL_RESCAN_SECONDS

            # ── Decide per-type NEW files, and the snapshot/date-index to publish.
            # Both branches build FRESH dict/set objects (copy-on-write) and rebind
            # the globals only at the end, so the REST handlers — which take a
            # stable reference and iterate — never see a set mutate underneath them.
            if do_full:
                # Full rescan: re-walk everything (reconciles deletions too).
                new_known = await asyncio.to_thread(list_detection_files)
                per_type_new = {
                    tk: cur - known_detection_files.get(tk, set())
                    for tk, cur in new_known.items()
                }
                new_date_index = _build_date_index(new_known)
                last_full_rescan = time.monotonic()
            else:
                # Incremental: only files modified since the last scan (minus an
                # overlap), pruned by directory mtime. O(dirs + new files).
                cutoff = last_scan_wall - DETECTION_SCAN_MTIME_OVERLAP
                recent = await asyncio.to_thread(scan_recent_detection_files, cutoff)
                new_known = dict(known_detection_files)
                new_date_index = dict(known_detection_by_date)
                per_type_new = {}
                for type_key, recent_files in recent.items():
                    previous = new_known.get(type_key, set())
                    fresh = recent_files - previous
                    per_type_new[type_key] = fresh
                    if not fresh:
                        continue
                    new_known[type_key] = previous | fresh          # new set (COW)
                    date_map = dict(new_date_index.get(type_key, {}))
                    for tagged_path in fresh:
                        try:
                            _, relative_path = tagged_path.split(":", 1)
                        except ValueError:
                            continue
                        d = _basic_detection_meta(relative_path).get("date")
                        if d:
                            date_map[d] = date_map.get(d, set()) | {tagged_path}
                    new_date_index[type_key] = date_map

            last_scan_wall = time.time()

            # ── Emit new files. One shared DB connection per cycle for enrichment
            # (was one open+close PER file — the per-cycle latency killer).
            total_new = sum(len(v) for v in per_type_new.values())
            conn = await asyncio.to_thread(_get_connection) if total_new else None
            try:
                for type_key, new_files in per_type_new.items():
                    if not new_files:
                        continue

                    def _new_file_mtime(tagged_path: str, _tk=type_key) -> float:
                        try:
                            origin, relative_path = tagged_path.split(":", 1)
                            base_dir = RTSP_RESULTS_DIR if origin == "rtsp" else RESULTS_DIR
                            return (base_dir / DETECTION_FOLDERS[_tk] / relative_path).stat().st_mtime
                        except Exception:
                            return 0

                    sorted_new = await asyncio.to_thread(
                        lambda paths: sorted(paths, key=_new_file_mtime),
                        new_files,
                    )

                    emitted = 0
                    for tagged_path in sorted_new:
                        try:
                            origin, relative_path = tagged_path.split(":", 1)
                            base_dir = RTSP_RESULTS_DIR if origin == "rtsp" else RESULTS_DIR
                            file_path = base_dir / DETECTION_FOLDERS[type_key] / relative_path
                            payload = await asyncio.to_thread(
                                parse_detection_filename,
                                relative_path, type_key, file_path,
                                True, origin,
                            )
                            payload = await asyncio.to_thread(
                                _enrich_detection_item_from_report, payload, conn,
                            )
                            # Role-based routing: 'all' room = admin/client (everything);
                            # gpu:{id} room = operators assigned to that gpu. Disjoint rooms,
                            # so no double-delivery. Missing gpu_id (old files) → admin/client only.
                            await sio.emit("detection:new", payload, room="all")
                            gid = payload.get("gpu_id")
                            if gid is not None:
                                await sio.emit("detection:new", payload, room=f"gpu:{gid}")
                            emitted += 1
                        except Exception as exc:
                            logger.error(f"Failed to emit live detection {type_key} {tagged_path}: {exc}")

                    logger.info(f"Live detections emitted: {type_key} count={emitted} new_files={len(new_files)}")
            finally:
                if conn is not None:
                    try:
                        await asyncio.to_thread(conn.close)
                    except Exception:
                        pass

            # Publish the new snapshot + date index atomically (readers see the
            # complete old or complete new structures, never a half-updated one).
            known_detection_files = new_known
            known_detection_by_date = new_date_index

            # O(1) today count via date index — no O(N) iteration.
            today_iso = datetime.now().date().isoformat()
            counts = {
                k: len(known_detection_by_date.get(k, {}).get(today_iso, set()))
                for k in DETECTION_FOLDERS
            }
            await sio.emit("detection:counts", counts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Detection watch loop failed: {exc}")


sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # cleanup_old_pipelines() pkills every running master_pipeline.py on the host.
    # That is destructive when a sibling FastAPI worker is mid-flight: it kills
    # the freshly-spawned detection pipelines instead of just orphans. Gate it
    # behind an opt-in env var so a true clean restart can still call it.
    # Why: pipeline_manager.cleanup_old_pipelines sends SIGTERM, SIGTERM, SIGKILL
    # to every match — which is exactly what was killing scripts ~1 s after they
    # started on the main server (multiple uvicorn instances each running it).
    if os.getenv("CLEANUP_OLD_PIPELINES_ON_STARTUP", "0") == "1":
        cleanup_old_pipelines()

    # Startup logic
    logger.info("Starting CCTV Script Execution Server (FastAPI)...")
    logger.info(f"Main backend URL: {MAIN_BACKEND_URL}")
    logger.info(f"Poll interval: {POLL_INTERVAL_MS}ms")
    logger.info(f"Scripts directory: {SCRIPTS_DIR}")
    logger.info(f"HTTP server port: {HTTP_PORT}")
    
    # Start poller (launches this worker's assigned detection executions)
    await poller_service.start()

    global known_detection_files, known_detection_by_date
    # Bulk-load all cameras into memory once — eliminates per-request DB queries.
    await asyncio.to_thread(_init_camera_index)
    # Run initial file scan in thread — 90k files takes ~0.5s synchronously.
    known_detection_files = await asyncio.to_thread(list_detection_files)
    known_detection_by_date = _build_date_index(known_detection_files)
    
    # Background log cleanup task
    async def cleanup_loop():
        while True:
            await asyncio.sleep(24 * 60 * 60)  # Run daily
            log_manager.cleanup_old_logs(30)
            
    cleanup_task = asyncio.create_task(cleanup_loop())
    detection_task = asyncio.create_task(detection_watch_loop())
    logger.info("Script execution server started successfully")
    
    yield
    
    # Shutdown logic
    logger.info("Shutting down...")
    await poller_service.stop()
    cleanup_task.cancel()
    detection_task.cancel()

# app = FastAPI(lifespan=lifespan)
app = FastAPI(
    lifespan=lifespan,
    root_path="/cctv_dev"
)

# ── Detection image directories ──────────────────────────────
RESULTS_DIR = Path(SCRIPTS_DIR).resolve() / "detection" / "results" / "tcs_detections"
RTSP_RESULTS_DIR = Path(SCRIPTS_DIR).resolve() / "detection" / "results"
CLIENT_SELECTED_DIR = Path(SCRIPTS_DIR).resolve() / "detection" / "results" / "client_selected_images"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
RTSP_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
CLIENT_SELECTED_DIR.mkdir(parents=True, exist_ok=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Origin", "X-Requested-With", "Content-Type", "Accept", "Authorization"],
)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.debug(f"{request.method} {request.url.path}")
    response = await call_next(request)
    return response


# ── Image serving endpoints (replaces StaticFiles mounts) ─────
# Pre-resolve base dirs once. base_dir.resolve() on an NFS-symlinked dir does
# a readlink syscall per request; doing it at import time saves N NFS round
# trips per image. Symlink targets don't change at runtime here.
_RESULTS_DIR_RESOLVED = str(RESULTS_DIR.resolve())
_RTSP_RESULTS_DIR_RESOLVED = str(RTSP_RESULTS_DIR.resolve())
_CLIENT_SELECTED_DIR_RESOLVED = str(CLIENT_SELECTED_DIR.resolve())


@lru_cache(maxsize=65536)
def _safe_resolve_cached(base_dir_resolved: str, relative: str) -> str:
    """Cached resolution + existence check for a (base_dir, relative) pair.

    Each call previously made 3-4 NFS round trips (.resolve(), .exists(),
    .is_file()). Detection images are immutable once written, so caching the
    resolved absolute path eliminates the repeat stat storm when the same
    thumbnail is fetched across page loads / multiple tabs.
    """
    base_dir = Path(base_dir_resolved)
    candidate = (base_dir / relative).resolve()
    if not str(candidate).startswith(base_dir_resolved):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not candidate.exists() or not candidate.is_file():
        # Negative results are NOT cached — let the next attempt retry once
        # the file appears. The cache key is only stored on success.
        raise HTTPException(status_code=404, detail="Image not found")
    return str(candidate)


def _safe_resolve(base_dir: Path, relative: str) -> Path:
    """Resolve a relative path against base_dir, preventing directory traversal."""
    if base_dir is RESULTS_DIR:
        base = _RESULTS_DIR_RESOLVED
    elif base_dir is RTSP_RESULTS_DIR:
        base = _RTSP_RESULTS_DIR_RESOLVED
    elif base_dir is CLIENT_SELECTED_DIR:
        base = _CLIENT_SELECTED_DIR_RESOLVED
    else:
        base = str(base_dir.resolve())
    return Path(_safe_resolve_cached(base, relative))


# NOTE: These endpoints are intentionally synchronous (`def`, not `async def`).
# FastAPI runs sync routes in the threadpool, which parallelizes the NFS stat
# work and keeps the event loop free for Socket.IO, /detections, and the
# watcher. Previously these were `async def`, so each NFS round trip blocked
# the loop and one slow image stalled every other request.
@app.get("/serve-detection-images/{file_path:path}")
def serve_detection_image(file_path: str):
    resolved = _safe_resolve(RESULTS_DIR, file_path)
    return FileResponse(resolved, media_type="image/jpeg")


@app.get("/serve-rtsp-detection-images/{file_path:path}")
def serve_rtsp_detection_image(file_path: str):
    resolved = _safe_resolve(RTSP_RESULTS_DIR, file_path)
    return FileResponse(resolved, media_type="image/jpeg")


@app.get("/serve-client-detection-images/{file_path:path}")
def serve_client_detection_image(file_path: str):
    resolved = _safe_resolve(CLIENT_SELECTED_DIR, file_path)
    return FileResponse(resolved, media_type="image/jpeg")

@app.get("/health")
async def health_check():
    import time
    
    # Get worker info if available
    worker_info = {}
    try:
        from app.camera_distributor import distributor
        worker_info = distributor.get_worker_stats()
    except ImportError:
        pass
    
    return {
        "success": True,
        "message": "Script execution server is running",
        "timestamp": datetime.now().isoformat(),
        "uptime": time.monotonic(),
        "worker": worker_info
    }

@app.get("/status")
async def status():
    return {
        "success": True,
        "data": poller_service.get_status()
    }

@app.get("/executions/running")
async def get_running():
    running_scripts = list(poller_service.running_scripts)
    return {
        "success": True,
        "count": len(running_scripts),
        "data": [{"assignmentId": id} for id in running_scripts]
    }

@app.post("/executions/{execution_id}/cancel")
async def cancel_execution(execution_id: str, forwarded: bool = False):
    """
    Manually cancel a running script without stopping the server.
    Sends SIGTERM (then SIGKILL) to the subprocess and its mp.Process workers,
    marks the assignment CANCELLED in the DB, and removes it from the poller's running set.

    The frontend always hits the main worker (nginx routes /ss → main), but a script
    can be running on any worker. So if we don't find the process locally, fan out
    the cancel to every remote worker. The `forwarded` flag prevents infinite loops
    when a remote worker receives the forwarded call.
    """
    from app.script_runner import script_runner

    killed = await script_runner.cancel(execution_id)
    forwarded_kill = False

    # A distributed analytic (detection: entry_exit / crowd / intruder / …, and
    # vision classification) runs the SAME execution_id on EVERY worker at once —
    # each over its own camera shard. So a cancel must reach ALL workers, not just
    # the one that happens to answer first.
    #   Previous bug: fan-out was gated on `not killed` (so if main had a local
    #   copy it never told the others) AND `break` after the first hit — leaving
    #   the rest of the fleet running the cancelled script (observed on worker-6).
    # Fix: always fan out (unless this call is itself a forward), to ALL remote
    # workers concurrently, no early break.
    if not forwarded:
        remote_workers = [
            w for w in GPU_WORKERS
            if w["ip"] not in ("localhost", "127.0.0.1", "0.0.0.0")
        ]

        async def _forward_cancel(w):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    r = await client.post(
                        f"http://{w['ip']}:{w['port']}/executions/{execution_id}/cancel",
                        params={"forwarded": "true"},
                    )
                    if r.status_code == 200 and r.json().get("process_killed"):
                        logger.info(f"⏹ {execution_id} cancelled on remote worker {w['id']} ({w['ip']}:{w['port']})")
                        return True
            except Exception as e:
                logger.debug(f"Cancel fan-out to {w['id']} ({w['ip']}:{w['port']}) failed: {e}")
            return False

        if remote_workers:
            results = await asyncio.gather(*[_forward_cancel(w) for w in remote_workers])
            forwarded_kill = any(results)

    if not killed and not forwarded_kill:
        # Not in active processes anywhere — check if poller still tracks it.
        if execution_id not in poller_service.running_scripts:
            raise HTTPException(
                status_code=404,
                detail=f"No running script with execution_id={execution_id}"
            )

    # Remove from poller's running set so it won't be re-queued
    poller_service.running_scripts.discard(execution_id)

    # Mark CANCELLED in the main backend DB
    if IS_MAIN_WORKER and not forwarded:
        await poller_service.update_assignment_status(
            execution_id, "CANCELLED", {"error_log": "Manually cancelled via API"}
        )

    logger.info(f"⏹ Assignment {execution_id} cancelled by user request")
    return {
        "success": True,
        "message": f"Assignment {execution_id} has been cancelled",
        "process_killed": killed or forwarded_kill,
    }

@app.get("/executions/recent")
async def get_recent(limit: int = 20, date: Optional[str] = None):
    try:
        parsed_date = datetime.fromisoformat(date) if date else datetime.now()
    except ValueError:
        parsed_date = datetime.now()
        
    log_files = log_manager.get_log_files_for_date(parsed_date)
    recent = []
    
    for file in log_files[:limit]:
        recent.append({
            "executionId": file["executionId"],
            "filename": file["filename"],
            "summary": log_manager.get_log_summary(file["executionId"], parsed_date)
        })
        
    return {
        "success": True,
        "count": len(recent),
        "data": recent
    }

@app.get("/logs/{execution_id}")
async def get_logs(execution_id: str, date: Optional[str] = None):
    try:
        parsed_date = datetime.fromisoformat(date) if date else datetime.now()
    except ValueError:
        parsed_date = datetime.now()
        
    logs = log_manager.read_logs(execution_id, parsed_date)
    
    if not logs:
        raise HTTPException(status_code=404, detail="Logs not found for this execution")
        
    return {
        "success": True,
        "data": {
            "executionId": execution_id,
            "logs": logs,
            "summary": log_manager.get_log_summary(execution_id, parsed_date)
        }
    }

# ==========================================
# Legacy Manual Analytics Endpoints
# ==========================================

@app.post("/analytics/start", response_model=MessageResponse)
def start_analytics_route(req: StartRequest):
    if req.all:
        started = start_analytics(ALL_ANALYTICS)
        return {"message": f"Started ALL analytics: {started}"}

    if not req.analytics:
        raise HTTPException(400, "Provide analytics list or set all=true")

    started = start_analytics(req.analytics)
    return {"message": f"Started analytics: {started}"}

@app.post("/analytics/stop", response_model=MessageResponse)
def stop_analytics_route(req: StopRequest):
    if req.all:
        stopped = stop_analytics(list(processes.keys()))
        return {"message": f"Stopped ALL analytics: {stopped}"}

    if not req.analytics:
        raise HTTPException(400, "Provide analytics list or set all=true")

    stopped = stop_analytics(req.analytics)
    return {"message": f"Stopped analytics: {stopped}"}

@app.get("/analytics/status", response_model=AnalyticsStatusResponse)
def status_analytics_route():
    running, stopped = status_analytics()
    return {"running": running, "stopped": stopped}


# ==========================================
# GPU Monitoring Endpoints
# ==========================================

@app.get("/gpu-stats")
async def get_local_gpu_stats():
    """
    Get GPU statistics for this local server.
    This endpoint is called by other workers to fetch GPU stats.
    """
    return get_local_gpu_stats_data()


async def fetch_worker_gpu_stats(worker: dict) -> dict:
    """Fetch GPU stats from a single worker server"""
    worker_id = worker["id"]

    # If this is the local server (localhost), call the function directly instead of HTTP request
    if worker['ip'] in ('localhost', '127.0.0.1', '0.0.0.0'):
        try:
            # get_local_gpu_stats_data spawns nvidia-smi subprocesses on cache miss.
            # Even with a TTL cache, the miss path must not run in the event loop
            # or the whole server stalls while nvidia-smi runs.
            local_data = await asyncio.to_thread(get_local_gpu_stats_data)
            _worker_fail_count[worker_id] = 0
            _worker_last_good[worker_id] = local_data
            return {
                "server_id": worker_id,
                "server_name": worker["name"],
                "ip": worker["ip"],
                "status": "online",
                "data": local_data,
                "error": None
            }
        except Exception as e:
            return {
                "server_id": worker_id,
                "server_name": worker["name"],
                "ip": worker["ip"],
                "status": "offline",
                "data": None,
                "error": f"Local GPU stats error: {str(e)}"
            }

    # For remote workers, make HTTP request
    error_msg = None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(f"http://{worker['ip']}:{worker['port']}/gpu-stats")
            data = response.json()
            _worker_fail_count[worker_id] = 0
            _worker_last_good[worker_id] = data
            return {
                "server_id": worker_id,
                "server_name": worker["name"],
                "ip": worker["ip"],
                "status": "online",
                "data": data,
                "error": None
            }
    except httpx.TimeoutException:
        error_msg = "Connection timeout"
    except httpx.ConnectError:
        error_msg = "Connection refused - Server not running"
    except Exception as e:
        error_msg = str(e)

    # Increment failure counter; only go offline after threshold consecutive failures
    fails = _worker_fail_count.get(worker_id, 0) + 1
    _worker_fail_count[worker_id] = fails

    if fails < _WORKER_OFFLINE_THRESHOLD and worker_id in _worker_last_good:
        # Return cached data with a stale warning — transient blip
        return {
            "server_id": worker_id,
            "server_name": worker["name"],
            "ip": worker["ip"],
            "status": "online",
            "data": _worker_last_good[worker_id],
            "error": None,
            "stale": True
        }

    return {
        "server_id": worker_id,
        "server_name": worker["name"],
        "ip": worker["ip"],
        "status": "offline",
        "data": None,
        "error": error_msg
    }


@app.get("/gpu-monitor/stats")
async def get_cluster_gpu_stats():
    """
    Get GPU statistics from all worker servers.
    Returns aggregated GPU utilization, processes, and camera counts.
    """
    import asyncio
    
    # Fetch stats from all workers concurrently
    tasks = [fetch_worker_gpu_stats(worker) for worker in GPU_WORKERS]
    results = await asyncio.gather(*tasks)
    
    # Aggregate statistics
    aggregated = {
        "total_servers": len(GPU_WORKERS),
        "online_servers": sum(1 for r in results if r["status"] == "online"),
        "offline_servers": sum(1 for r in results if r["status"] == "offline"),
        "total_gpus": 0,
        "total_processes": 0,
        "total_cameras": 0,
        "servers": []
    }
    
    for result in results:
        if result["status"] == "online" and result["data"]:
            data = result["data"]
            aggregated["total_gpus"] += data.get("total_gpus", 0)
            aggregated["total_processes"] += data.get("total_processes", 0)
            
            # Calculate total cameras from processes
            if data.get("processes"):
                for gpu_procs in data["processes"].values():
                    for proc in gpu_procs:
                        aggregated["total_cameras"] += proc.get("camera_count", 0)
        
        aggregated["servers"].append(result)
    
    return {
        "success": True,
        "timestamp": datetime.now().isoformat(),
        "data": aggregated
    }


@app.get("/gpu-monitor/stats/{server_id}")
async def get_worker_gpu_stats(server_id: str):
    """Get GPU statistics from a specific worker server"""
    worker = next((w for w in GPU_WORKERS if w["id"] == server_id), None)
    
    if not worker:
        raise HTTPException(status_code=404, detail=f"Worker {server_id} not found")
    
    result = await fetch_worker_gpu_stats(worker)
    
    return {
        "success": True,
        "timestamp": datetime.now().isoformat(),
        "data": result
    }


@app.get("/gpu-monitor/servers")
async def get_gpu_servers():
    """Get list of configured GPU worker servers with health status"""
    import asyncio
    
    async def check_health(worker):
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.get(f"http://{worker['ip']}:{worker['port']}/health")
                return {**worker, "status": "online"}
        except Exception:
            return {**worker, "status": "offline"}
    
    tasks = [check_health(worker) for worker in GPU_WORKERS]
    servers = await asyncio.gather(*tasks)
    
    return {
        "success": True,
        "servers": servers
    }


@app.get("/gpu-monitor/local")
async def get_local_gpu_monitor():
    """
    Get GPU statistics for the local server only (no remote calls).
    Useful for quick local monitoring without network overhead.
    """
    # Cache miss path runs nvidia-smi — keep it off the event loop.
    return await asyncio.to_thread(get_local_gpu_stats_data)


@app.get("/metrics")
def prometheus_metrics():
    """Prometheus scrape endpoint — exposes per-worker GPU metrics."""
    from app.metrics import render_metrics
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)


# ── Prometheus query proxy ────────────────────────────────────
# Browser hits the script-server, script-server forwards to Prometheus.
# Keeps Prometheus off the public surface and avoids CORS in the SPA.
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
_PROM_ALLOWED_PATHS = {"query", "query_range", "series", "labels", "label/__name__/values"}


@app.get("/prometheus-api/{path:path}")
async def prometheus_proxy(path: str, request: Request):
    """Forward whitelisted Prometheus HTTP API calls."""
    if path not in _PROM_ALLOWED_PATHS and not path.startswith("label/"):
        raise HTTPException(status_code=400, detail=f"Path not allowed: {path}")

    url = f"{PROMETHEUS_URL}/api/v1/{path}"
    # Short connect timeout so the browser doesn't spin when Prometheus is down.
    timeout = httpx.Timeout(connect=1.5, read=5.0, write=5.0, pool=1.5)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, params=dict(request.query_params))
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Prometheus not running (start it via docker-compose.prometheus.yml)")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Prometheus error: {exc}")


# ==========================================
# Detections Endpoints
# ==========================================

DETECTION_FOLDERS = {
    "proximity":        "proximity_detection",
    "crowd":            "crowd_detection",
    "crowd_biometric":  "crowd_detection_biometric",
    "crowd_five":       "crowd_detection_threshold",
    "crowd_zero":       "zero_person_detection",
    "server":           "server_room_detection",
    "intruder":         "intruder_detection",
    "entry_exit":       "entry_exit_detection",
    "phone":            "phone_detection",
    "blind_spot":       "blind_spot_detection",
    "invigilator":      "invigilator_detection",
    "inversion":        "camera_inversion_detection",
    "tampering":        "camera_tampering_detection",
    "strongroom":       "strongroom_detection",
    "qp_box":           "qp_box_missing",
    "furniture":        "improper_furniture_detection",
    "vision":           "scene_classification",
}

DETECTION_LABELS = {
    "proximity":        "Close Proximity",
    "crowd":            "Crowd Detection (Entry/Exit)",
    "crowd_biometric":  "Crowd Detection (Biometric)",
    "crowd_five":       "Crowd < 5",
    "crowd_zero":       "Zero Person",
    "server":           "Server Room",
    "intruder":         "Intruder Detection",
    "entry_exit":       "Entry/Exit",
    "phone":            "Phone Detection",
    "blind_spot":       "Blind Spot",
    "invigilator":      "Invigilator Tracking",
    "tampering":        "Camera Tampering",
    "inversion":        "Camera Inversion",
    "strongroom":       "Strong Room Movement",
    "qp_box":           "QP Box Detection",
    "furniture":        "Improper Furniture",
    "vision":           "Scene Classification",
}

def parse_detection_filename(
    filename: str,
    type_key: str,
    file_path: Optional[Path] = None,
    is_new_image: bool = False,
    source: str = "hls",
) -> dict:
    """Parse metadata from a detection image filename.

    For RTSP files the naming convention is:
        {script}_{camera_id_pk}_{camera_no}_{date}_{time}_{extra}.jpg
    For HLS files the naming convention is:
        {camera_id}_{date}_{time}_{extra}.jpg
    """
    file_obj = Path(filename)
    basename = file_obj.name
    stem = file_obj.stem  # without extension

    date_str, time_str = "", ""
    camera_id = ""
    suffix = ""
    rtsp_pk = 0
    exam_id = None
    uid = None

    # Extract exam_id if present
    exam_match = re.search(r'_exam(\d+)_', stem, flags=re.IGNORECASE)
    if not exam_match:
        # Check without trailing underscore just in case ts follows directly 
        exam_match = re.search(r'_exam(\d+)', stem, flags=re.IGNORECASE)
    if exam_match:
        exam_id = int(exam_match.group(1))

    # Extract shift_id embedded in the filename as _shift{id}_ (added by the
    # detection workers). This is the execution's shift and is the authoritative
    # source — the camera-metadata shift_id is often NULL (RTSP / all-shifts HLS).
    # A "0" placeholder (tampering / env unset) is treated as absent.
    filename_shift_id = None
    shift_match = re.search(r'_shift(\d+)', stem, flags=re.IGNORECASE)
    if shift_match:
        try:
            _sid = int(shift_match.group(1))
            filename_shift_id = _sid if _sid > 0 else None
        except ValueError:
            filename_shift_id = None

    # Extract gpu_id (= WORKER_INDEX) embedded by detection workers as _gpu{N}_.
    # This is the PRIMARY operator-sharding key. Older files have no _gpu token
    # (gpu_id stays None → falls back to DB enrichment, then admin/client-only).
    gpu_id_fn = None
    gpu_match = re.search(r'_gpu(\d+)', stem, flags=re.IGNORECASE)
    if gpu_match:
        gpu_id_fn = int(gpu_match.group(1))

    # The uid is typically the the last segment of the stem
    uid = stem.split("_")[-1]

    # ── Try RTSP naming pattern: {prefix}_{pk}_{camNo}_{date}_{time}... ──
    # Now agnostic to the source directory to handle complex samarth-style paths
    rtsp_split = _split_rtsp_stem(stem)
    if rtsp_split:
        _, rtsp_device_id, rtsp_camera_no = rtsp_split
        dt_match = re.search(r'(\d{8})_(\d{6})', stem)
        if dt_match:
            date_str = dt_match.group(1)
            time_str = dt_match.group(2)
            camera_id = f"{rtsp_device_id}_{rtsp_camera_no}"
            suffix = stem[dt_match.end():].strip('_')
        else:
            camera_id = stem
    else:
        # ── HLS / legacy naming ──
        dt_match = re.search(r'(\d{8})_(\d{6})', stem)
        if dt_match:
            date_str = dt_match.group(1)
            time_str = dt_match.group(2)
            camera_id = stem[:dt_match.start()].strip('_')
            suffix = stem[dt_match.end():].strip('_')
        else:
            camera_id = stem

    # Format datetime
    detected_at = ""
    if date_str and time_str:
        try:
            dt = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S")
            detected_at = dt.strftime("%d %b %Y, %I:%M:%S %p")
        except Exception:
            detected_at = f"{date_str} {time_str}"

    # Extract confidence
    conf_match = re.search(r'conf([\d.]+)', suffix)
    confidence = float(conf_match.group(1)) if conf_match else None

    # Extract count (persons / violations)
    count_match = re.search(r'(?:persons|violations)(\d+)', suffix)
    count = int(count_match.group(1)) if count_match else None

    issue_match = re.search(r'([a-zA-Z]+)\d*$', suffix)
    issue_type = issue_match.group(1).lower() if issue_match else ""
    if type_key == "invigilator":
        inv_parts = {p.lower() for p in file_obj.parts}
        if "no_targets" in inv_parts:
            issue_type = "no_targets"
        elif "static_person" in inv_parts:
            issue_type = "static_person"
        elif not issue_type:
            stem_l = stem.lower()
            if "static_person" in stem_l:
                issue_type = "static_person"
            elif "no_targets" in stem_l:
                issue_type = "no_targets"

    exam_date_iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}" if date_str else ""

    # ── Metadata lookup: RTSP uses device_id+camera_no, HLS uses ch_code ──
    cam_meta = {}
    if source == "rtsp":
        rtsp_split = _split_rtsp_stem(stem)
        if rtsp_split:
            _, dev_id, cam_no_part = rtsp_split
            cam_meta = _camera_meta_for_rtsp_device_ch(dev_id, cam_no_part)
            logger.debug(f"RTSP metadata lookup: dev_id={dev_id}, cam_no={cam_no_part}, found={bool(cam_meta)}")
    
    # Fallback to HLS lookup if RTSP lookup failed or if source is HLS
    if not cam_meta:
        ch_no = _extract_ch_no(camera_id)
        if ch_no:
            cam_meta = _camera_meta_for_ch(ch_no, exam_date_iso)
            logger.debug(f"HLS metadata lookup: ch_no={ch_no}, exam_date={exam_date_iso}, found={bool(cam_meta)}")
        
        # If HLS lookup failed and we have exam_id, try RTSP lookup as fallback
        if not cam_meta and exam_id and ch_no:
            # Try to find in RTSP table using camera_no
            cam_meta = _camera_meta_for_rtsp_device_ch("", ch_no)
            if cam_meta:
                logger.debug(f"Fallback RTSP metadata lookup succeeded: ch_no={ch_no}")

    ch_no_display = str(cam_meta.get("camera_no") or cam_meta.get("ch_code") or _extract_ch_no(camera_id))
    camera_name = cam_meta.get("camera_name") or camera_id
    center_name = cam_meta.get("centre_name") or ""
    city = cam_meta.get("city") or ""
    state = cam_meta.get("state") or ""
    tc_code = cam_meta.get("tc_code")
    # Prefer the shift embedded in the filename (the execution's shift); fall back
    # to the camera's assigned shift from metadata when the filename has none.
    shift_id = filename_shift_id or cam_meta.get("shift_id")
    category = cam_meta.get("camera_category") or cam_meta.get("classification") or ""
    cctv_url = cam_meta.get("cctv_url") or ""

    now_ms = int(datetime.now().timestamp() * 1000)
    timestamp_iso = datetime.now().isoformat()
    date_display = ""
    if date_str:
        date_display = f"{date_str[6:]}-{date_str[4:6]}-{date_str[:4]}"

    # Calculate detectedAt timestamp in milliseconds from the filename date/time
    detected_at_ms = None
    if date_str and time_str:
        try:
            dt = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S")
            detected_at_ms = int(dt.timestamp() * 1000)
        except Exception:
            detected_at_ms = now_ms  # Fallback to current time

    image_rel = quote(str(file_obj.as_posix()), safe="/")
    issue_label_map = {
        "no_targets": "No Targets",
        "static_person": "Static Person",
    }

    # ── Image URL: different endpoint for RTSP vs HLS ──
    if source == "rtsp":
        image_url = f"/serve-rtsp-detection-images/{DETECTION_FOLDERS[type_key]}/{image_rel}"
    else:
        image_url = f"/serve-detection-images/{DETECTION_FOLDERS[type_key]}/{image_rel}"

    return {
        "filename":    basename,
        "relativePath": str(file_obj.as_posix()),
        "type":        type_key,
        "label":       DETECTION_LABELS.get(type_key, type_key),
        "camera_id":   camera_id,
        "cameraId":    camera_id,
        "chNo":        int(ch_no_display) if ch_no_display and ch_no_display.isdigit() else None,
        "time":        time_str,
        "rawDate":     date_str,
        "issueType":   issue_type,
        "issueLabel":  issue_label_map.get(issue_type, issue_type.replace("_", " ").title() if issue_type else ""),
        "detectionType": type_key,
        "addedAt":     now_ms,
        "detectedAt":  detected_at_ms,  # ← Actual detection timestamp in ms
        "newBadgeAt":  now_ms if is_new_image else None,
        "timestamp":   timestamp_iso,
        "isNewImage":  is_new_image,
        "source":      source,
        "cameraName":  camera_name,
        "centerName":  center_name,
        "device_id":   cam_meta.get("device_id") or "",
        "camera_no":   cam_meta.get("camera_no") or ch_no_display,
        "city":        city,
        "state":       state,
        "tcCode":      tc_code,
        "tc_code":     tc_code,
        "shift_id":    shift_id,
        "category":    category,
        "cctvUrl":     cctv_url,
        "path":        str(file_path) if file_path else "",
        "detected_at": detected_at,
        "date":        exam_date_iso,
        "examDate":    date_display,
        "confidence":  confidence,
        "count":       count,
        "image_url":   image_url,
        "exam_id":     exam_id,
        "uid":         uid,
        "gpu_id":      gpu_id_fn,
    }



@app.get("/detections")
def list_detections(
    type: Optional[str] = None,
    date: Optional[str] = None,
    search: Optional[str] = None,
    source: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    exam_id: Optional[str] = None,
    shift_id: Optional[str] = None,
    gpu_id: Optional[int] = None,      # operator sharding: restrict to this worker's gpu
    page: int = 1,
    limit: int = 50,
):
    selected_types = set(type.split(',')) if type else set()
    check_types = selected_types if selected_types else set(DETECTION_FOLDERS.keys())
    needs_search = bool(search)
    # Filter by shift = filter by that shift's time window (robust on historical
    # data that lacks a reliable shift tag). Fold it into start/end time bounds.
    start_time, end_time = _apply_shift_window(shift_id, start_time, end_time)
    has_extra_filters = bool(exam_id or start_time or end_time)

    # Operator gpu sharding: pre-compile a cheap regex for the _gpu{N}_ token
    # embedded in detection filenames. None = no gpu filter (admin/client).
    _gpu_filter_pat = re.compile(rf'_gpu{int(gpu_id)}[_.]', re.IGNORECASE) if gpu_id is not None else None

    # Take stable references to avoid races with the background watcher.
    snapshot = known_detection_files
    date_index = known_detection_by_date

    # Single pass: compute counts for ALL types (for the sidebar / header pill)
    # AND build the candidate list for the requested type(s) only.
    # When a date filter is provided and the index is populated we iterate only
    # the O(k) bucket for that date instead of the full O(N) snapshot.
    all_counts = {k: 0 for k in DETECTION_FOLDERS}
    candidates = []  # (sort_key, type_key, relative_path, origin)

    for type_key in DETECTION_FOLDERS:
        # Use date index for O(k) lookup; fall back to full snapshot when the
        # index isn't ready yet or when no date filter is given.
        using_date_index = bool(date and date_index and type_key in date_index)
        if using_date_index:
            tagged_paths = date_index[type_key].get(date, set())
        else:
            tagged_paths = snapshot.get(type_key, set())

        for tagged_path in tagged_paths:
            try:
                origin, relative_path = tagged_path.split(":", 1)
            except ValueError:
                continue

            if source == "rtsp" and origin != "rtsp":
                continue
            if source == "hls" and origin != "hls":
                continue

            # Operator gpu sharding: skip files that don't carry this gpu's token.
            # Files without any _gpu token are old/untagged → only shown when
            # no gpu filter is active (i.e. admin/client sees everything).
            if _gpu_filter_pat is not None:
                basename_for_gpu = relative_path.split("/")[-1] if "/" in relative_path else relative_path
                if not _gpu_filter_pat.search(basename_for_gpu):
                    continue

            # Date is pre-filtered by the index; only run the regex for the
            # remaining filters to avoid redundant filename parsing.
            check_date = None if using_date_index else date
            if (check_date or has_extra_filters) and not _basic_detection_matches(
                relative_path, check_date, exam_id, start_time, end_time
            ):
                continue

            all_counts[type_key] += 1

            if type_key in check_types:
                basic = _basic_detection_meta(relative_path)
                sort_key = (basic.get("date") or "") + (basic.get("time") or "")
                candidates.append((sort_key, type_key, relative_path, origin))

    # Sort newest first (YYYYMMDDHHMMSS lexicographic == chronological)
    candidates.sort(key=lambda x: x[0], reverse=True)
    start_idx = (page - 1) * limit

    if needs_search:
        # Search path: parse all filtered candidates, apply text filter, paginate.
        rows = []
        q = search.lower()
        for _, type_key, relative_path, origin in candidates:
            base_dir = RTSP_RESULTS_DIR if origin == "rtsp" else RESULTS_DIR
            file_path = base_dir / DETECTION_FOLDERS[type_key] / relative_path
            item = parse_detection_filename(
                relative_path, type_key, file_path,
                is_new_image=False, source=origin,
            )
            haystack = " ".join([
                str(item.get("camera_id") or ""),
                str(item.get("cameraName") or ""),
                str(item.get("centerName") or ""),
                str(item.get("city") or ""),
                str(item.get("state") or ""),
                str(item.get("tcCode") or ""),
            ]).lower()
            if q in haystack:
                rows.append(item)

        # Recompute counts from the search-filtered result set.
        all_counts = {k: 0 for k in DETECTION_FOLDERS}
        for item in rows:
            t = item.get("type", "")
            if t in all_counts:
                all_counts[t] += 1

        total = len(rows)
        paginated = rows[start_idx: start_idx + limit]
    else:
        # Fast path: only parse the `limit` items on this page.
        total = len(candidates)
        page_candidates = candidates[start_idx: start_idx + limit]
        paginated = []
        for _, type_key, relative_path, origin in page_candidates:
            base_dir = RTSP_RESULTS_DIR if origin == "rtsp" else RESULTS_DIR
            file_path = base_dir / DETECTION_FOLDERS[type_key] / relative_path
            item = parse_detection_filename(
                relative_path, type_key, file_path,
                is_new_image=False, source=origin,
            )
            paginated.append(item)

    # ── DB: pushed_to_client + gpu_id for the paginated slice only ─────────────
    uids_to_check = [item.get("uid") for item in paginated if item.get("uid")]
    pushed_uids = set()
    uid_db_meta = {}
    if uids_to_check:
        try:
            from scripts.detection.utils.db_client import _get_connection
            conn = _get_connection()
            with conn.cursor() as cur:
                fmt = ','.join(['%s'] * len(uids_to_check))

                try:
                    cur.execute(
                        f"""
                        SELECT cr.unique_id, cr.pushed_to_client, cr.gpu_id,
                               cr.shift_id, cr.tc_code, cr.sms_sent, es.shift_name
                        FROM camera_report_hls AS cr USE INDEX (idx_unique_id)
                        LEFT JOIN exam_shifts AS es ON es.id = cr.shift_id
                        WHERE cr.unique_id IN ({fmt})
                        """,
                        tuple(uids_to_check)
                    )
                    for row in cur.fetchall():
                        uid = row.get("unique_id")
                        if uid:
                            meta = uid_db_meta.setdefault(uid, {})
                            if row.get("pushed_to_client") == 1:
                                pushed_uids.add(uid)
                            if row.get("gpu_id") is not None:
                                meta["gpu_id"] = row["gpu_id"]
                            if row.get("shift_id") is not None:
                                meta["shift_id"] = row.get("shift_id")
                            if row.get("shift_name"):
                                meta["shiftName"] = row.get("shift_name")
                            if row.get("tc_code"):
                                meta["tcCode"] = row.get("tc_code")
                            if row.get("sms_sent") is not None:
                                meta["sms_sent"] = row.get("sms_sent")
                except Exception as e:
                    logger.error(f"HLS pushed uids fetch failed: {e}")

                try:
                    cur.execute(
                        f"""
                        SELECT cr.unique_id, cr.pushed_to_client, cr.gpu_id,
                               cr.shift_id, cr.tc_code, cr.sms_sent, es.shift_name
                        FROM camera_report_rtsp AS cr USE INDEX (idx_unique_id)
                        LEFT JOIN exam_shifts AS es ON es.id = cr.shift_id
                        WHERE cr.unique_id IN ({fmt})
                        """,
                        tuple(uids_to_check)
                    )
                    for row in cur.fetchall():
                        uid = row.get("unique_id")
                        if uid:
                            meta = uid_db_meta.setdefault(uid, {})
                            if row.get("pushed_to_client") == 1:
                                pushed_uids.add(uid)
                            if row.get("gpu_id") is not None:
                                meta["gpu_id"] = row["gpu_id"]
                            if row.get("shift_id") is not None:
                                meta["shift_id"] = row.get("shift_id")
                            if row.get("shift_name"):
                                meta["shiftName"] = row.get("shift_name")
                            if row.get("tc_code"):
                                meta["tcCode"] = row.get("tc_code")
                            if row.get("sms_sent") is not None:
                                meta["sms_sent"] = row.get("sms_sent")
                except Exception as e:
                    logger.error(f"RTSP pushed uids fetch failed: {e}")
            conn.close()
        except Exception as e:
            logger.error(f"Failed to fetch pushed uids for paginated data: {e}")

    for item in paginated:
        uid = item.get("uid")
        meta = uid_db_meta.get(uid, {})
        item["pushed_to_client"] = 1 if uid in pushed_uids else 0
        item["sms_sent"] = 1 if meta.get("sms_sent") == 1 else 0
        # Filename gpu_id is primary (e.g. _gpu4_ token); only fall back to the
        # DB column when the filename had no _gpu token (older detections).
        db_gpu = meta.get("gpu_id")
        if db_gpu is not None and item.get("gpu_id") is None:
            item["gpu_id"] = db_gpu
        if meta.get("shift_id") is not None:
            item["shift_id"] = meta.get("shift_id")
        if meta.get("shiftName"):
            item["shiftName"] = meta.get("shiftName")
            item["shift_name"] = meta.get("shiftName")
        if meta.get("tcCode"):
            item["tcCode"] = meta.get("tcCode")
            item["tc_code"] = meta.get("tcCode")

    # Resolve shift names for the page (covers filename-derived shift_ids whose
    # camera_report row had a NULL shift_id) so the cards always show the shift.
    _attach_shift_names(paginated)

    return {
        "success": True,
        "total":   total,
        "page":    page,
        "limit":   limit,
        "counts":  all_counts,
        "data":    paginated,
    }



def _validate_relative_path(path_str: str) -> Path:
    candidate = Path(path_str)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise HTTPException(status_code=400, detail="Invalid relative path")
    return candidate


def _resolve_detection_source_path(source: str, type_key: str, relative_path: str) -> Path:
    if source not in {"hls", "rtsp"}:
        raise HTTPException(status_code=400, detail="Invalid source")
    if type_key not in DETECTION_FOLDERS:
        raise HTTPException(status_code=400, detail="Invalid detection type")

    folder_name = DETECTION_FOLDERS[type_key]
    base_dir = RESULTS_DIR if source == "hls" else RTSP_RESULTS_DIR
    safe_rel = _validate_relative_path(relative_path)
    abs_path = (base_dir / folder_name / safe_rel).resolve()

    if not str(abs_path).startswith(str((base_dir / folder_name).resolve())):
        raise HTTPException(status_code=400, detail="Invalid relative path")
    if not abs_path.exists() or not abs_path.is_file():
        raise HTTPException(status_code=404, detail=f"Source file not found: {relative_path}")

    return abs_path


class ClientDetectionCopyItem(BaseModel):
    source: str
    type: str
    relativePath: str
    uid: Optional[str] = None


class ClientDetectionCopyRequest(BaseModel):
    items: list[ClientDetectionCopyItem]
    # User id of the operator pushing these detections to the client.
    pushedBy: Optional[int] = None


# Strong refs to fire-and-forget background tasks so they are not GC'd mid-flight.
_BACKGROUND_TASKS: set = set()


async def _trigger_sms_alert(uid: str, source: str) -> None:
    """Fire-and-forget call to the Node SMS bridge after a detection is pushed.

    All gating (NTA exam, invigilator/proximity template, per-assignment toggle,
    idempotency, recipient resolution) lives in the Node endpoint. Any failure
    here must never affect the push, so everything is swallowed.
    """
    if not uid:
        return
    try:
        url = f"{MAIN_BACKEND_URL}/sms/send-push-alert"
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(url, json={"uid": uid, "source": source})
            try:
                logger.info(f"SMS bridge {uid} ({source}) -> {resp.status_code}: {resp.text[:200]}")
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"SMS bridge call failed for uid={uid}: {e}")


@app.post("/client-detections/copy")
async def copy_client_detections(payload: ClientDetectionCopyRequest):
    copied = []
    failed = []
    pushed_by = payload.pushedBy
    pushed_by_name = _user_names_for_ids([pushed_by]).get(pushed_by) if pushed_by else None

    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            for item in payload.items:
                if not item.uid:
                    failed.append({"relativePath": item.relativePath, "error": "No uid provided"})
                    continue
                
                table_name = "camera_report_rtsp" if item.source == "rtsp" else "camera_report_hls"
                folder_name = DETECTION_FOLDERS.get(item.type, item.type)
                item_uid = str(item.uid).lower()
                filename = Path(item.relativePath).name
                row = None
                
                # Fetch first. MySQL UPDATE rowcount can be 0 when the row is already pushed.
                try:
                    cur.execute(f"SELECT * FROM {table_name} WHERE unique_id = %s LIMIT 1", (item_uid,))
                    row = cur.fetchone()

                    if not row:
                        cur.execute(
                            f"""
                            SELECT * FROM {table_name}
                            WHERE detection_type = %s
                              AND (imageurl LIKE %s OR imageurl LIKE %s)
                            ORDER BY id DESC
                            LIMIT 1
                            """,
                            (folder_name, f"%/{filename}", f"%/{item.relativePath}"),
                        )
                        row = cur.fetchone()

                    if not row:
                        failed.append({"uid": item.uid, "error": f"uid {item.uid} not found"})
                        continue

                    cur.execute(
                        f"""
                        UPDATE {table_name}
                        SET pushed_to_client = 1,
                            pushed_by = %s,
                            unique_id = COALESCE(NULLIF(unique_id, ''), %s)
                        WHERE id = %s
                        """,
                        (pushed_by, item_uid, row.get("id")),
                    )
                    if not row.get("unique_id"):
                        row["unique_id"] = item_uid
                    pushed_at_ms = int(datetime.now().timestamp() * 1000)
                except Exception as e:
                    failed.append({"uid": item.uid, "error": f"DB update failed on {table_name}: {e}"})
                    continue
                
                if row:
                    imageurl_full = str(row.get("imageurl", ""))
                    filename = imageurl_full.split("/")[-1] if "/" in imageurl_full else imageurl_full
                    
                    copied_item = parse_detection_filename(
                        filename,
                        item.type,
                        file_path=None,
                        is_new_image=False,
                        source=item.source,
                    )
                    
                    copied_item.update({
                        "source": item.source,
                        "type": item.type,
                        "relativePath": item.relativePath,
                        "id": row.get("id"),
                        "cameraName": row.get("camera_name"),
                        "centerName": row.get("center_name", "") or row.get("centre_name", ""),
                        "city": row.get("city"),
                        "state": row.get("state"),
                        "tcCode": row.get("tc_code"),
                        "tc_code": row.get("tc_code"),
                        "shift_id": row.get("shift_id"),
                        "category": row.get("camera_category", ""),
                        "uid": row.get("unique_id"),
                        "updatedAt": pushed_at_ms,
                        "updated_at": pushed_at_ms,
                        # SMS now fires when the client ACCEPTS (not on push).
                        "sms_sent": 0,
                        "accepted": 0,
                        "pushedBy": pushed_by,
                        "pushed_by": pushed_by,
                        "pushedByName": pushed_by_name,
                    })
                    copied.append(copied_item)
                    
            shift_name_map = _shift_names_for_ids(item.get("shift_id") for item in copied)
            for item in copied:
                shift_id = item.get("shift_id")
                try:
                    shift_name = shift_name_map.get(int(shift_id)) if shift_id is not None else None
                except (TypeError, ValueError):
                    shift_name = None
                if shift_name:
                    item["shiftName"] = shift_name
                    item["shift_name"] = shift_name

            # After all updates, emit client detections and updated client counts
            for item in copied:
                await sio.emit("client-detection:new", item)
                
                # Emit detection:pushed event for multi-user synchronization
                # This notifies all connected users that this detection has been pushed
                await sio.emit("detection:pushed", {
                    "uid": item.get("uid"),
                    "pushedBy": pushed_by,
                    "pushedByName": pushed_by_name,
                    "pushedAt": item.get("updatedAt")
                })
            
            # Re-fetch total client counts for today
            try:
                today_iso = datetime.now().date().isoformat()
                cur.execute("""
                    SELECT camera_category, COUNT(*) as count 
                    FROM camera_report_hls 
                    WHERE exam_date = %s 
                    AND pushed_to_client = 1 
                    AND (remove_from_client IS NULL OR remove_from_client = 0)
                    GROUP BY camera_category
                """, (today_iso,))
                hls_c = cur.fetchall()
                
                cur.execute("""
                    SELECT camera_category, COUNT(*) as count 
                    FROM camera_report_rtsp 
                    WHERE exam_date = %s 
                    AND pushed_to_client = 1 
                    AND (remove_from_client IS NULL OR remove_from_client = 0)
                    GROUP BY camera_category
                """, (today_iso,))
                rtsp_c = cur.fetchall()
                
                client_counts = {}
                for row in hls_c + rtsp_c:
                    cat = row["camera_category"]
                    client_counts[cat] = client_counts.get(cat, 0) + row["count"]
                
                await sio.emit("client-detection:counts", client_counts)
                logger.info(f"Emitted client counts after copy: {client_counts}")
            except Exception as e:
                logger.error(f"Failed to emit client counts: {e}")
                
        conn.commit()
    except Exception as e:
        logger.error(f"DB Error in /client-detections/copy: {e}")
        return {"success": False, "error": str(e)}
    finally:
        conn.close()

    # NOTE: SMS alerts are NO LONGER sent on push. They are now sent when the
    # CLIENT accepts a detection (cctv_backend POST /client-detections/accept),
    # which delivers to the tc_code-mapped observer number(s). The push-time
    # _trigger_sms_alert bridge is intentionally not called here anymore.

    return {"success": True, "copied": copied, "failed": failed}


@app.get("/client-detections")
def list_client_detections(
    page: int = 1,
    limit: int = 100,
    source: Optional[str] = None,
    type: Optional[str] = None,
    date: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    search: Optional[str] = None,
    exam_id: Optional[int] = None,
    shift_id: Optional[int] = None,
    user_role: Optional[str] = None,
    user_id: Optional[int] = None,
    user_company: Optional[str] = None,
):
    all_counts = {k: 0 for k in DETECTION_FOLDERS}

    # Shift filter = that shift's time window folded into start/end (robust on
    # historical rows whose stored shift_id is NULL).
    start_time, end_time = _apply_shift_window(shift_id, start_time, end_time)

    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            # Resolve user exam IDs for non-admin role filtering via user_exam_assignments.
            user_exam_ids = []
            if user_role and user_role not in ('ADMIN', 'OPERATOR', 'service'):
                if user_id:
                    cur.execute(
                        "SELECT exam_id FROM user_exam_assignments WHERE user_id = %s",
                        (user_id,)
                    )
                    user_exam_ids = [row['exam_id'] for row in cur.fetchall()]
                    if not user_exam_ids:
                        return {
                            "success": True, "total": 0, "page": page, "limit": limit,
                            "counts": {k: 0 for k in DETECTION_FOLDERS}, "data": [],
                        }

            sql_folder_names: List[str] = []
            if type:
                sql_folder_names = [
                    DETECTION_FOLDERS[k] for k in type.split(',')
                    if k.strip() in DETECTION_FOLDERS
                ]

            # Date/time are filtered on created_at (an indexed TIMESTAMP that equals the
            # detection's insert time, ~seconds after capture) instead of the unreliable
            # varchar date_time. This lets total/per-type counts/pagination be computed
            # accurately in SQL (COUNT / GROUP BY / LIMIT-OFFSET) instead of capping the
            # fetch at 5000 and counting in Python (which under-reported the real total).
            created_lo = created_hi = None
            if date:
                try:
                    _d = datetime.strptime(str(date)[:10], "%Y-%m-%d")
                    created_lo = _d.strftime("%Y-%m-%d 00:00:00")
                    created_hi = (_d + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
                except Exception:
                    created_lo = created_hi = None

            def _build_where(include_type: bool) -> tuple:
                """WHERE shared by count/group/data queries. `include_type` adds the
                detection_type filter (kept OUT for the sidebar counts pass)."""
                # Accepted detections move to the "Accepted Detections" page and
                # should not linger here after a refresh — same convention as
                # remove_from_client already excluding removed ones.
                conds = ["pushed_to_client = 1", "remove_from_client = 0",
                         "(accepted_by_client IS NULL OR accepted_by_client = 0)"]
                params: list = []
                if exam_id:
                    conds.append("exam_id = %s"); params.append(exam_id)
                elif user_exam_ids:
                    ph = ','.join(['%s'] * len(user_exam_ids))
                    conds.append(f"exam_id IN ({ph})"); params.extend(user_exam_ids)
                if created_lo and created_hi:
                    conds.append("created_at >= %s AND created_at < %s")
                    params.extend([created_lo, created_hi])
                if start_time:
                    conds.append("TIME(created_at) >= %s"); params.append(f"{start_time}:00")
                if end_time:
                    conds.append("TIME(created_at) <= %s"); params.append(f"{end_time}:59")
                if search:
                    like = f"%{search}%"
                    conds.append("(camera_name LIKE %s OR center_name LIKE %s OR city LIKE %s "
                                 "OR state LIKE %s OR tc_code LIKE %s OR imageurl LIKE %s)")
                    params.extend([like] * 6)
                if include_type and sql_folder_names:
                    ph = ','.join(['%s'] * len(sql_folder_names))
                    conds.append(f"detection_type IN ({ph})"); params.extend(sql_folder_names)
                return " AND ".join(conds), params

            where_data, params_data = _build_where(include_type=True)
            where_cnt,  params_cnt  = _build_where(include_type=False)

            active_tables = []
            for table in ("camera_report_hls", "camera_report_rtsp"):
                if source == "rtsp" and table == "camera_report_hls":
                    continue
                if source == "hls" and table == "camera_report_rtsp":
                    continue
                active_tables.append(table)

            # ── Accurate total: SQL COUNT(*) summed across the active tables ──
            total = 0
            for table in active_tables:
                try:
                    cur.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE {where_data}",
                                tuple(params_data) if params_data else None)
                    total += int((cur.fetchone() or {}).get("c") or 0)
                except Exception as e:
                    logger.error(f"Client detections count failed ({table}): {e}")

            # ── Per-type sidebar counts: SQL GROUP BY (type filter excluded) ──
            for table in active_tables:
                try:
                    cur.execute(f"SELECT detection_type, COUNT(*) AS c FROM {table} "
                                f"WHERE {where_cnt} GROUP BY detection_type",
                                tuple(params_cnt) if params_cnt else None)
                    for r in cur.fetchall():
                        folder_name = r.get("detection_type", "")
                        key = next((k for k, v in DETECTION_FOLDERS.items() if v == folder_name), folder_name)
                        if key in all_counts:
                            all_counts[key] += int(r.get("c") or 0)
                except Exception as e:
                    logger.error(f"Client detections counts failed ({table}): {e}")

            # ── Page data: SQL ORDER BY created_at DESC, fetch up to this page, then
            #    merge the two tables and slice. No 5000 cap — any page is reachable. ──
            need = max(1, page * limit)
            db_rows: List[dict] = []
            for table in active_tables:
                try:
                    cur.execute(f"SELECT * FROM {table} WHERE {where_data} "
                                f"ORDER BY created_at DESC LIMIT {int(need)}",
                                tuple(params_data) if params_data else None)
                    for r in cur.fetchall():
                        r["_source"] = "rtsp" if table.endswith("rtsp") else "hls"
                        db_rows.append(r)
                except Exception as e:
                    logger.error(f"Client detections fetch failed ({table}): {e}")

            db_rows.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
            start_idx = (page - 1) * limit
            page_rows = db_rows[start_idx: start_idx + limit]

            shift_name_map = _shift_names_for_ids(row.get("shift_id") for row in page_rows)
            user_name_map = _user_names_for_ids(row.get("pushed_by") for row in page_rows)

            page_slice: List[tuple] = []  # (row, filename, type_key, source_part)
            for row in page_rows:
                imageurl_full = str(row.get("imageurl", ""))
                filename = imageurl_full.split("/")[-1] if "/" in imageurl_full else imageurl_full
                folder_name = row.get("detection_type", "")
                type_key = next((k for k, v in DETECTION_FOLDERS.items() if v == folder_name), folder_name)
                source_part = row.get("_source") or ("rtsp" if "rtsp" in imageurl_full else "hls")
                page_slice.append((row, filename, type_key, source_part))

            # ── Full parse only for the current page (at most `limit` rows) ──
            rows = []
            for row, filename, type_key, source_part in page_slice:
                item = parse_detection_filename(
                    filename, type_key, file_path=None, is_new_image=False, source=source_part,
                )
                item.update({
                    "source":      source_part,
                    "type":        type_key,
                    "filename":    filename,
                    "relativePath": filename,
                    "cameraName":  row.get("camera_name"),
                    "centerName":  row.get("center_name", "") or row.get("centre_name", ""),
                    "city":        row.get("city"),
                    "state":       row.get("state"),
                    "tcCode":      row.get("tc_code"),
                    "tc_code":     row.get("tc_code"),
                    # DB row shift_id wins; fall back to the shift parsed from the
                    # filename (set by parse_detection_filename) when the row is NULL.
                    "shift_id":    row.get("shift_id") or item.get("shift_id"),
                    "shiftName":   shift_name_map.get(row.get("shift_id")) or item.get("shiftName"),
                    "shift_name":  shift_name_map.get(row.get("shift_id")) or item.get("shift_name"),
                    # DB camera_category (HLS) wins; fall back to the scene
                    # classification parsed via cam_meta (RTSP classification),
                    # which is what the admin /detections endpoint already returns.
                    "category":    row.get("camera_category") or item.get("category") or "",
                    "exam_id":     row.get("exam_id"),
                    "uid":         row.get("unique_id"),
                    "pushedBy":    row.get("pushed_by"),
                    "pushed_by":   row.get("pushed_by"),
                    "pushedByName": user_name_map.get(row.get("pushed_by")),
                    "sms_sent":    1 if row.get("sms_sent") == 1 else 0,
                    "sms_sent_at": _epoch_ms(row.get("sms_sent_at")),
                    # Client "Accept" state (cctv_backend sets accepted_by_client).
                    "accepted":    1 if row.get("accepted_by_client") == 1 else 0,
                    "accepted_at": _epoch_ms(row.get("accepted_at")),
                    "_mtime":      _epoch_ms(row.get("created_at")) or 0,
                    "updatedAt":   _epoch_ms(row.get("updated_at")),
                    "updated_at":  _epoch_ms(row.get("updated_at")),
                })
                rows.append(item)

            _attach_shift_names(rows)

    finally:
        conn.close()

    return {
        "success": True,
        "total":   total,
        "page":    page,
        "limit":   limit,
        "counts":  all_counts,
        "data":    rows,
    }

class ClientDetectionRemoveRequest(BaseModel):
    uid: str

@app.post("/client-detections/remove")
async def remove_client_detection(payload: ClientDetectionRemoveRequest):
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            try:
                sql_hls = "UPDATE camera_report_hls SET remove_from_client = 1 WHERE unique_id = %s"
                cur.execute(sql_hls, (payload.uid,))
            except Exception: pass
            
            try:
                sql_rtsp = "UPDATE camera_report_rtsp SET remove_from_client = 1 WHERE unique_id = %s"
                cur.execute(sql_rtsp, (payload.uid,))
            except Exception: pass
            
            # Re-fetch total client counts after removal
            try:
                today_iso = datetime.now().date().isoformat()
                cur.execute("""
                    SELECT camera_category, COUNT(*) as count 
                    FROM camera_report_hls 
                    WHERE exam_date = %s 
                    AND pushed_to_client = 1 
                    AND (remove_from_client IS NULL OR remove_from_client = 0)
                    GROUP BY camera_category
                """, (today_iso,))
                hls_c = cur.fetchall()
                
                cur.execute("""
                    SELECT camera_category, COUNT(*) as count 
                    FROM camera_report_rtsp 
                    WHERE exam_date = %s 
                    AND pushed_to_client = 1 
                    AND (remove_from_client IS NULL OR remove_from_client = 0)
                    GROUP BY camera_category
                """, (today_iso,))
                rtsp_c = cur.fetchall()
                
                client_counts = {}
                for row in hls_c + rtsp_c:
                    cat = row["camera_category"]
                    client_counts[cat] = client_counts.get(cat, 0) + row["count"]
                
                # Emit updated counts to all connected clients
                await sio.emit("client-detection:counts", client_counts)
                logger.info(f"Emitted updated client counts after removal: {client_counts}")
            except Exception as e:
                logger.error(f"Failed to emit client counts after removal: {e}")
        
        conn.commit()
        
        await sio.emit("client-detection:remove", {"uid": payload.uid, "success": True})
        
        return {"success": True, "message": "Removed successfully"}
    except Exception as e:
        logger.error(f"DB Error in /client-detections/remove: {e}")
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


# ============================================================
# Operator sharding: operators see only detections from their assigned gpu_ids.
# admin / client roles (and unauthenticated) see ALL detections.
# ============================================================

def get_operator_gpu_ids(user_id) -> list:
    """Return the list of gpu_ids assigned to an operator (empty if none)."""
    try:
        conn = _get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT gpu_id FROM users WHERE id = %s", (int(user_id),))
                rows = cur.fetchall()
                if rows and rows[0].get("gpu_id") is not None:
                    return [int(rows[0]["gpu_id"])]
                return []
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"[operator_shard] lookup failed for user {user_id}: {e}")
        return []


@sio.event
async def connect(sid, environ, auth):
    """Role-based rooms:
       - operator  → join only gpu:{id} rooms for their assigned gpu_ids
       - admin/client/unauthenticated → join the 'all' room (every detection)
    """
    auth = auth or {}
    role = str(auth.get("role") or "").strip().lower()
    user_id = auth.get("user_id")

    joined = []
    if role == "operator" and user_id not in (None, "", 0, "0"):
        gpu_ids = await asyncio.to_thread(get_operator_gpu_ids, user_id)
        for g in gpu_ids:
            await sio.enter_room(sid, f"gpu:{g}")
            joined.append(f"gpu:{g}")
        if not joined:
            logger.warning(f"Operator user_id={user_id} has no gpu assignment — will receive no live detections")
    else:
        # admin / client / unauthenticated → see everything
        await sio.enter_room(sid, "all")
        joined.append("all")

    logger.info(f"Socket.IO connected sid={sid} role={role or 'none'} rooms={joined}")
    await sio.emit("detections:connected", {"success": True, "role": role, "rooms": joined}, to=sid)


@sio.event
async def disconnect(sid):
    logger.info(f"Socket.IO client disconnected: {sid}")


fastapi_app = app
_socket_app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app, socketio_path="socket.io")


class _ResilientASGIApp:
    """Top-level ASGI guard.

    The embedded Socket.IO/engineio polling handler intermittently raises
    ``RuntimeError: Response content longer/shorter than Content-Length`` under
    heavy ``/socket`` load. As the outermost ASGI app that exception propagated
    into uvicorn's serve TaskGroup and crashed the whole process — which made pm2
    restart the poller and SIGINT-kill any in-flight (multi-hour) NEET
    classification job, so classification never completed.

    Swallow per-request (http/websocket) errors here so one bad request can never
    take down the server. Lifespan errors are always re-raised (startup/shutdown
    must surface). Errors after the response has started can't be recovered, but
    ending the request quietly is far better than killing the process.
    """

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "lifespan":
            await self._app(scope, receive, send)
            return
        try:
            await self._app(scope, receive, send)
        except RuntimeError as exc:
            if "Content-Length" in str(exc):
                logger.warning(
                    f"Suppressed ASGI Content-Length error on {scope.get('path')!r}: {exc}"
                )
                return
            logger.exception(f"Suppressed ASGI RuntimeError on {scope.get('path')!r}")
            return
        except Exception:  # noqa: BLE001 — never let a request crash the server
            logger.exception(f"Suppressed unhandled ASGI error on {scope.get('path')!r}")
            return


app = _ResilientASGIApp(_socket_app)
