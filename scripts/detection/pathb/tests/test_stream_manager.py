"""StreamManager behaviour with fake pipelines, resolver, evaluator and clock."""
from concurrent.futures import Future

import numpy as np

from pathb.stream_budget import CircuitBreaker, StreamBudget
from pathb.stream_manager import ACTIVE, IDLE, Cam, StreamManager
from pathb.tests.test_units import _manifest

G = ("yolo26",)


class InlineExecutor:
    def submit(self, fn, *a):
        f = Future()
        try:
            f.set_result(fn(*a))
        except Exception as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def shutdown(self, **_):
        pass


class FakePipeline:
    def __init__(self):
        self.active = {}
        self.log = []

    def add_source(self, sid, uri):
        self.active[sid] = uri
        self.log.append(("add", sid, uri))

    def remove_source(self, sid):
        self.active.pop(sid, None)
        self.log.append(("remove", sid))


class FakeResolver:
    def __init__(self, dead=()):
        self.dead = set(dead)
        self.invalidated = []

    def resolve(self, cfg):
        return [] if cfg["key"] in self.dead else [f"rtsp://x/{cfg['key']}"]

    def invalidate(self, cfg):
        self.invalidated.append(cfg["key"])


class FakeEval:
    def __init__(self):
        self.packets = []

    def submit(self, p):
        self.packets.append(p)
        return True


def _setup(n_held, n_rot, max_sources, per_server=100, per_device=100, dead=(), server=lambda i: "d11"):
    now = [0.0]
    cams = []
    for i in range(n_held + n_rot):
        cfg = {"key": f"c{i}", "enabled_scripts": ["crowd"]}
        cams.append(Cam(f"c{i}", cfg, G, server(i), f"dev{i // 2}", persistent=i < n_held))
    pipe, res, ev = FakePipeline(), FakeResolver(dead), FakeEval()
    sm = StreamManager(cams, {G: pipe}, max_sources, StreamBudget(per_server, per_device),
                       CircuitBreaker(fail_threshold=3, cooloff_s=300, clock=lambda: now[0]), res, ev,
                       _manifest(), "RTSP", dwell_s=20, stall_s=15, eval_interval_s=2,
                       clock=lambda: now[0], executor=InlineExecutor())
    return sm, pipe, res, ev, now, cams


def _step(sm, now, dt=1.0, frames=True):
    now[0] += dt
    if frames:
        for (g, sid), cam in list(sm.by_sid.items()):
            sm.on_frame(g, sid, [], None)
    sm.tick()


def test_held_first_then_rotation():
    sm, pipe, _, _, now, cams = _setup(n_held=3, n_rot=5, max_sources=4)
    sm.tick()          # 4 slots taken: resolving
    _step(sm, now)     # resolved -> active
    active = {c.key for c in cams if c.state == ACTIVE}
    assert active == {"c0", "c1", "c2", "c3"}, active
    for _ in range(25):  # past the 20 s dwell: the rotating camera hands over
        _step(sm, now)
    active = {c.key for c in cams if c.state == ACTIVE}
    assert {"c0", "c1", "c2"} <= active and "c3" not in active, active
    assert len(active) == 4


def test_budget_per_device():
    # 6 cameras on 3 devices (2 each), per-device budget 1 -> at most 3 at once
    sm, _, _, _, now, cams = _setup(n_held=6, n_rot=0, max_sources=10, per_device=1)
    sm.tick()
    _step(sm, now)
    assert sum(c.state == ACTIVE for c in cams) == 3
    assert sm.stats["budget_blocked"] > 0


def test_stall_backoff_and_breaker():
    sm, pipe, res, _, now, cams = _setup(n_held=3, n_rot=0, max_sources=3)
    sm.tick()
    _step(sm, now)
    assert all(c.state == ACTIVE for c in cams)
    for _ in range(16):             # no frames for > stall_s
        _step(sm, now, frames=False)
    assert all(c.state == IDLE for c in cams), [c.state for c in cams]
    assert sorted(res.invalidated) == ["c0", "c1", "c2"]
    assert sm.breaker.is_open("d11")  # 3 failures on one server -> paused
    _step(sm, now, dt=20)             # backoff over, but the server is paused
    assert all(c.state == IDLE for c in cams) and sm.stats["breaker_skipped"] > 0


def test_error_drops_stream():
    sm, pipe, res, _, now, cams = _setup(n_held=1, n_rot=0, max_sources=1)
    sm.tick()
    _step(sm, now)
    sm.on_source_error(G, cams[0].sid, "end of stream")
    _step(sm, now)
    assert cams[0].state == IDLE and res.invalidated == ["c0"]
    assert ("remove", 0) in pipe.log


def test_dead_camera_backs_off():
    sm, _, _, _, now, cams = _setup(n_held=1, n_rot=0, max_sources=1, dead={"c0"})
    sm.tick()
    _step(sm, now)
    assert cams[0].state == IDLE and cams[0].retry_at > now[0] and sm.stats["resolve_failed"] == 1
    assert sm.free[G] and len(sm.free[G]) == 1   # slot and budget handed back
    assert sm.budget.in_use("d11") == 0


def test_evaluation_interval():
    sm, _, _, ev, now, cams = _setup(n_held=1, n_rot=0, max_sources=1)
    sm.tick()
    _step(sm, now)
    sid = cams[0].sid
    frame = np.zeros((1080, 1920, 3), np.uint8)
    assert sm.want_frame(G, sid)
    sm.on_frame(G, sid, [(1, 0, 0.9, 100, 100, 50, 100)], frame)
    assert len(ev.packets) == 1 and ev.packets[0]["detections"][0].cls == 0
    assert not sm.want_frame(G, sid)       # evaluated just now
    now[0] += 2.1
    assert sm.want_frame(G, sid)


def test_box_space_from_pipeline():
    """New streammux: boxes come in the camera's own resolution, not the mux size."""
    sm, pipe, res, ev, now, cams = _setup(1, 0, max_sources=1)
    sm.tick()
    _step(sm, now)
    (g, sid), = sm.by_sid
    frame = np.zeros((882, 1570, 3), np.uint8)
    raw = [(1, 0, 0.9, 100.0, 50.0, 200.0, 400.0)]
    now[0] += 5
    sm.on_frame(g, sid, raw, frame, space=(1570, 882))
    d = ev.packets[-1]["detections"][0]
    assert (d.x1, d.y1, d.x2, d.y2) == (100.0, 50.0, 300.0, 450.0)
    now[0] += 5
    sm.on_frame(g, sid, raw, frame)                   # legacy mux: 1920x1080 space
    d = ev.packets[-1]["detections"][0]
    assert np.allclose((d.x1, d.y2), (100 * 1570 / 1920, 450 * 882 / 1080))
