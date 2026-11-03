from datetime import date, datetime

import pymysql

from uptime.roster import RosterGuard, build_run_roster, load_run_roster, merge_rosters, rtsp_camera
from uptime.schedule import Window


def win(exam_id=46, shift_id=169, link_type="RTSP"):
    return Window(exam_id, shift_id, date(2026, 11, 2), "x", link_type, "Shift 1", "09:00:00", "11:00:00",
                  datetime(2026, 11, 2, 9, 5), datetime(2026, 11, 2, 11, 0))


def rtsp_row(i, url="d14.camview.io", dev="1000796", ch="17", tc="T1"):
    return {"id": i, "device_id": dev, "camera_no": ch, "camera_name": f"cam{i}", "centre_name": "C",
            "tc_code": tc, "city": None, "state": None, "cctv_url": url}


def test_rtsp_key_normalises_host():
    a = rtsp_camera(rtsp_row(1, "https://D14.camview.io/"), None)
    b = rtsp_camera(rtsp_row(2, "d14.camview.io"), None)
    assert a.key == b.key == "rtsp|d14.camview.io|1000796|17"
    assert a.dvr_key == "d14.camview.io|1000796" and a.lane == "d14.camview.io"


def test_same_device_on_two_servers_is_two_cameras():
    cams = build_run_roster([rtsp_row(1, "d15.camview.io"), rtsp_row(2, "d16.camview.io")], win())
    assert len(cams) == 2
    assert {c.dvr_key for c in cams.values()} == {"d15.camview.io|1000796", "d16.camview.io|1000796"}


def test_hls_exact_rows_win_over_null_rows():
    rows = [
        {"id": 1, "ch_code": "C1", "camera_name": "all", "centre_name": "c", "tc_code": "T", "city": None,
         "state": None, "cctv_url": "https://g01.tcsion.com/x?a=1", "shift_id": None, "exam_date": None},
        {"id": 2, "ch_code": "C1", "camera_name": "exact", "centre_name": "c", "tc_code": "T", "city": None,
         "state": None, "cctv_url": "https://g01.tcsion.com/x?a=2", "shift_id": 169, "exam_date": date(2026, 11, 2)},
    ]
    cams = build_run_roster(rows, win(link_type="HLS"))
    assert len(cams) == 1
    cam = next(iter(cams.values()))
    assert cam.members[0].camera_name == "exact" and cam.key == "hls|g01.tcsion.com|C1" and cam.lane == "hls"


def test_one_camera_in_two_runs_is_merged():
    w1, w2 = win(46, 169), win(47, 200)
    r1 = build_run_roster([rtsp_row(1)], w1)
    r2 = build_run_roster([rtsp_row(9)], w2)
    merged = merge_rosters({w1.key: r1, w2.key: r2})
    assert len(merged) == 1
    cam = next(iter(merged.values()))
    assert {m.run_key for m in cam.members} == {w1.key, w2.key}
    assert len(r1[cam.key].members) == 1  # inputs untouched


class FakeDb:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    def query(self, sql, params=()):
        self.calls.append(sql)
        if self.error and "exam_centre_schedule" in sql:
            raise self.error
        return [rtsp_row(1)]


def test_missing_schedule_tables_fall_back_to_unfiltered():
    db = FakeDb(pymysql.err.ProgrammingError(1146, "Table 'exam_centre_schedule' doesn't exist"))
    cams = load_run_roster(db, win())
    assert len(cams) == 1 and len(db.calls) == 2


def test_other_errors_keep_previous_roster():
    db = FakeDb(pymysql.err.OperationalError(1267, "Illegal mix of collations"))
    assert load_run_roster(db, win()) is None


def test_guard_needs_big_jumps_confirmed():
    g = RosterGuard()
    base = {str(i): i for i in range(100)}
    assert g.offer(base)
    assert g.offer({str(i): i for i in range(110)})  # +10 %: applied
    big = {str(i): i for i in range(300)}
    assert not g.offer(big) and len(g.applied) == 110  # schedule re-upload blip
    assert g.offer(big) and len(g.applied) == 300  # repeated: real change
    assert not g.offer({}) and len(g.applied) == 300


# ---------------------------------------------------------------- sharding (one instance per worker)
def test_shards_partition_dvrs_without_overlap():
    from uptime.roster import owns_dvr, shard_of
    from conftest import make_settings

    dvrs = [f"d{h}.camview.io|{i}" for h in (11, 14, 15, 16) for i in range(1000000, 1000200)]
    shards = [make_settings(shard_index=i, shard_count=5) for i in range(5)]
    for d in dvrs:
        owners = [i for i, s in enumerate(shards) if owns_dvr(s, d)]
        assert owners == [shard_of(d, 5)]  # exactly one owner
    sizes = [sum(owns_dvr(s, d) for d in dvrs) for s in shards]
    assert min(sizes) > len(dvrs) / 5 * 0.7  # roughly even


def test_single_shard_owns_everything():
    from uptime.roster import owns_dvr
    from conftest import make_settings

    assert owns_dvr(make_settings(), "d11.camview.io|1") is True


def test_in_scope_respects_shard():
    from uptime.roster import in_scope, rtsp_camera, shard_of
    from conftest import make_settings

    cam = rtsp_camera({"id": 1, "device_id": "1000001", "camera_no": "1", "cctv_url": "https://d11.camview.io/x"}, None)
    mine = shard_of(cam.dvr_key, 5)
    assert in_scope(cam, make_settings(shard_index=mine, shard_count=5))
    assert not in_scope(cam, make_settings(shard_index=(mine + 1) % 5, shard_count=5))


def test_seed_only_removes_own_shard(monkeypatch):
    from uptime import runs

    class Db:
        def __init__(self):
            self.updates = []

        def execute(self, sql, params=None):
            if sql.startswith("UPDATE camera_uptime_summary SET in_roster = 0"):
                self.updates.append(params[1:])

        def query(self, sql, params=None):
            return [{"camera_key": "a", "dvr_key": "mine"}, {"camera_key": "b", "dvr_key": "theirs"},
                    {"camera_key": "c", "dvr_key": "mine"}]

    db = Db()
    seeded, removed = runs.seed(db, 1, type("W", (), {"key": None, "exam_id": 1, "shift_id": 1,
                                                      "exam_date": None, "link_type": "RTSP"})(), {},
                                owns=lambda k: k == "mine")
    assert removed == 2 and sorted(db.updates[0]) == ["a", "c"]  # "b" belongs to another worker


# ---------------------------------------------------------------- auto sharding (Shard follows the live instances)
def test_shard_object_rebalances_without_overlap():
    from uptime.roster import Shard, owns_dvr

    dvrs = [f"d{i % 4}.camview.io|{1000 + i}" for i in range(400)]
    for n in (1, 2, 3, 5):
        shards = [Shard() for _ in range(n)]
        for i, sh in enumerate(shards):
            sh.set(i, n)
        for d in dvrs:
            assert sum(owns_dvr(sh, d) for sh in shards) == 1  # every DVR has exactly one owner


def test_shard_set_reports_changes_and_clamps():
    from uptime.roster import Shard

    sh = Shard()
    assert sh.set(0, 1) is False
    assert sh.set(1, 3) is True and (sh.shard_index, sh.shard_count) == (1, 3)
    assert sh.set(5, 2) is True and sh.shard_index == 0  # out of range -> 0


def test_in_scope_uses_shard_over_settings():
    from conftest import make_settings
    from uptime.roster import Shard, in_scope, rtsp_camera, shard_of

    s = make_settings(shard_index=0, shard_count=1)
    row = {"id": 1, "device_id": "1234", "camera_no": "1", "cctv_url": "https://d11.camview.io"}
    cam = rtsp_camera(row, None)
    owner = shard_of(cam.dvr_key, 4)
    assert in_scope(cam, s, Shard(owner, 4)) is True
    assert in_scope(cam, s, Shard((owner + 1) % 4, 4)) is False
