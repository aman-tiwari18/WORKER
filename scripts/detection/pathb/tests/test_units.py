"""Unit tests for the Plan B manifest, box conversion, budgets and circuit breaker."""
import os
import tempfile

from pathb.boxes import build_result, to_detections
from pathb.manifest import load_manifest
from pathb.stream_budget import CircuitBreaker, StreamBudget

MANIFEST = """
models:
  yolo26:
    gie_id: 1
    parser: NvDsInferParseYolo26
    weights: w.pt
    onnx: y.onnx
    engine: y.engine
    nvinfer_template: y.txt
    input_size: 640
    num_classes: 80
    conf_threshold: 0.45
    class_map: coco
  rfdetr:
    gie_id: 2
    parser: NvDsInferParseRFDETR
    weights: r.pth
    onnx: r.onnx
    engine: r.engine
    nvinfer_template: r.txt
    input_size: 560
    num_classes: 2
    conf_threshold: 0.5
    enabled: {rf_enabled}
    class_map: {{0: 0, 1: 67}}
analytics:
  default: [yolo26]
  phone: [yolo26, rfdetr]
analytic_min_conf:
  qp_box: 0.30
"""


def _manifest(rf_enabled=True):
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        f.write(MANIFEST.format(rf_enabled=str(rf_enabled).lower()))
    try:
        return load_manifest(path)
    finally:
        os.remove(path)


def test_models_per_analytic():
    m = _manifest()
    assert m.models_for_analytic("crowd") == ("yolo26",)
    assert m.models_for_analytic("phone") == ("yolo26", "rfdetr")
    assert m.models_for(["crowd", "phone"]) == ("yolo26", "rfdetr")
    # disabled RF-DETR -> phone falls back to YOLO26 only
    m2 = _manifest(rf_enabled=False)
    assert m2.models_for_analytic("phone") == ("yolo26",)


def test_thresholds_and_class_map():
    m = _manifest()
    assert m.floor_conf("yolo26") == 0.30          # qp_box needs 0.30 from yolo26
    assert m.min_conf("crowd", "yolo26") == 0.45
    assert m.min_conf("qp_box", "yolo26") == 0.30
    raw = [
        # gie, cls, conf, left, top, width, height  (mux 1920x1080)
        (1, 0, 0.90, 100, 100, 50, 100),   # person, strong
        (1, 24, 0.35, 300, 300, 40, 40),   # backpack 0.35: qp_box only
        (1, 0, 0.20, 10, 10, 10, 10),      # below every threshold -> dropped
        (2, 1, 0.60, 500, 500, 20, 30),    # rfdetr class 1 -> COCO 67 (phone)
        (2, 5, 0.90, 600, 600, 20, 30),    # rfdetr class 5 not in map -> dropped
    ]
    dets = to_detections(raw, m, frame_w=1280, frame_h=720, mux_w=1920, mux_h=1080)
    assert [(d.model, d.cls) for d in dets] == [("yolo26", 0), ("yolo26", 24), ("rfdetr", 67)]
    # scaled from mux 1920x1080 to frame 1280x720
    p = dets[0]
    assert (round(p.x1), round(p.y1), round(p.x2), round(p.y2)) == (67, 67, 100, 133)
    crowd = build_result(dets, "crowd", m, (720, 1280, 3))
    assert [int(b.cls[0]) for b in crowd.boxes] == [0]
    qp = build_result(dets, "qp_box", m, (720, 1280, 3))
    assert [int(b.cls[0]) for b in qp.boxes] == [0, 24]
    phone = build_result(dets, "phone", m, (720, 1280, 3))
    assert [int(b.cls[0]) for b in phone.boxes] == [0, 67]
    assert phone.names[67] == "cell phone"
    import numpy as np
    assert np.allclose(phone.boxes[0].xyxy[0].cpu().numpy(), [p.x1, p.y1, p.x2, p.y2])


def test_rfdetr_int8_rejected():
    bad = MANIFEST.format(rf_enabled="true").replace("input_size: 560", "input_size: 560\n    precision: int8")
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        f.write(bad)
    try:
        load_manifest(path)
    except ValueError as e:
        assert "INT8" in str(e)
    else:
        raise AssertionError("INT8 RF-DETR should be rejected")
    finally:
        os.remove(path)


def test_stream_budget():
    b = StreamBudget(per_server=3, per_device=2)
    assert b.try_acquire("d11", "dev1")
    assert b.try_acquire("d11", "dev1")
    assert not b.try_acquire("d11", "dev1")        # device full (2)
    assert b.try_acquire("d11", "dev2")
    assert not b.try_acquire("d11", "dev3")        # server full (3)
    assert b.try_acquire("d14", "dev9")            # other server unaffected
    b.release("d11", "dev1")
    assert b.try_acquire("d11", "dev3")
    assert b.in_use("d11") == 3


def test_circuit_breaker():
    now = [0.0]
    cb = CircuitBreaker(fail_threshold=3, cooloff_s=300, clock=lambda: now[0])
    assert not cb.record_failure("d11") and not cb.record_failure("d11")
    assert cb.record_failure("d11")                # third failure opens it
    assert cb.is_open("d11") and cb.open_keys() == ["d11"]
    now[0] = 299
    assert cb.is_open("d11")
    now[0] = 301
    assert not cb.is_open("d11")                   # cool-off over, counter reset
    cb.record_failure("d11")
    cb.record_success("d11")
    assert not cb.record_failure("d11")


def test_decode_ref_matches_nvinfer_mapping():
    """Letterbox + YOLO26 / RF-DETR decoding used by the accuracy gate."""
    import numpy as np
    from pathb.decode_ref import decode_rfdetr, decode_yolo26, letterbox, match
    img = np.zeros((1080, 1920, 3), np.uint8)
    x, r, pad = letterbox(img, 640)
    assert x.shape == (1, 3, 640, 640) and abs(r - 1 / 3) < 1e-6 and pad == (0, 140)
    # a box covering the whole letterboxed area maps back to the full frame
    b = decode_yolo26(np.array([[0, 140, 640, 500, 0.9, 0]], np.float32), 0.45, r, pad, img.shape)
    assert [round(v) for v in b[0][2:]] == [0, 0, 1920, 1080]
    b = decode_rfdetr(np.array([[0.5, 0.5, 0.5, 0.5]]), np.array([[-3.0, 2.0]]), 0.5, img.shape)
    assert b[0][0] == 1 and [round(v) for v in b[0][2:]] == [480, 270, 1440, 810]
    assert match([(0, .9, 0, 0, 10, 10)], [(0, .8, 1, 1, 10, 10), (2, .9, 0, 0, 10, 10)]) == (1, 1, 2)
