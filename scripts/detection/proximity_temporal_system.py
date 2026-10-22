#!/usr/bin/env python3
"""
Proximity Temporal System — self-contained release build for production integration.

A single-file, dependency-free (no local imports) implementation of the complete temporal
proximity-violation detector. It consolidates, with identical logic and thresholds, what the
development tree spreads across ``Proximity_Temporal.py``, ``Proximity_Pipeline_V2.py`` and the
``proximity/`` package. Behaviour is unchanged — this is a packaging change, not an algorithm
change.

Pipeline (per camera, on an ordered frame stream):

    1.  DETECTION + TRACKING   YOLO11m detection with ByteTrack  -> stable per-person track ids
    2.  POSE                    YOLO11m-pose, matched by IoU       -> keypoints per track
    3.  MONOCULAR DEPTH         Depth-Anything-V2                  -> per-person relative depth
    4.  ROLE by MOBILITY        net travel over the track          -> invigilator (mobile) / student
    5.  SAME-ROW GATE CHAIN     size-ratio + depth + vertical      -> reject different-row / behind
    6.  LATERAL CLOSENESS       head distance / shoulder-width     -> actually near (not just same row)
    7.  SELF-CALIBRATION        learn the room's normal seat pitch -> threshold = 0.65 x pitch (tighten-only)
    8.  PERSISTENCE             sustained over time, not one frame -> kills pass-by false positives
    9.  CATEGORY + CROWD        student-student / invig-student /  -> invig lingers; crowd = >=3 cluster
                                invig-invig / crowd

Why temporal: a single frame tops out at ~0.6 AUC (geometry is camera/layout relative). Tracking
adds role-by-motion, sustained-closeness (the main false-positive killer), and per-room
self-calibration so no per-camera tuning is needed.

Usage:
    python proximity_temporal_system.py --video clip.mp4 --out out_dir --calibrate
    python proximity_temporal_system.py --image frame.jpg --out out_dir
    # or import:  from proximity_temporal_system import ProximityTemporalSystem

Integration notes for the production team:
    * It is STATEFUL per camera. Feed one camera's frames IN ORDER to one instance; keep the
      instance (and its ByteTrack state) alive across frames. Do not share one instance between
      cameras. Production already provides a continuous ~5 fps per-camera stream and a per-camera
      state dict, which is exactly what this needs.
    * Models: set PROX_DET_WEIGHTS / PROX_POSE_WEIGHTS / PROX_DEPTH_MODEL (env) or the constants
      below. Depth + pose add GPU cost per frame; budget for it at fleet scale.
    * Self-calibration: run ``calibrate()`` once after a bootstrap window (~10 min of frames);
      until then the safe fallback threshold (1.8) is used. It can only TIGHTEN (never looser
      than 1.8), so it reduces false positives in dense rooms and never adds them in spacious ones.

This script only READS model weights; it never writes to or depends on any production path.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from collections import defaultdict

import cv2
import numpy as np
import torch
from PIL import Image
from transformers import pipeline as hf_pipeline
from ultralytics import YOLO

# ======================================================================================
# Configuration — models
# ======================================================================================
_MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
DET_WEIGHTS = os.environ.get("PROX_DET_WEIGHTS", os.path.join(_MODELS_DIR, "yolo11m.pt"))
POSE_WEIGHTS = os.environ.get("PROX_POSE_WEIGHTS", os.path.join(_MODELS_DIR, "yolo11m-pose.pt"))
DEPTH_MODEL = os.environ.get("PROX_DEPTH_MODEL", "depth-anything/Depth-Anything-V2-Small-hf")

# ======================================================================================
# Configuration — detection / pose
# ======================================================================================
DET_CONF = 0.35          # detection confidence (high-recall; noise absorbed by persistence)
DET_IMGSZ = 1280         # large -> better small/far person recall on CCTV
PERSON_CLASS = 0         # COCO 'person'
POSE_MATCH_IOU = 0.5     # IoU to attach a pose skeleton to a detection box
KP_CONF_MIN = 0.5        # min keypoint confidence to trust an anchor joint

# COCO keypoint indices
NOSE, L_SHO, R_SHO, L_HIP, R_HIP, L_ANK, R_ANK = 0, 5, 6, 11, 12, 15, 16

# ======================================================================================
# Configuration — per-frame closeness gates (reject if ANY cue says "different row")
# ======================================================================================
MIN_AREA_FRAC = 0.002    # min person-box area / frame area  -> drops far/unclear detections
MIN_CONF = 0.45          # min detection confidence          -> drops unclear detections
T_SIZE_RATIO = 1.45      # apparent-size ratio above this    -> different depth/row/behind
T_DEPTH_GAP = 0.5        # |depth_i-depth_j| / frame-depth-std<= this -> same row (monocular)
T_VERT_RATIO = 1.0       # heads stacked vertically |dy|>|dx| -> front/behind, not same row
CLOSE_ND = 1.8           # <= this many shoulder-widths apart -> physically close (fallback)

# ======================================================================================
# Configuration — tracking (ByteTrack, long buffer for partition occlusion)
# ======================================================================================
TRACK_BUFFER = 120       # frames a lost track is kept for re-association
BYTETRACK_YAML = """\
tracker_type: bytetrack
track_high_thresh: 0.25
track_low_thresh: 0.10
new_track_thresh: 0.30
track_buffer: 120
match_thresh: 0.85
fuse_score: true
"""

# ======================================================================================
# Configuration — role by mobility (safe default = student)
# ======================================================================================
ROLE_MINHIST = 10        # need this many observations before calling anyone an invigilator
ROLE_MOVE_BW = 3.0       # net travel across the track > this many body-widths => mobile (invigilator)

# ======================================================================================
# Configuration — persistence / linger (the false-positive killer)
# ======================================================================================
PERSIST_RATIO = 0.5      # close in >= this fraction of co-present frames ...
PERSIST_MINFR = 8        # ... and at least this many frames (absolute) => sustained
LINGER_MINFR = 8         # invigilator must be ~stationary this many close frames (lingering, not passing)
LINGER_MOVE_BW = 0.8     # invigilator net move during close period < this => lingering
CROWD_MIN = 3            # >= this many mutually sustained-close students => crowd / gathering

# ======================================================================================
# Configuration — self-calibration
# ======================================================================================
CALIB_FACTOR = 0.65      # fire below this fraction of the room's learned normal seating pitch
CALIB_MAX_ND = 2.2       # cap for the (opt-in) two-sided mode
CALIB_MIN_SAMPLES = 40   # need this many static-neighbour measurements, else keep the fallback


# ======================================================================================
# Geometry + pose helpers
# ======================================================================================
def _iou(a, b):
    """Intersection-over-union of two (x1, y1, x2, y2) boxes."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _mid(kps, a, b, kmin):
    """Midpoint of keypoints a and b, or None if either is low-confidence."""
    if kps is None:
        return None
    if kps[a, 2] >= kmin and kps[b, 2] >= kmin:
        return ((kps[a, 0] + kps[b, 0]) / 2.0, (kps[a, 1] + kps[b, 1]) / 2.0)
    return None


def _shoulder_w(kps, box, kmin):
    """Shoulder width in px (scale-free ruler); box-width fallback."""
    if kps is not None and kps[L_SHO, 2] >= kmin and kps[R_SHO, 2] >= kmin:
        w = abs(kps[L_SHO, 0] - kps[R_SHO, 0])
        if w > 4:
            return float(w)
    return max(1.0, (box[2] - box[0]) * 0.6)


def _lean_toward(box, kps, other_box, kmin):
    """Signed lean+turn of this person TOWARD the other, in shoulder-widths (None if no pose)."""
    sho = _mid(kps, L_SHO, R_SHO, kmin)
    hip = _mid(kps, L_HIP, R_HIP, kmin)
    if sho is None or hip is None:
        return None
    lean_dx = sho[0] - hip[0]
    turn_dx = (kps[NOSE, 0] - sho[0]) if kps[NOSE, 2] >= kmin else 0.0
    sw = _shoulder_w(kps, box, kmin)
    direction = 1.0 if (other_box[0] + other_box[2]) >= (box[0] + box[2]) else -1.0
    return ((lean_dx + turn_dx) * direction) / sw


def pair_lean_max(box_a, kps_a, box_b, kps_b, kmin):
    """Max lean-toward score over the two people (shoulder-widths); -inf if neither has pose."""
    scores = [s for s in (_lean_toward(box_a, kps_a, box_b, kmin),
                          _lean_toward(box_b, kps_b, box_a, kmin)) if s is not None]
    return max(scores) if scores else float("-inf")


class Person:
    """A detected person in one frame: box, confidence, and optional (17, 3) keypoints."""

    __slots__ = ("x1", "y1", "x2", "y2", "conf", "kps")

    def __init__(self, x1, y1, x2, y2, conf, kps):
        self.x1, self.y1, self.x2, self.y2 = float(x1), float(y1), float(x2), float(y2)
        self.conf = float(conf)
        self.kps = kps


# ======================================================================================
# The system
# ======================================================================================
class ProximityTemporalSystem:
    """Stateful, per-camera temporal proximity detector.

    Create one instance per camera and feed its frames in order. The ByteTrack state persists
    inside the detection model across ``track()`` calls.
    """

    def __init__(self, device=None, det_weights=DET_WEIGHTS, pose_weights=POSE_WEIGHTS,
                 depth_model=DEPTH_MODEL, shared=None):
        """`shared` = another instance whose loaded models this one reuses: a fleet runs one set of
        weights, while every camera keeps its own calibration and decision state."""
        self.km = KP_CONF_MIN
        if shared is not None:
            self.device, self.det, self.pose, self.depth = shared.device, shared.det, shared.pose, shared.depth
            self._tracker_cfg = shared._tracker_cfg
        else:
            self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
            self.det = YOLO(det_weights)
            self.det.to(self.device)
            self.pose = YOLO(pose_weights)
            self.pose.to(self.device)
            self.depth = hf_pipeline("depth-estimation", model=depth_model,
                                     device=0 if self.device == "cuda" else -1)
            # ByteTrack config written once to a temp file (ultralytics wants a path)
            fd, self._tracker_cfg = tempfile.mkstemp(prefix="bytetrack_", suffix=".yaml")
            with os.fdopen(fd, "w") as fh:
                fh.write(BYTETRACK_YAML)
        # persistence / role thresholds, in observations: clips sample ~5 fps, a live camera far less often
        self.persist_minfr = PERSIST_MINFR
        self.linger_minfr = LINGER_MINFR
        self.role_minhist = ROLE_MINHIST
        # self-calibration state
        self.is_calibrated = False
        self.fallback_close_nd = CLOSE_ND
        self.active_close_nd = CLOSE_ND
        self.room_normal_mode = None
        self.calib_factor = CALIB_FACTOR
        self.calib_two_sided = False
        self.static_distance_accumulator = []

    # ---- per-person signals -----------------------------------------------------------
    def _depth_map(self, bgr):
        dm = np.array(self.depth(Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)))["depth"],
                      dtype=np.float32)
        if dm.shape != bgr.shape[:2]:
            dm = cv2.resize(dm, (bgr.shape[1], bgr.shape[0]))
        return dm

    def _depth_of(self, dm, p):
        """Median relative depth of a person's upper torso (robust to legs/desk)."""
        x1, x2 = int(max(0, p.x1)), int(min(dm.shape[1], p.x2))
        y1, y2 = int(max(0, p.y1)), int(min(dm.shape[0], p.y1 + (p.y2 - p.y1) * 0.5))
        patch = dm[y1:y2, x1:x2]
        return float(np.median(patch)) if patch.size > 20 else None

    def _sw(self, p):
        """Shoulder width in px (scale-free ruler); box-width fallback."""
        k = p.kps
        if k is not None and k[L_SHO, 2] >= self.km and k[R_SHO, 2] >= self.km:
            w = abs(k[L_SHO, 0] - k[R_SHO, 0])
            if w > 4:
                return float(w)
        return (p.x2 - p.x1) * 0.5

    def _head(self, p):
        """Head point: nose if visible, else top-centre of the box (works from behind)."""
        k = p.kps
        if k is not None and k[NOSE, 2] >= 0.3:
            return (float(k[NOSE, 0]), float(k[NOSE, 1]))
        return ((p.x1 + p.x2) / 2.0, p.y1 + (p.y2 - p.y1) * 0.15)

    def _standing(self, p):
        """Standing = both ankles visible (weak secondary role cue; mobility is primary)."""
        k = p.kps
        return bool(k is not None and k[L_ANK, 2] >= self.km and k[R_ANK, 2] >= self.km)

    def pose_people(self, bgr):
        """Pose skeletons of one frame as (boxes, keypoints), for matching onto detection boxes."""
        pose = self.pose.predict(bgr, conf=max(0.15, DET_CONF - 0.15), imgsz=DET_IMGSZ,
                                 classes=[PERSON_CLASS], device=self.device, half=True,
                                 verbose=False)[0]
        pboxes = pose.boxes.xyxy.cpu().numpy() if pose.boxes is not None else np.empty((0, 4))
        pkps = (pose.keypoints.data.cpu().numpy()
                if (pose.keypoints is not None and len(pboxes)) else [])
        return pboxes, pkps

    def observe(self, shape, boxes, confs, ids, fi, pose, dm):
        """Per-person observations of one frame -> {track_id: observation-dict}.

        shape = frame (H, W, ...); boxes/confs/ids = the tracked detections; pose = (boxes,
        keypoints) from pose_people(); dm = the frame's depth map."""
        area_norm = float(shape[0] * shape[1])
        pboxes, pkps = pose
        present = {}
        for (x1, y1, x2, y2), cf, tid in zip(boxes, confs, ids):
            kps, best = None, POSE_MATCH_IOU
            for pb, pk in zip(pboxes, pkps):
                v = _iou((x1, y1, x2, y2), pb)
                if v >= best:
                    best, kps = v, pk
            q = Person(x1, y1, x2, y2, cf, kps)
            present[int(tid)] = dict(
                box=(q.x1, q.y1, q.x2, q.y2), head=self._head(q), size=self._sw(q),
                depth=self._depth_of(dm, q), standing=self._standing(q), conf=q.conf,
                area=((q.x2 - q.x1) * (q.y2 - q.y1)) / area_norm, kps=q.kps, frame=fi)
        spread = np.std([o["depth"] for o in present.values() if o["depth"] is not None]) + 1e-6
        for o in present.values():
            o["spread"] = spread
        return present

    def infer(self, bgr):
        """Stateless inference of one frame (detection, pose, depth): what a shared model pass
        returns. The per-camera ProximityStream tracks and decides on it."""
        det = self.det.predict(bgr, conf=DET_CONF, imgsz=DET_IMGSZ, classes=[PERSON_CLASS],
                               device=self.device, half=True, verbose=False)[0]
        if det.boxes is None or len(det.boxes) == 0:
            return FrameInference(bgr.shape, np.empty((0, 4)), np.empty(0), None, None)
        return FrameInference(bgr.shape, det.boxes.xyxy.cpu().numpy(), det.boxes.conf.cpu().numpy(),
                              self.pose_people(bgr), self._depth_map(bgr))

    # ---- tracking ---------------------------------------------------------------------
    def reset_tracker(self):
        """Clear ByteTrack state (call between independent clips/cameras)."""
        try:
            for t in self.det.predictor.trackers:
                t.reset()
        except Exception:
            pass

    def track(self, frames):
        """Run detection+tracking+pose+depth over an ordered iterable of BGR frames.

        Returns (tracks, per_frame) where per_frame[i] = {track_id: observation-dict}.
        """
        self.reset_tracker()
        per_frame = []
        for fi, bgr in enumerate(frames):
            res = self.det.track(bgr, persist=True, conf=DET_CONF, imgsz=DET_IMGSZ,
                                 classes=[PERSON_CLASS], device=self.device, half=True,
                                 verbose=False, tracker=self._tracker_cfg)[0]
            present = {}
            if res.boxes is not None and res.boxes.id is not None:
                present = self.observe(bgr.shape, res.boxes.xyxy.cpu().numpy(), res.boxes.conf.cpu().numpy(),
                                       res.boxes.id.cpu().numpy().astype(int), fi,
                                       self.pose_people(bgr), self._depth_map(bgr))
            per_frame.append(present)
        return _tracks_from_per_frame(per_frame), per_frame

    # ---- role by mobility -------------------------------------------------------------
    def roles(self, tracks):
        """Classify each track: invigilator if it travels far across its life, else student."""
        role, mobility = {}, {}
        for tid, hist in tracks.items():
            net = 0.0
            if len(hist) >= self.role_minhist:
                centres = np.array([((o["box"][0] + o["box"][2]) / 2,
                                     (o["box"][1] + o["box"][3]) / 2) for o in hist])
                size = np.median([o["size"] for o in hist]) + 1e-6
                span = np.percentile(centres, 90, 0) - np.percentile(centres, 10, 0)
                net = float(np.hypot(*span) / size)  # robust travel span in body-widths
            mobility[tid] = net
            role[tid] = "invigilator" if net > ROLE_MOVE_BW else "student"
        self._mobility = mobility
        return role

    # ---- self-calibration -------------------------------------------------------------
    def calibrate(self, per_frame, role):
        """Learn the room's normal adjacent-seat pitch from stationary (seated) people and set the
        violation threshold relative to it. Nearest SAME-ROW neighbour distance is used (not all
        pairs — most pairs are far across the room and would bury the adjacent-seat peak)."""
        static = {t for t, r in role.items() if r == "student"}
        acc = []
        for present in per_frame:
            ids = [t for t in present
                   if t in static and present[t]["size"] > 0 and present[t]["depth"] is not None]
            for a in ids:
                oa = present[a]
                best = None
                for b in ids:
                    if b == a:
                        continue
                    ob = present[b]
                    sr = max(oa["size"], ob["size"]) / max(1e-6, min(oa["size"], ob["size"]))
                    if sr > T_SIZE_RATIO:
                        continue
                    if abs(oa["depth"] - ob["depth"]) / max(oa["spread"], ob["spread"]) > T_DEPTH_GAP:
                        continue
                    dx = abs(oa["head"][0] - ob["head"][0])
                    dy = abs(oa["head"][1] - ob["head"][1])
                    if dy > T_VERT_RATIO * dx:
                        continue
                    d = float(np.hypot(dx, dy)) / max(1.0, (oa["size"] + ob["size"]) / 2.0)
                    if best is None or d < best:
                        best = d
                if best is not None and best > 0.5:
                    acc.append(best)
        self.static_distance_accumulator = acc
        if len(acc) < CALIB_MIN_SAMPLES:
            self.active_close_nd = self.fallback_close_nd
            self.is_calibrated = True
            return None
        d = np.array(acc)
        try:
            from scipy import stats
            kde = stats.gaussian_kde(d)
            xs = np.linspace(d.min(), np.percentile(d, 95), 400)
            mode = float(xs[np.argmax(kde(xs))])
        except Exception:
            hist, edges = np.histogram(d, bins=30)
            mode = float((edges[np.argmax(hist)] + edges[np.argmax(hist) + 1]) / 2)
        self.room_normal_mode = mode
        cand = mode * self.calib_factor
        if self.calib_two_sided:
            self.active_close_nd = float(min(cand, CALIB_MAX_ND))   # may loosen (can add FPs)
        else:
            self.active_close_nd = float(min(cand, self.fallback_close_nd))  # tighten-only (safe)
        self.is_calibrated = True
        return mode

    # ---- per-frame closeness (three independent same-row cues) ------------------------
    def _close(self, a, b):
        """True if two observations are physically close on the same row. Returns (bool, nd)."""
        if a["depth"] is None or b["depth"] is None:
            return False, 0.0
        if a["area"] < MIN_AREA_FRAC or b["area"] < MIN_AREA_FRAC:
            return False, 0.0
        if a["conf"] < MIN_CONF or b["conf"] < MIN_CONF:
            return False, 0.0
        sr = max(a["size"], b["size"]) / max(1e-6, min(a["size"], b["size"]))
        if sr > T_SIZE_RATIO:                                   # (1) size-ratio
            return False, 0.0
        spread = max(a["spread"], b["spread"])
        if abs(a["depth"] - b["depth"]) / spread > T_DEPTH_GAP:  # (2) depth
            return False, 0.0
        dx = abs(a["head"][0] - b["head"][0])
        dy = abs(a["head"][1] - b["head"][1])
        if dy > T_VERT_RATIO * dx:                              # (3) vertical-stacking
            return False, 0.0
        nd = float(np.hypot(dx, dy)) / max(1.0, (a["size"] + b["size"]) / 2.0)
        return (nd <= self.active_close_nd), nd

    # ---- temporal decision ------------------------------------------------------------
    def process_clip(self, frames):
        """Full pipeline over an ordered iterable of frames -> (info, tracks, per_frame, role)."""
        tracks, per_frame = self.track(frames)
        role = self.roles(tracks)
        info = self.decide(per_frame, role)
        info["n_tracks"] = len(tracks)
        return info, tracks, per_frame, role

    def decide(self, per_frame, role):
        """Aggregate per-frame closeness over time and fire sustained violations + crowds."""
        n_frames = len(per_frame)
        copres = defaultdict(int)
        closef = defaultdict(int)
        streak = defaultdict(int)
        curstreak = defaultdict(int)
        leans = defaultdict(float)
        invig_move = defaultdict(list)  # invigilator position during close frames (for linger test)

        for present in per_frame:
            ids = list(present)
            for ii in range(len(ids)):
                for jj in range(ii + 1, len(ids)):
                    i, j = ids[ii], ids[jj]
                    key = (min(i, j), max(i, j))
                    copres[key] += 1
                    a, b = present[i], present[j]
                    is_close, _ = self._close(a, b)
                    if not is_close:
                        curstreak[key] = 0
                        continue
                    closef[key] += 1
                    curstreak[key] += 1
                    streak[key] = max(streak[key], curstreak[key])
                    ln = pair_lean_max(a["box"], a["kps"], b["box"], b["kps"], self.km)
                    if ln is not None and ln > leans[key]:
                        leans[key] = ln
                    for who in (i, j):
                        if role[who] == "invigilator":
                            o = present[who]
                            invig_move[(key, who)].append(
                                ((o["box"][0] + o["box"][2]) / 2, (o["box"][1] + o["box"][3]) / 2, o["size"]))

        sustained_map = {}
        for key, cp in copres.items():
            cf = closef[key]
            if cf == 0:
                continue
            sustained_map[key] = ((cf / max(1, cp) >= PERSIST_RATIO and cf >= self.persist_minfr)
                                  or streak[key] >= self.persist_minfr)

        # crowd = connected cluster of >= CROWD_MIN students mutually sustained-close
        adj = defaultdict(set)
        for (i, j), sus in sustained_map.items():
            if sus and role[i] == "student" and role[j] == "student":
                adj[i].add(j)
                adj[j].add(i)
        crowd_members, crowds, seen = set(), [], set()
        for node in list(adj):
            if node in seen:
                continue
            comp, stack = set(), [node]
            while stack:
                x = stack.pop()
                if x in seen:
                    continue
                seen.add(x)
                comp.add(x)
                stack.extend(adj[x] - seen)
            if len(comp) >= CROWD_MIN:
                crowds.append(sorted(comp))
                crowd_members |= comp

        violations = []
        for comp in crowds:
            ms = max(streak[(min(a, b), max(a, b))]
                     for a in comp for b in comp if a < b and (min(a, b), max(a, b)) in streak)
            violations.append(dict(pair=[int(x) for x in comp], kind="crowd",
                                   why=f"{len(comp)} students clustered", close_frames=0,
                                   copresent=0, max_streak=int(ms), ratio=0.0, lean=0.0))

        for key, cp in copres.items():
            cf = closef[key]
            if cf == 0:
                continue
            i, j = key
            ratio = cf / max(1, cp)
            sustained = sustained_map.get(key, False)
            ri, rj = role[i], role[j]
            kind = "-".join(sorted([ri, rj]))
            fire, why = False, ""
            if kind == "student-student":
                if sustained and not (i in crowd_members and j in crowd_members):
                    fire, why = True, "sustained adjacency"
            elif kind == "invigilator-student":
                inv = i if ri == "invigilator" else j
                mv = invig_move.get((key, inv), [])
                if len(mv) >= self.linger_minfr:
                    arr = np.array([(x, y) for x, y, _ in mv])
                    size = np.median([s for *_, s in mv]) + 1e-6
                    if np.hypot(*(arr.max(0) - arr.min(0))) / size < LINGER_MOVE_BW:
                        fire, why = True, "invigilator lingering at desk"
            elif kind == "invigilator-invigilator":
                if sustained:
                    fire, why = True, "two staff sustained close"
            if fire:
                violations.append(dict(pair=[int(i), int(j)], kind=kind, why=why,
                                       close_frames=int(cf), copresent=int(cp),
                                       max_streak=int(streak[key]), ratio=round(float(ratio), 2),
                                       lean=round(float(leans[key]), 3)))

        return dict(n_frames=n_frames,
                    roles={str(k): v for k, v in role.items()},
                    violations=violations,
                    baseline_pairframes=sum(closef.values()),
                    baseline_everclose_pairs=sum(1 for k in closef if closef[k] > 0),
                    temporal_violations=len(violations))


class FrameInference:
    """One frame's stateless model output: person boxes + confidences, pose, depth map."""

    __slots__ = ("shape", "boxes", "confs", "pose", "depth")

    def __init__(self, shape, boxes, confs, pose, depth):
        self.shape, self.boxes, self.confs, self.pose, self.depth = shape, boxes, confs, pose, depth


class _TrackerInput:
    """The slice of an ultralytics Results.boxes that BYTETracker.update reads."""

    def __init__(self, boxes, confs):
        self.conf = np.asarray(confs, dtype=np.float32)
        self.xyxy = np.asarray(boxes, dtype=np.float32).reshape(-1, 4)
        self.cls = np.zeros(len(self.conf), dtype=np.float32)

    @property
    def xywh(self):
        x1, y1, x2, y2 = self.xyxy.T
        return np.stack([(x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1], axis=1)

    def __len__(self):
        return len(self.conf)

    def __getitem__(self, sel):
        out = _TrackerInput.__new__(_TrackerInput)
        out.conf, out.xyxy, out.cls = self.conf[sel], self.xyxy[sel], self.cls[sel]
        return out


class ProximityStream:
    """One camera's live state: its own ByteTrack and a rolling window of per-sample observations.

    `system` is this camera's ProximityTemporalSystem (models shared, calibration its own). Samples
    are a few seconds to minutes apart, so the window is capped by count and by age."""

    def __init__(self, system, window_n=60, window_s=900.0):
        from types import SimpleNamespace
        from ultralytics.trackers.byte_tracker import BYTETracker
        import yaml
        self.system = system
        args = SimpleNamespace(**yaml.safe_load(BYTETRACK_YAML))
        try:     # ultralytics 8.4.23 (old setup): max lost frames = frame_rate / 30 * track_buffer
            self.tracker = BYTETracker(args, frame_rate=30)
        except TypeError:   # newer ultralytics (worker image): max lost frames = track_buffer — the same at 30
            self.tracker = BYTETracker(args)
        self.window_n, self.window_s = window_n, window_s
        self.per_frame, self.times = [], []
        self.samples = 0

    def update(self, inf, t):
        """Track one FrameInference taken at time t; returns {track_id: observation}."""
        present = {}
        tracks = self.tracker.update(_TrackerInput(inf.boxes, inf.confs))
        if len(tracks):
            idx = tracks[:, -1].astype(int)
            present = self.system.observe(inf.shape, inf.boxes[idx], inf.confs[idx],
                                          tracks[:, 4].astype(int), self.samples, inf.pose, inf.depth)
        self.samples += 1
        self.per_frame.append(present)
        self.times.append(t)
        while self.per_frame and (len(self.per_frame) > self.window_n or t - self.times[0] > self.window_s):
            self.per_frame.pop(0)
            self.times.pop(0)
        return present

    def clear_window(self):
        """Forget the evidence so far (after an alert); tracker ids and calibration stay."""
        self.per_frame, self.times = [], []


def _tracks_from_per_frame(per_frame):
    """Collapse per-frame assignments into {track_id: [observation, ...]}."""
    hist = defaultdict(list)
    for present in per_frame:
        for tid, obs in present.items():
            hist[tid].append(obs)
    return dict(hist)


# ======================================================================================
# Annotation
# ======================================================================================
_RED = (0, 0, 255)
_AMBER = (0, 180, 255)
_GREY = (150, 150, 150)


def annotate(frame, present, role, members, label, color=_RED):
    """Draw faint context boxes, bold boxes around the flagged members, and connecting lines."""
    member_set = set(members)
    for tid, obs in present.items():
        if tid in member_set:
            continue
        x1, y1, x2, y2 = (int(v) for v in obs["box"])
        col = _AMBER if role.get(tid) == "invigilator" else _GREY
        cv2.rectangle(frame, (x1, y1), (x2, y2), col, 1)
    heads = []
    for tid in members:
        if tid not in present:
            continue
        x1, y1, x2, y2 = (int(v) for v in present[tid]["box"])
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3)
        cv2.putText(frame, f"{tid}:{role.get(tid, '?')[:3]}", (x1, max(14, y1 - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        heads.append((int(present[tid]["head"][0]), int(present[tid]["head"][1])))
    for p in range(len(heads)):
        for q in range(p + 1, len(heads)):
            cv2.line(frame, heads[p], heads[q], color, 2)
    cv2.putText(frame, label, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    return frame


# ======================================================================================
# CLI runners
# ======================================================================================
def _frame_stream(path, stride, maxw, orig_frame_numbers, limit):
    """Yield subsampled, width-capped BGR frames; record each one's original frame number."""
    cap = cv2.VideoCapture(path)
    i, taken = 0, 0
    while True:
        if not cap.grab():
            break
        if i % stride == 0:
            ok, fr = cap.retrieve()
            if not ok or fr is None:
                break
            if fr.shape[1] > maxw:
                fr = cv2.resize(fr, (maxw, int(fr.shape[0] * maxw / fr.shape[1])))
            orig_frame_numbers.append(i)
            yield fr
            taken += 1
            if limit and taken >= limit:
                break
        i += 1
    cap.release()


def _read_original(path, frame_no, maxw):
    cap = cv2.VideoCapture(path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
    ok, fr = cap.read()
    cap.release()
    if not ok or fr is None:
        return None
    if fr.shape[1] > maxw:
        fr = cv2.resize(fr, (maxw, int(fr.shape[0] * maxw / fr.shape[1])))
    return fr


def _close_frames(system, per_frame, i, j):
    """Sampled-frame indices where tracks i and j are close (for picking a representative frame)."""
    out = []
    for fi, present in enumerate(per_frame):
        if i in present and j in present and system._close(present[i], present[j])[0]:
            out.append(fi)
    return out


def run_video(system, path, out_dir, fps, maxw, limit, do_calibrate):
    os.makedirs(out_dir, exist_ok=True)
    cap = cv2.VideoCapture(path)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 10
    cap.release()
    stride = max(1, round(src_fps / fps))
    orig, t0 = [], time.time()
    print(f"video={os.path.basename(path)} src_fps={src_fps:.0f} stride={stride} -> ~{fps}fps", flush=True)
    tracks, per_frame = system.track(_frame_stream(path, stride, maxw, orig, limit))
    role = system.roles(tracks)
    if do_calibrate:
        mode = system.calibrate(per_frame, role)
        print(f"self-calibrated: room pitch={mode and round(mode, 2)} sw -> "
              f"threshold={system.active_close_nd:.2f} sw", flush=True)
    info = system.decide(per_frame, role)
    info["n_tracks"] = len(tracks)

    n_role = {"invigilator": sum(v == "invigilator" for v in role.values()),
              "student": sum(v == "student" for v in role.values())}
    print(f"sampled {len(per_frame)} frames, {info['n_tracks']} tracks, roles {n_role} "
          f"({time.time() - t0:.0f}s)")
    print(f"per-frame close pairs: {info['baseline_everclose_pairs']} "
          f"({info['baseline_pairframes']} pair-frames)")
    print(f"TEMPORAL violations: {info['temporal_violations']}")
    for v in info["violations"]:
        print(f"    {v['kind']:22s} tracks{v['pair']} streak={v['max_streak']} :: {v['why']}")

    for e, v in enumerate(info["violations"]):
        members = v["pair"]
        if v["kind"] == "crowd":
            fi = max(range(len(per_frame)), key=lambda f: sum(m in per_frame[f] for m in members))
        else:
            cfs = _close_frames(system, per_frame, members[0], members[1])
            if not cfs:
                continue
            fi = cfs[len(cfs) // 2]
        fr = _read_original(path, orig[fi], maxw)
        if fr is None:
            continue
        fr = annotate(fr, per_frame[fi], role, members, f"EVENT {e}: {v['kind']} ({v['why']})")
        cv2.imwrite(os.path.join(out_dir, f"event_{e:02d}_{v['kind']}.jpg"), fr)

    report = dict(video=os.path.basename(path), sampled_frames=len(per_frame),
                  tracks=info["n_tracks"], roles=n_role,
                  calibrated=system.is_calibrated, room_pitch=system.room_normal_mode,
                  threshold=system.active_close_nd, events=info["violations"],
                  per_frame_close_pairs=info["baseline_everclose_pairs"])
    with open(os.path.join(out_dir, "report.json"), "w") as fh:
        json.dump(report, fh, indent=1)
    print(f"-> {out_dir}/ (annotated events + report.json)")
    return report


def run_image(system, path, out_dir):
    """Single image: no temporal persistence is possible, so show per-frame close pairs."""
    os.makedirs(out_dir, exist_ok=True)
    frame = cv2.imread(path)
    if frame is None:
        print(f"could not read {path}")
        return
    _, per_frame = system.track([frame])
    present = per_frame[0]
    role = {tid: "student" for tid in present}  # one frame -> mobility undefined
    ids = list(present)
    pairs = [(ids[a], ids[b]) for a in range(len(ids)) for b in range(a + 1, len(ids))
             if system._close(present[ids[a]], present[ids[b]])[0]]
    vis = frame.copy()
    for tid, obs in present.items():
        x1, y1, x2, y2 = (int(v) for v in obs["box"])
        cv2.rectangle(vis, (x1, y1), (x2, y2), _GREY, 1)
    for i, j in pairs:
        for tid in (i, j):
            x1, y1, x2, y2 = (int(v) for v in present[tid]["box"])
            cv2.rectangle(vis, (x1, y1), (x2, y2), _RED, 3)
        ci, cj = present[i]["head"], present[j]["head"]
        cv2.line(vis, (int(ci[0]), int(ci[1])), (int(cj[0]), int(cj[1])), _RED, 2)
    cv2.putText(vis, f"per-frame close pairs: {len(pairs)} (no temporal context in a single image)",
                (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, _RED, 2)
    out = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + "_proximity.jpg")
    cv2.imwrite(out, vis)
    print(f"{len(pairs)} close pair(s) -> {out}")


def main():
    ap = argparse.ArgumentParser(description="Temporal proximity-violation detector (release build).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--video", help="path to a video clip")
    src.add_argument("--image", help="path to a single image")
    ap.add_argument("--out", default="proximity_out", help="output directory")
    ap.add_argument("--fps", type=float, default=5.0, help="sampling rate for video")
    ap.add_argument("--maxw", type=int, default=1280, help="max frame width (downscaled if wider)")
    ap.add_argument("--limit", type=int, default=0, help="max sampled frames (0 = all)")
    ap.add_argument("--calibrate", action="store_true", help="self-calibrate threshold from this room")
    ap.add_argument("--device", default=None, help="cuda / cpu (auto if unset)")
    args = ap.parse_args()

    system = ProximityTemporalSystem(device=args.device)
    if args.video:
        run_video(system, args.video, args.out, args.fps, args.maxw, args.limit, args.calibrate)
    else:
        run_image(system, args.image, args.out)


if __name__ == "__main__":
    main()
