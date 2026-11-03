"""Summary upsert semantics against the real MySQL schema, inside a transaction that is rolled back.
Opt-in: UPTIME_TEST_DB=1 (uses the service .env credentials)."""
import json
import os
import uuid
from datetime import date, datetime

import pytest

from uptime import config
from uptime.db import Database
from uptime.downtime import CLOSE_OPEN_SQL
from uptime.writer import CHECK_SQL, SUMMARY_SQL, check_params, summary_params

pytestmark = pytest.mark.skipif(os.getenv("UPTIME_TEST_DB") != "1", reason="set UPTIME_TEST_DB=1")

RUN_ID = 4294960000  # never a real run id


def rec(t, result, reason="ok", frame=None, blank=False, uid=None):
    return {"check_uuid": uid or str(uuid.uuid4()), "run_id": RUN_ID, "exam_id": -1, "shift_id": -1,
            "exam_date": date(2000, 1, 1), "link_type": "RTSP", "camera_row_id": 1, "camera_key": "test|cam",
            "dvr_key": "test|dvr", "server": "test", "tc_code": "T", "camera_name": "n", "centre_name": "c",
            "city": None, "state": None, "checked_at": t, "result": result, "reason": reason, "stage": "ffmpeg",
            "http_status": None, "api_code": None, "frame_blank": blank, "startvideo_ms": 1, "ffmpeg_ms": 2,
            "total_ms": 3, "frame_path": frame}


def test_out_of_order_and_replay():
    db = Database(config.load())
    conn = db.conn()
    conn.begin()
    try:
        with conn.cursor() as cur:
            t1, t2, t3 = (datetime(2000, 1, 1, 9, m) for m in (10, 20, 30))
            dup = str(uuid.uuid4())
            # newest check (t3) is DOWN; an older UP (t1) arrives last; t3 is replayed once
            for r in (rec(t2, "UP", frame="a.jpg"), rec(t3, "DOWN", "no_frame", uid=dup),
                      rec(t1, "UP", frame="old.jpg", blank=True), rec(t3, "DOWN", "no_frame", uid=dup)):
                if cur.execute(CHECK_SQL, check_params(r)) == 1:
                    cur.execute(SUMMARY_SQL, summary_params(r))
            cur.execute("SELECT * FROM camera_uptime_summary WHERE run_id = %s", (RUN_ID,))
            s = cur.fetchone()
            cur.execute("SELECT COUNT(*) AS n FROM camera_uptime_checks WHERE run_id = %s", (RUN_ID,))
            n = cur.fetchone()["n"]
    finally:
        conn.rollback()
    assert n == 3  # the replayed duplicate was not stored twice
    assert (s["checks_total"], s["checks_up"], s["checks_down"], s["checks_blank"]) == (3, 2, 1, 1)
    assert s["first_checked_at"] == t1 and s["last_checked_at"] == t3
    assert s["last_result"] == "DOWN" and s["last_reason"] == "no_frame"  # t3 wins although t1 arrived later
    assert s["first_up_at"] == t1 and s["last_up_at"] == t2 and s["last_down_at"] == t3
    assert s["last_frame_path"] == "a.jpg" and s["last_frame_at"] == t2  # older frame did not overwrite


def test_down_periods():
    db = Database(config.load())
    conn = db.conn()
    conn.begin()
    try:
        with conn.cursor() as cur:
            t = [datetime(2000, 1, 1, 9, m) for m in (0, 10, 20, 30, 40, 50)]
            # UP, DOWN, UNKNOWN, UP -> one period 09:10-09:30; DOWN at 09:40 stays open; an old DOWN replayed
            for r in (rec(t[0], "UP"), rec(t[1], "DOWN", "no_frame"), rec(t[2], "UNKNOWN", "api_error"),
                      rec(t[3], "UP"), rec(t[4], "DOWN", "no_frame"), rec(t[2], "DOWN", "no_frame")):
                if cur.execute(CHECK_SQL, check_params(r)) == 1:
                    cur.execute(SUMMARY_SQL, summary_params(r))
            cur.execute("SELECT down_since, down_reason, outages, downtime_s, downtime_json FROM camera_uptime_summary "
                        "WHERE run_id = %s", (RUN_ID,))
            s1 = cur.fetchone()
            cur.execute(CLOSE_OPEN_SQL, (t[5], t[5], t[5], RUN_ID))
            cur.execute("SELECT down_since, down_reason, downtime_s, downtime_json FROM camera_uptime_summary "
                        "WHERE run_id = %s",
                        (RUN_ID,))
            s2 = cur.fetchone()
    finally:
        conn.rollback()
    assert (s1["down_since"], s1["down_reason"], s1["outages"], s1["downtime_s"]) == (t[4], "no_frame", 2, 1200)
    assert json.loads(s1["downtime_json"]) == [
        {"down_at": "2000-01-01 09:10:00", "up_at": "2000-01-01 09:30:00", "down_s": 1200, "reason": "no_frame"}]
    assert (s2["down_since"], s2["down_reason"], s2["downtime_s"]) == (None, None, 1800)
    assert json.loads(s2["downtime_json"])[-1] == {"down_at": "2000-01-01 09:40:00", "up_at": None,
                                                   "until": "2000-01-01 09:50:00", "down_s": 600,
                                                   "reason": "no_frame", "still_down": True}


def test_service_user_cannot_write_existing_tables():
    db = Database(config.load())
    with pytest.raises(Exception) as e:
        db.execute("UPDATE camera_links_rtsp SET status = status WHERE 1 = 0")
    assert "denied" in str(e.value).lower()
