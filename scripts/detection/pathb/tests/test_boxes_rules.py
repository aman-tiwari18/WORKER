"""The Plan B adapter must make every analytics rule behave exactly as it does on
a real Ultralytics Results object built from the same boxes."""
import numpy as np
import torch
from ultralytics.engine.results import Results

from pathb.boxes import COCO_NAMES, DetectionResult, Detection

import unified_detector
H, W = 1080, 1920

# person-heavy scene: two close persons, one far, a chair, a table, a bench
BOXES = [
    # x1,   y1,   x2,   y2,  conf, cls
    (400, 300, 560, 700, 0.91, 0),
    (520, 310, 690, 720, 0.88, 0),
    (1400, 200, 1520, 520, 0.77, 0),
    (900, 600, 1010, 800, 0.66, 56),
    (1100, 650, 1400, 820, 0.71, 60),
    (200, 850, 500, 960, 0.60, 13),
]


def _frame():
    rng = np.random.default_rng(0)
    return rng.integers(0, 255, size=(H, W, 3), dtype=np.uint8)


def _item(frame, cam):
    return {"frame": frame, "camera_no": cam, "ch_no": cam, "device_id": f"dev{cam}",
            "camera_id": f"dev{cam}", "exam_id": "0", "shift_id": "0",
            "source_type": "RTSP", "tc_code": ""}


def _ultra():
    t = torch.tensor(BOXES, dtype=torch.float32)
    r = Results(np.zeros((H, W, 3), np.uint8), path="x", names=COCO_NAMES, boxes=t)
    # qp_box only uses shared boxes that come from Plan B; mark the reference
    # result too so both runs compare the same boxes instead of qp_box's own YOLO.
    r.from_pathb = True
    return r


def _adapter():
    dets = [Detection("yolo26", int(c), float(s), x1, y1, x2, y2) for x1, y1, x2, y2, s, c in BOXES]
    return DetectionResult(dets, (H, W))


def _run(code, result, cam):
    import importlib
    mod_name, fn_name, _ = unified_detector.ANALYTIC_SPEC[code]
    # Fresh module per run: rules keep per-camera state (tracks, count windows),
    # and the annotation draws the camera id, so both runs use the same id on a
    # clean module.
    fn = getattr(importlib.reload(importlib.import_module(mod_name)), fn_name)
    frame = _frame()
    out = fn([_item(frame, cam)], results=[result])[0]
    return out


def _check(code):
    a = _run(code, _ultra(), f"cam_{code}")
    b = _run(code, _adapter(), f"cam_{code}")
    assert bool(a[1]) == bool(b[1]), f"{code}: detected {a[1]} vs {b[1]}"
    if len(a) > 2:
        assert a[2] == b[2], f"{code}: count {a[2]} vs {b[2]}"
    assert np.array_equal(a[0], b[0]), f"{code}: annotated frames differ"
    return bool(a[1]), (a[2] if len(a) > 2 else None)


def test_all_rules_match():
    results = {}
    for code in unified_detector.ANALYTIC_SPEC:
        results[code] = _check(code)
    return results
