"""Shared types, box geometry and parameters.

Vendored from monitor_alerts_deploy_v2/monitor_alerts/core.py. Changes for the
pipeline, which samples each camera sparsely: config loading and the unused
`simple` logic params dropped; missing_min_obs / capture_min_frames / touch_cover
added so the rules hold on bursts every few seconds instead of a steady 5 fps."""
from __future__ import annotations

import math
from dataclasses import dataclass, fields

Box = tuple[float, float, float, float]  # (x1, y1, x2, y2) in original-frame pixels


@dataclass(frozen=True)
class Detection:
    box: Box
    score: float


@dataclass
class FrameDetections:
    persons: list[Detection]
    monitors: list[Detection]


def _area(b: Box) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _intersection(a: Box, b: Box) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def iou(a: Box, b: Box) -> float:
    inter = _intersection(a, b)
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0 else 0.0


def overlap(a: Box, b: Box) -> float:
    """Intersection over the smaller box. Unlike IoU it stays high when one box
    is a partly hidden version of the other (same monitor, different visible size)."""
    smaller = min(_area(a), _area(b))
    return _intersection(a, b) / smaller if smaller > 0 else 0.0


def centre_distance(a: Box, b: Box) -> float:
    return math.hypot((a[0] + a[2] - b[0] - b[2]) / 2, (a[1] + a[3] - b[1] - b[3]) / 2)


def box_size(b: Box) -> float:
    """Longer side: the scale used for "within one box size" linking."""
    return max(b[2] - b[0], b[3] - b[1])


def coverage(person: Box, spot: Box) -> float:
    """Fraction of `spot` hidden behind `person` (used to decide 'occluded')."""
    spot_area = _area(spot)
    return _intersection(person, spot) / spot_area if spot_area > 0 else 0.0


@dataclass(frozen=True)
class Params:
    """Every tunable number of the anomaly logic (spec §5.5)."""
    fps: float = 5                        # highest rate a camera is fed at (sizes the evidence buffer)
    capture_seconds: float = 30
    capture_min_frames: int = 1           # ...and at least this many frames seen during capture
    capture_group_iou: float = 0.5
    capture_min_presence: float = 0.5
    match_overlap: float = 0.5
    occlude_cover: float = 0.3
    touch_cover: float | None = None      # a person over this share of a VISIBLE monitor's spot
                                          # also counts as touching it (None: only when it's hidden)
    missing_seconds: float = 3.0
    missing_min_obs: int = 1              # ...and EMPTY in at least this many frames in a row
    offhome_min_frames: int = 8
    offhome_move_frac: float = 0.5
    jitter_tolerance: float = 0.01        # shifts under this share of the frame width are never movement
    person_window_seconds: float = 30
    view_changed_frac: float = 0.5
    cooldown_seconds: float = 30
    # A monitor still HELD (it and a person overlapping it both detected in the frame)
    # alerts again every this many seconds; the camera cooldown still applies.
    repeat_held_seconds: float = 15.0
    stray_forget_seconds: float = 120
    learn_seconds: float = 10
    gap_reset_seconds: float = 2.0
    # evidence (culprit frames)
    evidence_buffer_seconds: float = 60       # recent footage kept per camera
    evidence_delay_seconds: float = 10        # wait after the trigger before writing the alert
    evidence_lookback_seconds: float = 45     # how far before the trigger to search
    evidence_frames: int = 3                  # maximum evidence frames per alert
    evidence_min_spacing_seconds: float = 1.0 # minimum time between chosen frames
    carry_overlap: float = 0.3                # share of the monitor box inside a person box = held
    min_carry_frames: int = 2                 # "carrying" needs the monitor seen in someone's hands this many times
    link_gap_frames: int = 5                  # frames the monitor may go undetected before its path ends

    def __post_init__(self) -> None:
        # The search window must fit in the buffer, or the oldest frames it
        # asks for are already gone.
        if self.evidence_lookback_seconds + self.evidence_delay_seconds > self.evidence_buffer_seconds:
            raise ValueError("evidence_lookback_seconds + evidence_delay_seconds must not exceed "
                             "evidence_buffer_seconds")

    @classmethod
    def from_dict(cls, d: dict) -> Params:
        # Reject unknown keys so a typo fails loudly instead of silently
        # falling back to a default.
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(d) - known)
        if unknown:
            raise ValueError(f"unknown params: {unknown}")
        return cls(**d)
