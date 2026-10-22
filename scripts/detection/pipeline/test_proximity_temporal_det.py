"""Tests for proximity_temporal_det (synthetic people and depth; no model needed). Run: python test_proximity_temporal_det.py"""
import os
import sys
import tempfile
from types import SimpleNamespace

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "utils"))

import proximity_temporal_det as D          # noqa: E402
import proximity_temporal_system as T       # noqa: E402
import proximity_repeat                     # noqa: E402

proximity_repeat.REPEAT_DIR = tempfile.mkdtemp(prefix="prox_temporal_test_")
D._models = lambda: SimpleNamespace(device="cpu", det=None, pose=None, depth=None, _tracker_cfg=None)
H, W = 720, 1280
FRAME = np.zeros((H, W, 3), np.uint8)
DEPTH = np.full((H, W), 5.0, np.float32)


def skeleton(cx, top, h, sw, seated):
    k = np.zeros((17, 3), np.float32)
    k[0] = (cx, top + 0.1 * h, 0.9)
    k[5], k[6] = (cx - sw / 2, top + 0.25 * h, 0.9), (cx + sw / 2, top + 0.25 * h, 0.9)
    k[11], k[12] = (cx - sw / 3, top + 0.6 * h, 0.9), (cx + sw / 3, top + 0.6 * h, 0.9)
    if not seated:
        k[15], k[16] = (cx - sw / 3, top + h, 0.9), (cx + sw / 3, top + h, 0.9)
    return k


def inference(xs, seated=True, top=300, h=160, sw=40):
    boxes = np.array([[x - sw * 0.8, top, x + sw * 0.8, top + h] for x in xs], np.float32)
    kps = [skeleton(x, top, h, sw, seated) for x in xs]
    return T.FrameInference(FRAME.shape, boxes, np.full(len(xs), 0.9, np.float32), (boxes, kps), DEPTH)


def fresh_state():
    return {"report_to": {"source_type": "RTSP", "exam_id": "40", "shift_id": "151"}}


def run(state, inf, t, cooldown=120):
    return D.evaluate(inf, FRAME, state, t + 1_700_000_000.0, "1000001_1", cooldown)


fails = 0


def check(name, cond, extra=""):
    global fails
    fails += 0 if cond else 1
    print("PASS" if cond else "FAIL", name, extra)


# ---- a seated pair, heads 1.25 shoulder-widths apart, others far
close = lambda: inference([400, 450, 900])
s = fresh_state()
out = run(s, close(), 1000.0)
check("first sighting: no alert yet, asks for the confirming frame", out == [] and s["consec"] == 1)
out = run(s, close(), 1010.0)
check("second sample 10 s later: alert", len(out) == 1 and out[0][1] == "proximity")
check("alert carries violation count 1 and a chain id", out and out[0][2]["violation_count"] == 1 and out[0][2]["violation_chain"])
check("annotated frame has the frame's shape", out and out[0][0].shape == FRAME.shape and (out[0][0] != FRAME).any())
check("last_save set, consec reset", s["consec"] == 0 and s.get("last_save") == 1010.0 + 1_700_000_000.0)
out = run(s, close(), 1020.0)
out += run(s, close(), 1030.0)
check("within the cooldown: no second alert", out == [])

# ---- far apart: nothing, and no confirming frame asked for
s = fresh_state()
out = run(s, inference([300, 700, 1100]), 1000.0) + run(s, inference([300, 700, 1100]), 1010.0)
check("seated people far apart: no alert, no candidate", out == [] and s["consec"] == 0)

# ---- a pair seen once only: no alert
s = fresh_state()
run(s, close(), 1000.0)
out = run(s, inference([300, 700, 1100]), 1010.0)
check("pair gone at the second sample: no alert", out == [] and s["consec"] == 0)

# ---- standing people are not a student pair
s = fresh_state()
run(s, inference([400, 450], seated=False), 1000.0)
out = run(s, inference([400, 450], seated=False), 1010.0)
check("standing pair: no alert", out == [] and s["consec"] == 0)

# ---- the posture model calling one of them standing also drops the pair
s = fresh_state()
s["posture"] = [((380.0, 300.0, 420.0, 460.0), D.POSTURE_STANDING_CLASS, 0.9)]
run(s, close(), 1000.0)
out = run(s, close(), 1010.0)
check("posture model says standing: no alert", out == [])

# ---- a pair on top of each other (one person detected twice) is dropped
s = fresh_state()
run(s, inference([400, 408]), 1000.0)
out = run(s, inference([400, 408]), 1010.0)
check("duplicate box: no alert", out == [])

# ---- three students in a row: one crowd alert
s = fresh_state()
run(s, inference([400, 450, 500]), 1000.0)
out = run(s, inference([400, 450, 500]), 1010.0)
check("three clustered students: one alert", len(out) == 1)

print("\n%d failed" % fails)
sys.exit(1 if fails else 0)
