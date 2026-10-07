"""
db_gateway.py — HTTP-backed replacement for db_client's DB calls (plan §4.0/§4.5B).

When DB_ACCESS_MODE=backend, db_client rebinds its 17 public functions to the
ones here, so the detection scripts keep calling the SAME functions with the SAME
signatures — only the transport changes (HTTP to the backend instead of a direct
pymysql connection). The worker then needs no DB credentials or network path to
MySQL at all, which is what lets the identical image run on any machine that can
merely reach the backend.

Two behaviours are preserved deliberately:
  - Reads still apply filter_cameras() locally. Camera distribution / lease
    ownership is worker-identity-specific and must stay on the worker; the
    backend returns the raw rows exactly as the old SELECT did before filtering.
  - Detection writes still EXIF-stamp the local image before reporting (the image
    lives on this worker's disk; only the DB row travels to the backend), and are
    buffered-and-retried so a brief backend blip never loses a detection — a
    safety net the direct-INSERT path never had.
"""
import os
import re
import json
import time
import threading
from datetime import datetime

import requests

from app.logger import logger  # worker logger; falls back below if unavailable

_BACKEND = os.environ.get("MAIN_BACKEND_URL", "http://host.docker.internal:4000/current_dev_mode/api").rstrip("/")
_HEADERS = {"X-API-Key": os.environ.get("API_KEY", "")}
_TIMEOUT = float(os.environ.get("DB_GATEWAY_TIMEOUT_S", "15"))
_GPU_ID = None  # resolved lazily from db_client to avoid an import cycle


def _gpu_id():
    global _GPU_ID
    if _GPU_ID is None:
        try:
            from utils import db_client
            _GPU_ID = db_client._GPU_ID
        except Exception:
            _GPU_ID = int(os.environ.get("GPU_ID", os.environ.get("CUDA_VISIBLE_DEVICES", "0") or "0"))
    return _GPU_ID


# ── camera reads ─────────────────────────────────────────────────────────────
def _filter(rows):
    """Apply the worker's distribution/lease filter, exactly as db_client did."""
    try:
        try:
            from app.camera_distributor import filter_cameras
        except ImportError:
            import sys
            from pathlib import Path
            root = str(Path(__file__).resolve().parents[3])
            if root not in sys.path:
                sys.path.append(root)
            from app.camera_distributor import filter_cameras
        return filter_cameras(rows)
    except Exception as e:
        logger.warning(f"[gateway] camera filter unavailable, returning all: {e}")
        return rows


def _get_cameras(params, apply_filter):
    try:
        r = requests.get(f"{_BACKEND}/worker-db/cameras", params=params, headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        rows = r.json().get("data") or []
        return _filter(rows) if apply_filter else rows
    except Exception as e:
        logger.error(f"[gateway] get cameras failed ({params}): {e}")
        return []


def get_hls_cameras(exam_id, shift_id, exam_date):
    return _get_cameras({"link_type": "hls", "exam_id": exam_id, "shift_id": shift_id,
                         "exam_date": exam_date, "accessible": 0}, True)


def get_hls_cameras_accessible(exam_id, shift_id, exam_date):
    return _get_cameras({"link_type": "hls", "exam_id": exam_id, "shift_id": shift_id,
                         "exam_date": exam_date, "accessible": 1}, True)


_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _run_slot():
    """(date 'YYYY-MM-DD', shift_id) of this run from the launch env, or (None, None)."""
    m = _DATE_PREFIX.match(os.environ.get("ASSIGNMENT_DATE", "").strip())
    try:
        shift_id = int(os.environ.get("SHIFT_ID", "").strip())
    except ValueError:
        shift_id = None
    return (m.group(0) if m else None), shift_id


def _rtsp_params(exam_id, accessible):
    """RTSP camera query; the backend applies the exam's centre schedule for date + shift."""
    p = {"link_type": "rtsp", "exam_id": exam_id, "accessible": accessible}
    date, shift_id = _run_slot()
    if date and shift_id is not None:
        p["exam_date"], p["shift_id"] = date, shift_id
    return p


def get_rtsp_cameras(exam_id):
    return _get_cameras(_rtsp_params(exam_id, 0), True)


def get_rtsp_cameras_accessible(exam_id):
    return _get_cameras(_rtsp_params(exam_id, 1), True)


def get_rtsp_camera_by_id(camera_id_pk):
    if not camera_id_pk:
        return {}
    rows = _get_cameras({"link_type": "rtsp", "id": camera_id_pk}, False)
    return rows[0] if rows else {}


def get_hls_camera_by_ch_code(ch_code, exam_date=None):
    if not ch_code:
        return {}
    p = {"link_type": "hls", "ch_code": ch_code}
    if exam_date:
        p["exam_date"] = exam_date
    rows = _get_cameras(p, False)
    return rows[0] if rows else {}


def get_rtsp_camera_by_device_and_ch(device_id, camera_no):
    if not device_id or not camera_no:
        return {}
    rows = _get_cameras({"link_type": "rtsp", "device_id": device_id, "camera_no": camera_no}, False)
    return rows[0] if rows else {}


# ── classification writes ────────────────────────────────────────────────────
def _classify(payload):
    try:
        r = requests.post(f"{_BACKEND}/worker-db/classification", json=payload, headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        return int(r.json().get("affected", 0))
    except Exception as e:
        logger.error(f"[gateway] classification {payload.get('op')} failed: {e}")
        return 0


def update_hls_classification(exam_id, shift_id, exam_date, ch_code, classification, status=None, frame_path=None):
    return _classify({"op": "update_hls", "exam_id": exam_id, "shift_id": shift_id, "exam_date": exam_date,
                      "ch_code": ch_code, "classification": classification, "status": status, "frame_path": frame_path})


def update_hls_status(exam_id, shift_id, exam_date, ch_code, status):
    return _classify({"op": "update_hls_status", "exam_id": exam_id, "shift_id": shift_id,
                      "exam_date": exam_date, "ch_code": ch_code, "status": status})


def update_rtsp_classification(exam_id, device_id, camera_no, classification, status=None, frame_path=None):
    return _classify({"op": "update_rtsp", "exam_id": exam_id, "device_id": device_id, "camera_no": camera_no,
                      "classification": classification, "status": status, "frame_path": frame_path})


def update_rtsp_status(exam_id, device_id, camera_no, status):
    return _classify({"op": "update_rtsp_status", "exam_id": exam_id, "device_id": device_id,
                      "camera_no": camera_no, "status": status})


def update_rtsp_status_and_frame_path(exam_id, device_id, camera_no, status, frame_path):
    return _classify({"op": "update_rtsp_status_frame", "exam_id": exam_id, "device_id": device_id,
                      "camera_no": camera_no, "status": status, "frame_path": frame_path})


def insert_hls_classification_history(camera_id, shift_id, exam_date, frame_path, classification):
    return _classify({"op": "history_hls", "camera_id": camera_id, "shift_id": shift_id,
                      "exam_date": exam_date, "frame_path": frame_path, "classification": classification})


def insert_rtsp_classification_history(camera_id, shift_id, exam_date, frame_path, classification):
    return _classify({"op": "history_rtsp", "camera_id": camera_id, "shift_id": shift_id,
                      "exam_date": exam_date, "frame_path": frame_path, "classification": classification})


# ── detection ingest (buffered + retried) ────────────────────────────────────
# A brief backend outage must never drop a detection, so failed POSTs spill to a
# small on-disk buffer and a background thread re-sends them. The GPU never blocks
# on the DB: the report call returns immediately whether or not the backend is up.
_BUFFER_DIR = os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), "results", "_ingest_buffer"
)
_buffer_lock = threading.Lock()
_flusher_started = False


def _ensure_flusher():
    global _flusher_started
    if _flusher_started:
        return
    _flusher_started = True
    os.makedirs(_BUFFER_DIR, exist_ok=True)
    t = threading.Thread(target=_flush_loop, name="ingest-buffer-flusher", daemon=True)
    t.start()


def _flush_loop():
    interval = float(os.environ.get("INGEST_FLUSH_INTERVAL_S", "20"))
    while True:
        time.sleep(interval)
        try:
            pending = [f for f in os.listdir(_BUFFER_DIR) if f.endswith(".json")]
        except FileNotFoundError:
            continue
        for name in pending:
            path = os.path.join(_BUFFER_DIR, name)
            try:
                with open(path) as fh:
                    payload = json.load(fh)
            except Exception:
                try: os.remove(path)
                except OSError: pass
                continue
            if _post_ingest(payload):
                try: os.remove(path)
                except OSError: pass
                logger.info(f"[gateway] buffered detection flushed: {name}")


def _post_ingest(payload):
    try:
        r = requests.post(f"{_BACKEND}/worker-db/ingest", json=payload, headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"[gateway] ingest POST failed ({payload.get('link_type')}): {e}")
        return False


def _buffer(payload):
    _ensure_flusher()
    try:
        with _buffer_lock:
            fname = f"{payload.get('link_type','x')}_{payload.get('unique_id') or int(time.time()*1000)}.json"
            with open(os.path.join(_BUFFER_DIR, fname), "w") as fh:
                json.dump(payload, fh)
        logger.warning(f"[gateway] detection buffered for retry: {fname}")
    except Exception as e:
        logger.error(f"[gateway] failed to buffer detection: {e}")


def _extract_unique_id(image_name):
    try:
        from utils import db_client
        return db_client._extract_unique_id_from_image_name(image_name)
    except Exception:
        base = os.path.basename(str(image_name))
        m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', base, re.I)
        return m.group(1).lower() if m else None


def _embed(image_rel_path, folder, hls):
    """EXIF-stamp the local image (unchanged from db_client — file is local)."""
    try:
        from utils import db_client
        base = db_client._DETECTION_BASE
        if hls:
            path = os.path.join(base, "results", "tcs_detections", folder, image_rel_path)
        else:
            path = os.path.join(base, "results", folder, image_rel_path)
        db_client._embed_gpu_id_in_image(path, _gpu_id())
    except Exception as e:
        logger.debug(f"[gateway] EXIF embed skipped: {e}")


def report_to_db_hls(ch_code, image_name, folder="crowd_detection",
                     person_count=None, threshold=None, violation_count=None, violation_chain=None):
    exam_id = os.environ.get("EXAM_ID")
    if not exam_id:
        logger.error("[gateway] EXAM_ID not set; cannot report HLS detection")
        return False
    _embed(image_name, folder, hls=True)
    payload = {"link_type": "hls", "ch_code": ch_code, "image_name": image_name, "folder": folder,
               "exam_id": exam_id, "shift_id": os.environ.get("SHIFT_ID"), "gpu_id": _gpu_id(),
               "unique_id": _extract_unique_id(image_name),
               "person_count": person_count, "threshold": threshold, "violation_count": violation_count,
               "violation_chain": violation_chain}
    if _post_ingest(payload):
        return True
    _buffer(payload)
    return True  # accepted for delivery (buffered) — never block the detector


def report_to_db_rtsp(device_id, camera_no, image_name, folder="crowd_detection",
                      person_count=None, threshold=None, violation_count=None, violation_chain=None):
    exam_id = os.environ.get("EXAM_ID")
    if not exam_id:
        logger.error("[gateway] EXAM_ID not set; cannot report RTSP detection")
        return False
    if not device_id or not camera_no:
        parts = image_name.split('_')
        if len(parts) >= 4:
            device_id = device_id or parts[1]
            camera_no = camera_no or parts[2]
    _embed(image_name, folder, hls=False)
    payload = {"link_type": "rtsp", "device_id": device_id, "camera_no": camera_no, "image_name": image_name,
               "folder": folder, "exam_id": exam_id, "shift_id": os.environ.get("SHIFT_ID"),
               "gpu_id": _gpu_id(), "unique_id": _extract_unique_id(image_name),
               "person_count": person_count, "threshold": threshold, "violation_count": violation_count,
               "violation_chain": violation_chain}
    if _post_ingest(payload):
        return True
    _buffer(payload)
    return True
