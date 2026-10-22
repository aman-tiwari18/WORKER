"""
Proximity (two people too close) - the temporal proximity system (proximity_temporal_system.py) as an
analytic of the unified detector. PROXIMITY_ENGINE=temporal, the default.

What comes from proximity_temporal_system (logic unchanged):
  - YOLO11m detection + YOLO11m-pose + Depth-Anything depth, run as ONE shared stateless pass per frame
    (detection_models.ProximityTemporalBackend); ByteTrack, roles by mobility, the same-row gate chain
    (size ratio, depth, vertical stacking), self-calibrated seat pitch and the decision rules
    (student-student sustained, invigilator lingering, two staff, crowd) are kept per camera here.

What this module adds so it runs on live streams (one sample per camera every few seconds to minutes,
not a 5 fps clip):
  - persistence is counted in samples: a pair is sustained when it is close in PROXIMITY_PERSIST_MINFR
    samples (default 2: the first sighting and the confirming sample the session pipeline asks for
    ~10 s later, or the two frames of a round-robin visit), inside a window of the camera's last
    PROXIMITY_WINDOW_N samples / PROXIMITY_WINDOW_S seconds.
  - a violation is only reported while the pair is close in the current sample.
  - two review filters of the previous engine still apply: pairs closer than PROXIMITY_MIN_ND (one person
    detected twice) are dropped, and student pairs are seated only: someone the pose (ankles) or the posture
    model calls standing is not a student pair. PROXIMITY_MIN_PAIR_H (min box height / frame height, the
    previous engine's far-box filter) is available but off by default.
  - the repeat count and chain id come from proximity_repeat.next_violation, so "Proximity Violation 2, 3 ..."
    and the client-side count keep working; one saved image per alert with the pair(s) in red.

Entry points used by unified_detector._make_stateful_evaluator: evaluate(...) and flush(...).
Switch to the previous logic with PROXIMITY_ENGINE=new, or the original with PROXIMITY_ENGINE=old.
"""
import logging
import os

import cv2
import numpy as np

import proximity_temporal_system as T

logger = logging.getLogger(__name__)

MIN_ND = float(os.environ.get("PROXIMITY_MIN_ND", "0.35"))                # closer than this = one person twice
MIN_PAIR_H = float(os.environ.get("PROXIMITY_MIN_PAIR_H", "0"))           # box height / frame height; 0 = off (the system's own
                                                                          # area gate and persistence handle far boxes)
SEATED_ONLY = os.environ.get("PROXIMITY_SEATED_ONLY", "1").strip() not in ("0", "false", "no", "")
POSTURE_STANDING_MIN = float(os.environ.get("PROXIMITY_POSTURE_STANDING_MIN", "0.4"))   # posture model confidence
POSTURE_STANDING_CLASS = 1       # posture_rfdetr classes: 0 sitting, 1 standing, 2 unsure
POSTURE_MATCH_IOU = 0.3
PERSIST_MINFR = int(os.environ.get("PROXIMITY_PERSIST_MINFR", "2"))       # close samples that make a pair sustained
LINGER_MINFR = int(os.environ.get("PROXIMITY_LINGER_MINFR", "2"))
ROLE_MINHIST = int(os.environ.get("PROXIMITY_ROLE_MINHIST", str(T.ROLE_MINHIST)))
WINDOW_N = int(os.environ.get("PROXIMITY_WINDOW_N", "60"))
WINDOW_S = float(os.environ.get("PROXIMITY_WINDOW_S", "900"))
CALIB_FIRST_N = int(os.environ.get("PROXIMITY_CALIB_FIRST_N", "5"))       # samples before the first calibration
CALIB_EVERY_N = int(os.environ.get("PROXIMITY_CALIB_EVERY_N", "25"))      # then re-learn the seat pitch this often
KINDS = {k.strip() for k in os.environ.get(
    "PROXIMITY_KINDS", "student-student,invigilator-student,invigilator-invigilator,crowd").split(",") if k.strip()}


def _models():
    """The loaded shared system (detection_models.BACKENDS['proximity_temporal'] owns the weights)."""
    from detection_models import BACKENDS
    return BACKENDS["proximity_temporal"]._model


def _camera_key(state, camera_id):
    rep = state.get("report_to") or {}
    if str(rep.get("source_type", "RTSP")).upper() == "HLS":
        return f"hls_ch{rep.get('camera_no')}"
    return f"rtsp_{camera_id}"


def _called_standing(box, posture):
    """True when the posture model calls the person at `box` standing (posture = [(xyxy, class, conf)])."""
    best, best_iou = None, POSTURE_MATCH_IOU
    for pb, cls, conf in posture or ():
        v = T._iou(box, pb)
        if v >= best_iou:
            best, best_iou = (cls, conf), v
    return best is not None and best[0] == POSTURE_STANDING_CLASS and best[1] >= POSTURE_STANDING_MIN


def _camera(state):
    """This camera's ProximityStream (own ByteTrack, window and calibration over the shared models)."""
    cam = state.get("temporal")
    if cam is None:
        system = T.ProximityTemporalSystem(shared=_models())
        system.persist_minfr, system.linger_minfr, system.role_minhist = PERSIST_MINFR, LINGER_MINFR, ROLE_MINHIST
        cam = state["temporal"] = T.ProximityStream(system, WINDOW_N, WINDOW_S)
    return cam


def _valid_members(system, ids, present, frame_h, posture, seated):
    """The review filters on the people of one violation; `seated`: students only, no one standing."""
    for tid in ids:
        o = present[tid]
        if o["box"][3] - o["box"][1] < MIN_PAIR_H * frame_h:
            return False
        if seated and SEATED_ONLY and (o["standing"] or _called_standing(o["box"], posture)):
            return False
    return True


def _close_now(system, present, ids):
    """Pairs among `ids` that are close in the current sample -> [(i, j, nd)]."""
    out = []
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            i, j = ids[a], ids[b]
            if i in present and j in present:
                is_close, nd = system._close(present[i], present[j])
                if is_close and nd >= MIN_ND:
                    out.append((i, j, nd))
    return out


class _Marked:
    """A person to draw: just the box."""
    __slots__ = ("box",)

    def __init__(self, box):
        self.box = box


def _annotate(frame, pairs, count, header_extra=""):
    out = frame.copy()
    h, w = out.shape[:2]
    s = max(0.5, w / 1280.0)
    for a, b in pairs:
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
    """One sample of one camera. `result` = the FrameInference of the shared model pass for this frame.
    Returns [(annotated_frame, "proximity", {"violation_count", "violation_chain"})] when a violation is confirmed.

    state["consec"] / state["last_save"] are the fields the session pipeline's feedback reads: consec = 1 asks
    for the confirming frame soon, a new last_save holds the camera until the cooldown ends."""
    cam = _camera(state)
    system = cam.system
    frame_h = frame.shape[0]
    posture = state.get("posture")

    present = cam.update(result, t)
    window = cam.per_frame
    tracks = T._tracks_from_per_frame(window)
    role = system.roles(tracks)
    if cam.samples == CALIB_FIRST_N or (cam.samples > CALIB_FIRST_N and cam.samples % CALIB_EVERY_N == 0):
        system.calibrate(window, role)            # tighten-only: never looser than the fallback threshold
    info = system.decide(window, role)

    # a violation counts only while its people are close in this sample, and passes the review filters
    live = []                                     # [(violation, [track ids], close pairs now)]
    for v in info["violations"]:
        if v["kind"] not in KINDS:
            continue
        ids = [i for i in v["pair"] if i in present]
        if len(ids) < 2:
            continue
        close = _close_now(system, present, ids)
        if v["kind"] == "crowd":
            seated = True
        else:
            seated = v["kind"] == "student-student"
            close = [c for c in close if {c[0], c[1]} == set(v["pair"])]
        if close and _valid_members(system, ids, present, frame_h, posture, seated):
            live.append((v, ids, close))

    # a candidate pair asks for the confirming sample: any valid close pair of students right now
    cand = False
    if not live:
        sid = [i for i in present if role.get(i) == "student"]
        for i, j, nd in _close_now(system, present, sid):
            if _valid_members(system, (i, j), present, frame_h, posture, True):
                cand = True
                break
    if not live:
        state["consec"] = 1 if cand else 0
        return []
    state["consec"] = 0
    last = state.get("last_save")
    if last is not None and t - last < cooldown_s:      # no cooldown before this camera's first alert
        return []

    pairs = {}                                    # (i, j) -> (box_i, box_j), deduplicated across violations
    for v, ids, close in live:
        # a crowd is drawn as a chain through its members, any other violation as its one pair
        links = list(zip(ids, ids[1:])) if v["kind"] == "crowd" else [close[0][:2]]
        for i, j in links:
            pairs[(min(i, j), max(i, j))] = (present[i]["box"], present[j]["box"])
    camera_key = _camera_key(state, camera_id)
    rep = state.get("report_to") or {}
    try:
        from proximity_repeat import next_violation
        count, chain = next_violation(camera_key, [(list(a), list(b)) for a, b in pairs.values()],
                                      rep.get("exam_id"), rep.get("shift_id"))
    except Exception as e:                        # a counter problem must not lose the alert
        logger.error(f"[Proximity] violation counter failed for {camera_key}: {e}")
        count, chain = 1, None
    state["last_save"] = t
    logger.info(f"[Proximity] {camera_id}: " + "; ".join(f"{v['kind']} {v['pair']} ({v['why']})" for v, _, _ in live))
    annotated = _annotate(frame, [(_Marked(a), _Marked(b)) for a, b in pairs.values()], count)
    cam.clear_window()                            # the same pair needs fresh evidence for its next alert
    return [(annotated, "proximity", {"violation_count": count, "violation_chain": chain})]


def flush(state, now):
    """Nothing is held back: an alert is reported the moment it is confirmed."""
    return []
