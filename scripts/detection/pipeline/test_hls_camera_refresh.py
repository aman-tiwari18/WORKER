"""Tests for the HLS camera refresh (master_pipeline._hls_capture_supervisor / _hls_collapse): fake capture,
fake camera list that changes over time. Run: python test_hls_camera_refresh.py"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "utils"))

import master_pipeline as M                 # noqa: E402
import workers.hls_scheduler as H           # noqa: E402

calls = []                                   # (set of camera keys, {key: enabled scripts}) per capture start
lock = threading.Lock()


def fake_run_hls_cameras(configs, stop_event, camera_fn, **kw):
    with lock:
        calls.append(({M._cam_key(c) for c in configs}, {M._cam_key(c): sorted(c["enabled_scripts"]) for c in configs}))
    stop_event.wait()


H.run_hls_cameras = fake_run_hls_cameras
M._publish_camera_count = lambda *a, **k: None

fails = 0


def check(name, cond, extra=""):
    global fails
    fails += 0 if cond else 1
    print("PASS" if cond else "FAIL", name, extra)


def cam(key, script):
    return {"ch_no": key, "camera_id": key, "script_code": script}


# ---- _hls_collapse
cfgs, others = M._hls_collapse([cam("1", "crowd"), cam("1", "proximity"), cam("2", "crowd"), cam("3", "tamper"), cam("4", "nonsense")])
by = {M._cam_key(c): c for c in cfgs}
check("collapse: one config per camera", set(by) == {"1", "2", "3"} and len(cfgs) == 3)
check("collapse: analytics merged on a camera", sorted(by["1"]["enabled_scripts"]) == ["crowd", "proximity"])
check("collapse: side analytic separate", by["3"]["side_scripts"] == ["tamper"] and by["3"]["enabled_scripts"] == [])
check("collapse: unknown analytic is left for the per-script path", [M._cam_key(c) for c in others] == ["4"])
cfgs2, _ = M._hls_collapse([cam("1", "crowd"), cam("1", "proximity")], served_unified={"crowd"}, served_side=set())
check("collapse: analytic with no worker is not handed frames", cfgs2[0]["enabled_scripts"] == ["crowd"])

# ---- supervisor
world = {"cams": [cam("1", "crowd"), cam("2", "crowd")], "boom": False}


def reload_fn():
    if world["boom"]:
        raise RuntimeError("db down")
    return [dict(c) for c in world["cams"]]


initial, _ = M._hls_collapse(world["cams"])
stop = threading.Event()
t = threading.Thread(target=M._hls_capture_supervisor,
                     args=(initial, reload_fn, stop, None, {}, {"crowd", "proximity"}, set()),
                     kwargs={"interval": 0.3}, daemon=True)
t.start()
time.sleep(0.2)
check("starts the capture on the launch cameras", len(calls) == 1 and calls[0][0] == {"1", "2"})
time.sleep(1.0)
check("no restart while nothing changed", len(calls) == 1)

world["cams"].append(cam("3", "crowd"))
time.sleep(1.0)
check("new accessible camera -> capture restarted with it", len(calls) == 2 and calls[1][0] == {"1", "2", "3"}, str(calls[-1][0]))

world["cams"] = [c for c in world["cams"] if c["ch_no"] != "2"]          # camera 2 stops being accessible
time.sleep(1.0)
check("camera that dropped out is kept, no churn", len(calls) == 2)

world["boom"] = True
time.sleep(0.8)
check("failed re-read is survived", t.is_alive() and len(calls) == 2)
world["boom"] = False

world["cams"].append(cam("1", "proximity"))                                # camera 1 now also eligible for proximity
time.sleep(1.0)
check("camera newly eligible for another analytic -> restart", len(calls) == 3, str(calls[-1][1]))
check("restart keeps the union of analytics", calls[-1][1].get("1") == ["crowd", "proximity"] and "2" in calls[-1][1])

stop.set()
time.sleep(0.5)
check("stops cleanly", not t.is_alive())
print("\n%d failed" % fails)
sys.exit(1 if fails else 0)
