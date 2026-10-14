#!/usr/bin/env python
"""
Proximity_New_Script.py  —  COMPLETE self-contained proximity / closeness detection system.

This single file is the WHOLE pipeline (detector + pose + tracking + geometry modes +
closeness/relative/cheating decision + invigilator filter + temporal confirmation + drawing + CLI),
concatenated from the proximity/ package with no logic changes. The package files are kept as-is;
this is a merged equivalent.

USAGE (identical to run_proximity.py):
    python Proximity_New_Script.py --video clip.mp4 --closeness            # physical-closeness, red-only
    python Proximity_New_Script.py --video clip.mp4 --closeness --no-draw
    python Proximity_New_Script.py --video clip.mp4 --relative             # calibration-free cheating-proximity
    python Proximity_New_Script.py --dir ./clips --closeness

Models: set yolo_weights / pose_weights in the Config class (defaults point at the shared models).
MoGe (default metric mode) is imported lazily, so --closeness / --relative need no heavy deps.
"""

import argparse
import cv2
import glob
import json
import numpy as np
import os
import time
import torch
from dataclasses import dataclass, field
from itertools import combinations
from scipy.optimize import linear_sum_assignment
from ultralytics import YOLO

# ==============================================================================
# ==== proximity/config.py ====
# ==============================================================================
"""Central configuration for the proximity-violation system.

All distances are REAL physical metres (from MoGe 3D geometry), never pixels.
"""


@dataclass
class Config:
    # ---- person detection (two-stage: strong detector for boxes + pose for keypoints) ----
    # Detector gives high-recall boxes; pose keypoints let us localise seated people
    # (hips visible) instead of trusting the box bottom (which lands on the desk).
    # Pose is matched onto detector boxes; boxes with no pose fall back to box-bottom.
    yolo_weights: str = "/root/cctv/monitor_script/models/yolo11m.pt"       # detection
    pose_weights: str = "/root/cctv/monitor_script/models/yolo11m-pose.pt"  # keypoints
    person_class_id: int = 0          # COCO 'person'
    det_conf: float = 0.40            # confident detections only -> fewer false people
    det_imgsz: int = 1280             # large -> better small-person recall on CCTV
    min_box_h_frac: float = 0.06      # ignore tiny boxes (< 6% of frame height) = noise/far clutter
    drop_bottom_truncated: bool = True  # feet cut off at frame bottom -> unreliable
    kp_conf_min: float = 0.5          # min keypoint confidence to trust an anchor joint
    pose_match_iou: float = 0.5       # IoU to attach a pose skeleton to a detector box

    # ---- per-camera floor homography (Fix 4) ----
    # When set (3x3), floor distances come from the homography (image floor -> metres),
    # bypassing MoGe entirely. Reliable on fixed cameras; MoGe is the fallback when None.
    homography: list = None

    # ---- automatic calibration from pedestrians (people-as-rulers) ----
    # An AutoCalibration object (fitted from the video's standing people). When set,
    # floor positions come from it: feet for standing, head for seated. No manual input.
    autocalib: object = None

    # Calibration-free fallback: height-normalized distance (each person's pixel
    # height is a ~1.7 m ruler). Used when auto-calibration is too weak to trust,
    # so we never fall back to the unreliable MoGe path.
    height_norm: bool = False
    autocalib_min_samples: int = 40   # need at least this many standing samples to trust autocalib
    autocalib_hc_range: tuple = (2.0, 5.0)  # plausible camera height (m) to accept a fit

    # ---- 3D geometry (MoGe-2 metric) ----
    moge_model: str = "Ruicheng/moge-2-vitl-normal"  # metric; also predicts normals
    moge_max_size: int = 800          # longest image side fed to MoGe
    foot_patch: int = 4               # half-window (px, MoGe res) for median 3D lookup at a foot
    max_depth_m: float = 40.0         # discard foot points farther than this (implausible)

    # ---- floor plane ----
    use_floor_plane: bool = True
    ransac_iters: int = 500
    ransac_thresh_m: float = 0.12     # inlier band for plane fit (metres)
    min_floor_inliers: int = 4

    # ---- calibration-free "head proximity" (scales to any camera, no setup) ----
    # Cheating = heads brought together. Distance between heads, normalized by body
    # (shoulder) size, is depth-robust and scale-free; a size-ratio gate rejects pairs
    # at different depths (different rows) or standing-vs-seated. No calibration.
    # Thresholds below are calibrated to 235 human-labelled pairs (diagnostics/PHASE2/3/7).
    # Key empirical findings that drove them:
    #   - DISTANCE is NOT monotonic: true violations sit in a BAND of head-distance, not "closest".
    #     (old rel_thresh=1.3 "closer=worse" scored F1 0.06). Band = [rel_min_thresh, rel_thresh].
    #   - HORIZONTAL head-alignment (small |dy|/|dx|) is the strongest same-row cue (rel_vert_ratio).
    #   - SIZE-RATIO must stay loose: real same-row leans reach sr~3.5, so a tight gate kills recall.
    relative_proximity: bool = False
    rel_thresh: float = 2.3           # BAND UPPER: heads farther than this (shoulder-widths) = not a pair
    rel_gather_thresh: float = 1.6
    rel_size_ratio_max: float = 3.5   # body-size ratio above this = clearly different depth -> reject (loose on purpose)
    rel_min_thresh: float = 1.0       # BAND LOWER: closer than this = duplicate/occlusion artifact, ignore
    rel_vert_ratio: float = 0.25      # heads stacked vertically (|dy|>ratio*|dx|) = different rows, reject
    lean_combo_min: float = 0.1       # min pose lean+turn TOWARD partner (shoulder-widths); the interaction gate

    # ---- closeness mode: flag two people who are ACTUALLY physically close (same row/depth),
    #      rejecting different-row perspective look-alikes. Geometric fact (not "cheating").
    #      Generous thresholds so no genuine closeness is missed; student-only via invigilator gate.
    closeness_mode: bool = False
    close_size_ratio_max: float = 1.45  # similar apparent size => same depth/row (reject bigger gap = behind)
    close_vert_ratio_max: float = 1.0   # heads not stacked vertically (|dy|<=ratio*|dx|) => same row
    close_nd: float = 1.8               # <= this many shoulder-widths apart => physically close
    draw_red_only: bool = False         # annotate ONLY flagged pairs (red); no green boxes on other people

    # ---- proximity thresholds (metres, on the floor) ----
    proximity_m: float = 1.2          # two people closer than this = possible cheating pair
    gather_m: float = 1.5             # edge distance for grouping
    gather_min: int = 3               # connected cluster of >= this many = gathering

    # ---- row-aware filtering (metric modes only) ----
    # Exam cheating is between SIDE-BY-SIDE (same-row) neighbours; different rows have a
    # desk/aisle between them. Require small depth difference so cross-row pairs that only
    # *look* close from the camera angle are rejected. Also drop duplicate detections.
    row_aware: bool = True
    same_row_dz: float = 0.6          # max depth (row) difference, metres, for a valid pair
    dedup_iou: float = 0.55           # boxes overlapping more than this = same person, ignore

    # ---- invigilator vs student ----
    # Invigilators stand (feet visible) and walk the aisles; seated students have
    # occluded legs and stay put. Exclude mobile/standing people so only
    # student-to-student proximity is flagged.
    exclude_invigilators: bool = True
    invig_standing_frac: float = 0.2   # ankles/feet visible this often = standing (staff); seated students ~0
    invig_mobility_std: float = 0.5    # std-dev of floor position (noise-robust); students ~0.1, walkers >0.5
    invig_max_partners: int = 3        # close to more than this many DIFFERENT people over the clip = mobile (staff)

    # ---- tracking + temporal persistence (the main false-positive filter) ----
    sample_fps: float = 3.0           # frames/sec analysed (L4 handles MoGe comfortably)
    track_iou_min: float = 0.2        # min box IoU to keep same track id (SORT-style, stable)
    track_gate_m: float = 1.0         # fallback: floor movement gate when IoU match fails
    track_max_age: int = 6            # keep a lost track this many samples for re-association
    persist_frames: int = 3           # a violation must hold this many consecutive samples
    release_frames: int = 2           # drop a violation after this many clean samples
    min_event_s: float = 1.5          # only report violations sustained at least this long

    # ---- interaction cue (Fix 2b) ----
    # In a fixed seat grid, adjacency is normal; a violation is someone LEANING /
    # TURNING toward a neighbour. When on, a close pair is confirmed only if the
    # pose shows lean/turn interaction. Off by default (tune on labelled exam data).
    require_interaction: bool = False
    lean_frac: float = 0.18           # sideways torso lean, fraction of box width
    turn_frac: float = 0.30           # head turn (nose vs shoulders), fraction of box width
    gaze_frac: float = 0.15           # head yaw toward neighbour, fraction of head width

    # ---- cubicle dividers (Fix 2) ----
    # Per-camera image-space barrier segments: [[x1,y1],[x2,y2]], ... .
    # A flagged pair whose connecting line crosses a divider is suppressed
    # (adjacent-cubicle neighbours can't actually interact). Empty = inactive.
    barriers: list = field(default_factory=list)

    # ---- runtime / drawing ----
    device: str = "cuda"
    draw: bool = True


# ==============================================================================
# ==== proximity/headpose.py ====
# ==============================================================================
"""Head-pose / gaze from facial keypoints (nose, eyes, ears).

The real exam-cheating signal isn't closeness (adjacent seats are always close) —
it's a student turning their HEAD toward a neighbour's screen/paper. We read head
yaw directly from the pose model's facial keypoints: the nose points in the facing
direction relative to the head centre (ear midpoint, or eyes when an ear is hidden).
No extra model needed.
"""
NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 1, 2, 3, 4


def facing_offset(kps, kmin):
    """Return (head_center_x, nose_offset, head_width) or None.

    nose_offset > 0 means the head faces image-right. A strong turn hides one ear,
    so we fall back to the eyes; a frontal/averted face offsets the nose sideways.
    """
    if kps is None or kps[NOSE, 2] < kmin:
        return None
    ears = [kps[i, :2] for i in (L_EAR, R_EAR) if kps[i, 2] >= kmin]
    eyes = [kps[i, :2] for i in (L_EYE, R_EYE) if kps[i, 2] >= kmin]
    # prefer a two-point reference (ears, then eyes); else a single point (strong turn)
    if len(ears) == 2:
        side = ears
    elif len(eyes) == 2:
        side = eyes
    elif len(ears) == 1:
        side = ears
    elif len(eyes) == 1:
        side = eyes
    else:
        return None
    if len(side) == 2:
        cx = (side[0][0] + side[1][0]) / 2.0
        w = abs(side[0][0] - side[1][0])
    else:
        cx = side[0][0]           # only one eye/ear visible => strongly turned
        w = 0.0
    return cx, kps[NOSE, 0] - cx, w


def looking_toward(kps, box, other_box, kmin, gaze_frac=0.15):
    """True if this person's head is turned toward `other_box`."""
    fo = facing_offset(kps, kmin)
    if fo is None:
        return False
    cx, off, w = fo
    head_w = w if w > 5 else (box[2] - box[0]) * 0.30     # fallback head width
    my_cx = (box[0] + box[2]) / 2.0
    other_cx = (other_box[0] + other_box[2]) / 2.0
    direction = 1.0 if other_cx >= my_cx else -1.0
    # facing toward the other side by more than gaze_frac of head width,
    # OR a single-side face (one ear hidden) pointing that way (strong turn)
    strong_turn = (w == 0.0) and (off * direction > 0)
    return (off * direction) > gaze_frac * max(1.0, head_w) or strong_turn


def mutual_or_single_gaze(box_a, kps_a, box_b, kps_b, kmin, gaze_frac=0.15):
    return (looking_toward(kps_a, box_a, box_b, kmin, gaze_frac) or
            looking_toward(kps_b, box_b, box_a, kmin, gaze_frac))


# ==============================================================================
# ==== proximity/barriers.py ====
# ==============================================================================
"""Cubicle-divider suppression (Fix 2).

A flagged pair is dropped when the line between the two people (image space,
their anchor/foot pixels) crosses a configured divider segment. For fixed
cameras, dividers are annotated once and reused.
"""


def _ccw(a, b, c):
    return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(p1, p2, p3, p4):
    """True if segment p1p2 intersects segment p3p4."""
    return (_ccw(p1, p3, p4) != _ccw(p2, p3, p4) and
            _ccw(p1, p2, p3) != _ccw(p1, p2, p4))


def blocked_by_divider(pa, pb, barriers):
    """pa, pb: (x,y) image pixels. barriers: list of [[x1,y1],[x2,y2]]."""
    for seg in barriers:
        if segments_cross(pa, pb, seg[0], seg[1]):
            return True
    return False


# ==============================================================================
# ==== proximity/calibration.py ====
# ==============================================================================
"""Per-camera floor homography (Fix 4).

Maps floor pixels to real-world metres so distances don't depend on MoGe's
monocular metric. Calibrate once per fixed camera from >=4 floor correspondences
(pixel <-> real metres), e.g. from the Floor Calibration tool.
"""


def compute_homography(correspondences):
    """correspondences: [{"px":[u,v], "world":[X,Y]}, ...] (>=4).

    Returns (H 3x3, reproj_err per point in metres).
    """
    src = np.array([c["px"] for c in correspondences], dtype=np.float64)
    dst = np.array([c["world"] for c in correspondences], dtype=np.float64)
    if len(src) < 4:
        raise ValueError("need >= 4 correspondences")
    method = cv2.RANSAC if len(src) > 4 else 0
    H, _ = cv2.findHomography(src, dst, method, 0.05)
    if H is None:
        raise ValueError("homography could not be computed (points degenerate?)")
    proj = cv2.perspectiveTransform(src.reshape(-1, 1, 2), H).reshape(-1, 2)
    err = np.linalg.norm(proj - dst, axis=1)
    return H, err


def pixel_to_world(H, u, v):
    p = cv2.perspectiveTransform(np.array([[[float(u), float(v)]]]), np.asarray(H, float))
    return float(p[0, 0, 0]), float(p[0, 0, 1])


def world_distance(H, uv1, uv2):
    a = pixel_to_world(H, uv1[0], uv1[1])
    b = pixel_to_world(H, uv2[0], uv2[1])
    return float(np.hypot(a[0] - b[0], a[1] - b[1]))


# ==============================================================================
# ==== proximity/detector.py ====
# ==============================================================================
"""Two-stage person detection (GPU).

Stage 1: a strong detection model gives high-recall person boxes.
Stage 2: a pose model gives keypoints, matched onto those boxes by IoU.

Each person is anchored at the hip (robust for seated people); boxes with no
matched pose fall back to the box bottom-centre.
"""

# COCO keypoint indices
L_SHO, R_SHO, L_HIP, R_HIP, L_ANK, R_ANK = 5, 6, 11, 12, 15, 16


def _iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
    return inter/ua if ua > 0 else 0.0


class Person:
    __slots__ = ("x1", "y1", "x2", "y2", "conf", "kps", "foot_px", "anchor_kind")

    def __init__(self, x1, y1, x2, y2, conf, kps, kp_conf_min):
        self.x1, self.y1, self.x2, self.y2 = float(x1), float(y1), float(x2), float(y2)
        self.conf = float(conf)
        self.kps = kps                       # (17,3) [x,y,conf] or None
        self.foot_px, self.anchor_kind = self._anchor(kp_conf_min)

    @property
    def box_h(self):
        return self.y2 - self.y1

    def _mid(self, a, b, kmin):
        k = self.kps
        if k is not None and k[a, 2] >= kmin and k[b, 2] >= kmin:
            return ((k[a, 0] + k[b, 0]) / 2.0, (k[a, 1] + k[b, 1]) / 2.0)
        return None

    def _anchor(self, kmin):
        """Floor anchor pixel. Hip midpoint preferred (works seated); then
        shoulders; else box bottom-centre. Horizontal position is what matters
        since distance removes the vertical component."""
        hip = self._mid(L_HIP, R_HIP, kmin)
        if hip is not None:
            return hip, "hip"
        sho = self._mid(L_SHO, R_SHO, kmin)
        if sho is not None:
            return sho, "shoulder"
        return ((self.x1 + self.x2) / 2.0, self.y2), "bbox"


class PersonDetector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.model = YOLO(cfg.yolo_weights)
        self.model.to(cfg.device)
        self.pose = YOLO(cfg.pose_weights)
        self.pose.to(cfg.device)

    def detect(self, frame_bgr):
        h = frame_bgr.shape[0]
        c = self.cfg
        det = self.model.predict(frame_bgr, conf=c.det_conf, imgsz=c.det_imgsz,
                                 classes=[c.person_class_id], device=c.device,
                                 verbose=False, half=True)[0]
        if det.boxes is None or len(det.boxes) == 0:
            return []
        dboxes = det.boxes.xyxy.cpu().numpy()
        dconf = det.boxes.conf.cpu().numpy()

        # stage 2: pose skeletons on the same frame, matched to detector boxes
        pose = self.pose.predict(frame_bgr, conf=max(0.15, c.det_conf - 0.15),
                                 imgsz=c.det_imgsz, classes=[c.person_class_id],
                                 device=c.device, verbose=False, half=True)[0]
        pboxes = pose.boxes.xyxy.cpu().numpy() if pose.boxes is not None else np.empty((0, 4))
        pkps = pose.keypoints.data.cpu().numpy() if (pose.keypoints is not None and len(pboxes)) else []

        people = []
        for (x1, y1, x2, y2), cf in zip(dboxes, dconf):
            # attach best-overlapping pose skeleton
            kps, best = None, c.pose_match_iou
            for pb, pk in zip(pboxes, pkps):
                v = _iou((x1, y1, x2, y2), pb)
                if v >= best:
                    best, kps = v, pk
            p = Person(x1, y1, x2, y2, cf, kps, c.kp_conf_min)
            if p.box_h < c.min_box_h_frac * h:
                continue
            if c.drop_bottom_truncated and y2 >= h - 2 and p.anchor_kind == "bbox":
                continue
            people.append(p)
        return people


# ==============================================================================
# ==== proximity/interaction.py ====
# ==============================================================================
"""Interaction cue (Fix 2b) — the real cheating signal in a fixed seat grid.

Two students sitting in adjacent cubicles are always "close"; what distinguishes
cheating is one LEANING or TURNING toward the other. We read that from pose:
horizontal offset of the shoulders relative to the hips (sideways lean) and of
the nose relative to the shoulders (head turn), checking it points toward the
neighbour, plus HEAD GAZE toward the neighbour (the strongest cheating cue).
"""
NOSE, L_SHO, R_SHO, L_HIP, R_HIP = 0, 5, 6, 11, 12


def _mid(kps, a, b, kmin):
    if kps is None:
        return None
    if kps[a, 2] >= kmin and kps[b, 2] >= kmin:
        return ((kps[a, 0] + kps[b, 0]) / 2.0, (kps[a, 1] + kps[b, 1]) / 2.0)
    return None


def lean_offsets(kps, kmin):
    """Return (lean_dx, turn_dx) in pixels, or None.

    lean_dx = shoulders_x - hips_x   (sideways torso lean)
    turn_dx = nose_x - shoulders_x   (head turn)
    Positive = toward image-right.
    """
    sho = _mid(kps, L_SHO, R_SHO, kmin)
    hip = _mid(kps, L_HIP, R_HIP, kmin)
    if sho is None or hip is None:
        return None
    lean_dx = sho[0] - hip[0]
    turn_dx = (kps[NOSE, 0] - sho[0]) if kps[NOSE, 2] >= kmin else 0.0
    return lean_dx, turn_dx


def _shoulder_w(kps, box, kmin):
    """Shoulder width in px (scale-free ruler); box-width fallback, matching detector._body_size."""
    if kps is not None and kps[L_SHO, 2] >= kmin and kps[R_SHO, 2] >= kmin:
        w = abs(kps[L_SHO, 0] - kps[R_SHO, 0])
        if w > 4:
            return float(w)
    return max(1.0, (box[2] - box[0]) * 0.6)


def _lean_toward(box, kps, other_box, kmin):
    """Signed lean+turn of this person TOWARD the other, in shoulder-widths.

    = (torso_lean + head_turn) projected onto the direction of the neighbour, where
    torso_lean = (shoulders-hips)_x and head_turn = (nose-shoulders)_x, both / shoulder-width.
    Returns None when hips/shoulders aren't both visible (can't judge lean). Validated as the
    best single interaction cue on labelled exam data (Phase 7, AUC 0.70; ear/gaze proxy useless).
    """
    off = lean_offsets(kps, kmin)
    if off is None:
        return None
    lean_dx, turn_dx = off
    sw = _shoulder_w(kps, box, kmin)
    direction = 1.0 if (other_box[0] + other_box[2]) >= (box[0] + box[2]) else -1.0
    return ((lean_dx + turn_dx) * direction) / sw


def pair_lean_max(box_a, kps_a, box_b, kps_b, kmin):
    """Max lean-toward score over the two people (shoulder-widths); -inf if neither has pose."""
    scores = [s for s in (_lean_toward(box_a, kps_a, box_b, kmin),
                          _lean_toward(box_b, kps_b, box_a, kmin)) if s is not None]
    return max(scores) if scores else float("-inf")


def interacting(box_a, kps_a, box_b, kps_b, kmin, lean_frac=0.18, turn_frac=0.30,
                gaze_frac=0.15):
    """True if either person leans, turns, or looks toward the other.

    box_* = (x1,y1,x2,y2); kps_* = (17,3) keypoints or None.
    lean/turn thresholds are fractions of box width; gaze is from head yaw.
    """
    def cx(b):
        return (b[0] + b[2]) / 2.0

    def toward(box, kps, other):
        off = lean_offsets(kps, kmin)
        if off is None:
            return False
        lean_dx, turn_dx = off
        w = max(1.0, box[2] - box[0])
        direction = 1.0 if cx(other) >= cx(box) else -1.0
        leans = (lean_dx * direction) > lean_frac * w
        turns = (turn_dx * direction) > turn_frac * w
        return leans or turns

    lean_or_turn = toward(box_a, kps_a, box_b) or toward(box_b, kps_b, box_a)
    gaze = mutual_or_single_gaze(box_a, kps_a, box_b, kps_b, kmin, gaze_frac)
    return lean_or_turn or gaze


# ==============================================================================
# ==== proximity/autocalib.py ====
# ==============================================================================
"""Automatic per-camera calibration from pedestrians (people-as-rulers).

No manual input: standing people (visible head + feet) are vertical segments of
~constant real height. From many of them we recover the camera's ground model
(horizon, focal, height) and then place ANY person on the floor in real metres —
including seated students, from their head point (feet hidden under the desk).

Assumptions (typical CCTV): ~zero roll (horizontal horizon), principal point at
image centre, square pixels. Accumulate samples over video for a stable fit.
"""

ADULT_H = 1.70          # standing head-top height above floor (m)
SEATED_HEAD_H = 1.25    # seated head-top height above floor (m)
SHOULDER_W = 0.40       # shoulder width (m) — a horizontal ruler visible from behind
SEATED_SHO_H = 1.15     # seated shoulder height above floor (m)
L_SHO, R_SHO = 5, 6


def _line_through(p, q):
    """Homogeneous line through two image points."""
    return np.cross([p[0], p[1], 1.0], [q[0], q[1], 1.0])


def _ransac_line(x, y, iters=200, thresh=12.0):
    """Robust y = a*x + b via RANSAC. Returns (a, b) or (None, None)."""
    x = np.asarray(x, float); y = np.asarray(y, float)
    n = len(x)
    if n < 2:
        return None, None
    rng = np.random.default_rng(0)
    best_in, best = None, (None, None)
    for _ in range(iters):
        i, j = rng.choice(n, 2, replace=False)
        if x[i] == x[j]:
            continue
        a = (y[j] - y[i]) / (x[j] - x[i]); b = y[i] - a * x[i]
        inl = np.abs(y - (a * x + b)) < thresh
        if best_in is None or inl.sum() > best_in.sum():
            best_in, best = inl, (a, b)
    if best_in is None or best_in.sum() < max(4, 0.3 * n):
        return np.polyfit(x, y, 1)           # fall back to plain fit
    a, b = np.polyfit(x[best_in], y[best_in], 1)   # refit on inliers
    return a, b


def estimate_vvp(segments):
    """Vertical vanishing point = least-squares intersection of head->foot lines.
    segments: list of ((ux,foot_y),(ux,head_y)) i.e. (foot_pt, head_pt)."""
    A = []
    for foot, head in segments:
        A.append(_line_through(foot, head))
    A = np.array(A)
    # null space of A (point x with A x = 0)
    _, _, vt = np.linalg.svd(A)
    v = vt[-1]
    if abs(v[2]) < 1e-9:
        return None
    return np.array([v[0] / v[2], v[1] / v[2]])


class AutoCalibration:
    def __init__(self, v_horizon, focal, cam_height, cx, cy):
        self.v_h = float(v_horizon)
        self.f = float(focal)
        self.Hc = float(cam_height)      # camera height above floor (m)
        self.cx = float(cx)
        self.cy = float(cy)

    @classmethod
    def from_pedestrians(cls, samples, img_w, img_h):
        """samples: list of (foot_pt, head_pt) image points for STANDING people.
        Returns AutoCalibration or None if the fit is unreliable."""
        if len(samples) < 4:
            return None
        foot_y = np.array([s[0][1] for s in samples], float)
        px_h = np.array([s[0][1] - s[1][1] for s in samples], float)   # pixel height
        ok = px_h > 5
        foot_y, px_h = foot_y[ok], px_h[ok]
        if len(foot_y) < 4:
            return None
        # robust line px_h = a*foot_y + b (RANSAC: reject bending/walking outliers)
        a, b = _ransac_line(foot_y, px_h)
        if a is None or a <= 1e-4:            # must increase with foot_y (closer = bigger)
            return None
        v_h = -b / a                          # horizon row
        Hc = ADULT_H / a                      # a = ADULT_H/Hc
        if not (np.isfinite(Hc) and 1.5 < Hc < 8.0 and np.isfinite(v_h)):
            return None
        cx, cy = img_w / 2.0, img_h / 2.0
        # focal from the vertical vanishing point; fall back to ~60deg HFOV if weak
        vvp = estimate_vvp(samples)
        f = 0.9 * img_w
        if vvp is not None:
            val = -(vvp[1] - cy) * (v_h - cy)
            if val > 1:
                f = float(np.sqrt(val))
        return cls(v_h, f, abs(Hc), cx, cy)

    @classmethod
    def from_shoulders(cls, samples, img_w, img_h, shoulder_h=SEATED_SHO_H):
        """Calibrate from shoulder widths (works for SEATED students, even from
        behind). samples: list of (shoulder_y, shoulder_pixel_width).

        A fixed-length horizontal segment's pixel width is linear in (v - horizon):
        width = SHOULDER_W/(Hc - shoulder_h) * (v - v_h). Regress to get v_h and Hc.
        """
        if len(samples) < 8:
            return None
        vy = np.array([s[0] for s in samples], float)
        w = np.array([s[1] for s in samples], float)
        ok = w > 3
        vy, w = vy[ok], w[ok]
        if len(vy) < 8:
            return None
        a, b = _ransac_line(vy, w, thresh=6.0)
        if a is None or a <= 1e-4:
            return None
        v_h = -b / a
        Hc = SHOULDER_W / a + shoulder_h       # a = SHOULDER_W/(Hc - shoulder_h)
        cx, cy = img_w / 2.0, img_h / 2.0
        if not (np.isfinite(Hc) and 1.8 < Hc < 6.0 and np.isfinite(v_h)):
            return None
        return cls(v_h, 0.9 * img_w, abs(Hc), cx, cy)   # default focal (HFOV ~60deg)

    def floor_from_foot(self, u, v_foot):
        """Ground point (X lateral, Z depth) in metres from a foot pixel."""
        dv = v_foot - self.v_h
        if dv <= 1e-3:
            return None
        Z = self.f * self.Hc / dv
        X = (u - self.cx) * Z / self.f
        return np.array([X, Z])

    def floor_from_head(self, u, v_head, head_height=SEATED_HEAD_H):
        """Ground point from a head pixel + assumed head height (for seated people
        whose feet are occluded). Solves depth from the head's height above floor."""
        dv = v_head - self.v_h
        if dv <= 1e-3:
            return None
        Z = self.f * (self.Hc - head_height) / dv
        if Z <= 0:
            return None
        X = (u - self.cx) * Z / self.f
        return np.array([X, Z])

    @staticmethod
    def distance(a, b):
        return float(np.hypot(a[0] - b[0], a[1] - b[1]))


L_ANK, R_ANK, NOSE = 15, 16, 0


def standing_samples(people, kp_conf_min=0.5):
    """(foot, head) pixel pairs for STANDING, full-body people (calibration input).

    Foot uses the ankle x at the box bottom; head uses the nose x at the box top,
    so the segment carries the true perspective tilt (needed for the vanishing point)
    while spanning the full person height.
    """
    out = []
    for p in people:
        k = p.kps
        if k is None:
            continue
        if (k[L_ANK, 2] >= kp_conf_min and k[R_ANK, 2] >= kp_conf_min
                and k[NOSE, 2] >= 0.3 and (p.y2 - p.y1) / max(1.0, p.x2 - p.x1) > 1.5):
            ank_x = (k[L_ANK, 0] + k[R_ANK, 0]) / 2.0
            out.append(((ank_x, p.y2), (k[NOSE, 0], p.y1)))
    return out


def shoulder_samples(people, kp_conf_min=0.5):
    """(shoulder_y, shoulder_pixel_width) for people with both shoulders visible."""
    out = []
    for p in people:
        k = p.kps
        if k is None:
            continue
        if k[L_SHO, 2] >= kp_conf_min and k[R_SHO, 2] >= kp_conf_min:
            vy = (k[L_SHO, 1] + k[R_SHO, 1]) / 2.0
            w = abs(k[L_SHO, 0] - k[R_SHO, 0])
            if w > 3:
                out.append((vy, w))
    return out


def person_floor(cal, p, kp_conf_min=0.5):
    """Floor position (X, Z) metres.

    Priority of body reference (depth comes from the image row via the ground model):
    1. ankles visible  -> feet on floor (standing)
    2. both shoulders   -> shoulder midpoint at seated shoulder height (visible even
                           from BEHIND — the exam case where the face is hidden)
    3. nose             -> head at seated head height (facing the camera)
    4. box bottom       -> last resort
    """
    k = p.kps
    cx = (p.x1 + p.x2) / 2.0
    if k is not None and k[L_ANK, 2] >= kp_conf_min and k[R_ANK, 2] >= kp_conf_min:
        ax = (k[L_ANK, 0] + k[R_ANK, 0]) / 2.0
        return cal.floor_from_foot(ax, p.y2)
    if k is not None and k[L_SHO, 2] >= kp_conf_min and k[R_SHO, 2] >= kp_conf_min:
        sx = (k[L_SHO, 0] + k[R_SHO, 0]) / 2.0
        sy = (k[L_SHO, 1] + k[R_SHO, 1]) / 2.0
        return cal.floor_from_head(sx, sy, SEATED_SHO_H)
    if k is not None and k[NOSE, 2] >= 0.3:
        return cal.floor_from_head(k[NOSE, 0], k[NOSE, 1], SEATED_HEAD_H)
    return cal.floor_from_foot(cx, p.y2)


# ==============================================================================
# ==== proximity/geometry.py ====
# ==============================================================================
"""3D geometry from a single frame via MoGe-2 (metric).

Gives each person a real-world 3D floor-contact point, fits the floor plane
from those points, and measures distance IN METRES on the floor plane.
No pixel distances are ever used.
"""


class Scene3D:
    def __init__(self, cfg):
        # MoGe is imported lazily so the package (and the calibration-free relative mode)
        # can be used without the heavy MoGe dependency installed. MoGe is only needed
        # when the default metric-depth mode is actually selected.
        from moge.model.v2 import MoGeModel
        self.cfg = cfg
        self.device = torch.device(cfg.device if torch.cuda.is_available() else "cpu")
        self.model = MoGeModel.from_pretrained(cfg.moge_model).to(self.device).eval()

    @torch.no_grad()
    def infer(self, frame_bgr):
        """Run MoGe on a (possibly downscaled) frame.

        Returns (points, scale) where points is (h, w, 3) metric camera-space
        coordinates with NaN at invalid pixels, and scale maps original-image
        pixel coords -> points-array coords.
        """
        H, W = frame_bgr.shape[:2]
        s = self.cfg.moge_max_size / max(H, W)
        s = min(s, 1.0)
        rw, rh = int(round(W * s)), int(round(H * s))
        img = cv2.resize(frame_bgr, (rw, rh), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        t = torch.from_numpy(rgb).float().permute(2, 0, 1).div(255.0).to(self.device)
        out = self.model.infer(t, use_fp16=True)
        pts = out["points"].float().cpu().numpy()      # (rh, rw, 3)
        return pts, (rw / W, rh / H)

    def point_at(self, points, scale, x_orig, y_orig):
        """Median valid 3D point in a small patch around a floor pixel."""
        sx, sy = scale
        h, w = points.shape[:2]
        px = int(round(x_orig * sx))
        py = int(round(y_orig * sy))
        r = self.cfg.foot_patch
        x0, x1 = max(0, px - r), min(w, px + r + 1)
        y0, y1 = max(0, py - r), min(h, py + r + 1)
        if x0 >= x1 or y0 >= y1:
            return None
        patch = points[y0:y1, x0:x1, :].reshape(-1, 3)
        good = patch[np.isfinite(patch).all(axis=1)]
        if good.shape[0] < 3:
            return None
        p = np.median(good, axis=0)
        depth = abs(p[2])
        if not np.isfinite(depth) or depth <= 0 or depth > self.cfg.max_depth_m:
            return None
        return p

    def fit_floor_plane(self, foot_pts):
        """RANSAC plane (normal, d) from a set of 3D foot points.

        People stand on the floor, so their foot points approximately lie on the
        floor plane. Returns (normal_unit, d, inlier_mask) or None.
        """
        P = np.asarray(foot_pts, dtype=np.float64)
        n = len(P)
        if n < self.cfg.min_floor_inliers:
            return None
        best_inl, best_plane = None, None
        rng = np.random.default_rng(0)
        thr = self.cfg.ransac_thresh_m
        for _ in range(self.cfg.ransac_iters):
            idx = rng.choice(n, 3, replace=False)
            a, b, c = P[idx]
            nrm = np.cross(b - a, c - a)
            ln = np.linalg.norm(nrm)
            if ln < 1e-6:
                continue
            nrm = nrm / ln
            d = -nrm.dot(a)
            dist = np.abs(P.dot(nrm) + d)
            inl = dist < thr
            if best_inl is None or inl.sum() > best_inl.sum():
                best_inl, best_plane = inl, (nrm, d)
        if best_inl is None or best_inl.sum() < self.cfg.min_floor_inliers:
            return None
        # refit on inliers (least squares)
        Q = P[best_inl]
        centroid = Q.mean(axis=0)
        _, _, vt = np.linalg.svd(Q - centroid)
        nrm = vt[-1]
        nrm = nrm / np.linalg.norm(nrm)
        d = -nrm.dot(centroid)
        return nrm, d, best_inl

    @staticmethod
    def project_to_plane(p, plane):
        nrm, d = plane[0], plane[1]
        return p - (p.dot(nrm) + d) * nrm

    def floor_distance(self, p, q, plane):
        """Horizontal (on-floor) metric distance between two anchor points.

        We remove the component along the floor normal, so the anchors' height
        above the floor (hip vs feet, seated vs standing) does not matter — only
        their position on the floor. Falls back to 3D distance with no plane.
        """
        diff = p - q
        if plane is not None:
            n = plane[0]
            diff = diff - diff.dot(n) * n      # drop vertical component
        return float(np.linalg.norm(diff))


# ==============================================================================
# ==== proximity/analyzer.py ====
# ==============================================================================
"""Tracking + temporal violation confirmation (the false-positive filter).

Works entirely in metric floor coordinates. A pair/gathering is only reported
after it persists for `persist_frames` consecutive analysed frames, and is held
until it is clean for `release_frames` frames.
"""


def _iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


class Track:
    __slots__ = ("id", "pos", "box", "conf", "kps", "misses",
                 "sx", "sz", "sxx", "szz", "n_seen", "n_stand")

    def __init__(self, tid, pos, box, conf, kps=None, standing=False):
        self.id = tid
        self.pos = pos        # 3D floor point (metres)
        self.box = box        # (x1,y1,x2,y2) image px, for drawing
        self.conf = conf
        self.kps = kps        # pose keypoints (17,3) or None
        self.misses = 0
        # posture + noise-robust mobility (std of floor position) over the track's life
        x, z = float(pos[0]), float(pos[2])
        self.sx = x; self.sz = z; self.sxx = x * x; self.szz = z * z
        self.n_seen = 1; self.n_stand = 1 if standing else 0

    def _observe(self, pos, standing):
        x, z = float(pos[0]), float(pos[2])
        self.sx += x; self.sz += z; self.sxx += x * x; self.szz += z * z
        self.n_seen += 1
        if standing:
            self.n_stand += 1

    @property
    def mobility(self):
        """Std-dev of floor position (robust to per-frame jitter)."""
        n = self.n_seen
        vx = max(0.0, self.sxx / n - (self.sx / n) ** 2)
        vz = max(0.0, self.szz / n - (self.sz / n) ** 2)
        return float(np.sqrt(vx + vz))

    @property
    def standing_frac(self):
        return self.n_stand / max(1, self.n_seen)

    def is_invigilator(self, cfg):
        return (self.standing_frac >= cfg.invig_standing_frac or
                self.mobility > cfg.invig_mobility_std)


class Analyzer:
    def __init__(self, cfg, dist_fn):
        self.cfg = cfg
        self.dist = dist_fn          # dist_fn(p, q) -> metres on floor
        self.tracks = {}
        self._next = 0
        self._pair = {}              # frozenset(id,id) -> {"hits":int,"miss":int,"active":bool}
        self._group = {}             # tuple(sorted ids) -> same dict

    # ---------- tracking ----------
    def _assign(self, cand, dets, dj, cost_fn, gate_ok):
        """Hungarian match candidate track ids to detection indices in dj."""
        if not cand or not dj:
            return []
        C = np.full((len(cand), len(dj)), 1e6)
        for i, tid in enumerate(cand):
            for jj, j in enumerate(dj):
                c = cost_fn(self.tracks[tid], dets[j])
                if gate_ok(c):
                    C[i, jj] = c
        ri, cj = linear_sum_assignment(C)
        out = []
        for i, jj in zip(ri, cj):
            if C[i, jj] < 1e6:
                out.append((cand[i], dj[jj]))
        return out

    def update_tracks(self, dets):
        """dets: list of (pos3d, box, conf). Returns Tracks live this frame.

        Stage 1 associates by box IoU (stable at this fps); stage 2 re-associates
        leftovers by metric floor distance. Lost tracks are coasted for re-id.
        """
        cand = [tid for tid, t in self.tracks.items() if t.misses <= self.cfg.track_max_age]
        matched_t, matched_d = set(), set()

        def _kps(d):
            return d[3] if len(d) > 3 else None

        def _stand(d):
            return bool(d[4]) if len(d) > 4 else False

        def commit(tid, j):
            t = self.tracks[tid]
            t.pos, t.box, t.conf, t.misses = dets[j][0], dets[j][1], dets[j][2], 0
            t.kps = _kps(dets[j])
            t._observe(dets[j][0], _stand(dets[j]))
            matched_t.add(tid)
            matched_d.add(j)

        # stage 1: IoU
        dj = list(range(len(dets)))
        for tid, j in self._assign(
                cand, dets, dj,
                cost_fn=lambda t, d: 1.0 - _iou(t.box, d[1]),
                gate_ok=lambda c: c <= 1.0 - self.cfg.track_iou_min):
            commit(tid, j)
        # stage 2: metric distance on leftovers
        cand2 = [tid for tid in cand if tid not in matched_t]
        dj2 = [j for j in dj if j not in matched_d]
        for tid, j in self._assign(
                cand2, dets, dj2,
                cost_fn=lambda t, d: self.dist(t.pos, d[0]),
                gate_ok=lambda c: c <= self.cfg.track_gate_m):
            commit(tid, j)

        live_ids = set(matched_t)
        for j in range(len(dets)):          # new tracks
            if j in matched_d:
                continue
            pos, box, conf = dets[j][0], dets[j][1], dets[j][2]
            self.tracks[self._next] = Track(self._next, pos, box, conf,
                                            _kps(dets[j]), _stand(dets[j]))
            live_ids.add(self._next)
            self._next += 1
        for tid in list(self.tracks):        # age / evict
            if tid not in matched_t and tid not in live_ids:
                self.tracks[tid].misses += 1
                if self.tracks[tid].misses > self.cfg.track_max_age:
                    del self.tracks[tid]
        return [self.tracks[tid] for tid in live_ids if tid in self.tracks]

    # ---------- violation confirmation ----------
    def _step(self, store, key, violating):
        st = store.get(key)
        if st is None:
            st = {"hits": 0, "miss": 0, "active": False}
            store[key] = st
        if violating:
            st["hits"] += 1
            st["miss"] = 0
            if st["hits"] >= self.cfg.persist_frames:
                st["active"] = True
        else:
            st["miss"] += 1
            if st["miss"] >= self.cfg.release_frames:
                st["hits"] = 0
                st["active"] = False
        return st["active"]

    def analyze(self, live):
        """Given live tracks, return confirmed violations for this frame."""
        pairs, groups = [], []

        # pairwise proximity
        active_now = set()
        for a, b in combinations(live, 2):
            dm = self.dist(a.pos, b.pos)
            key = frozenset((a.id, b.id))
            active_now.add(key)
            if self._step(self._pair, key, dm < self.cfg.proximity_m):
                pairs.append((a, b, dm))
        for key in list(self._pair):            # decay pairs not present this frame
            if key not in active_now:
                if self._step(self._pair, key, False) is False and self._pair[key]["hits"] == 0:
                    del self._pair[key]

        # gatherings: connected components over edges < gather_m
        adj = {t.id: set() for t in live}
        by_id = {t.id: t for t in live}
        for a, b in combinations(live, 2):
            if self.dist(a.pos, b.pos) < self.cfg.gather_m:
                adj[a.id].add(b.id)
                adj[b.id].add(a.id)
        comps, seen = [], set()
        for sid in adj:
            if sid in seen:
                continue
            stack, comp = [sid], []
            while stack:
                u = stack.pop()
                if u in seen:
                    continue
                seen.add(u)
                comp.append(u)
                stack.extend(adj[u] - seen)
            if len(comp) >= self.cfg.gather_min:
                comps.append(tuple(sorted(comp)))
        active_g = set(comps)
        for key in comps:
            if self._step(self._group, key, True):
                groups.append([by_id[i] for i in key if i in by_id])
        for key in list(self._group):
            if key not in active_g:
                if self._step(self._group, key, False) is False and self._group[key]["hits"] == 0:
                    del self._group[key]
        return pairs, groups


# ==============================================================================
# ==== proximity/pipeline.py ====
# ==============================================================================
"""End-to-end proximity-violation pipeline for one video."""



def _foot_px(box):
    x1, y1, x2, y2 = box
    return (int((x1 + x2) / 2), int(y2))


def _standing(p, kmin):
    """Ankles/feet visible => standing (seated students' legs are under the desk)."""
    k = p.kps
    return k is not None and k[15, 2] >= kmin and k[16, 2] >= kmin


def _head_pt(p, kmin):
    """Head location: nose if visible, else top of the box (works from behind)."""
    k = p.kps
    if k is not None and k[0, 2] >= 0.3:
        return (float(k[0, 0]), float(k[0, 1]))
    return ((p.x1 + p.x2) / 2.0, p.y1 + (p.y2 - p.y1) * 0.15)


def _body_size(p, kmin):
    """Shoulder width in px (a ~constant real ruler, visible from behind)."""
    k = p.kps
    if k is not None and k[5, 2] >= kmin and k[6, 2] >= kmin:
        w = abs(k[5, 0] - k[6, 0])
        if w > 4:
            return float(w)
    return (p.x2 - p.x1) * 0.6


def _dedup_people(people, iou_thr):
    """Remove duplicate detections of the same person (heavily overlapping boxes),
    keeping the higher-confidence one."""
    kept = []
    for p in sorted(people, key=lambda q: -q.conf):
        box = (p.x1, p.y1, p.x2, p.y2)
        if all(_iou(box, (k.x1, k.y1, k.x2, k.y2)) < iou_thr for k in kept):
            kept.append(p)
    return kept


def run_video(video_path, cfg: Config, out_dir, detector=None, scene=None):
    os.makedirs(out_dir, exist_ok=True)
    name = os.path.splitext(os.path.basename(video_path))[0]

    detector = detector or PersonDetector(cfg)
    use_close = cfg.closeness_mode
    use_rel = cfg.relative_proximity or use_close   # both use the head/body-size representation
    use_homog = cfg.homography is not None and not use_rel
    use_autocal = cfg.autocalib is not None and not use_rel
    use_hnorm = cfg.height_norm and not (use_homog or use_autocal or use_rel)
    use_metric2d = use_homog or use_autocal     # positions already in world metres
    H = np.array(cfg.homography, dtype=float) if use_homog else None
    scene = scene or (None if (use_metric2d or use_hnorm or use_rel) else Scene3D(cfg))

    state = {"plane": None}

    def _dist(p, q):
        if use_close:                        # physical-closeness: same depth + same row + near
            sr = max(p[2], q[2]) / max(1e-6, min(p[2], q[2]))
            if sr > cfg.close_size_ratio_max:        # different apparent size => different depth (row)
                return 1e6
            dx = abs(p[0] - q[0]); dy = abs(p[1] - q[1])
            if dy > cfg.close_vert_ratio_max * dx:   # heads stacked vertically => front/behind
                return 1e6
            nd = float(np.hypot(dx, dy)) / max(1.0, (p[2] + q[2]) / 2.0)
            return nd if nd <= cfg.close_nd else 1e6
        if use_rel:                         # p = [head_x, head_y, body_size_px]
            sr = max(p[2], q[2]) / max(1e-6, min(p[2], q[2]))
            if sr > cfg.rel_size_ratio_max:  # different depth (row) / standing-vs-seated
                return 1e6
            dx = abs(p[0] - q[0]); dy = abs(p[1] - q[1])
            if dy > cfg.rel_vert_ratio * dx:  # heads stacked vertically = different rows
                return 1e6
            return float(np.hypot(dx, dy)) / max(1.0, (p[2] + q[2]) / 2.0)   # shoulder-widths
        if use_hnorm:                       # p = [foot_x, foot_y, pixel_height]
            dpx = float(np.hypot(p[0] - q[0], p[1] - q[1]))
            s = (1.70 / max(1.0, p[2]) + 1.70 / max(1.0, q[2])) / 2.0
            return dpx * s
        if use_metric2d:                    # positions are world metres [X,Y,Z]
            # row-aware: different rows (depth) can't interact -> treat as far
            if cfg.row_aware and abs(p[2] - q[2]) >= cfg.same_row_dz:
                return 1e6
            return float(np.linalg.norm(p - q))
        return scene.floor_distance(p, q, state["plane"])
    analyzer = Analyzer(cfg, dist_fn=_dist)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    step = max(1, int(round(fps / cfg.sample_fps)))

    writer = None
    if cfg.draw:
        out_vid = os.path.join(out_dir, f"{name}_proximity.mp4")
        writer = cv2.VideoWriter(out_vid, cv2.VideoWriter_fourcc(*"mp4v"),
                                 cfg.sample_fps, (W, H))

    findings = []
    confirmed_pairs = {}     # frozenset(ids) -> {first,last,min_dist}
    confirmed_groups = {}    # tuple(ids) -> {first,last,size}
    track_stats = {}         # tid -> (mobility, standing_frac) over the whole clip
    fi = 0
    t0 = time.time()
    processed = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if fi % step != 0:
            fi += 1
            continue
        t_s = round(fi / fps, 2)

        people = _dedup_people(detector.detect(frame), cfg.dedup_iou)
        dets = []
        if use_rel:
            # calibration-free: position carries [head_x, head_y, body_size_px]
            for p in people:
                hx, hy = _head_pt(p, cfg.kp_conf_min)
                dets.append((np.array([hx, hy, _body_size(p, cfg.kp_conf_min)]),
                             (p.x1, p.y1, p.x2, p.y2), p.conf, p.kps, _standing(p, cfg.kp_conf_min)))
            state["plane"] = None
        elif use_homog:
            # calibrated camera: floor-contact pixel (box bottom) -> real metres
            for p in people:
                wx, wy = pixel_to_world(H, (p.x1 + p.x2) / 2.0, p.y2)
                dets.append((np.array([wx, wy, 0.0]), (p.x1, p.y1, p.x2, p.y2), p.conf, p.kps, _standing(p, cfg.kp_conf_min)))
            state["plane"] = None
        elif use_autocal:
            # auto-calibrated: feet for standing, head for seated -> real metres
            for p in people:
                fp = person_floor(cfg.autocalib, p, cfg.kp_conf_min)
                if fp is None:
                    continue
                dets.append((np.array([fp[0], 0.0, fp[1]]), (p.x1, p.y1, p.x2, p.y2), p.conf, p.kps, _standing(p, cfg.kp_conf_min)))
            state["plane"] = None
        elif use_hnorm:
            # calibration-free: position carries [foot_x, foot_y, pixel_height]
            for p in people:
                dets.append((np.array([(p.x1 + p.x2) / 2.0, p.y2, p.y2 - p.y1]),
                             (p.x1, p.y1, p.x2, p.y2), p.conf, p.kps, _standing(p, cfg.kp_conf_min)))
            state["plane"] = None
        else:
            pts, scale = scene.infer(frame)
            hip_pts = []          # consistent-height anchors -> clean floor normal
            for p in people:
                p3 = scene.point_at(pts, scale, p.foot_px[0], p.foot_px[1])
                if p3 is None:
                    continue
                box = (p.x1, p.y1, p.x2, p.y2)
                dets.append((p3, box, p.conf, p.kps, _standing(p, cfg.kp_conf_min)))
                if p.anchor_kind == "hip":
                    hip_pts.append(p3)
            state["plane"] = None
            if cfg.use_floor_plane and len(hip_pts) >= cfg.min_floor_inliers:
                fit = scene.fit_floor_plane(hip_pts)
                if fit is not None:
                    state["plane"] = (fit[0], fit[1])

        live = analyzer.update_tracks(dets)
        pairs, groups = analyzer.analyze(live)

        # accumulate per-track mobility/posture over the WHOLE clip (classify later,
        # so invigilators are excluded even for events near the clip start)
        if cfg.exclude_invigilators:
            for t in live:
                track_stats[t.id] = (t.mobility, t.standing_frac)

        # Fix 2: drop pairs separated by a cubicle divider
        if cfg.barriers:
            pairs = [(a, b, dm) for (a, b, dm) in pairs
                     if not blocked_by_divider(_foot_px(a.box), _foot_px(b.box), cfg.barriers)]
        # Fix 2b: in a seat grid, require lean/turn interaction to confirm a pair.
        # Relative mode uses the validated shoulder-width-normalised combined-lean score
        # (Phase 7); other modes use the original lean/turn/gaze OR-rule.
        if cfg.require_interaction:
            if use_rel:
                pairs = [(a, b, dm) for (a, b, dm) in pairs
                         if pair_lean_max(a.box, a.kps, b.box, b.kps, cfg.kp_conf_min)
                         >= cfg.lean_combo_min]
            else:
                pairs = [(a, b, dm) for (a, b, dm) in pairs
                         if interacting(a.box, a.kps, b.box, b.kps, cfg.kp_conf_min,
                                        cfg.lean_frac, cfg.turn_frac, cfg.gaze_frac)]

        # ---- record findings ----
        frame_rec = {"time_s": t_s, "frame": fi, "n_people": len(live),
                     "pairs": [], "gatherings": []}
        for a, b, dm in pairs:
            key = frozenset((a.id, b.id))
            rec = confirmed_pairs.setdefault(key, {"ids": sorted(key), "first_s": t_s,
                                                    "last_s": t_s, "min_dist_m": round(dm, 2)})
            rec["last_s"] = t_s
            rec["min_dist_m"] = min(rec["min_dist_m"], round(dm, 2))
            frame_rec["pairs"].append({"ids": sorted(key), "dist_m": round(dm, 2)})
        for g in groups:
            ids = tuple(sorted(t.id for t in g))
            rec = confirmed_groups.setdefault(ids, {"ids": list(ids), "first_s": t_s,
                                                    "last_s": t_s, "size": len(ids)})
            rec["last_s"] = t_s
            frame_rec["gatherings"].append({"ids": list(ids), "size": len(ids)})
        if frame_rec["pairs"] or frame_rec["gatherings"]:
            findings.append(frame_rec)

        if writer is not None:
            _annotate(frame, live, pairs, groups, t_s, use_homog or state["plane"] is not None,
                      red_only=cfg.draw_red_only, metric=not use_rel)
            writer.write(frame)

        processed += 1
        fi += 1

    cap.release()
    if writer is not None:
        writer.release()

    # classify invigilators from whole-clip stats, then exclude their events
    invig_ids = set()
    if cfg.exclude_invigilators:
        invig_ids = {tid for tid, (mob, sf) in track_stats.items()
                     if mob > cfg.invig_mobility_std or sf >= cfg.invig_standing_frac}
        # mobile people are close to MANY different others over the clip (students have 1-2 fixed neighbours)
        partners = {}
        for key in confirmed_pairs:
            a, b = tuple(key)
            partners.setdefault(a, set()).add(b); partners.setdefault(b, set()).add(a)
        for rec in confirmed_groups.values():
            ids = rec["ids"]
            for x in ids:
                partners.setdefault(x, set()).update(i for i in ids if i != x)
        invig_ids |= {tid for tid, ps in partners.items() if len(ps) > cfg.invig_max_partners}

    def _no_invig(ids):
        return not any(i in invig_ids for i in ids)

    summary = {
        "video": video_path,
        "frames_analyzed": processed,
        "duration_s": round(total / fps, 1) if total else None,
        "params": {"proximity_m": cfg.proximity_m, "gather_m": cfg.gather_m,
                   "gather_min": cfg.gather_min, "persist_frames": cfg.persist_frames,
                   "sample_fps": cfg.sample_fps},
        "confirmed_pair_violations": sorted(
            (r for r in confirmed_pairs.values()
             if r["last_s"] - r["first_s"] >= cfg.min_event_s and _no_invig(r["ids"])
             and (use_close or not use_rel or r["min_dist_m"] >= cfg.rel_min_thresh)),
            key=lambda r: r["first_s"]),
        "confirmed_gatherings": sorted(
            (r for r in confirmed_groups.values()
             if r["last_s"] - r["first_s"] >= cfg.min_event_s and _no_invig(r["ids"])),
            key=lambda r: r["first_s"]),
        "excluded_invigilator_tracks": sorted(invig_ids),
        "proc_seconds": round(time.time() - t0, 1),
    }
    out_json = os.path.join(out_dir, f"{name}_findings.json")
    with open(out_json, "w") as f:
        json.dump({"summary": summary, "per_frame": findings}, f, indent=2)
    return summary, out_json


def _annotate(frame, live, pairs, groups, t_s, have_plane, red_only=False, metric=True):
    viol_ids = set()
    for a, b, _ in pairs:
        viol_ids.update((a.id, b.id))
    group_ids = set()
    for g in groups:
        group_ids.update(t.id for t in g)

    # red_only: draw NOTHING on normal people; only the flagged (close/gathering) pairs in red.
    if not red_only:
        for t in live:
            x1, y1, x2, y2 = [int(v) for v in t.box]
            if t.id in group_ids:
                col = (0, 0, 255)
            elif t.id in viol_ids:
                col = (0, 140, 255)
            else:
                col = (0, 200, 0)
            cv2.rectangle(frame, (x1, y1), (x2, y2), col, 2)
            cv2.circle(frame, _foot_px(t.box), 4, col, -1)
            cv2.putText(frame, f"#{t.id}", (x1, max(0, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 2)
    else:
        # draw the two boxes of every flagged pair (and gathering members) in red
        flag = {t.id: t for t in live if t.id in viol_ids or t.id in group_ids}
        for t in flag.values():
            x1, y1, x2, y2 = [int(v) for v in t.box]
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 3)

    for a, b, dm in pairs:
        pa, pb = _foot_px(a.box), _foot_px(b.box)
        cv2.line(frame, pa, pb, (0, 0, 255), 2)
        mid = ((pa[0] + pb[0]) // 2, (pa[1] + pb[1]) // 2)
        label = f"{dm:.2f}m" if metric else f"close {dm:.1f}"
        cv2.putText(frame, label, mid, cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

    tag = "CLOSE" if not metric else "pairs"
    banner = f"t={t_s:.1f}s  people={len(live)}  {tag}={len(pairs)}  gatherings={len(groups)}"
    cv2.rectangle(frame, (0, 0), (frame.shape[1], 26), (0, 0, 0), -1)
    cv2.putText(frame, banner, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)


# ==============================================================================
# ==== CLI (run_proximity.py) ====
# ==============================================================================
#!/usr/bin/env python
"""CLI: detect real-metre proximity / gathering violations in CCTV video.

Examples:
    # validated calibration-free exam detector (recommended):
    python run_proximity.py --video clip.mp4 --relative --no-draw
    python run_proximity.py --dir ./clips --relative
"""



def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--video", help="single video file")
    g.add_argument("--dir", help="folder of .mp4 files")
    ap.add_argument("--out", default="output", help="output dir")
    ap.add_argument("--weights", help="override YOLO weights")
    ap.add_argument("--proximity", type=float, help="pair threshold (m)")
    ap.add_argument("--gather", type=float, help="gathering edge threshold (m)")
    ap.add_argument("--gather-min", type=int, help="min people for a gathering")
    ap.add_argument("--fps", type=float, help="sample fps")
    ap.add_argument("--persist", type=int, help="persistence frames")
    ap.add_argument("--no-draw", action="store_true", help="skip annotated video")
    ap.add_argument("--interaction", action="store_true",
                    help="require lean/turn/gaze toward neighbour to confirm a pair (exam mode)")
    ap.add_argument("--relative", action="store_true",
                    help="calibration-free head-proximity (heads close, normalized by body size); scales to any camera")
    ap.add_argument("--closeness", action="store_true",
                    help="flag two people who are ACTUALLY physically close (same row/depth), reject different-row; "
                         "student-only (drops standing invigilators), temporal-confirmed, red-only drawing. Any camera.")
    ap.add_argument("--autocalib", action="store_true",
                    help="auto-calibrate the camera from standing people in the video (no manual input)")
    ap.add_argument("--camera", help="camera key in camera_barriers.json "
                                     "(loads its homography + dividers)")
    args = ap.parse_args()

    cfg = Config()
    if args.camera:
        import json
        cams = json.load(open("camera_barriers.json"))
        c = cams[args.camera]
        if c.get("homography"):
            cfg.homography = c["homography"]
        if c.get("barriers"):
            cfg.barriers = c["barriers"]
        print(f"camera {args.camera}: homography={'yes' if cfg.homography else 'no'}, "
              f"dividers={len(cfg.barriers)}")
    if args.weights:
        cfg.yolo_weights = args.weights
    if args.proximity:
        cfg.proximity_m = args.proximity
    if args.gather:
        cfg.gather_m = args.gather
    if args.gather_min:
        cfg.gather_min = args.gather_min
    if args.fps:
        cfg.sample_fps = args.fps
    if args.persist:
        cfg.persist_frames = args.persist
    if args.no_draw:
        cfg.draw = False
    if args.interaction:
        cfg.require_interaction = True
    if args.relative:
        cfg.relative_proximity = True
        cfg.proximity_m = cfg.rel_thresh       # BAND UPPER (shoulder-widths); lower = rel_min_thresh
        cfg.gather_m = cfg.rel_gather_thresh
        cfg.row_aware = False                  # size-ratio + vertical-alignment gates replace depth check
        cfg.require_interaction = True         # validated detector = band + same-row gate + pose lean
        cfg.invig_mobility_std = 1e9           # disable pixel-noise mobility; rely on standing
        cfg.autocalib = None                   # no calibration needed
        args.autocalib = False
    if args.closeness:
        cfg.closeness_mode = True              # flag genuine physical closeness, reject different-row
        cfg.proximity_m = cfg.close_nd         # analyzer flags pairs the closeness _dist returns < this
        cfg.gather_m = cfg.close_nd            # gatherings = clusters of physically-close people
        cfg.row_aware = False
        cfg.require_interaction = False         # NO cheating/lean gate — pure closeness
        cfg.exclude_invigilators = True        # student-only: drop standing invigilators
        cfg.invig_mobility_std = 1e9           # positions are pixels here; rely on standing (legs visible) not pixel-mobility
        cfg.draw_red_only = True               # annotate ONLY flagged pairs in red
        cfg.min_event_s = 1.0                  # temporal: must stay close ~1 s (kills momentary pass-bys)
        cfg.persist_frames = 2
        cfg.autocalib = None
        args.autocalib = False

    videos = [args.video] if args.video else sorted(glob.glob(os.path.join(args.dir, "*.mp4")))
    if not videos:
        print("no videos found")
        return

    # load models once, reuse across videos
    detector = PersonDetector(cfg)

    if args.autocalib and not cfg.homography:
        import cv2
        from proximity.autocalib import AutoCalibration, standing_samples, shoulder_samples
        cap = cv2.VideoCapture(videos[0])
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        W, Hh = int(cap.get(3)), int(cap.get(4))
        step = max(1, int(fps / 2)); fi = 0; stand = []; shldr = []
        while True:
            ok, fr = cap.read()
            if not ok or (len(stand) >= 250 and len(shldr) >= 400):
                break
            if fi % step == 0:
                ppl = detector.detect(fr)
                stand += standing_samples(ppl, cfg.kp_conf_min)
                shldr += shoulder_samples(ppl, cfg.kp_conf_min)
            fi += 1
        cap.release()
        lo, hi = cfg.autocalib_hc_range
        cal = AutoCalibration.from_pedestrians(stand, W, Hh)
        if cal is not None and len(stand) >= cfg.autocalib_min_samples and lo <= cal.Hc <= hi:
            cfg.autocalib = cal
            print(f"auto-calibrated from {len(stand)} standing samples (people-as-ruler): "
                  f"cam_height {cal.Hc:.2f} m, focal {cal.f:.0f} px")
        else:
            cal2 = AutoCalibration.from_shoulders(shldr, W, Hh)
            if cal2 is not None and len(shldr) >= 40:
                cfg.autocalib = cal2
                print(f"auto-calibrated from {len(shldr)} shoulder-width samples (seated ruler): "
                      f"cam_height {cal2.Hc:.2f} m, horizon_row {cal2.v_h:.0f}")
            else:
                cfg.height_norm = True
                print(f"auto-calibration too weak (standing={len(stand)}, shoulders={len(shldr)}) "
                      f"-> calibration-free height-normalized distance")

    # Relative (calibration-free) mode needs no depth model; only the default metric
    # mode instantiates MoGe. Keeping this guarded means MoGe is never loaded for the
    # recommended exam detector.
    scene = (None if (cfg.relative_proximity or cfg.closeness_mode or cfg.homography
                      or cfg.autocalib or cfg.height_norm)
             else Scene3D(cfg))

    for v in videos:
        print(f"\n=== {os.path.basename(v)} ===")
        summary, out_json = run_video(v, cfg, args.out, detector=detector, scene=scene)
        print(f"  people-frames analyzed: {summary['frames_analyzed']} "
              f"in {summary['proc_seconds']}s")
        print(f"  confirmed pair violations: {len(summary['confirmed_pair_violations'])}")
        for r in summary["confirmed_pair_violations"]:
            print(f"    ids {r['ids']}  min {r['min_dist_m']}m  "
                  f"{r['first_s']}-{r['last_s']}s")
        print(f"  confirmed gatherings: {len(summary['confirmed_gatherings'])}")
        for r in summary["confirmed_gatherings"]:
            print(f"    ids {r['ids']}  size {r['size']}  {r['first_s']}-{r['last_s']}s")
        print(f"  -> {out_json}")


if __name__ == "__main__":
    main()

