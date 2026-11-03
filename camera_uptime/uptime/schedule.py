"""Which (exam, shift, date) windows are live right now. Pure functions, no I/O.

All schedule values in the DB are naive IST dates/times; `now` must be naive IST too.
Independent of shift_script_assignments: an exam day comes from the exam's own date range
and skip rules, plus the optional exam_slot_schedule gate.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

log = logging.getLogger("uptime.schedule")

DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

_warned_null_dates: set[int] = set()


@dataclass(frozen=True)
class Window:
    exam_id: int
    shift_id: int
    exam_date: date
    exam_name: str
    link_type: str  # "RTSP" or "HLS"
    shift_name: str
    shift_start: str  # "HH:MM:SS" as configured, copied into the run row
    shift_end: str
    window_start: datetime  # includes the start hold
    window_end: datetime

    @property
    def key(self) -> tuple[int, int, date]:
        return (self.exam_id, self.shift_id, self.exam_date)

    def contains(self, t: datetime) -> bool:
        return self.window_start <= t < self.window_end


def time_seconds(v) -> int | None:
    """MySQL TIME -> seconds after midnight. PyMySQL returns TIME as timedelta (24:00:00 allowed)."""
    if v is None:
        return None
    if isinstance(v, timedelta):
        return int(v.total_seconds())
    if isinstance(v, time):
        return v.hour * 3600 + v.minute * 60 + v.second
    parts = str(v).strip().split(":")
    try:
        h, m = int(parts[0]), int(parts[1])
        s = int(float(parts[2])) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return None
    return h * 3600 + m * 60 + s


def fmt_seconds(sec: int | None) -> str | None:
    if sec is None:
        return None
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def json_list(v) -> list:
    """skip_days / skip_dates arrive from PyMySQL as JSON strings."""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, (bytes, bytearray)):
        v = v.decode()
    s = str(v).strip()
    if not s:
        return []
    try:
        data = json.loads(s)
    except ValueError:
        return [p.strip() for p in s.split(",") if p.strip()]
    if isinstance(data, list):
        return data
    if isinstance(data, str):
        return [data]
    return []


def as_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


def is_exam_day(exam: dict, d: date) -> bool:
    start, end = as_date(exam.get("start_date")), as_date(exam.get("end_date"))
    if start is None or end is None:
        eid = int(exam.get("id") or 0)
        if eid not in _warned_null_dates:
            _warned_null_dates.add(eid)
            log.warning("Exam %s has no start/end date — treated as not live", eid)
        return False
    if not (start <= d <= end):
        return False
    skip_dates = {str(x).strip()[:10] for x in json_list(exam.get("skip_dates"))}
    if d.isoformat() in skip_dates:
        return False
    skip_days = {str(x).strip().lower()[:3] for x in json_list(exam.get("skip_days"))}
    return DAY_NAMES[d.weekday()][:3] not in skip_days


def shift_window(d: date, start_s: int | None, end_s: int | None,
                 hold_min: int, max_hours: int) -> tuple[datetime, datetime] | None:
    """[start + hold, end) of a shift on exam date d; end before start = overnight shift."""
    if start_s is None or end_s is None or start_s == end_s:
        return None
    base = datetime.combine(d, time(0))
    ws = base + timedelta(seconds=start_s)
    we = base + timedelta(seconds=end_s)
    if end_s < start_s:
        we += timedelta(days=1)
    if we - ws > timedelta(hours=max_hours):
        return None
    ws += timedelta(minutes=hold_min)
    if ws >= we:
        return None
    return ws, we


def normalize_link_type(v) -> str:
    # Same rule as the backend: RTSP exams use camera_links_rtsp, everything else camera_links_hls.
    return "RTSP" if str(v or "").strip().upper() == "RTSP" else "HLS"


def live_windows(exams: list[dict], shifts: list[dict], slots: list[dict], now: datetime,
                 hold_min: int = 5, max_hours: int = 16) -> list[Window]:
    """
    exams:  active exams rows (id, exam_name, link_type, start_date, end_date, skip_days, skip_dates)
    shifts: active exam_shifts rows (id, exam_id, shift_name, start_time, end_time)
    slots:  exam_slot_schedule rows (exam_id, exam_date, shift_id, is_running). An exam with any
            slot rows runs only on the (date, shift) slots marked running.
    """
    by_id = {int(e["id"]): e for e in exams}
    gated = {int(r["exam_id"]) for r in slots}
    running = {
        (int(r["exam_id"]), as_date(r["exam_date"]), int(r["shift_id"]))
        for r in slots
        if int(r.get("is_running") or 0) == 1
    }
    today = now.date()
    out: list[Window] = []
    for sh in shifts:
        exam = by_id.get(int(sh["exam_id"]))
        if exam is None:
            continue
        start_s, end_s = time_seconds(sh.get("start_time")), time_seconds(sh.get("end_time"))
        for offset in (-1, 0, 1):
            d = today + timedelta(days=offset)
            if not is_exam_day(exam, d):
                continue
            win = shift_window(d, start_s, end_s, hold_min, max_hours)
            if win is None or not (win[0] <= now < win[1]):
                continue
            eid, sid = int(exam["id"]), int(sh["id"])
            if eid in gated and (eid, d, sid) not in running:
                continue
            out.append(Window(
                exam_id=eid,
                shift_id=sid,
                exam_date=d,
                exam_name=str(exam.get("exam_name") or ""),
                link_type=normalize_link_type(exam.get("link_type")),
                shift_name=str(sh.get("shift_name") or ""),
                shift_start=fmt_seconds(start_s),
                shift_end=fmt_seconds(end_s),
                window_start=win[0],
                window_end=win[1],
            ))
    return out


EXAMS_SQL = (
    "SELECT id, exam_name, link_type, start_date, end_date, skip_days, skip_dates "
    "FROM exams WHERE is_active = 1 AND start_date IS NOT NULL AND end_date IS NOT NULL "
    "AND end_date >= %s AND start_date <= %s"
)
SHIFTS_SQL = "SELECT id, exam_id, shift_name, start_time, end_time FROM exam_shifts WHERE is_active = 1 AND exam_id IN ({ids})"
SLOTS_SQL = "SELECT exam_id, exam_date, shift_id, is_running FROM exam_slot_schedule WHERE exam_id IN ({ids})"


def load_live_windows(db, now: datetime, hold_min: int, max_hours: int) -> list[Window]:
    """Read the schedule tables and return the windows live at `now`."""
    from .db import ER_NO_SUCH_TABLE
    import pymysql

    lo, hi = (now - timedelta(days=1)).date(), (now + timedelta(days=1)).date()
    exams = db.query(EXAMS_SQL, (lo, hi))
    if not exams:
        return []
    ids = [int(e["id"]) for e in exams]
    ph = ",".join(["%s"] * len(ids))
    shifts = db.query(SHIFTS_SQL.format(ids=ph), ids)
    try:
        slots = db.query(SLOTS_SQL.format(ids=ph), ids)
    except pymysql.err.ProgrammingError as e:
        if e.args and e.args[0] == ER_NO_SUCH_TABLE:
            slots = []  # schedule tables not migrated: no gate
        else:
            raise
    return live_windows(exams, shifts, slots, now, hold_min, max_hours)
