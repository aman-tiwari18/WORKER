"""
server_room_thresholds_db.py — server-room person limits uploaded per exam (Exams page).

Replaces editing server_room_constants.py for every new exam: the admin uploads the
threshold sheet on the exam (HLS or RTSP) and workers read it from the database.

    lookup(tc_code, exam_id, exam_date=None, shift_id=None) -> (has_rows, limit)

    has_rows  False  the exam has no uploaded sheet (or it could not be read) — the caller
                     falls back to the legacy constants file
              True   the exam HAS an uploaded sheet; it is authoritative. limit is None when
                     the centre is not in it -> no server-room detection there (never a guess)

Resolution, most specific first: this exact sitting (exam_date + shift_id), then the
exam-wide row. exam_date / shift_id default to the run's launch env (ASSIGNMENT_DATE,
SHIFT_ID), so the analytic scripts do not have to pass them.

The sheet gives fractional limits (11.01) and detection alerts when person_count > limit;
person counts are whole numbers, so the limit is rounded DOWN ("more than 11.01" and
"more than 11" both mean 12+) — the same convention the constants file used.

Transport follows DB_ACCESS_MODE: backend -> GET /worker-db/server-room-thresholds,
direct -> pymysql through db_client. Results are cached SERVER_ROOM_THRESHOLD_TTL_S
(default 300 s) per exam; if a refresh fails the last good copy keeps being used.
"""
import logging
import math
import os
import re
import threading
import time

logger = logging.getLogger(__name__)

_TTL_S = float(os.environ.get("SERVER_ROOM_THRESHOLD_TTL_S", "300"))
_FAIL_RETRY_S = 30.0
_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")

_lock = threading.Lock()
# exam_id -> {"at": monotonic, "exam": {tc: limit}, "sittings": {(date, shift_id): {tc: limit}}, "rows": n, "ok": bool}
_cache = {}
_warned = {}


def _warn_once(key, msg, every=300):
    now = time.monotonic()
    if now - _warned.get(key, -1e9) >= every:
        _warned[key] = now
        logger.warning(msg)


def _floor(v):
    return int(math.floor(float(v) + 1e-9))


def _run_slot():
    m = _DATE_PREFIX.match(os.environ.get("ASSIGNMENT_DATE", "").strip())
    try:
        shift_id = int(os.environ.get("SHIFT_ID", "").strip())
    except ValueError:
        shift_id = None
    return (m.group(0) if m else None), shift_id


def _fetch_rows(exam_id):
    """Rows [{tc_code, exam_date, shift_id, max_persons}] from the backend or the DB."""
    if os.environ.get("DB_ACCESS_MODE", "direct").lower() == "backend":
        import requests
        base = os.environ.get("MAIN_BACKEND_URL", "").rstrip("/")
        r = requests.get(f"{base}/worker-db/server-room-thresholds", params={"exam_id": exam_id},
                         headers={"X-API-Key": os.environ.get("API_KEY", "")},
                         timeout=float(os.environ.get("DB_GATEWAY_TIMEOUT_S", "15")))
        r.raise_for_status()
        return r.json().get("data") or []
    try:
        from utils import db_client
    except ImportError:
        import db_client  # when utils/ is on sys.path directly
    import pymysql
    conn = db_client._get_connection()
    with conn:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT tc_code, exam_date, shift_id, max_persons FROM server_room_thresholds WHERE exam_id = %s", (exam_id,))
            except pymysql.err.ProgrammingError as e:
                if e.args and e.args[0] == 1146:        # migration 046 not applied: no uploaded sheets
                    return []
                raise
            rows = cur.fetchall()
    for r in rows:
        if r.get("exam_date") is not None:
            r["exam_date"] = str(r["exam_date"])[:10]
    return rows


def _load(exam_id):
    """Entry for an exam, refreshed at most every TTL; keeps the last good copy on failure."""
    key = str(exam_id)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit["at"] < (_TTL_S if hit["ok"] else _FAIL_RETRY_S):
            return hit
    try:
        rows = _fetch_rows(int(exam_id))
        exam_wide, sittings = {}, {}
        for r in rows:
            tc = str(r["tc_code"]).strip()
            limit = _floor(r["max_persons"])
            if r.get("exam_date") and r.get("shift_id") is not None:
                sittings.setdefault((str(r["exam_date"])[:10], int(r["shift_id"])), {})[tc] = limit
            else:
                exam_wide[tc] = limit
        entry = {"at": now, "exam": exam_wide, "sittings": sittings, "rows": len(rows), "ok": True}
        if not hit or hit.get("rows") != entry["rows"]:
            logger.info(f"[ServerRoom] exam {exam_id}: {len(rows)} uploaded threshold row(s)"
                        if rows else f"[ServerRoom] exam {exam_id}: no uploaded thresholds — legacy constants apply")
    except Exception as e:
        _warn_once(f"fetch:{key}", f"[ServerRoom] could not read thresholds for exam {exam_id}: {e}"
                                   + (" — using the last copy" if hit else " — falling back to the legacy constants"))
        entry = dict(hit) if hit else {"exam": {}, "sittings": {}, "rows": 0}
        entry["at"], entry["ok"] = now, False
    with _lock:
        _cache[key] = entry
    return entry


def lookup(tc_code, exam_id, exam_date=None, shift_id=None):
    """(has_rows, limit) — see the module docstring."""
    if exam_id is None or str(exam_id).strip() in ("", "0"):
        return False, None
    try:
        entry = _load(exam_id)
    except Exception:           # never let a threshold lookup break detection
        return False, None
    if not entry["rows"]:
        return False, None
    tc = str(tc_code or "").strip()
    if exam_date is None or shift_id is None:
        d, s = _run_slot()
        exam_date = exam_date or d
        shift_id = shift_id if shift_id is not None else s
    if exam_date and shift_id is not None:
        sitting = entry["sittings"].get((str(exam_date)[:10], int(shift_id)))
        if sitting and tc in sitting:
            return True, sitting[tc]
    return True, entry["exam"].get(tc)


def clear_cache():
    with _lock:
        _cache.clear()
