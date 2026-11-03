from datetime import date, datetime, timedelta

from uptime.schedule import is_exam_day, json_list, live_windows, shift_window, time_seconds

EXAM = {"id": 46, "exam_name": "SSC JE", "link_type": "RTSP", "start_date": date(2026, 11, 2),
        "end_date": date(2026, 11, 6), "skip_days": '["Sunday", "Saturday"]', "skip_dates": "[]"}
SHIFTS = [
    {"id": 169, "exam_id": 46, "shift_name": "Shift 1", "start_time": timedelta(hours=9), "end_time": timedelta(hours=11)},
    {"id": 170, "exam_id": 46, "shift_name": "Shift 2", "start_time": timedelta(hours=13), "end_time": timedelta(hours=15)},
]


def test_time_seconds_forms():
    assert time_seconds(timedelta(hours=9, minutes=30)) == 34200
    assert time_seconds("17:00:00") == 61200
    assert time_seconds(timedelta(hours=24)) == 86400  # MySQL TIME 24:00:00
    assert time_seconds(None) is None


def test_live_inside_window_after_hold():
    at = datetime(2026, 11, 2, 9, 6)
    wins = live_windows([EXAM], SHIFTS, [], at, hold_min=5)
    assert [(w.shift_id, w.exam_date) for w in wins] == [(169, date(2026, 11, 2))]
    w = wins[0]
    assert w.window_start == datetime(2026, 11, 2, 9, 5) and w.window_end == datetime(2026, 11, 2, 11, 0)
    assert w.shift_start == "09:00:00" and w.link_type == "RTSP"


def test_hold_and_end_edges():
    assert live_windows([EXAM], SHIFTS, [], datetime(2026, 11, 2, 9, 4), hold_min=5) == []
    assert live_windows([EXAM], SHIFTS, [], datetime(2026, 11, 2, 11, 0), hold_min=5) == []  # end exclusive
    assert len(live_windows([EXAM], SHIFTS, [], datetime(2026, 11, 2, 10, 59, 59), hold_min=5)) == 1


def test_skip_days_and_dates():
    assert not is_exam_day(EXAM, date(2026, 11, 7))  # Saturday
    assert not is_exam_day(EXAM, date(2026, 11, 8))  # Sunday
    exam = dict(EXAM, skip_dates='["2026-11-03"]', skip_days="[]")
    assert not is_exam_day(exam, date(2026, 11, 3))
    assert is_exam_day(exam, date(2026, 11, 4))
    assert not is_exam_day(EXAM, date(2026, 11, 1))  # before start_date


def test_null_dates_not_live():
    assert not is_exam_day(dict(EXAM, start_date=None), date(2026, 11, 3))


def test_overnight_shift_belongs_to_start_date():
    exam = dict(EXAM, start_date=date(2026, 11, 2), end_date=date(2026, 11, 2), skip_days="[]")
    night = [{"id": 1, "exam_id": 46, "shift_name": "Night", "start_time": "22:00:00", "end_time": "02:00:00"}]
    wins = live_windows([exam], night, [], datetime(2026, 11, 3, 1, 0), hold_min=0)
    assert len(wins) == 1 and wins[0].exam_date == date(2026, 11, 2)
    assert wins[0].window_end == datetime(2026, 11, 3, 2, 0)
    # the next night is not an exam day
    assert live_windows([exam], night, [], datetime(2026, 11, 3, 23, 0), hold_min=0) == []


def test_invalid_windows():
    assert shift_window(date(2026, 11, 2), 36000, 36000, 0, 16) is None  # end == start
    assert shift_window(date(2026, 11, 2), 3600, 72000, 0, 16) is None  # 19 h > cap
    assert shift_window(date(2026, 11, 2), 36000, 36300, 10, 16) is None  # hold longer than the window


def test_slot_gate():
    at = datetime(2026, 11, 2, 9, 30)
    slots_off = [{"exam_id": 46, "exam_date": date(2026, 11, 2), "shift_id": 169, "is_running": 0}]
    assert live_windows([EXAM], SHIFTS, slots_off, at) == []
    slots_on = [{"exam_id": 46, "exam_date": date(2026, 11, 2), "shift_id": 169, "is_running": 1}]
    assert len(live_windows([EXAM], SHIFTS, slots_on, at)) == 1
    other_day = [{"exam_id": 46, "exam_date": date(2026, 11, 3), "shift_id": 169, "is_running": 1}]
    assert live_windows([EXAM], SHIFTS, other_day, at) == []  # gated exam, no row for today


def test_json_list_tolerates_shapes():
    assert json_list(None) == [] and json_list("") == []
    assert json_list('["2026-11-03"]') == ["2026-11-03"]
    assert json_list("Sunday, Saturday") == ["Sunday", "Saturday"]
    assert json_list(["x"]) == ["x"]


def test_hls_link_type_normalised():
    wins = live_windows([dict(EXAM, link_type="hls")], SHIFTS, [], datetime(2026, 11, 2, 9, 30))
    assert wins[0].link_type == "HLS"
