"""Python reference for what nvinfer + the Plan B parsers do, for offline checks.

Mirrors configs/nvinfer_*.txt preprocessing and parsers/nvdsinfer_pathb_parsers.cpp
decoding, so an exported ONNX/TensorRT model can be compared against the
PyTorch model on videos or images without DeepStream.
"""
import cv2
import numpy as np


def letterbox(frame_bgr, size, pad=114):
    """Ultralytics-style letterbox (centred padding). Returns (NCHW float32, scale, (px, py))."""
    h, w = frame_bgr.shape[:2]
    r = min(size / h, size / w)
    nw, nh = int(round(w * r)), int(round(h * r))
    img = cv2.resize(frame_bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
    px, py = (size - nw) / 2, (size - nh) / 2
    top, left = int(round(py - 0.1)), int(round(px - 0.1))
    out = np.full((size, size, 3), pad, np.uint8)
    out[top:top + nh, left:left + nw] = img
    x = out[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    return np.ascontiguousarray(x), r, (left, top)


def decode_yolo26(out, thr, scale, pad, frame_shape):
    """out: [N, 6] (x1, y1, x2, y2, score, cls) in network pixels -> frame boxes."""
    h, w = frame_shape[:2]
    boxes = []
    for x1, y1, x2, y2, s, c in out:
        if s < thr:
            continue
        x1, x2 = (x1 - pad[0]) / scale, (x2 - pad[0]) / scale
        y1, y2 = (y1 - pad[1]) / scale, (y2 - pad[1]) / scale
        x1, x2 = max(0.0, x1), min(float(w), x2)
        y1, y2 = max(0.0, y1), min(float(h), y2)
        if x2 > x1 and y2 > y1:
            boxes.append((int(round(c)), float(s), x1, y1, x2, y2))
    return boxes


def decode_rfdetr(dets, logits, thr, frame_shape):
    """dets [Q, 4] cxcywh 0-1, logits [Q, C] -> frame boxes (square resize, no letterbox)."""
    h, w = frame_shape[:2]
    scores = 1.0 / (1.0 + np.exp(-logits))
    boxes = []
    for (cx, cy, bw, bh), sc in zip(dets, scores):
        c = int(np.argmax(sc))
        s = float(sc[c])
        if s < thr:
            continue
        x1, y1 = max(0.0, (cx - bw / 2) * w), max(0.0, (cy - bh / 2) * h)
        x2, y2 = min(float(w), (cx + bw / 2) * w), min(float(h), (cy + bh / 2) * h)
        if x2 > x1 and y2 > y1:
            boxes.append((c, s, x1, y1, x2, y2))
    return boxes


def iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def match(ref, test, iou_thr=0.5):
    """Greedy same-class matching. Returns (matched, len(ref), len(test))."""
    used = set()
    matched = 0
    for c, _, *rb in sorted(ref, key=lambda b: -b[1]):
        best, best_j = 0.0, None
        for j, (tc, _, *tb) in enumerate(test):
            if j in used or tc != c:
                continue
            v = iou(rb, tb)
            if v > best:
                best, best_j = v, j
        if best_j is not None and best >= iou_thr:
            used.add(best_j)
            matched += 1
    return matched, len(ref), len(test)
