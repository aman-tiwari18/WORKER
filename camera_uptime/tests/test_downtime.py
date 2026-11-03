from datetime import datetime

from uptime.downtime import periods


def t(m):
    return datetime(2026, 11, 3, 9, m)


def test_down_period_ends_at_next_up():
    ps, total, outages, since, why = periods([(t(0), "UP", "ok"), (t(10), "DOWN", "no_stream"),
                                              (t(20), "DOWN", "no_frame"), (t(30), "UP", "ok")])
    assert ps == [{"down_at": "2026-11-03 09:10:00", "up_at": "2026-11-03 09:30:00", "down_s": 1200,
                   "reason": "no_stream"}]
    assert (total, outages, since, why) == (1200, 1, None, None)


def test_unknown_neither_starts_nor_ends_a_period():
    ps, total, outages, since, why = periods([(t(0), "UNKNOWN", "dss_timeout"), (t(10), "DOWN", "no_frame"),
                                              (t(20), "UNKNOWN", "dvr_capacity"), (t(30), "UP", "ok"),
                                              (t(40), "UNKNOWN", "dss_timeout")])
    assert [p["down_s"] for p in ps] == [1200] and outages == 1


def test_open_period_kept_or_closed_at_end():
    checks = [(t(0), "DOWN", "no_frame"), (t(10), "UP", "ok"), (t(20), "DOWN", "refused_2084")]
    ps, total, outages, since, why = periods(checks)
    assert (len(ps), total, outages, since, why) == (1, 600, 2, t(20), "refused_2084")
    ps, total, outages, since, why = periods(checks, end=t(50))
    assert ps[-1] == {"down_at": "2026-11-03 09:20:00", "up_at": None, "until": "2026-11-03 09:50:00",
                      "down_s": 1800, "reason": "refused_2084", "still_down": True}
    assert (total, since, why) == (2400, None, None)
