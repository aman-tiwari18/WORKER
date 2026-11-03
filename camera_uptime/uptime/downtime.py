"""Down periods per camera and run.

A period starts at the first DOWN check and ends at the next UP check (down_s = up check - down check);
its reason is the reason of the DOWN check that started it.
UNKNOWN checks (our side failed) neither start nor end one. A camera still down when the run closes
gets a period with up_at = null, `until` = window end and still_down = true.

The writer keeps these up to date check by check (camera_uptime_summary.down_since / outages /
downtime_s / downtime_json); `periods()` recomputes them from the raw checks for rebuild-summary.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

FMT = "%Y-%m-%d %H:%M:%S"
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _secs(a: datetime, b: datetime) -> int:
    return max(0, int((b - a).total_seconds()))


def periods(checks, end: datetime | None = None) -> tuple[list[dict], int, int, datetime | None, str | None]:
    """checks: (checked_at, result, reason) in time order. With `end`, a period still open is closed there.
    Returns (periods, downtime_s, outages, down_since, down_reason)."""
    out, total, outages, since, why = [], 0, 0, None, None
    for t, result, reason in checks:
        if result == "DOWN" and since is None:
            since, why, outages = t, reason, outages + 1
        elif result == "UP" and since is not None:
            s = _secs(since, t)
            out.append({"down_at": since.strftime(FMT), "up_at": t.strftime(FMT), "down_s": s, "reason": why})
            total, since, why = total + s, None, None
    if since is not None and end is not None:
        s = _secs(since, end)
        out.append({"down_at": since.strftime(FMT), "up_at": None, "until": end.strftime(FMT), "down_s": s,
                    "reason": why, "still_down": True})
        total, since, why = total + s, None, None
    return out, total, outages, since, why


# Run closed: open periods end at the window end (or now, if the run closes early). Single-table
# UPDATE assigns left to right, so down_since is cleared only after the other columns used it.
CLOSE_OPEN_SQL = """
UPDATE camera_uptime_summary
SET downtime_json = JSON_ARRAY_APPEND(COALESCE(downtime_json, JSON_ARRAY()), '$', JSON_OBJECT(
      'down_at', DATE_FORMAT(down_since, '%%Y-%%m-%%d %%H:%%i:%%s'), 'up_at', NULL,
      'until', DATE_FORMAT(%s, '%%Y-%%m-%%d %%H:%%i:%%s'),
      'down_s', GREATEST(0, TIMESTAMPDIFF(SECOND, down_since, %s)), 'reason', down_reason, 'still_down', TRUE)),
    downtime_s = downtime_s + GREATEST(0, TIMESTAMPDIFF(SECOND, down_since, %s)),
    down_reason = NULL,
    down_since = NULL
WHERE run_id = %s AND down_since IS NOT NULL
"""


def close_open(db, run_id: int, end: datetime) -> int:
    return db.execute(CLOSE_OPEN_SQL, (end, end, end, run_id))


def rebuild(db, run_id: int) -> int:
    """Recompute every camera's down periods of a run from its raw checks."""
    run = db.query("SELECT status, window_end FROM camera_uptime_runs WHERE id = %s", (run_id,))
    if not run:
        return 0
    end = None
    if run[0]["status"] != "RUNNING":
        end = min(run[0]["window_end"], now_ist())
    by_cam: dict[str, list] = {}
    for r in db.query("SELECT camera_key, checked_at, result, reason FROM camera_uptime_checks WHERE run_id = %s "
                      "ORDER BY camera_key, checked_at, id", (run_id,)):
        by_cam.setdefault(r["camera_key"], []).append((r["checked_at"], r["result"], r["reason"]))
    db.execute("UPDATE camera_uptime_summary SET down_since = NULL, down_reason = NULL, outages = 0, downtime_s = 0, "
               "downtime_json = NULL WHERE run_id = %s", (run_id,))
    n = 0
    for key, checks in by_cam.items():
        ps, total, outages, since, why = periods(checks, end)
        if not outages:
            continue
        n += db.execute("UPDATE camera_uptime_summary SET down_since = %s, down_reason = %s, outages = %s, "
                        "downtime_s = %s, downtime_json = %s WHERE run_id = %s AND camera_key = %s",
                        (since, why, outages, total, json.dumps(ps) if ps else None, run_id, key))
    return n
