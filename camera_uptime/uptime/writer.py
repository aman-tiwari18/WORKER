"""DB writer thread: raw check rows, per-run summary upserts and events, in batches.

If MySQL is unreachable the batch goes to spool/*.jsonl and is replayed later; check_uuid makes
the replay idempotent (a check already stored is not counted twice in the summary).
"""
from __future__ import annotations

import collections
import json
import logging
import queue
import threading
import time
from datetime import date, datetime
from pathlib import Path

from . import promote

log = logging.getLogger("uptime.writer")

CHECK_SQL = """
INSERT INTO camera_uptime_checks
  (check_uuid, run_id, exam_id, shift_id, exam_date, link_type, camera_row_id, camera_key, dvr_key,
   server, tc_code, checked_at, result, reason, stage, http_status, api_code, frame_blank,
   startvideo_ms, ffmpeg_ms, total_ms)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON DUPLICATE KEY UPDATE id = id
"""

# Assignment order matters: MySQL applies them left to right with already-updated values, so the
# "is this check newer?" tests read last_checked_at / last_frame_at before those are moved, and the
# down-period columns (see downtime.py) read down_since before it is moved. A check that arrives out of
# order (spool replay) does not touch down periods; rebuild-summary recomputes them from the raw checks.
_S = "camera_uptime_summary"  # existing-row columns must be qualified next to the `new` row alias
_IN_ORDER = f"({_S}.last_checked_at IS NULL OR new.last_checked_at >= {_S}.last_checked_at)"
SUMMARY_SQL = f"""
INSERT INTO camera_uptime_summary
  (run_id, exam_id, shift_id, exam_date, link_type, camera_key, camera_row_id, camera_name, centre_name,
   tc_code, city, state, server, dvr_key, checks_total, checks_up, checks_down, checks_unknown,
   checks_blank, first_checked_at, last_checked_at, first_up_at, last_up_at, last_down_at,
   last_result, last_reason, last_frame_path, last_frame_at, down_since, down_reason, outages)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS new
ON DUPLICATE KEY UPDATE
  downtime_json = IF({_IN_ORDER} AND new.last_result = 'UP' AND {_S}.down_since IS NOT NULL,
                     JSON_ARRAY_APPEND(COALESCE({_S}.downtime_json, JSON_ARRAY()), '$', JSON_OBJECT(
                       'down_at', DATE_FORMAT({_S}.down_since, '%%Y-%%m-%%d %%H:%%i:%%s'),
                       'up_at', DATE_FORMAT(new.last_checked_at, '%%Y-%%m-%%d %%H:%%i:%%s'),
                       'down_s', GREATEST(0, TIMESTAMPDIFF(SECOND, {_S}.down_since, new.last_checked_at)),
                       'reason', {_S}.down_reason)),
                     {_S}.downtime_json),
  downtime_s = {_S}.downtime_s + IF({_IN_ORDER} AND new.last_result = 'UP' AND {_S}.down_since IS NOT NULL,
                                    GREATEST(0, TIMESTAMPDIFF(SECOND, {_S}.down_since, new.last_checked_at)), 0),
  outages = {_S}.outages + IF({_IN_ORDER} AND new.last_result = 'DOWN' AND {_S}.down_since IS NULL, 1, 0),
  down_reason = IF({_IN_ORDER} AND new.last_result = 'DOWN' AND {_S}.down_since IS NULL, new.last_reason,
                   IF({_IN_ORDER} AND new.last_result = 'UP', NULL, {_S}.down_reason)),
  down_since = IF({_IN_ORDER} AND new.last_result = 'DOWN' AND {_S}.down_since IS NULL, new.last_checked_at,
                  IF({_IN_ORDER} AND new.last_result = 'UP', NULL, {_S}.down_since)),
  last_result = IF({_S}.last_checked_at IS NULL OR new.last_checked_at >= {_S}.last_checked_at,
                   new.last_result, {_S}.last_result),
  last_reason = IF({_S}.last_checked_at IS NULL OR new.last_checked_at >= {_S}.last_checked_at,
                   new.last_reason, {_S}.last_reason),
  last_frame_path = IF(new.last_frame_at IS NOT NULL AND ({_S}.last_frame_at IS NULL OR new.last_frame_at >= {_S}.last_frame_at),
                       new.last_frame_path, {_S}.last_frame_path),
  last_frame_at = IF(new.last_frame_at IS NOT NULL AND ({_S}.last_frame_at IS NULL OR new.last_frame_at >= {_S}.last_frame_at),
                     new.last_frame_at, {_S}.last_frame_at),
  checks_total = {_S}.checks_total + 1,
  checks_up = {_S}.checks_up + new.checks_up,
  checks_down = {_S}.checks_down + new.checks_down,
  checks_unknown = {_S}.checks_unknown + new.checks_unknown,
  checks_blank = {_S}.checks_blank + new.checks_blank,
  first_checked_at = IF({_S}.first_checked_at IS NULL OR new.first_checked_at < {_S}.first_checked_at,
                        new.first_checked_at, {_S}.first_checked_at),
  last_checked_at = IF({_S}.last_checked_at IS NULL OR new.last_checked_at > {_S}.last_checked_at,
                       new.last_checked_at, {_S}.last_checked_at),
  first_up_at = IF(new.first_up_at IS NOT NULL AND ({_S}.first_up_at IS NULL OR new.first_up_at < {_S}.first_up_at),
                   new.first_up_at, {_S}.first_up_at),
  last_up_at = IF(new.last_up_at IS NOT NULL AND ({_S}.last_up_at IS NULL OR new.last_up_at > {_S}.last_up_at),
                  new.last_up_at, {_S}.last_up_at),
  last_down_at = IF(new.last_down_at IS NOT NULL AND ({_S}.last_down_at IS NULL OR new.last_down_at > {_S}.last_down_at),
                    new.last_down_at, {_S}.last_down_at)
"""

EVENT_SQL = """
INSERT INTO camera_uptime_events (at, scope, scope_key, event, reason, detail_json, run_id)
VALUES (%s,%s,%s,%s,%s,%s,%s)
"""


def check_params(r: dict) -> tuple:
    return (r["check_uuid"], r["run_id"], r["exam_id"], r["shift_id"], r["exam_date"], r["link_type"],
            r.get("camera_row_id"), r["camera_key"], r.get("dvr_key"), r.get("server"), r.get("tc_code"),
            r["checked_at"], r["result"], r["reason"], r.get("stage"), r.get("http_status"), r.get("api_code"),
            None if r.get("frame_blank") is None else int(bool(r["frame_blank"])),
            r.get("startvideo_ms"), r.get("ffmpeg_ms"), r.get("total_ms"))


def summary_params(r: dict) -> tuple:
    t, res = r["checked_at"], r["result"]
    up, down = res == "UP", res == "DOWN"
    frame_at = t if r.get("frame_path") else None
    return (r["run_id"], r["exam_id"], r["shift_id"], r["exam_date"], r["link_type"], r["camera_key"],
            r.get("camera_row_id"), r.get("camera_name"), r.get("centre_name"), r.get("tc_code"), r.get("city"),
            r.get("state"), r.get("server"), r.get("dvr_key"),
            int(up), int(down), int(res == "UNKNOWN"), int(bool(r.get("frame_blank"))),
            t, t, t if up else None, t if up else None, t if down else None,
            res, r["reason"], r.get("frame_path"), frame_at, t if down else None,
            r["reason"] if down else None, int(down))


def _json_default(v):
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="milliseconds")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


class Writer(threading.Thread):
    def __init__(self, db, spool_dir: str, flush_s: float = 2.0, batch_max: int = 500):
        super().__init__(name="uptime-writer", daemon=True)
        self.db = db
        self.q: queue.Queue = queue.Queue()
        self.spool_dir = Path(spool_dir)
        self.flush_s, self.batch_max = flush_s, batch_max
        self._stopping = threading.Event()
        self.stats = collections.Counter()
        self.last_error: str | None = None

    def submit(self, kind: str, rec: dict) -> None:
        self.q.put((kind, rec))

    def stop(self, timeout: float = 15.0) -> None:
        self._stopping.set()
        self.join(timeout)

    def run(self) -> None:
        next_replay = 0.0
        while True:
            batch = self._drain()
            if batch:
                self._flush(batch)
            if time.monotonic() >= next_replay:
                self._replay_spool()
                next_replay = time.monotonic() + 30
            if self._stopping.is_set() and self.q.empty():
                break

    def _drain(self) -> list:
        batch, deadline = [], time.monotonic() + self.flush_s
        while len(batch) < self.batch_max:
            timeout = max(0.0, deadline - time.monotonic())
            if self._stopping.is_set():
                timeout = 0.0
            try:
                batch.append(self.q.get(timeout=timeout) if timeout else self.q.get_nowait())
            except queue.Empty:
                break
        return batch

    def _write(self, batch: list) -> None:
        conn = self.db.conn()
        conn.begin()
        promotes = [rec for kind, rec in batch if kind == "promote"]
        try:
            with conn.cursor() as cur:
                for kind, rec in batch:
                    if kind == "check":
                        if cur.execute(CHECK_SQL, check_params(rec)) == 1:  # 0 = replayed duplicate
                            cur.execute(SUMMARY_SQL, summary_params(rec))
                            self.stats["checks"] += 1
                        else:
                            self.stats["duplicates"] += 1
                    elif kind == "event":
                        cur.execute(EVENT_SQL, (rec["at"], rec["scope"], rec.get("scope_key"), rec["event"],
                                                rec.get("reason"), json.dumps(rec["detail"], default=_json_default)
                                                if rec.get("detail") is not None else None, rec.get("run_id")))
                        self.stats["events"] += 1
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        if promotes:
            self._promote(conn, promotes)

    def _promote(self, conn, recs: list) -> None:
        """Cameras-tab updates, in their own transaction: a failure here never costs an uptime check."""
        try:
            conn.begin()
            with conn.cursor() as cur:
                for rec in recs:
                    promote.apply(cur, rec)
            conn.commit()
            self.stats["promoted"] += len(recs)
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            self.stats["promote_errors"] += len(recs)
            log.error("Cameras-tab update failed for %d camera(s): %s", len(recs), e)

    def _flush(self, batch: list) -> None:
        try:
            self._write(batch)
            self.last_error = None
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            log.error("DB write failed (%s) — spooling %d records", self.last_error, len(batch))
            self._spool(batch)

    def _spool(self, batch: list) -> None:
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            path = self.spool_dir / f"spool-{datetime.now():%Y%m%d}.jsonl"
            with open(path, "a") as fh:
                for kind, rec in batch:
                    fh.write(json.dumps({"kind": kind, "rec": rec}, default=_json_default) + "\n")
            self.stats["spooled"] += len(batch)
        except OSError as e:
            log.error("Spool write failed, %d records lost: %s", len(batch), e)
            self.stats["lost"] += len(batch)

    def _replay_spool(self) -> None:
        if not self.spool_dir.is_dir():
            return
        for path in sorted(self.spool_dir.glob("spool-*.jsonl")):
            try:
                with open(path) as fh:
                    batch = [(d["kind"], d["rec"]) for d in map(json.loads, filter(None, map(str.strip, fh)))]
            except (OSError, ValueError) as e:
                log.error("Unreadable spool file %s: %s", path, e)
                continue
            try:
                for i in range(0, len(batch), self.batch_max):
                    self._write(batch[i:i + self.batch_max])
            except Exception as e:
                log.warning("Spool replay of %s postponed: %s", path.name, e)
                return
            path.rename(path.with_suffix(".replayed"))  # kept on disk, not deleted
            log.info("Replayed %d spooled records from %s", len(batch), path.name)
