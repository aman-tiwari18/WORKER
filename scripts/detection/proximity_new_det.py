"""
Proximity (two people too close) - the Proximity_New_Script logic as an analytic of the unified detector.

What comes from Proximity_New_Script (used unchanged, imported as P):
  - PersonDetector: yolo11m person boxes + yolo11m-pose skeletons matched onto them
  - closeness rules: heads within `close_nd` shoulder-widths, similar apparent size (same row / depth),
    heads not stacked vertically; duplicate boxes dropped
  - Analyzer: per-camera tracking and "the pair must hold for persist_frames samples"
  - student-only: people seen standing (ankles visible) are staff, not candidates

What this module adds so it runs on live streams (one frame per camera every few seconds, not a clip):
  - a pair is confirmed when it is seen in two samples close in time (the session pipeline asks for the
    second frame ~10 s after the first; round-robin visits give two frames ~1 s apart). A first sighting
    older than PROXIMITY_CONFIRM_MAX_S does not count.
  - filters from the 14 Oct review of the script's output: ignore pairs closer than PROXIMITY_MIN_ND
    (one person detected twice), both people must be seated now, both boxes at least
    PROXIMITY_MIN_PAIR_H of the frame height (tiny far-away boxes were mostly aisle traffic).
  - the repeat count and chain id of a pair come from proximity_repeat.next_violation, the same counter the
    old proximity used, so "Proximity Violation 2, 3 ..." and the client-side count keep working.
  - one saved image per alert: the pair in red, a header with the violation count and the number of pairs.

Entry points used by unified_detector._make_stateful_evaluator: evaluate(...) and flush(...).
Switch back to the old logic with PROXIMITY_ENGINE=old.
"""
import logging
import os
import time

import cv2
import numpy as np

import Proximity_New_Script as P

logger = logging.getLogger(__name__)

CLOSE_ND = float(os.environ.get("PROXIMITY_CLOSE_ND", "1.8"))             # shoulder-widths apart or less
MIN_ND = float(os.environ.get("PROXIMITY_MIN_ND", "0.35"))                # closer than this = one person twice
MIN_PAIR_H = float(os.environ.get("PROXIMITY_MIN_PAIR_H", "0.08"))        # box height / frame height
CONFIRM_MAX_S = float(os.environ.get("PROXIMITY_CONFIRM_MAX_S", "90"))    # first sighting -> confirming sample
SEATED_ONLY = os.environ.get("PROXIMITY_SEATED_ONLY", "1").strip() not in ("0", "false", "no", "")
POSTURE_STANDING_MIN = float(os.environ.get("PROXIMITY_POSTURE_STANDING_MIN", "0.4"))   # posture model confidence
POSTURE_STANDING_CLASS = 1       # posture_rfdetr classes: 0 sitting, 1 standing, 2 unsure
POSTURE_MATCH_IOU = 0.3

_cfg = None


def make_config(models_dir=None):
    """Proximity_New_Script.Config with the settings its --closeness mode applies."""
    cfg = P.Config()
    if models_dir:
        cfg.yolo_weights = os.path.join(models_dir, "yolo11m.pt")
        cfg.pose_weights = os.path.join(models_dir, "yolo11m-pose.pt")
    cfg.closeness_mode = True
    cfg.close_nd = CLOSE_ND
    cfg.proximity_m = cfg.close_nd          # the analyzer flags pairs whose closeness distance is below this
    cfg.gather_m = cfg.close_nd
    cfg.row_aware = False
    cfg.require_interaction = False         # pure closeness, no lean / gaze gate
    cfg.exclude_invigilators = True         # student-only
    cfg.invig_mobility_std = 1e9            # positions are pixels: judge staff by standing, not by pixel movement
    cfg.draw_red_only = True
    cfg.persist_frames = 2
    cfg.autocalib = None
    return cfg


def make_dist(cfg):
    """The closeness distance of Proximity_New_Script.run_video (positions are [head_x, head_y, body_size_px]):
    1e6 when the pair cannot be on one row, else the head distance in shoulder-widths."""
    def dist(p, q):
        sr = max(p[2], q[2]) / max(1e-6, min(p[2], q[2]))
        if sr > cfg.close_size_ratio_max:             # different apparent size = different depth (row)
            return 1e6
        dx, dy = abs(p[0] - q[0]), abs(p[1] - q[1])
        if dy > cfg.close_vert_ratio_max * dx:        # heads stacked vertically = front / behind
            return 1e6
        nd = float(np.hypot(dx, dy)) / max(1.0, (p[2] + q[2]) / 2.0)
        return nd if nd <= cfg.close_nd else 1e6
    return dist


def _config():
    global _cfg
    if _cfg is None:
        _cfg = make_config()
    return _cfg


def _camera_key(state, camera_id):
    rep = state.get("report_to") or {}
    if str(rep.get("source_type", "RTSP")).upper() == "HLS":
        return f"hls_ch{rep.get('camera_no')}"
    return f"rtsp_{camera_id}"


def _called_standing(box, posture):
    """True when the posture model calls the person at `box` standing (posture = [(xyxy, class, conf)])."""
    best, best_iou = None, POSTURE_MATCH_IOU
    for pb, cls, conf in posture or ():
        v = P._iou(box, pb)
        if v >= best_iou:
            best, best_iou = (cls, conf), v
    return best is not None and best[0] == POSTURE_STANDING_CLASS and best[1] >= POSTURE_STANDING_MIN


def _valid_pair(cfg, a, b, dm, frame_h, posture=None):
    """The review filters; Track objects carry box and keypoints."""
    if dm < MIN_ND:
        return False
    for t in (a, b):
        if t.box[3] - t.box[1] < MIN_PAIR_H * frame_h:
            return False
        if SEATED_ONLY and (t.is_invigilator(cfg) or P._standing(t, cfg.kp_conf_min)
                            or _called_standing(t.box, posture)):
            return False
    return True


def _annotate(frame, pairs, count, header_extra=""):
    out = frame.copy()
    h, w = out.shape[:2]
    s = max(0.5, w / 1280.0)
    for a, b, dm in pairs:
        for t in (a, b):
            x1, y1, x2, y2 = (int(v) for v in t.box)
            cv2.rectangle(out, (x1, y1), (x2, y2), (0, 0, 255), max(2, int(3 * s)))
        ca = (int((a.box[0] + a.box[2]) / 2), int((a.box[1] + a.box[3]) / 2))
        cb = (int((b.box[0] + b.box[2]) / 2), int((b.box[1] + b.box[3]) / 2))
        cv2.line(out, ca, cb, (0, 0, 255), max(1, int(2 * s)))
    lines = [("PROXIMITY VIOLATION", (0, 0, 255)),
             (f"Violation {count}  -  {len(pairs)} pair{'s' if len(pairs) != 1 else ''} too close{header_extra}", (255, 255, 255))]
    pad, bw, bh = int(8 * s), int(min(w - 16, 620 * s)), int(((34 * len(lines)) + 12) * s)
    over = out.copy()
    cv2.rectangle(over, (pad, pad), (pad + bw, pad + bh), (0, 0, 0), -1)
    cv2.addWeighted(out, 0.6, over, 0.4, 0, out)
    for i, (text, color) in enumerate(lines):
        cv2.putText(out, text, (pad + int(10 * s), pad + int((28 + 32 * i) * s)), cv2.FONT_HERSHEY_SIMPLEX,
                    (0.85 if i == 0 else 0.6) * s, color, 2 if i == 0 else 1)
    return out


def evaluate(result, frame, state, t, camera_id, cooldown_s, session=None):
    """One sample of one camera. `result` = the people of Proximity_New_Script.PersonDetector for this frame.
    Returns [(annotated_frame, "proximity", {"violation_count", "violation_chain"})] when a pair is confirmed.

    state["consec"] / state["last_save"] are the fields the session pipeline's feedback reads: consec = 1 asks
    for the confirming frame soon, a new last_save holds the camera until the cooldown ends."""
    cfg = _config()
    if "analyzer" not in state:
        state["analyzer"] = P.Analyzer(cfg, make_dist(cfg))
        state["pair_first"] = {}
    an, first = state["analyzer"], state["pair_first"]
    frame_h = frame.shape[0]
    posture = state.get("posture")

    people = P._dedup_people(result or [], cfg.dedup_iou)
    dets = []
    for p in people:
        hx, hy = P._head_pt(p, cfg.kp_conf_min)
        dets.append((np.array([hx, hy, P._body_size(p, cfg.kp_conf_min)]), (p.x1, p.y1, p.x2, p.y2),
                     p.conf, p.kps, P._standing(p, cfg.kp_conf_min)))
    live = an.update_tracks(dets)
    pairs, _groups = an.analyze(live)
    by_id = {tr.id: tr for tr in live}

    # pairs close right now (first sighting or already confirmed) - the candidates the confirming frame is for
    cand = {}
    for key, st in an._pair.items():
        if st["hits"] >= 1 and key <= by_id.keys():
            a, b = (by_id[i] for i in sorted(key))
            dm = an.dist(a.pos, b.pos)
            if dm < cfg.proximity_m and _valid_pair(cfg, a, b, dm, frame_h, posture):
                cand[key] = (a, b, dm)
    for key in list(first):                       # forget pairs that are no longer close
        if key not in cand:
            del first[key]
    for key in cand:
        first.setdefault(key, t)

    confirmed = []
    for a, b, dm in pairs:
        key = frozenset((a.id, b.id))
        if key not in cand:
            continue
        if t - first[key] > CONFIRM_MAX_S:        # first sighting too old: start counting again
            first[key] = t
            an._pair[key]["hits"], an._pair[key]["active"] = 1, False
            continue
        confirmed.append((a, b, dm))

    if not confirmed:
        state["consec"] = 1 if cand else 0        # a candidate pair asks for the confirming frame
        return []
    state["consec"] = 0
    last = state.get("last_save")
    if last is not None and t - last < cooldown_s:      # no cooldown before this camera's first alert
        return []

    camera_key = _camera_key(state, camera_id)
    rep = state.get("report_to") or {}
    try:
        from proximity_repeat import next_violation
        count, chain = next_violation(camera_key, [(list(a.box), list(b.box)) for a, b, _ in confirmed],
                                      rep.get("exam_id"), rep.get("shift_id"))
    except Exception as e:                        # a counter problem must not lose the alert
        logger.error(f"[Proximity] violation counter failed for {camera_key}: {e}")
        count, chain = 1, None
    state["last_save"] = t
    for key in [(a.id, b.id) for a, b, _ in confirmed]:
        first.pop(frozenset(key), None)
    return [(_annotate(frame, confirmed, count), "proximity", {"violation_count": count, "violation_chain": chain})]


def flush(state, now):
    """Nothing is held back: an alert is reported the moment it is confirmed."""
    return []
