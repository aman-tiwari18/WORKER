"""
Monitor Tampering detection — a monitor moved, lifted or carried away in an exam lab.

Built from monitor_alerts_deploy_v2 (the standalone service) and its person + monitor
model (rfdetr_model/checkpoint_best_total.pth, RF-DETR Medium, copied to
models/monitor_person_rfdetr.pth). The rule engine is vendored in monitor_tamper/;
this module adds the per-camera lifecycle and plugs it into the unified detector:

  1. capture : the first CAPTURE seconds of a camera's frames learn its "home spots"
               (where each monitor normally sits) and measure how fast the camera is
               fed: v2's rules for a steady feed, wider windows for sparse bursts.
  2. watch   : every later frame is compared with them. Alerts:
       MONITOR_MISSING  a spot stays empty (nobody in front of it) after a person
                        was at it recently
       MONITOR_OFF_HOME a monitor away from every spot keeps moving while a person
                        touches it (carried / lifted)
       VIEW_CHANGED     most spots empty at once (camera covered / moved, lights off)
  3. evidence: every frame is also kept for evidence_buffer_seconds. An alert is held
               for evidence_delay_seconds, then the buffer is searched for the frames
               that show who carried the monitor (monitor_tamper/evidence.py).

Stateful: unified_detector keeps one state dict per camera for the whole run, calls
evaluate() for every frame of that camera and flush() about once a second (and at
shutdown) so a held alert is reported even if the camera sends no more frames. The
watcher does its own cooldown and incident grouping, so it bypasses the unified
consecutive-hit gate.

The reported image is the annotated trigger frame with the best evidence frame
stacked under it. The full v2 alert (JSON, raw frame, every evidence frame) is also
written to results/monitor_tamper_alerts/<camera>/<date>/.

Home spots are saved per exam shift in results/monitor_tamper_homes/<session>/ with a
reference image, so an operator can check that every monitor was found; a restart
within the same shift reuses them, a new shift learns again.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime

import cv2
import numpy as np

from monitor_tamper.alerts import BASIS_LABEL, draw_annotated, draw_incident, write_alert
from monitor_tamper.core import Detection, FrameDetections, Params, box_size, centre_distance, coverage, overlap
from monitor_tamper.evidence import Evidence, FrameBuffer, decode, select_evidence
from monitor_tamper.home_map import HomeMap, capture_home_map, load_home_map, save_home_map
from monitor_tamper.watcher import Event, Watcher

logger = logging.getLogger(__name__)

# ================= CONFIG ==================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLASS_IDS = {"person": 0, "monitor": 1}
# RF-DETR Medium (models/monitor_person_rfdetr.pth), the default model
# (UNIFIED_MONITOR_MODEL=rfdetr). Thresholds from monitor_alerts_deploy_v2/config.yaml.
THRESHOLDS_RFDETR = {"person": 0.475, "monitor": 0.45}
# Fine-tuned YOLO26m (models/monitor_person_yolo26m.pt, same classes), selected with
# UNIFIED_MONITOR_MODEL=yolo26m. Best-F1 thresholds from the model's test split.
THRESHOLDS = {"person": 0.425, "monitor": 0.35}
HOMES_DIR = os.path.join(BASE_DIR, "results", "monitor_tamper_homes")
ALERTS_DIR = os.path.join(BASE_DIR, "results", "monitor_tamper_alerts")
# Debug: every frame this analytic checks, annotated with what it saw, whether or not it
# alerted. MONITOR_TAMPER_SAVE_FRAMES=0 turns it off.
FRAMES_DIR = os.path.join(BASE_DIR, "results", "monitor_tamper_frames")
SAVE_ALL_FRAMES = os.environ.get("MONITOR_TAMPER_SAVE_FRAMES", "1") != "0"
# At most one saved frame per camera per this many seconds (a continuous 5 fps feed on
# 12 cameras would otherwise be ~60 images/s). 0 = save every checked frame.
SAVE_EVERY_S = float(os.environ.get("MONITOR_TAMPER_SAVE_EVERY_S", "1.0"))

# The pipeline feeds a camera either at a steady rate (continuous RTSP, SAMPLE_FPS=5)
# or in sparse bursts (RTSP round-robin: 2 frames per visit; HLS: a frame every ~3 s).
# v2's rules assume the steady 5 fps and catch nothing on sparse frames (its 2 s gap
# reset clears every timer between visits), so each camera measures its own rate
# while it learns its home spots and then watches with one of two profiles.
STEADY_MIN_FPS = 2.5        # measured rate at or above this = "steady"
RATE_WARMUP_SECONDS = 5.0   # with saved home spots: measure this long before watching

_COMMON_PARAMS = {
    "fps": 5,                      # highest rate a camera is fed at (SAMPLE_FPS); sizes the buffer
    "capture_seconds": 30,         # learn home spots for at least this long...
    "capture_min_frames": 6,       # ...and over at least this many frames
    "capture_group_iou": 0.5,
    "capture_min_presence": 0.5,
    "match_overlap": 0.5,
    "occlude_cover": 0.3,
    "missing_seconds": 4.0,
    "offhome_move_frac": 0.5,
    "jitter_tolerance": 0.01,
    "view_changed_frac": 0.5,
    # --- culprit evidence ---
    "evidence_buffer_seconds": 60,
    "evidence_delay_seconds": 10,
    "evidence_lookback_seconds": 45,
    "evidence_frames": 5,
    "evidence_min_spacing_seconds": 1.0,
    "carry_overlap": 0.3,
    "min_carry_frames": 2,
    "link_gap_frames": 5,
}
PROFILES = {
    # monitor_alerts_deploy_v2/config.yaml as shipped (recall-first).
    "steady": {
        **_COMMON_PARAMS,
        "offhome_min_frames": 6,
        "person_window_seconds": 60,
        "stray_forget_seconds": 120,
        "learn_seconds": 10,
        "gap_reset_seconds": 2.0,
    },
    # Windows that count frames or assume a steady rate are wider, and "missing" also
    # needs several EMPTY frames in a row.
    "sparse": {
        **_COMMON_PARAMS,
        "touch_cover": 0.2,            # person over a visible monitor = touching it (see watcher.py)
        "missing_min_obs": 3,
        "offhome_min_frames": 3,       # bursts give few frames per carry
        "person_window_seconds": 120,  # a revisit can be tens of seconds away
        "stray_forget_seconds": 300,
        "learn_seconds": 30,
        "gap_reset_seconds": 90,       # longer than a round-robin revisit
    },
}

SEPARATOR_H = 6  # grey bar between the trigger frame and the evidence frame


# ================= MODEL OUTPUT ============
def to_frame_detections(result) -> FrameDetections:
    """supervision.Detections (RF-DETR) or Ultralytics Results (YOLO26m) -> our boxes."""
    class_of = {i: c for c, i in CLASS_IDS.items()}
    if hasattr(result, "boxes"):
        xyxy, conf, cls = (result.boxes.xyxy.cpu().numpy(), result.boxes.conf.cpu().numpy(),
                           result.boxes.cls.cpu().numpy())
        thresholds = THRESHOLDS
    else:
        xyxy, conf, cls = result.xyxy, result.confidence, result.class_id
        thresholds = THRESHOLDS_RFDETR
    persons, monitors = [], []
    for box, score, cls in zip(xyxy, conf, cls.astype(int)):
        c = class_of.get(int(cls))
        if c is None or score < thresholds[c]:
            continue
        det = Detection(tuple(float(v) for v in box), float(score))
        (persons if c == "person" else monitors).append(det)
    return FrameDetections(persons, monitors)


def weak_monitors(result) -> list:
    """Monitor boxes scored between HELD["min_conf"] and the normal monitor threshold:
    too weak for the v2 rules, but used by the held-monitor fallback."""
    if hasattr(result, "boxes"):
        xyxy, conf, cls = (result.boxes.xyxy.cpu().numpy(), result.boxes.conf.cpu().numpy(),
                           result.boxes.cls.cpu().numpy())
        cut = THRESHOLDS["monitor"]
    else:
        xyxy, conf, cls = result.xyxy, result.confidence, result.class_id
        cut = THRESHOLDS_RFDETR["monitor"]
    return [Detection(tuple(float(v) for v in box), float(score))
            for box, score, c in zip(xyxy, conf, cls.astype(int))
            if c == CLASS_IDS["monitor"] and HELD["min_conf"] <= score < cut]


# ================= HELD-MONITOR FALLBACK ===
# The v2 rules only see a carried monitor the model scores >= 0.45 for several frames in
# a row. Held against the body with its screen off, a monitor often scores lower: on
# 29 Sep (LAB1 CAM1, 15:56) a man carried one through the view with the model at
# 0.58-0.63 offline but under 0.45 live, and nothing fired. This rule watches weak
# monitor boxes too and alerts when a standing person is carrying a dark monitor that is
# not on any learned home spot, for several frames, while it moves.
HELD = {
    "min_conf": 0.15,          # weakest monitor box considered
    "person_cover": 0.6,       # >= this share of the monitor inside the person's box
    "min_width_frac": 0.3,     # monitor at least this share of the person's width (not a phone)
    "min_depth_frac": 0.3,     # monitor centre below this share of the person's height (held, not a lit screen behind the head)
    "min_standing_ratio": 1.5, # person box height / width: standing, not seated
    "dark_max_mean": 90,       # mean grey of the monitor box: screen off / back side (a lit desk screen is ~130-180)
    "window_s": 3.0,           # hits and travel are counted over this window
    "min_hits": {"steady": 3, "sparse": 2},
    "min_travel_frac": 0.15,   # the monitor moves >= this share of its size over the window
    "repeat_s": 15.0,          # still held (it is a candidate in this frame): alert again this often
}


@dataclass
class _HeldTrack:
    box: tuple
    hits: list            # [(t, box)] inside the window
    last_t: float
    person: Detection
    score: float
    alerted_at: float | None = None   # last alert of this held monitor


class _HeldMonitorRule:
    def __init__(self, camera_id: str):
        self.camera_id = camera_id
        self.tracks: list[_HeldTrack] = []

    def _candidates(self, frame, dets: FrameDetections, monitors, home_map: HomeMap):
        gray = None
        out = []
        for m in monitors:
            if any(overlap(m.box, s.box) >= 0.5 for s in home_map.spots):
                continue                                   # sitting on a desk spot: v2 rules own it
            holder = max(dets.persons, key=lambda p: coverage(p.box, m.box), default=None)
            if holder is None or coverage(holder.box, m.box) < HELD["person_cover"]:
                continue
            px1, py1, px2, py2 = holder.box
            pw, ph = max(px2 - px1, 1.0), max(py2 - py1, 1.0)
            mx1, my1, mx2, my2 = m.box
            if (mx2 - mx1) < HELD["min_width_frac"] * pw or ph / pw < HELD["min_standing_ratio"]:
                continue
            if ((my1 + my2) / 2 - py1) / ph < HELD["min_depth_frac"]:
                continue
            if gray is None:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            ix, iy = (mx2 - mx1) * 0.15, (my2 - my1) * 0.15   # inner part: skip the bezel/background
            patch = gray[int(my1 + iy):int(my2 - iy), int(mx1 + ix):int(mx2 - ix)]
            if patch.size == 0 or float(patch.mean()) > HELD["dark_max_mean"]:
                continue
            out.append((m, holder))
        return out

    def update(self, frame, dets: FrameDetections, monitors, home_map: HomeMap, t: float, profile: str):
        """Returns an Event when a carried monitor is confirmed, else None."""
        self.tracks = [tr for tr in self.tracks if t - tr.last_t <= HELD["window_s"]]
        event = None
        for m, holder in self._candidates(frame, dets, monitors, home_map):
            near = [tr for tr in self.tracks if centre_distance(tr.box, m.box) <= box_size(tr.box)]
            tr = min(near, key=lambda tr: centre_distance(tr.box, m.box), default=None)
            if tr is None:
                tr = _HeldTrack(m.box, [], t, holder, m.score)
                self.tracks.append(tr)
            tr.hits = [(ht, hb) for ht, hb in tr.hits if t - ht <= HELD["window_s"]] + [(t, m.box)]
            tr.box, tr.last_t, tr.person, tr.score = m.box, t, holder, max(tr.score, m.score)
            travel = centre_distance(tr.hits[0][1], m.box)
            # First alert: confirmed over several frames while moving. After that the monitor
            # is re-reported every repeat_s for as long as it is still held (a candidate in
            # this frame means the holder and the monitor are both visible), moving or not.
            first = (tr.alerted_at is None and len(tr.hits) >= HELD["min_hits"][profile]
                     and travel >= HELD["min_travel_frac"] * box_size(m.box))
            repeat = tr.alerted_at is not None and t - tr.alerted_at >= HELD["repeat_s"]
            if first or repeat:
                tr.alerted_at = t
                event = Event("MONITOR_CARRIED", self.camera_id, t, [], [Detection(m.box, tr.score)],
                              list(dets.persons), nearest_person=holder)
        return event


# ================= REPORTING GATE ==========
# Only alerts that SHOW a monitor being moved are reported. On 29 Sep (CGL mock test) 60
# of 70 alerts were MONITOR_MISSING whose image showed no monitor at all - an empty desk,
# often just a monitor the model missed for a few frames - and the held-monitor fallback
# reported boxes scored 0.18-0.25 that were not clearly monitors. The moved monitor's box
# must clear these bars (fraction of the frame area / pixels):
MOVED_MONITOR = {
    "min_conf": 0.35,          # best score of the moved monitor (fallback: best over its frames)
    "min_area_frac": 0.001,    # >= 0.1 % of the frame (e.g. 32x32 px at 960x1080)
    "min_side_px": 20,         # shorter side
}
# Alerts whose trigger itself is a monitor seen away from home (moving / carried).
MOVE_ALERTS = {"MONITOR_OFF_HOME", "MONITOR_CARRIED"}


def _moved_box_ok(box, frame_size) -> str | None:
    """None if the box is big enough to be a visible monitor, else why not."""
    w, h = box[2] - box[0], box[3] - box[1]
    if min(w, h) < MOVED_MONITOR["min_side_px"]:
        return f"monitor box too small ({w:.0f}x{h:.0f} px)"
    if w * h < MOVED_MONITOR["min_area_frac"] * frame_size[0] * frame_size[1]:
        return f"monitor box too small ({100 * w * h / (frame_size[0] * frame_size[1]):.2f}% of frame)"
    return None


def _skip_reason(event: Event, evidence: Evidence, frame_size) -> str | None:
    """None if the alert shows a monitor being moved (report it), else why it is skipped."""
    if event.type in MOVE_ALERTS:
        if not event.off_home_monitors:
            return "no moved monitor box"
        m = max(event.off_home_monitors, key=lambda d: d.score)
        if m.score < MOVED_MONITOR["min_conf"]:
            return f"moved monitor score {m.score:.2f} < {MOVED_MONITOR['min_conf']}"
        return _moved_box_ok(m.box, frame_size)
    if event.type == "MONITOR_MISSING":
        # An empty desk alone is not reported; it is if the evidence caught the monitor
        # being carried away (a monitor box inside the culprit's box in >= 2 frames).
        boxes = [f.monitor_box for f in evidence.frames if f.monitor_box is not None]
        if evidence.basis != "carrying" or not boxes:
            return f"no moved monitor visible (evidence: {evidence.basis})"
        return _moved_box_ok(max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])), frame_size)
    return f"{event.type} never shows a moved monitor"


# ================= PER-CAMERA LIFECYCLE ====
@dataclass
class _Pending:
    event: Event
    frame: np.ndarray  # frame at trigger time
    due: float


class _Camera:
    """Capture home spots first, then watch; hold each alert until its evidence is in.
    Mirrors monitor_alerts.camera.CameraRunner; the alert is returned to the caller
    to report instead of only being written to disk. The profile (steady / sparse) is
    picked from the rate the camera was fed at while learning."""

    def __init__(self, camera_id: str, profiles: dict[str, Params], homes_dir: str | None):
        self.camera_id = camera_id
        self.profiles = profiles
        # Until watching starts only the settings both profiles share are used.
        self.params = profiles["steady"]
        self.homes_dir = homes_dir  # None: never reuse a saved map
        self.frame_size = None
        self.home_map: HomeMap | None = None
        self.watcher: Watcher | None = None
        self.buffer = FrameBuffer(self.params.evidence_buffer_seconds, self.params.fps)
        self._pending: list[_Pending] = []
        self._capture_frames: list[FrameDetections] = []
        self._capture_start: float | None = None
        self._saved: HomeMap | None = None  # saved spots, used once the rate is known
        self._first_t: float | None = None  # rate measurement since the stream (re)started
        self._seen = 0
        self.held = _HeldMonitorRule(camera_id)

    def process(self, frame, dets: FrameDetections, t: float, weak=()) -> list:
        size = (frame.shape[1], frame.shape[0])
        if size != self.frame_size:
            self._on_new_frame_size(size)
        self.buffer.push(t, frame, dets)
        if self._first_t is None:
            self._first_t = t
        self._seen += 1
        if self.watcher is None:
            if self._saved is None:
                self._capture(frame, dets, t)
            elif t - self._first_t >= RATE_WARMUP_SECONDS:
                self._start_watching(self._saved, t)
                logger.info(f"[MonitorTamper] {self.camera_id}: reusing {len(self._saved.spots)} saved monitor spots")
                self._saved = None
            return []
        prev_alert_t = self.watcher.last_alert_t
        for event in self.watcher.update(dets, t):
            if event.type not in MOVE_ALERTS:
                # Reported only if its evidence later shows the monitor being carried, so
                # it must not put the camera in cooldown and block that carry alert.
                self.watcher.last_alert_t = prev_alert_t
            self._pending.append(_Pending(event, frame, t + self.params.evidence_delay_seconds))
        profile = "steady" if self.params is self.profiles["steady"] else "sparse"
        held = self.held.update(frame, dets, list(dets.monitors) + list(weak), self.home_map, t, profile)
        if held is not None and not self.watcher._cooling_down(t):
            # Same per-camera cooldown / incident grouping as the v2 alerts.
            self.watcher._start_incident(t)
            logger.info(f"[MonitorTamper] {self.camera_id}: MONITOR_CARRIED (held-monitor fallback, "
                        f"monitor score {held.off_home_monitors[0].score:.2f})")
            self._pending.append(_Pending(held, frame, t + self.params.evidence_delay_seconds))
        return self.flush(t)

    def flush(self, now: float | None) -> list:
        """[(event, trigger frame, evidence)] for every held alert due by `now` (None: all)."""
        due = [a for a in self._pending if now is None or a.due <= now]
        out = []
        for alert in due:
            self._pending.remove(alert)
            evidence = select_evidence(alert.event, self.buffer, self.home_map, self.params)
            why = _skip_reason(alert.event, evidence, self.frame_size)
            if why:
                logger.info(f"[MonitorTamper] {self.camera_id}: skipped {alert.event.type} - {why}")
                continue
            out.append((alert.event, alert.frame, evidence))
        return out

    def _on_new_frame_size(self, size) -> None:
        if self.frame_size is not None:
            # A different resolution (other stream / camera reconfigured) makes the
            # old spots and the buffered boxes meaningless.
            logger.warning(f"[MonitorTamper] {self.camera_id}: frame size {self.frame_size} -> {size}; "
                           f"re-learning home spots")
            if self._pending:
                logger.error(f"[MonitorTamper] {self.camera_id}: dropping {len(self._pending)} held "
                             f"alert(s): their boxes do not fit the new frame size")
        saved = None
        if self.frame_size is None and self.homes_dir:
            saved = load_home_map(self.homes_dir, self.camera_id, size)
        self.frame_size, self._pending, self._saved = size, [], saved
        self.buffer.clear()
        self._capture_frames, self._capture_start = [], None
        self._first_t, self._seen = None, 0
        self.home_map, self.watcher = None, None

    def _capture(self, frame, dets: FrameDetections, t: float) -> None:
        if self._capture_start is None:
            self._capture_start = t
        self._capture_frames.append(dets)
        if (t - self._capture_start < self.params.capture_seconds
                or len(self._capture_frames) < self.params.capture_min_frames):
            return
        home_map = capture_home_map(self.camera_id, self.frame_size, self._capture_frames, self.params)
        self._capture_frames = []
        self._start_watching(home_map, t)
        try:
            save_home_map(home_map, self.homes_dir or HOMES_DIR, reference_frame=frame)
        except Exception as e:
            logger.error(f"[MonitorTamper] {self.camera_id}: could not save home spots: {e}")
        logger.info(f"[MonitorTamper] {self.camera_id}: learned {len(home_map.spots)} monitor spots")

    def _start_watching(self, home_map: HomeMap, t: float) -> None:
        elapsed = t - self._first_t
        fps = (self._seen - 1) / elapsed if elapsed > 0 else 0.0
        profile = "steady" if fps >= STEADY_MIN_FPS else "sparse"
        self.params = self.profiles[profile]
        self.home_map = home_map
        self.watcher = Watcher(home_map, self.params)
        logger.info(f"[MonitorTamper] {self.camera_id}: fed {fps:.1f} frames/s -> {profile} rules")


def _homes_dir(session: str | None) -> str | None:
    return os.path.join(HOMES_DIR, session) if session else None


def evaluate(result, frame, state: dict, t: float, camera_id: str, cooldown_s: float,
             session: str | None = None) -> list:
    """Feed one frame's detections to this camera's watcher.

    `state` is the unified detector's per-camera dict and holds the _Camera.
    `t` is the frame's capture time (time.time() clock). `session` (e.g. the exam
    and shift) scopes the saved home spots; None never reuses them.
    Returns [(image, event_type)] for every alert whose evidence wait ended.
    """
    cam = state.get("camera")
    if cam is None:
        profiles = {name: Params.from_dict({**p, "cooldown_seconds": cooldown_s}) for name, p in PROFILES.items()}
        cam = state["camera"] = _Camera(camera_id, profiles, _homes_dir(session))
    dets = to_frame_detections(result)
    weak = weak_monitors(result)
    alerts = cam.process(frame, dets, t, weak)
    if SAVE_ALL_FRAMES and (alerts or t - state.get("_last_frame_save", 0.0) >= SAVE_EVERY_S):
        state["_last_frame_save"] = t
        try:
            _save_checked_frame(cam, frame, dets, t, session, [a[0].type for a in alerts], weak)
        except Exception as e:
            logger.error(f"[MonitorTamper] {camera_id}: could not save checked frame: {e}")
    return _render(cam, alerts)


_STATE_COLOURS = {"OK": (0, 200, 0), "OCCLUDED": (0, 215, 255), "EMPTY": (0, 0, 255)}


def _save_checked_frame(cam, frame, dets: FrameDetections, t: float, session, alert_types, weak=()) -> None:
    """Write the frame as the analytic saw it: detected monitors (cyan) and persons
    (magenta), and - once home spots are learned - every spot coloured by its state
    (OK green / person in front yellow / EMPTY red, with its empty-frame count)."""
    img = frame.copy()
    for d in dets.persons:
        x1, y1, x2, y2 = map(int, d.box)
        cv2.rectangle(img, (x1, y1), (x2, y2), (255, 0, 255), 1)
    for d in dets.monitors:
        x1, y1, x2, y2 = map(int, d.box)
        cv2.rectangle(img, (x1, y1), (x2, y2), (255, 255, 0), 1)
    for d in weak:                                   # weak monitor boxes (fallback input): orange, dashed-looking thin
        x1, y1, x2, y2 = map(int, d.box)
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 140, 255), 1)
        cv2.putText(img, f"{d.score:.2f}", (x1, max(y1 - 2, 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 140, 255), 1)
    watcher = getattr(cam, "watcher", None)
    if watcher is not None:
        phase = "WATCHING " + ("steady" if cam.params is cam.profiles.get("steady") else "sparse")
        for sp in watcher.spots:
            x1, y1, x2, y2 = map(int, sp.home.box)
            col = _STATE_COLOURS.get(sp.state, (255, 255, 255))
            cv2.rectangle(img, (x1, y1), (x2, y2), col, 2)
            label = f"{sp.home.id}" + (f" e{sp.empty_obs}" if sp.state == "EMPTY" else "")
            cv2.putText(img, label, (x1 + 2, max(y1 - 3, 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, col, 1, cv2.LINE_AA)
        counts = watcher.state_counts()
        status = f"{phase}  OK={counts['OK']} OCC={counts['OCCLUDED']} EMPTY={counts['EMPTY']}"
    else:
        status = f"LEARNING home spots ({len(getattr(cam, '_capture_frames', []) or [])} frames so far)"
    when = datetime.fromtimestamp(t).astimezone()
    head = f"{cam.camera_id}  {when.strftime('%H:%M:%S')}  {status}  mon={len(dets.monitors)} weak={len(weak)} per={len(dets.persons)}"
    if alert_types:
        head += "  ALERT:" + ",".join(alert_types)
    cv2.rectangle(img, (0, 0), (img.shape[1], 24), (0, 0, 0), -1)
    cv2.putText(img, head, (6, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    out = os.path.join(FRAMES_DIR, session or "no_session", cam.camera_id)
    os.makedirs(out, exist_ok=True)
    cv2.imwrite(os.path.join(out, when.strftime("%Y%m%d_%H%M%S_%f")[:-3] + ".jpg"), img,
                [cv2.IMWRITE_JPEG_QUALITY, 80])


def flush(state: dict, now: float | None) -> list:
    """Alerts held for evidence that are due by `now` (None: all of them, at shutdown)."""
    cam = state.get("camera")
    return _render(cam, cam.flush(now)) if cam is not None else []


def _render(cam: _Camera, alerts) -> list:
    out = []
    for event, frame, evidence in alerts:
        try:
            write_alert(event, frame, cam.home_map, ALERTS_DIR, evidence)
        except Exception as e:
            logger.error(f"[MonitorTamper] {cam.camera_id}: could not write alert files: {e}")
        logger.info(f"[MonitorTamper] {cam.camera_id}: {event.type} (evidence: {evidence.basis}, "
                    f"{len(evidence.frames)} frames)")
        out.append((compose_report_image(event, frame, cam.home_map, evidence), event.type))
    return out


def compose_report_image(event: Event, frame, home_map: HomeMap, evidence: Evidence):
    """The annotated trigger frame (empty desk red, moved monitor orange) with the best
    evidence frame (desk red, monitor orange, culprit magenta) stacked under it."""
    top = draw_annotated(event, frame, home_map)
    if not evidence.frames:
        return top
    best = max(evidence.frames, key=lambda f: f.score)
    when = datetime.fromtimestamp(best.t).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    header = f"EVIDENCE {BASIS_LABEL[evidence.basis]}  {event.camera_id}  {when}"
    bottom = draw_incident(decode(best.jpeg), best.monitor_box, best.culprit_box, evidence.desk_box, header)
    if bottom.shape[1] != top.shape[1]:
        bottom = cv2.resize(bottom, (top.shape[1], round(bottom.shape[0] * top.shape[1] / bottom.shape[1])))
    bar = np.full((SEPARATOR_H, top.shape[1], 3), 128, dtype=np.uint8)
    return np.vstack([top, bar, bottom])
