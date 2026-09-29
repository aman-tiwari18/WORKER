"""Home spots: where each monitor normally sits in a camera's view.

Captured once, when a camera first connects, by collecting monitor detections
for a few seconds and keeping the boxes that are present most of the time.
Saved to disk so reconnects and restarts reuse the same map.

Vendored unchanged from monitor_alerts_deploy_v2/monitor_alerts/home_map.py.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .core import Box, FrameDetections, Params, iou

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HomeSpot:
    id: int
    box: Box


@dataclass
class HomeMap:
    camera_id: str
    frame_size: tuple[int, int]  # (width, height)
    captured_at: str             # ISO 8601 with timezone
    spots: list[HomeSpot]


@dataclass
class _Group:
    """Boxes from different frames that belong to the same physical monitor."""
    boxes: list[Box] = field(default_factory=list)
    frames: set[int] = field(default_factory=set)
    median: Box = (0.0, 0.0, 0.0, 0.0)  # cached: recomputed only when a box is added

    def add(self, box: Box, frame_index: int) -> None:
        self.boxes.append(box)
        self.frames.add(frame_index)
        self.median = tuple(float(v) for v in np.median(np.array(self.boxes), axis=0))


def capture_home_map(camera_id: str, frame_size: tuple[int, int],
                     frames: list[FrameDetections], params: Params) -> HomeMap:
    groups: list[_Group] = []
    for i, dets in enumerate(frames):
        for det in dets.monitors:
            score, best = max(((iou(det.box, g.median), g) for g in groups),
                              key=lambda sg: sg[0], default=(0.0, None))
            if score < params.capture_group_iou:
                best = _Group()
                groups.append(best)
            best.add(det.box, i)

    # Keep monitors seen in most frames: brief occlusion or missed detections
    # during capture don't lose a spot, and one-off false detections don't add one.
    n = max(1, len(frames))
    boxes = [g.median for g in groups if len(g.frames) / n >= params.capture_min_presence]
    boxes.sort(key=lambda b: ((b[1] + b[3]) / 2, (b[0] + b[2]) / 2))  # top-to-bottom, left-to-right
    spots = [HomeSpot(i, b) for i, b in enumerate(boxes)]
    return HomeMap(camera_id, tuple(frame_size), datetime.now().astimezone().isoformat(), spots)


def save_home_map(home_map: HomeMap, homes_dir: Path,
                  reference_frame: np.ndarray | None = None) -> Path:
    homes_dir = Path(homes_dir)
    homes_dir.mkdir(parents=True, exist_ok=True)
    path = homes_dir / f"{home_map.camera_id}.json"
    path.write_text(json.dumps({
        "camera_id": home_map.camera_id,
        "frame_size": list(home_map.frame_size),
        "captured_at": home_map.captured_at,
        "spots": [{"id": s.id, "box": list(s.box)} for s in home_map.spots],
    }, indent=2))
    if reference_frame is not None:
        # A picture of the captured spots, so an operator can check them at a glance.
        img = reference_frame.copy()
        for s in home_map.spots:
            x1, y1, x2, y2 = map(int, s.box)
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(img, str(s.id), (x1, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        cv2.imwrite(str(homes_dir / f"{home_map.camera_id}_reference.jpg"), img)
    return path


def load_home_map(homes_dir: Path, camera_id: str, frame_size: tuple[int, int]) -> HomeMap | None:
    path = Path(homes_dir) / f"{camera_id}.json"
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    stored_size = tuple(d["frame_size"])
    if stored_size != tuple(frame_size):
        log.warning("%s: saved home map is %s but stream is %s; recapturing", camera_id, stored_size, tuple(frame_size))
        return None
    spots = [HomeSpot(s["id"], tuple(s["box"])) for s in d["spots"]]
    return HomeMap(d["camera_id"], stored_size, d["captured_at"], spots)
