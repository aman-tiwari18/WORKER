from datetime import date

from conftest import make_settings

from uptime.roster import Camera, Member
from uptime.scheduler import Scheduler

RUN = (46, 169, date(2026, 11, 2))


def cam(server, dev, ch, run=RUN):
    return Camera(key=f"rtsp|{server}|{dev}|{ch}", link_type="RTSP", lane=server, dvr_key=f"{server}|{dev}",
                  server=server, device_id=dev, camera_no=ch,
                  members=[Member(run, 1, "n", "c", "T", None, None)])


def hls_cam(i):
    return Camera(key=f"hls|g01|{i}", link_type="HLS", lane="hls", dvr_key=f"tcs|T{i % 2}", server="g01",
                  url="https://g01/x", members=[Member(RUN, i, "n", "c", "T", None, None)])


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(cams, clock, **kw):
    s = Scheduler(make_settings(**{"dvr_min_gap_s": 60.0, "max_new_streams_per_min": 1000, **kw}), clock=clock)
    s.set_cameras({c.key: c for c in cams})
    return s


def test_one_in_flight_per_server_and_round_robin_across_dvrs():
    clock = Clock()
    cams = [cam("d14", "A", "1"), cam("d14", "A", "2"), cam("d14", "B", "1"), cam("d11", "C", "1")]
    s = make(cams, clock)
    first = s.next_job()
    second = s.next_job()
    assert {first[0], second[0]} == {"d11", "d14"}  # one per lane
    assert s.next_job() is None  # both lanes busy
    d14 = first if first[0] == "d14" else second
    s.done(d14[1].key, "UP", "ok", 3.0, 1.0)
    nxt = s.next_job()
    assert nxt[0] == "d14" and nxt[1].dvr_key != d14[1].dvr_key  # other DVR first (gap on the first)


def test_dvr_gap_is_respected():
    clock = Clock()
    s = make([cam("d14", "A", "1"), cam("d14", "A", "2")], clock)
    lane, c1 = s.next_job()
    s.done(c1.key, "UP", "ok", 2.0, 1.0)
    assert s.next_job() is None  # same DVR within 60 s
    clock.t += 61
    lane, c2 = s.next_job()
    assert c2.key != c1.key


def test_global_new_stream_cap():
    clock = Clock()
    s = make([cam("d11", "A", "1"), cam("d14", "B", "1"), cam("d15", "C", "1")], clock, max_new_streams_per_min=2)
    assert s.next_job() and s.next_job()
    assert s.next_job() is None
    clock.t += 61
    assert s.next_job() is not None


def test_pass_completes_and_refills():
    clock = Clock()
    s = make([cam("d14", "A", "1"), cam("d14", "B", "1")], clock)
    seen = []
    for _ in range(2):
        lane, c = s.next_job()
        seen.append(c.key)
        s.done(c.key, "DOWN", "no_frame", 5.0, clock.t)
    clock.t += 61
    lane, c = s.next_job()
    assert s.status()["lanes"][0]["pass"] == 2 and sorted(seen) == sorted(k for k in s.cams)


def test_retryable_unknown_is_retried_once_per_pass():
    clock = Clock()
    s = make([cam("d14", "A", "1")], clock)
    _, c = s.next_job()
    assert not s.is_retry("d14", c.key)
    s.done(c.key, "UNKNOWN", "dss_timeout", 10.0, 1.0)
    clock.t += 61
    _, again = s.next_job()
    assert again.key == c.key and s.status()["lanes"][0]["pass"] == 1
    assert s.is_retry("d14", c.key)  # a 2084 now would count as a repeated refusal
    s.done(c.key, "UNKNOWN", "dss_timeout", 10.0, 2.0)
    clock.t += 61
    s.next_job()
    assert s.status()["lanes"][0]["pass"] == 2  # not requeued twice


def test_backoffs_and_token_pause():
    clock = Clock()
    s = make([cam("d14", "A", "1"), cam("d14", "A", "2"), cam("d16", "B", "1")], clock, dvr_capacity_backoff_s=300.0)
    jobs = dict([s.next_job(), s.next_job()])
    s.done(jobs["d14"].key, "UNKNOWN", "dvr_capacity", 1.0, 1.0)
    clock.t += 120
    assert s.next_job() is None  # d14's only DVR backs off 300 s; d16 still in flight
    clock.t += 200
    assert s.next_job()[0] == "d14"  # backoff over
    s.done(jobs["d16"].key, "UNKNOWN", "token_unavailable", 1.0, 1.0)
    st = {ln["lane"]: ln for ln in s.status()["lanes"]}
    assert st["d16"]["pause_reason"] == "token_unavailable"
    s.token_health({})  # token server no longer in backoff for d16 -> resume early
    assert {ln["lane"]: ln for ln in s.status()["lanes"]}["d16"]["pause_reason"] is None


def test_roster_change_keeps_progress_and_inflight_accounting():
    clock = Clock()
    a, b, c = cam("d14", "A", "1"), cam("d14", "B", "1"), cam("d14", "C", "1")
    s = make([a, b], clock)
    _, picked = s.next_job()
    s.set_cameras({x.key: x for x in [b, c]})  # `picked` may have been removed while in flight
    s.done(picked.key, "UP", "ok", 2.0, 1.0)
    lane = s.lanes["d14"]
    assert lane.inflight == 0 and all(d.inflight == 0 for d in lane.dvrs.values())
    assert c.key in lane.pending


def test_global_pause_and_vision_block():
    clock = Clock()
    s = make([cam("d14", "A", "1")], clock)
    s.pause("api", "test")
    assert s.next_job() is None
    s.resume("api")
    s.set_blocked_exams({46})
    assert s.next_job() is None
    s.set_blocked_exams(set())
    assert s.next_job() is not None


def test_only_servers_and_hls_lane():
    clock = Clock()
    s = make([cam("d14", "A", "1"), cam("d11", "B", "1"), hls_cam(1), hls_cam(2)], clock,
             only_servers=("d11",), include_hls=True)
    assert sorted(s.lanes) == ["d11", "hls"] and s.lanes["hls"].max_inflight == 1
    lanes = {s.next_job()[0], s.next_job()[0]}
    assert lanes == {"d11", "hls"}
