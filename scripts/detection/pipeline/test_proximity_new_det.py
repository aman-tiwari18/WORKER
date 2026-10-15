"""Tests for proximity_new_det (synthetic people; no model needed). Run: python test_proximity_new_det.py"""
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "utils"))

import proximity_new_det as D          # noqa: E402
import proximity_repeat                # noqa: E402

proximity_repeat.REPEAT_DIR = tempfile.mkdtemp(prefix="prox_repeat_test_")
P = D.P
FRAME = np.zeros((720, 1280, 3), np.uint8)


def person(cx, top=300, h=160, seated=True, sw=40):
    """A person box with a skeleton: nose, shoulders, hips; ankles only when standing."""
    k = np.zeros((17, 3), np.float32)
    k[0] = (cx, top + 0.1 * h, 0.9)
    k[5] = (cx - sw / 2, top + 0.25 * h, 0.9)
    k[6] = (cx + sw / 2, top + 0.25 * h, 0.9)
    k[11] = (cx - sw / 3, top + 0.6 * h, 0.9)
    k[12] = (cx + sw / 3, top + 0.6 * h, 0.9)
    if not seated:
        k[15] = (cx - sw / 3, top + h, 0.9)
        k[16] = (cx + sw / 3, top + h, 0.9)
    return P.Person(cx - sw * 0.8, top, cx + sw * 0.8, top + h, 0.9, k, 0.5)


def fresh_state():
    return {"report_to": {"source_type": "RTSP", "exam_id": "40", "shift_id": "151"}}


def run(state, people, t, cooldown=120, cam="1000001_1"):
    return D.evaluate(people, FRAME, state, t + 1_700_000_000.0, cam, cooldown)    # real epoch-sized times


fails = 0


def check(name, cond, extra=""):
    global fails
    fails += 0 if cond else 1
    print("PASS" if cond else "FAIL", name, extra)


# ---- a seated pair, heads 1.25 shoulder-widths apart
pair = lambda: [person(400), person(450)]
s = fresh_state()
out = run(s, pair(), 1000.0)
check("first sighting: no alert yet, asks for the confirming frame", out == [] and s["consec"] == 1)
out = run(s, pair(), 1010.0)
check("second sample 10 s later: alert", len(out) == 1 and out[0][1] == "proximity")
check("alert carries violation count 1 and a chain id", out and out[0][2]["violation_count"] == 1 and out[0][2]["violation_chain"])
check("annotated frame has the frame's shape", out and out[0][0].shape == FRAME.shape and (out[0][0] != FRAME).any())
check("consec reset and last_save set after the alert", s["consec"] == 0 and s.get("last_save") == 1010.0 + 1_700_000_000.0)
chain = out[0][2]["violation_chain"]

# ---- cooldown: the same pair again inside it is not reported
out = run(s, pair(), 1030.0)
check("inside the cooldown: no alert", out == [])
# ---- after the cooldown the same pair counts up and keeps its chain
out = run(s, pair(), 1200.0)       # first sample after the cooldown: pair hits are already 2 -> reports
if not out:
    out = run(s, pair(), 1210.0)
check("after the cooldown: violation count 2, same chain", out and out[0][2]["violation_count"] == 2 and out[0][2]["violation_chain"] == chain,
      str(out[0][2] if out else None))

# ---- filters
s = fresh_state(); run(s, [person(400), person(900)], 0.0); out = run(s, [person(400), person(900)], 10.0)
check("far apart: nothing", out == [] and s["consec"] == 0)

s = fresh_state(); mk = lambda: [person(400, seated=False), person(450)]
run(s, mk(), 0.0); out = run(s, mk(), 10.0)
check("one of the two standing (staff): nothing", out == [])

s = fresh_state(); mk = lambda: [person(400), person(404)]       # heads 0.1 shoulder-widths apart = one person twice
run(s, mk(), 0.0); out = run(s, mk(), 10.0)
check("double detection (heads almost on top of each other): nothing", out == [])

s = fresh_state(); mk = lambda: [person(400, h=50, sw=14), person(420, h=50, sw=14)]
run(s, mk(), 0.0); out = run(s, mk(), 10.0)
check("tiny far-away boxes: nothing", out == [])

s = fresh_state(); mk = lambda: [person(400, h=160, sw=40), person(450, h=60, sw=15)]   # very different size = different row
run(s, mk(), 0.0); out = run(s, mk(), 10.0)
check("different apparent size (different row): nothing", out == [])

# ---- posture model: a pair with someone called standing is not reported (legs hidden behind desks)
def posture_for(people, classes, conf=0.9):
    return [((p.x1, p.y1, p.x2, p.y2), c, conf) for p, c in zip(people, classes)]

def with_posture(classes, conf=0.9):
    s = fresh_state(); ppl = pair()
    s["posture"] = posture_for(ppl, classes, conf)
    run(s, ppl, 0.0)
    s["posture"] = posture_for(ppl, classes, conf)
    return run(s, ppl, 10.0)

check("posture says both sitting: alert", len(with_posture([0, 0])) == 1)
check("posture says one standing: nothing", with_posture([0, 1]) == [])
check("posture 'standing' below the confidence limit: ignored", len(with_posture([0, 1], conf=0.2)) == 1)
check("posture 'unsure': ignored", len(with_posture([0, 2])) == 1)
check("no posture result: relies on the pose check", len(with_posture([])) == 1)

# ---- a first sighting that is too old does not count
s = fresh_state()
run(s, pair(), 0.0)
out = run(s, pair(), 500.0)
check("second sample 500 s later: not confirmed, starts again", out == [] and s["consec"] == 1)
out = run(s, pair(), 510.0)
check("then confirmed 10 s after that", len(out) == 1)

# ---- a pair that stops being close resets the request for a confirming frame
s = fresh_state(); run(s, pair(), 0.0)
out = run(s, [person(400), person(900)], 10.0)
check("pair moved apart before the confirming frame: nothing, consec 0", out == [] and s["consec"] == 0)

# ---- several pairs in one frame are one alert with the number of pairs in it
s = fresh_state(); mk = lambda: [person(100), person(150), person(700), person(750)]
run(s, mk(), 0.0); out = run(s, mk(), 10.0)
check("two pairs in one frame: one alert", len(out) == 1)

# ---- HLS cameras use their own counter key
s = {"report_to": {"source_type": "HLS", "camera_no": "62898", "exam_id": "44", "shift_id": "165"}}
check("HLS camera key", D._camera_key(s, "x") == "hls_ch62898" and D._camera_key(fresh_state(), "1000001_1") == "rtsp_1000001_1")

print("\n%d FAILED" % fails if fails else "\nall passed")
sys.exit(1 if fails else 0)
