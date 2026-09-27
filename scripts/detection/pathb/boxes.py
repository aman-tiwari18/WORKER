"""Hand DeepStream detections to the existing analytics rules unchanged.

The 14 rule functions (detect_*_batch) read Ultralytics results only as:

    for box in result.boxes:
        int(box.cls[0]); float(box.conf[0]); box.xyxy[0]  (also .cpu().numpy().tolist())

`DetectionResult` provides exactly that surface over plain numpy data, so the
rules run as-is on boxes produced by TensorRT/DeepStream.
"""
from collections import namedtuple

import numpy as np

# One box in FRAME pixel coordinates, class already remapped to the COCO id.
Detection = namedtuple("Detection", "model cls conf x1 y1 x2 y2")

# COCO-80 names, indexed by the COCO id every class is remapped to. The phone
# rule compares `result.names[cls] == "cell phone"`.
COCO_NAMES = dict(enumerate((
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
    "teddy bear", "hair drier", "toothbrush",
)))


class _T(np.ndarray):
    """ndarray that also answers torch-style .cpu() / .numpy()."""

    def cpu(self):
        return self

    def numpy(self):
        return np.asarray(self)


def _t(values):
    return np.asarray(values, dtype=np.float32).view(_T)


class _Box:
    __slots__ = ("cls", "conf", "xyxy")

    def __init__(self, det):
        self.cls = _t([det.cls])
        self.conf = _t([det.conf])
        self.xyxy = _t([[det.x1, det.y1, det.x2, det.y2]])


class DetectionResult:
    """Stand-in for ultralytics.engine.results.Results as the rules use it."""

    names = COCO_NAMES
    # Rules that normally run their own model (qp_box) check this to use these
    # shared boxes instead — Plan B already applied their lower threshold.
    from_pathb = True

    def __init__(self, detections, orig_shape):
        self.boxes = [_Box(d) for d in detections]
        self.orig_shape = orig_shape

    def __len__(self):
        return len(self.boxes)


def to_detections(raw, manifest, frame_w, frame_h, mux_w, mux_h):
    """Raw DeepStream objects -> Detections in frame pixels with COCO class ids.

    `raw` items: (gie_id, class_id, confidence, left, top, width, height) in
    streammux resolution (mux_w x mux_h). Boxes below the lowest threshold any
    analytic needs from that model, or with an unmapped class, are dropped;
    per-analytic thresholds are applied later in build_result.
    """
    sx = frame_w / float(mux_w)
    sy = frame_h / float(mux_h)
    out = []
    for gie_id, class_id, conf, left, top, width, height in raw:
        spec = manifest.by_gie_id.get(gie_id)
        if spec is None or conf < manifest.floor_conf(spec.name):
            continue
        coco = spec.to_coco(class_id)
        if coco is None:
            continue
        x1 = max(0.0, left * sx)
        y1 = max(0.0, top * sy)
        x2 = min(float(frame_w), (left + width) * sx)
        y2 = min(float(frame_h), (top + height) * sy)
        if x2 <= x1 or y2 <= y1:
            continue
        out.append(Detection(spec.name, coco, float(conf), x1, y1, x2, y2))
    return out


def build_result(detections, analytic, manifest, frame_shape):
    """Result for one analytic: boxes from the models it reads, at its threshold."""
    models = manifest.models_for_analytic(analytic)
    keep = [d for d in detections
            if d.model in models and d.conf >= manifest.min_conf(analytic, d.model)]
    return DetectionResult(keep, frame_shape[:2])
