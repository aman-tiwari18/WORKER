"""camera_uptime_runs lifecycle, summary seeding and run counters."""
from __future__ import annotations

import json
import logging
from datetime import datetime

from . import downtime
from .roster import Camera
from .schedule import Window

log = logging.getLogger("uptime.runs")

OPEN_SQL = """
INSERT INTO camera_uptime_runs
  (exam_id, shift_id, exam_date, exam_name, link_type, shift_name, shift_start, shift_end,
   window_start, window_end, status, config_json, started_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'RUNNING',%s,NOW(3)) AS new
ON DUPLICATE KEY UPDATE
  exam_name = new.exam_name, link_type = new.link_type, shift_name = new.shift_name,
  shift_start = new.shift_start, shift_end = new.shift_end, window_start = new.window_start,
  window_end = new.window_end, status = 'RUNNING', ended_at = NULL, config_json = new.config_json
"""

SEED_COLS = ("run_id, exam_id, shift_id, exam_date, link_type, camera_key, camera_row_id, camera_name, "
             "centre_name, tc_code, city, state, server, dvr_key, in_roster")
SEED_UPDATE = (" AS new ON DUPLICATE KEY UPDATE camera_row_id = new.camera_row_id, camera_name = new.camera_name, "
               "centre_name = new.centre_name, tc_code = new.tc_code, city = new.city, state = new.state, "
               "server = new.server, dvr_key = new.dvr_key, in_roster = 1")

COUNTERS_SQL = """
UPDATE camera_uptime_runs r
JOIN (SELECT run_id, COUNT(*) AS total, SUM(checks_up + checks_down > 0) AS checked,
             SUM(checks_up > 0) AS up_once, SUM(checks_total) AS checks, MIN(checks_total) AS passes
        FROM camera_uptime_summary WHERE run_id = %s AND in_roster = 1 GROUP BY run_id) s ON s.run_id = r.id
SET r.cameras_total = s.total, r.cameras_checked = s.checked, r.cameras_up_once = s.up_once,
    r.checks_total = s.checks, r.passes_completed = s.passes, r.paused_seconds = %s
WHERE r.id = %s
"""


def open_run(db, w: Window, config: dict) -> int:
    db.execute(OPEN_SQL, (w.exam_id, w.shift_id, w.exam_date, w.exam_name[:255], w.link_type,
                          w.shift_name[:100], w.shift_start, w.shift_end, w.window_start, w.window_end,
                          json.dumps(config, default=str)))
    rows = db.query("SELECT id FROM camera_uptime_runs WHERE exam_id = %s AND shift_id = %s AND exam_date = %s",
                    (w.exam_id, w.shift_id, w.exam_date))
    return int(rows[0]["id"])


def seed(db, run_id: int, w: Window, cams: dict[str, Camera], chunk: int = 300,
         owns=None) -> tuple[int, int]:
    """NOT_CHECKED summary rows for every roster camera (so never-reached cameras still show up),
    metadata refreshed; cameras no longer in the roster get in_roster = 0. Returns (seeded, removed).
    `owns(dvr_key)` limits the removal to this instance's shard: other workers seed the rest of the run."""
    rows = []
    for key, cam in cams.items():
        m = next((m for m in cam.members if m.run_key == w.key), cam.members[0])
        rows.append((run_id, w.exam_id, w.shift_id, w.exam_date, w.link_type, key, m.camera_row_id,
                     m.camera_name, m.centre_name, m.tc_code, m.city, m.state, cam.server, cam.dvr_key, 1))
    ph = "(" + ",".join(["%s"] * 15) + ")"
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        db.execute(f"INSERT INTO camera_uptime_summary ({SEED_COLS}) VALUES " + ",".join([ph] * len(part)) + SEED_UPDATE,
                   [v for r in part for v in r])
    current = {r["camera_key"] for r in db.query(
        "SELECT camera_key, dvr_key FROM camera_uptime_summary WHERE run_id = %s AND in_roster = 1", (run_id,))
        if owns is None or owns(r["dvr_key"])}
    gone = sorted(current - set(cams))
    for i in range(0, len(gone), chunk):
        part = gone[i:i + chunk]
        db.execute("UPDATE camera_uptime_summary SET in_roster = 0 WHERE run_id = %s AND camera_key IN ("
                   + ",".join(["%s"] * len(part)) + ")", [run_id, *part])
    return len(rows), len(gone)


def update_counters(db, run_id: int, paused_seconds: float = 0.0) -> None:
    db.execute(COUNTERS_SQL, (run_id, int(paused_seconds), run_id))


def close_run(db, run_id: int, status: str, paused_seconds: float = 0.0) -> None:
    update_counters(db, run_id, paused_seconds)
    if status != "STOPPED":  # STOPPED = window changed, the same run reopens and its periods go on
        rows = db.query("SELECT window_end FROM camera_uptime_runs WHERE id = %s", (run_id,))
        if rows:
            downtime.close_open(db, run_id, min(rows[0]["window_end"], downtime.now_ist()))
    db.execute("UPDATE camera_uptime_runs SET status = %s, ended_at = NOW(3) WHERE id = %s", (status, run_id))


def close_stale(db, now: datetime) -> int:
    """Runs left RUNNING by a service that was down when their window ended."""
    stale = db.query("SELECT id FROM camera_uptime_runs WHERE status = 'RUNNING' AND window_end <= %s", (now,))
    for r in stale:
        close_run(db, int(r["id"]), "INTERRUPTED")
    return len(stale)


def last_checked_epochs(db, exam_ids: list[int]) -> dict[str, float]:
    if not exam_ids:
        return {}
    rows = db.query(
        "SELECT camera_key, MAX(last_checked_at) AS t FROM camera_uptime_summary WHERE exam_id IN ("
        + ",".join(["%s"] * len(exam_ids)) + ") AND last_checked_at IS NOT NULL GROUP BY camera_key",
        list(exam_ids))
    return {r["camera_key"]: r["t"].timestamp() for r in rows if r.get("t")}


VISION_RUNNING_SQL = """
SELECT DISTINCT es.exam_id AS exam_id
  FROM shift_script_assignments ssa
  JOIN scripts s ON s.id = ssa.script_id
  JOIN exam_shifts es ON es.id = ssa.shift_id
 WHERE ssa.status = 'RUNNING' AND ssa.is_active = 1 AND s.script_id LIKE %s
   AND ssa.assignment_date BETWEEN %s AND %s
   AND %s BETWEEN TIMESTAMP(ssa.assignment_date, COALESCE(ssa.custom_start_time, es.start_time))
              AND TIMESTAMP(ssa.assignment_date, COALESCE(ssa.custom_end_time, es.end_time)) + INTERVAL 5 MINUTE
"""


def vision_running_exams(db, now: datetime) -> set[int]:
    """Read-only courtesy check: exams with a vision classification run in progress right now."""
    from datetime import timedelta

    rows = db.query(VISION_RUNNING_SQL, ("vision%", (now - timedelta(days=1)).date(), now.date(), now))
    return {int(r["exam_id"]) for r in rows}


# ── auto sharding: live instances of the checker (one per server) ─────────────────────────────────
INSTANCES_TABLE = "camera_uptime_instances"


def heartbeat(db, worker_id: str, host: str) -> None:
    db.execute(f"INSERT INTO {INSTANCES_TABLE} (worker_id, host, started_at, last_seen) VALUES (%s, %s, NOW(3), NOW(3)) "
               "ON DUPLICATE KEY UPDATE host = VALUES(host), last_seen = NOW(3)", (worker_id, host))


def live_instances(db, ttl_s: int) -> list[str]:
    """Instances seen in the last ttl_s seconds, in a stable order (the same list on every server)."""
    rows = db.query(f"SELECT worker_id FROM {INSTANCES_TABLE} WHERE last_seen >= NOW(3) - INTERVAL %s SECOND "
                    "ORDER BY worker_id", (int(ttl_s),))
    return [r["worker_id"] for r in rows]


def leave(db, worker_id: str) -> None:
    """Called on a clean stop so the others take over this instance's DVRs right away."""
    db.execute(f"DELETE FROM {INSTANCES_TABLE} WHERE worker_id = %s", (worker_id,))
