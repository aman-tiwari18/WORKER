import cv2
import time
import os
import random
import json
import re
_DET_GPU_ID = int(os.environ["WORKER_INDEX"]) if os.environ.get("WORKER_INDEX", "").strip().isdigit() else int("".join(c for c in os.environ.get("WORKER_ID", "main").split("-")[-1] if c.isdigit()) or "0")
import logging
import threading
import torch
import numpy as np
from datetime import datetime
from ultralytics import YOLO

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= CONFIG ==================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# UNIFIED_PERSON_MODEL=finetuned -> the fine-tuned person+monitor model (person = class 0)
from detection_models import person_model_path
MODEL_PATH = person_model_path(os.path.join(BASE_DIR, "models", "yolov10m.pt"))
SAVE_FOLDER = os.path.join(BASE_DIR, "results/invigilator_detection")

# CAMERA CLASSIFICATION: Inside Lab
# This detection runs on cameras classified as:
# - Computer Lab / Inside Lab / Exam Lab / Inside Classroom

# --- Standing-person classification (used by both ABSENT and STATIC) ---
CONF_THRESHOLD = 0.40          # Tightened from 0.30 — weak phantom standing shapes were polluting the tracker
MIN_HEIGHT = 100               # Minimum bbox height (px) to be considered standing.
                               # Original mentor-set value; below this is YOLO noise from far-away phantoms.
STANDING_ASPECT_RATIO = 2.5    # h/w >= 2.5 → "standing" pose. Tightened from 2.3 because half-body
                               # seated bboxes of upright students (head + torso + arms at desk) were
                               # leaking through at h/w ~2.4-2.5. A standing invigilator with arms
                               # relaxed has h/w ~3-4; arms-out / oblique poses ~2.3-2.7. Brief
                               # rejection on arms-out frames is safe — the 30 s percentile window
                               # (>= 90 % inside radius) absorbs short detection gaps without
                               # resetting the 7-min STATIC timer. This is the practical ceiling
                               # of bbox-only standing classification; going further requires pose
                               # estimation, which no script in this codebase currently uses.
STATIC_MIN_ASPECT_RATIO = 2.0  # STATIC only: h/w still accepted for a person ALREADY anchored at a spot.
                               # An invigilator leaning on a desk sits at h/w ~2.4-2.6 and flickers under
                               # 2.5, so checkpoints missed him and the 7-min timer reset. New anchors
                               # and ABSENT still require STANDING_ASPECT_RATIO.

# 7 Oct 2026 - standing = foreshortened people too. From a ceiling-height camera a standing person
# is NOT 2.5x taller than wide: on 60 of today's frames the walking / standing staff were h/w
# 1.9-2.4 and every one was rejected by STANDING_ASPECT_RATIO (ABSENT fired with an invigilator
# in view). Seated candidates seen from behind reach ~1.7 and a few side views ~1.9-2.0, so the
# soft rule stops at 2.0 and also needs the person to be at least SOFT_STANDING_REL_MIN x the
# height of the other people at the same depth in the frame (a seated person is not taller than
# his neighbours). A box cut by the left/right frame edge is not a reliable person (a person
# half outside the frame is very narrow, i.e. looks "standing") and is ignored.
SOFT_STANDING_ASPECT_RATIO = float(os.environ.get("INVIGILATOR_SOFT_ASPECT", "2.0"))
SOFT_STANDING_REL_MIN      = float(os.environ.get("INVIGILATOR_SOFT_REL_MIN", "1.0"))
REL_DEPTH_BAND             = 0.15   # neighbours: box centre within 15 % of the frame height
REL_MIN_NEIGHBOUR_H        = 60     # ...and at least this tall
EDGE_MARGIN_PX             = 4      # a box touching the left/right edge within this is partial


def standing_flags(boxes_xyxy, frame_w=None, frame_h=None, soft=True):
    """Which of these person boxes (xyxy, already class/confidence filtered) are STANDING.

    sure   : h >= MIN_HEIGHT and h/w >= STANDING_ASPECT_RATIO
    soft   : h >= MIN_HEIGHT and h/w >= SOFT_STANDING_ASPECT_RATIO and not shorter than the people
             around it (see SOFT_STANDING_REL_MIN); the check is skipped when the frame has fewer
             than 2 neighbours
    Boxes touching the left/right frame edge are never standing. Returns list[bool].
    soft=False: only the "sure" rule. STATIC anchors use that: the soft rule also lets a seated
    candidate with a tall box (chair, monitor) through, and candidates sit still for 7 minutes."""
    boxes = [tuple(float(v) for v in b[:4]) for b in boxes_xyxy]
    heights = [b[3] - b[1] for b in boxes]
    out = []
    for i, (x1, y1, x2, y2) in enumerate(boxes):
        h = heights[i]; w = max(x2 - x1, 1.0)
        if h < MIN_HEIGHT or (frame_w and (x1 <= EDGE_MARGIN_PX or x2 >= frame_w - EDGE_MARGIN_PX)):
            out.append(False); continue
        if h / w >= STANDING_ASPECT_RATIO:
            out.append(True); continue
        if not soft or h / w < SOFT_STANDING_ASPECT_RATIO:
            out.append(False); continue
        yc = (y1 + y2) / 2.0
        band = REL_DEPTH_BAND * frame_h if frame_h else None
        near = [heights[j] for j, b in enumerate(boxes)
                if j != i and heights[j] >= REL_MIN_NEIGHBOUR_H
                and (band is None or abs((b[1] + b[3]) / 2.0 - yc) <= band)]
        if len(near) >= 2:
            near.sort(); med = near[len(near) // 2]
            out.append(h >= SOFT_STANDING_REL_MIN * med)
        else:
            out.append(True)
    return out

# --- ABSENT violation (no invigilator in the room) ---
# 13 Oct 2026: ABSENT now needs the room to be without a standing person for 7 minutes (was 45 s, which fired
# on the second empty frame). Time when the camera was not seen (> MAX_CHECK_GAP_SEC between frames) never
# counts as "room empty": the clock restarts, so a long gap cannot turn into an instant ABSENT.
NO_TARGET_TIME_THRESH = int(os.environ.get("INVIGILATOR_ABSENT_SEC", "420"))   # Seconds of zero-standing-person before ABSENT fires

# --- STATIC violation (checkpoint-based, same person at the same spot) ---
# Instead of following every frame, a checkpoint is taken every CHECK_INTERVAL_SEC. The first
# checkpoint that sees a standing person anchors their foot position + clothing colour signature;
# each later checkpoint asks "is the SAME person still standing at that spot?". This works with
# round-robin capture (a camera is revisited every 10-60 s), survives short occlusions, and the
# colour check stops a different person who steps into the spot from inheriting the timer.
STATIC_TIME_THRESH    = 420   # Seconds — 7 min at the same spot before STATIC fires (per client spec)
CHECK_INTERVAL_SEC    = 60    # One checkpoint per ~minute per camera, jittered ±25 % so someone
                              # pacing on a regular loop isn't sampled at the same spot every time
MAX_CHECK_GAP_SEC     = 180   # Camera unseen longer than this → continuity unknown, drop all anchors
MISS_STREAK_RESET     = 2     # Consecutive checkpoints without the same person at the spot → moved, reset
MIN_HITS              = 3     # Checkpoints the person must have been seen at the spot
MIN_HIT_RATIO         = 0.80  # ...and the fraction of checkpoints they were seen at
CONFIRM_HIT_STREAK    = 2     # The latest N checkpoints must all be hits — one lucky pass of a
                              # walking person through the spot can't re-confirm a stale anchor
ANCHOR_RADIUS_FLOOR   = 40    # px — foot must be within max(floor, bbox_h * ratio) of the anchor
ANCHOR_RADIUS_RATIO   = 0.25
MAX_ANCHORS           = 20    # Per camera; bounds work if a scene produces many standing boxes
CHECK_WINDOW_SEC      = 10    # A checkpoint is judged on every frame within ± this many seconds of it
                              # (5 fps feed: the frames around it; round-robin: the burst), not on the
                              # checkpoint frame alone, so one bad frame — box cut short, pose under
                              # the ratio — isn't a miss
CHECK_MIN_SEEN_FRAC   = 0.6   # ...and is a hit if the person was matched at the spot in at least this
                              # share of those frames. Someone standing there is matched in most of
                              # them (0.84-1.0 on real lab footage); someone walking through the spot
                              # only for the ~2 s it takes to cross it, and someone coming and going
                              # every 20-40 s about half of them — neither counts as staying.
                              # Round-robin burst of 2 frames: the first frame must match.

# --- Same-person check (clothing colour signature) ---
# HSV hue/saturation histogram of torso and legs, compared with Bhattacharyya distance
# (0 = identical, 1 = unrelated). Tuned on real lab footage: 0.40 keeps ~99 % of same-person
# checks and rejects ~90 % of different people per checkpoint; over the 7-min run of checkpoints
# a swapped-in person rarely inherits the timer. Weak spot: near-identical clothing
# (same-colour vests / dark jackets).
APPEARANCE_MAX_DIST   = 0.40
GALLERY_SIZE          = 3     # Anchor signature + the next confirmed matches, compared by min distance

# --- Checkpoint state on disk (JSON, one file per camera) ---
# Written after every checkpoint, read back when a camera's tracker is created, so a worker
# restart mid-shift keeps the 7-min timers. Each anchor also keeps its recent checkpoint results
# (position, px moved from the anchor, colour distance, hit/miss) for review.
# INVIGILATOR_STATE_DIR="" turns it off (state stays in memory only).
STATE_DIR         = os.environ.get("INVIGILATOR_STATE_DIR", os.path.join(BASE_DIR, "results/invigilator_state"))
STATE_HISTORY_LEN = 20        # Checkpoint results kept per anchor
# Saved anchors that started before this epoch time are ignored when a tracker is created (0 = keep all).
# 13 Oct 2026: set at deploy time to drop the anchors the old soft rule put on seated candidates.
IGNORE_ANCHORS_BEFORE = float(os.environ.get("INVIGILATOR_IGNORE_ANCHORS_BEFORE", "0") or 0)

os.makedirs(SAVE_FOLDER, exist_ok=True)
if STATE_DIR:
    os.makedirs(STATE_DIR, exist_ok=True)

# ================= YOLO ====================
yolo_model = None
_model_lock = threading.Lock()

def get_yolo_model():
    """Return a shared YOLO model instance."""
    global yolo_model
    if yolo_model is None:
        with _model_lock:
            if yolo_model is None:
                logger.info(f"Loading YOLO model from {MODEL_PATH}...")
                yolo_model = YOLO(MODEL_PATH)
                if torch.cuda.is_available():
                    yolo_model.to('cuda')
                    logger.info("YOLO model moved to CUDA")
    return yolo_model

# ================= TRACKING =================
def appearance_signature(frame, box):
    """Clothing colour signature of one person box: (torso_hist, legs_hist), or None if the crop is empty.
    Crop is normalised to 64x160; left/right 15 % (background) and the head (top 15 %) are skipped."""
    fh, fw = frame.shape[:2]
    x1, y1 = max(int(box[0]), 0), max(int(box[1]), 0)
    x2, y2 = min(int(box[2]), fw), min(int(box[3]), fh)
    if x2 - x1 < 4 or y2 - y1 < 8:
        return None
    crop = cv2.resize(frame[y1:y2, x1:x2], (64, 160))
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)

    def _hist(part):
        h = cv2.calcHist([part], [0, 1], None, [16, 8], [0, 180, 0, 256])
        # Spread each bin into its neighbours so a small colour shift — or red, whose hue
        # wraps around 0/180 — doesn't land the same clothing in an unrelated bin.
        h = h + 0.5 * (np.roll(h, 1, axis=0) + np.roll(h, -1, axis=0))
        h = h + 0.25 * (np.pad(h, ((0, 0), (1, 0)), mode="edge")[:, :-1] +
                        np.pad(h, ((0, 0), (0, 1)), mode="edge")[:, 1:])
        h = h.astype(np.float32)
        return cv2.normalize(h, h).flatten()

    return _hist(hsv[24:88, 9:55]), _hist(hsv[88:152, 9:55])


def appearance_distance(a, b):
    """0 = identical clothing colours, 1 = unrelated (mean Bhattacharyya of torso + legs)."""
    return 0.5 * (cv2.compareHist(a[0], b[0], cv2.HISTCMP_BHATTACHARYYA) +
                  cv2.compareHist(a[1], b[1], cv2.HISTCMP_BHATTACHARYYA))


class PersonTracker:
    """
    Per-camera invigilator state.

      ABSENT → fires when zero standing persons are detected for
               NO_TARGET_TIME_THRESH continuous seconds.
      STATIC → fires when the SAME person has stood at the same spot for
               STATIC_TIME_THRESH seconds, judged at checkpoints taken every
               CHECK_INTERVAL_SEC.

    At a checkpoint each anchor (foot + head position, colour gallery) is checked:
    the anchor's person counts as seen when a box sits at the stored spot — foot
    OR top of box within the anchor radius, since a desk or a seated student can
    hide the legs — and its colours match the gallery. The checkpoint is a hit if
    they were seen in at least CHECK_MIN_SEEN_FRAC of the frames within
    CHECK_WINDOW_SEC of it, otherwise a miss.
    MISS_STREAK_RESET misses in a row drop the anchor.
    Standing boxes that matched no anchor start new anchors — so a different
    person stepping into the spot starts their own timer instead of inheriting one.

    Only boxes passing STANDING_ASPECT_RATIO start anchors and drive ABSENT.
    `extra_boxes` (STATIC_MIN_ASPECT_RATIO <= h/w < STANDING_ASPECT_RATIO) are used
    only to keep following an already-anchored person; ABSENT never sees them.

    Once an anchor is confirmed (7 min elapsed, enough hits, latest
    CONFIRM_HIT_STREAK checkpoints all hits) STATIC stays true on every frame where that person is still at the
    spot, so the reporting gate (consecutive hits + cooldown) sees a stable
    signal and decides the save cadence.

    With a `state_path`, the anchors (position, counters, colour gallery and the
    last STATE_HISTORY_LEN checkpoint results) are written to that JSON file after
    every checkpoint and read back on creation, so a worker restart keeps the timers.
    """
    def __init__(self, state_path=None):
        self.no_target_start = None
        self.last_frame_t = None   # time of the previous frame: tells an unseen camera from an empty room
        self.anchors = []
        self.last_check = None
        self.next_check = None
        self.state_path = state_path
        self._load()

    @staticmethod
    def _foot(box):
        return (float(box[0] + box[2]) / 2.0, float(box[3]))

    @staticmethod
    def _top(box):
        return (float(box[0] + box[2]) / 2.0, float(box[1]))

    @staticmethod
    def _moved(anchor, foot):
        """Pixel distance of a foot point from the anchor's stored position."""
        return ((foot[0] - anchor['x']) ** 2 + (foot[1] - anchor['y']) ** 2) ** 0.5

    @classmethod
    def _near(cls, anchor, foot, top=None):
        radius = max(ANCHOR_RADIUS_FLOOR, anchor['h'] * ANCHOR_RADIUS_RATIO)
        if cls._moved(anchor, foot) < radius:
            return True
        # Legs hidden behind a desk or a seated student cut the box short: the foot point
        # jumps up while the top of the box (head) stays where it was.
        if top is None or anchor.get('ty') is None:
            return False
        return ((top[0] - anchor['tx']) ** 2 + (top[1] - anchor['ty']) ** 2) ** 0.5 < radius

    @staticmethod
    def _colour_dist(anchor, sig):
        """Distance to the closest gallery signature, or None if there is no crop."""
        return None if sig is None else min(appearance_distance(g, sig) for g in anchor['gallery'])

    @classmethod
    def _same_person(cls, anchor, sig):
        d = cls._colour_dist(anchor, sig)
        return d is not None and d < APPEARANCE_MAX_DIST

    @classmethod
    def _people(cls, boxes, frame):
        """[(box, strict)] -> per-person dicts; colour signatures are computed on first use."""
        return [{'box': b, 'strict': strict, 'frame': frame, 'foot': cls._foot(b), 'top': cls._top(b),
                 'h': max(float(b[3] - b[1]), 1.0)} for b, strict in boxes]

    @staticmethod
    def _sig(person):
        if 'sig' not in person:
            person['sig'] = appearance_signature(person['frame'], person['box'])
        return person['sig']

    def _match(self, anchor, people):
        """Index of the first person at the anchor's spot wearing the anchor's colours, else None."""
        for i, p in enumerate(people):
            if self._near(anchor, p['foot'], p['top']) and self._same_person(anchor, self._sig(p)):
                return i
        return None

    @staticmethod
    def _seen_frac(a, t0, t1):
        """Share of this camera's frames in [t0, t1] where the anchor's person was matched at the spot."""
        frames = [m for t, m in a.get('seen', []) if t0 <= t <= t1]
        return sum(frames) / len(frames) if frames else 0.0

    def _record(self, a, t, hit, foot=None, sig=None, frac=None):
        """Count one checkpoint result for anchor `a`. foot/sig: where the person was seen, for review."""
        a['checks'] += 1
        cd = self._colour_dist(a, sig)
        a['history'] = (a['history'] + [{
            't': round(t, 1), 'hit': hit, 'seen_frac': None if frac is None else round(frac, 2),
            'x': None if foot is None else round(foot[0], 1),
            'y': None if foot is None else round(foot[1], 1),
            'moved_px': None if foot is None else round(self._moved(a, foot), 1),
            'colour_dist': None if cd is None else round(cd, 3),
        }])[-STATE_HISTORY_LEN:]
        if not hit:
            a['miss_streak'] += 1
            a['hit_streak'] = 0
            return
        a['hits'] += 1
        a['miss_streak'] = 0
        a['hit_streak'] += 1
        if sig is not None and len(a['gallery']) < GALLERY_SIZE:
            a['gallery'].append(sig)

    def _settle(self, a, now=None):
        """Decide a pending checkpoint on the frames around it (all of them up to `now`)."""
        t = a['pending']
        frac = self._seen_frac(a, t - CHECK_WINDOW_SEC, t + CHECK_WINDOW_SEC if now is None else now)
        if frac >= CHECK_MIN_SEEN_FRAC:
            self._record(a, t, True, a.get('last_foot'), a.get('last_sig'), frac)
        else:
            self._record(a, t, False, *a.get('pending_seen', (None, None)), frac)
        a['pending'] = None

    def _follow(self, now, people):
        """Every frame: note whether each anchor's person is at the spot, and settle checkpoints
        whose window (CHECK_WINDOW_SEC after them) has passed."""
        settled = False
        for a in self.anchors:
            i = self._match(a, people)
            if i is not None:
                a['last_seen'] = now
                a['last_foot'] = people[i]['foot']
                a['last_sig'] = self._sig(people[i])
            a['seen'] = [(t, m) for t, m in a.get('seen', []) if now - t <= 2 * CHECK_WINDOW_SEC]
            a['seen'].append((now, i is not None))
            if a.get('pending') is not None and now - a['pending'] > CHECK_WINDOW_SEC:
                self._settle(a)
                settled = True
        if settled:
            self.anchors = [a for a in self.anchors if a['miss_streak'] < MISS_STREAK_RESET]
            self._save(now)

    def _checkpoint(self, now, people):
        # Camera unseen for too long → can't vouch for continuity.
        if self.last_check is not None and now - self.last_check > MAX_CHECK_GAP_SEC:
            self.anchors = []
        self.last_check = now
        self.next_check = now + CHECK_INTERVAL_SEC * random.uniform(0.75, 1.25)

        matched = set()
        for a in self.anchors:
            if a.get('pending') is not None:   # previous window still open: decide on what it has
                self._settle(a, now)
            hit = self._match(a, people)
            if hit is not None:
                matched.add(hit)
            frac = self._seen_frac(a, now - CHECK_WINDOW_SEC, now)
            if frac >= CHECK_MIN_SEEN_FRAC:
                self._record(a, now, True, a.get('last_foot'), a.get('last_sig'), frac)
            else:
                # Too few sightings in the frames before the checkpoint: decide once the frames
                # after it are in too. Keep the person closest to the spot now, to record where
                # they were if it ends as a miss.
                seen = min(people, key=lambda p: self._moved(a, p['foot']), default=None)
                a['pending'] = now
                a['pending_seen'] = (None, None) if seen is None else (seen['foot'], self._sig(seen))
        self.anchors = [a for a in self.anchors if a['miss_streak'] < MISS_STREAK_RESET]

        for i, p in enumerate(people):
            if i in matched or not p['strict'] or len(self.anchors) >= MAX_ANCHORS:
                continue
            sig = self._sig(p)
            if sig is None:
                continue
            foot, top = p['foot'], p['top']
            self.anchors.append({
                'x': foot[0], 'y': foot[1], 'tx': top[0], 'ty': top[1], 'h': p['h'], 'gallery': [sig],
                'start': now, 'checks': 1, 'hits': 1, 'miss_streak': 0, 'hit_streak': 1,
                'last_seen': now, 'last_foot': foot, 'last_sig': sig, 'seen': [(now, True)], 'pending': None,
                'history': [{'t': round(now, 1), 'hit': True, 'x': round(foot[0], 1),
                             'y': round(foot[1], 1), 'moved_px': 0.0, 'colour_dist': 0.0}],
            })

        self._save(now)

    # ---- JSON state ---------------------------------------------------------
    def _save(self, now):
        if not self.state_path:
            return
        state = {
            'updated_at': datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S'),
            'last_check': now,
            'anchors': [{
                'x': round(float(a['x']), 1), 'y': round(float(a['y']), 1), 'h': round(float(a['h']), 1),
                'tx': None if a.get('tx') is None else round(float(a['tx']), 1),
                'ty': None if a.get('ty') is None else round(float(a['ty']), 1),
                'start': a['start'],
                'start_at': datetime.fromtimestamp(a['start']).strftime('%Y-%m-%d %H:%M:%S'),
                'stood_sec': round(now - a['start'], 1),
                'last_seen': a.get('last_seen'),
                'checks': a['checks'], 'hits': a['hits'],
                'miss_streak': a['miss_streak'], 'hit_streak': a['hit_streak'],
                'history': a['history'],
                # colour gallery last: long float lists (torso hist, legs hist) per signature
                'gallery': [[np.round(part, 5).tolist() for part in sig] for sig in a['gallery']],
            } for a in self.anchors],
        }
        tmp = f"{self.state_path}.tmp"
        try:
            with open(tmp, 'w') as f:
                json.dump(state, f, indent=1)
            os.replace(tmp, self.state_path)   # atomic: readers never see a half-written file
        except Exception as e:
            _warn_state_io(f"could not write {self.state_path}: {e}")
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _load(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path) as f:
                state = json.load(f)
            anchors = []
            for a in state.get('anchors', []):
                a['gallery'] = [tuple(np.asarray(part, dtype=np.float32) for part in sig)
                                for sig in a['gallery']]
                for k in ('start_at', 'stood_sec'):
                    a.pop(k, None)
                if a.get('start', 0) < IGNORE_ANCHORS_BEFORE:
                    continue        # anchored before the cutoff (e.g. on seated candidates by the old soft rule)
                anchors.append(a)
            self.anchors = anchors
            # Stale state (camera unseen > MAX_CHECK_GAP_SEC) is dropped by the next checkpoint.
            self.last_check = state.get('last_check')
        except Exception as e:
            self.anchors, self.last_check = [], None
            _warn_state_io(f"could not read {self.state_path}, starting fresh: {e}")

    def update(self, person_boxes, frame, extra_boxes=(), soft_boxes=()):
        """
        person_boxes : list of [x1,y1,x2,y2] for STANDING-classified boxes.
        frame        : the un-annotated frame the boxes came from (for colour signatures).
        extra_boxes  : boxes with STATIC_MIN_ASPECT_RATIO <= h/w < STANDING_ASPECT_RATIO. STATIC only:
                       they keep following an anchored person but never start an anchor,
                       and ABSENT never sees them.
        Returns (absent_violation: bool, static_violation: bool, static_feet: list[(fx,fy)]).
        `static_feet` holds the foot point of every box that is a confirmed STATIC
        person in this frame, so the caller can highlight only that subject.
        """
        now = time.time()
        if self.last_frame_t is not None and now - self.last_frame_t > MAX_CHECK_GAP_SEC:
            self.no_target_start = None        # camera unseen: we do not know the room was empty meanwhile
        self.last_frame_t = now

        people = self._people([(b, True) for b in person_boxes] + [(b, False) for b in extra_boxes], frame)
        self._follow(now, people)
        if self.next_check is None or now >= self.next_check:
            self._checkpoint(now, people)

        # --- ABSENT branch (standing boxes only) ---------------------------
        if not person_boxes and not len(soft_boxes):
            if self.no_target_start is None:
                self.no_target_start = now
            absent = (now - self.no_target_start) > NO_TARGET_TIME_THRESH
        else:
            # At least one standing person → disarm ABSENT clock
            self.no_target_start = None
            absent = False

        # --- STATIC branch -------------------------------------------------
        # The last CHECK_WINDOW_SEC must also show them staying there, as at a checkpoint —
        # someone who left after the last checkpoint and only walks back through the spot
        # is not STATIC.
        confirmed = [a for a in self.anchors
                     if a['hit_streak'] >= CONFIRM_HIT_STREAK
                     and now - a['start'] >= STATIC_TIME_THRESH
                     and a['hits'] >= MIN_HITS
                     and a['hits'] / a['checks'] >= MIN_HIT_RATIO
                     and self._seen_frac(a, now - CHECK_WINDOW_SEC, now) >= CHECK_MIN_SEEN_FRAC]
        static_feet = []
        for p in people:
            near = [a for a in confirmed if self._near(a, p['foot'], p['top'])]
            if near and any(self._same_person(a, self._sig(p)) for a in near):
                static_feet.append(p['foot'])

        return absent, bool(static_feet), static_feet

# ================= STATE ===================
trackers = {}
_state_lock = threading.Lock()
_last_state_warn = [0.0]


def _warn_state_io(msg):
    """State-file problems never stop detection; warn at most once a minute."""
    if time.time() - _last_state_warn[0] >= 60:
        _last_state_warn[0] = time.time()
        logger.warning(f"[Invigilator] state JSON: {msg}")


def _new_tracker(key):
    path = None
    if STATE_DIR:
        path = os.path.join(STATE_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")
    return PersonTracker(state_path=path)

# =====================================================
# CORE DETECTION LOGIC
# =====================================================
def _split_person_boxes(boxes, frame_size=None):
    """Person boxes -> (standing, standing_confs, extra, extra_confs, soft_boxes).
    standing : the sure rule (h/w >= STANDING_ASPECT_RATIO); drives ABSENT and starts STATIC anchors.
    soft_boxes : (13 Oct) h/w >= 2.0 and not shorter than the neighbours; they are in `extra` too (they
               may follow an anchor) and count as "someone is standing" for ABSENT only - they never
               start a STATIC anchor, so seated candidates cannot raise STATIC.
    extra    : height >= MIN_HEIGHT AND STATIC_MIN_ASPECT_RATIO <= h/w < STANDING_ASPECT_RATIO and
               not standing - only let STATIC keep following an already-anchored person.
    frame_size : (width, height) of the frame; enables the frame-edge and same-depth checks."""
    cand = []
    for box in boxes:
        if int(box.cls[0]) != 0 or float(box.conf[0]) < CONF_THRESHOLD:
            continue
        cand.append((box.xyxy[0].cpu().numpy(), float(box.conf[0])))
    fw, fh = (frame_size if frame_size else (None, None))
    boxes_xy = [c for c, _ in cand]
    flags_sure = standing_flags(boxes_xy, fw, fh, soft=False)
    flags_any = standing_flags(boxes_xy, fw, fh, soft=True)
    standing, standing_confs, extra, extra_confs, soft_boxes = [], [], [], [], []
    for (coords, conf), is_sure, is_any in zip(cand, flags_sure, flags_any):
        h = coords[3] - coords[1]
        w = max(coords[2] - coords[0], 1.0)
        if h < MIN_HEIGHT:
            continue
        if is_sure:
            standing.append(coords); standing_confs.append(conf)
        elif fw and (coords[0] <= EDGE_MARGIN_PX or coords[2] >= fw - EDGE_MARGIN_PX):
            continue
        elif (h / w) >= STATIC_MIN_ASPECT_RATIO:
            extra.append(coords); extra_confs.append(conf)
            if is_any:
                soft_boxes.append(coords)       # counts as "someone is standing" for ABSENT only
    return standing, standing_confs, extra, extra_confs, soft_boxes


def _draw_person_boxes(frame, person_boxes, person_confs, extra_boxes, static_violation, static_feet):
    """Box drawing:
    - On STATIC: the track that fired (matched by foot position) gets a thick
      red box with a "STATIC" label — also when it is an extra (leaning) box;
      other standees get thin green boxes. Extra boxes are otherwise not drawn.
    - On ABSENT: no person_boxes exist this frame, so nothing green is drawn.
    - Active: everyone standing in thin green."""
    def is_static_box(x1, y1, x2, y2):
        fx, fy = (x1 + x2) / 2.0, float(y2)
        match_r = max((y2 - y1) * 0.3, 25)
        return static_violation and any(
            ((fx - sx) ** 2 + (fy - sy) ** 2) ** 0.5 < match_r
            for sx, sy in static_feet
        )

    boxes = [(b, c) for b, c in zip(person_boxes, person_confs)] + [(b, None) for b in extra_boxes]
    for box, conf in boxes:
        x1, y1, x2, y2 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
        if is_static_box(x1, y1, x2, y2):
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 3)
            cv2.putText(frame, "STATIC", (x1, max(y1 - 8, 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        elif conf is not None:
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 1)
            cv2.putText(frame, f"Invig {conf:.2f}", (x1, max(y1 - 8, 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)


def detect_invigilator_in_frame(frame, device_id="unknown", camera_no="unknown", script_name="invigilator"):
    """Standalone detection for a single frame."""
    model = get_yolo_model()
    results = model(frame, conf=CONF_THRESHOLD, verbose=False)[0]

    person_boxes, person_confs, extra_boxes, _, soft_boxes = _split_person_boxes(results.boxes, frame_size=(frame.shape[1], frame.shape[0]))

    key = f"{device_id}_{camera_no}"
    with _state_lock:
        if key not in trackers:
            trackers[key] = _new_tracker(key)
        tracker = trackers[key]

    absent, static_violation, static_feet = tracker.update(person_boxes, frame, extra_boxes, soft_boxes)
    violation_detected = absent or static_violation
    status = "ABSENT" if absent else ("STATIC" if static_violation else "Active")

    _draw_person_boxes(frame, person_boxes, person_confs, extra_boxes, static_violation, static_feet)

    # Status overlay
    overlay = frame.copy()
    cv2.rectangle(overlay, (10, 10), (450, 110), (0, 0, 0), -1)
    cv2.addWeighted(frame, 0.7, overlay, 0.3, 0, frame)
    status_color = (0, 0, 255) if violation_detected else (0, 255, 0)
    cv2.putText(frame, f"Invigilator: {status}", (20, 38),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, status_color, 2)
    cv2.putText(frame, f"Standing persons: {len(person_boxes)}", (20, 65),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    cv2.putText(frame, f"{device_id} - Cam {camera_no}", (20, 90),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

    if violation_detected:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        import uuid
        uid = str(uuid.uuid4())
        fname = f"{script_name}_{device_id}_{camera_no}_gpu{_DET_GPU_ID}_{ts}_{uid}.jpg"
        filename = os.path.join(SAVE_FOLDER, fname)
        cv2.imwrite(filename, frame)
        logger.warning(f"[{device_id}-{camera_no}] 🚨 Invigilator {status}: {filename}")

    return frame, violation_detected, 1 if violation_detected else 0

# 14 Oct 2026: STATIC fires only on a person the posture model (posture_rfdetr) calls standing.
# Tested on 13 Oct's 2,252 STATIC alerts: 170 kept, all real standing people; the dropped ones
# were almost all seated candidates. No posture result (model not loaded) = no gate, as before.
POSTURE_STANDING_MIN = float(os.environ.get("INVIGILATOR_POSTURE_STANDING_MIN", "0.6"))
POSTURE_STANDING_CLASS = 1          # posture_rfdetr classes: 0 sitting, 1 standing, 2 unsure
POSTURE_MATCH_IOU = 0.3


def _box_iou(a, b):
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def posture_gate(static_feet, boxes, posture):
    """Keep the STATIC persons (foot points) whose person box the posture model calls standing.
    boxes: the person boxes the tracker saw this frame; posture: [(xyxy, class, conf)] or None."""
    if not static_feet or posture is None:
        return static_feet
    standing = [b for b, c, conf in posture if c == POSTURE_STANDING_CLASS and conf >= POSTURE_STANDING_MIN]
    kept = []
    for foot in static_feet:
        box = next((b for b in boxes
                    if abs(PersonTracker._foot(b)[0] - foot[0]) < 1.0 and abs(PersonTracker._foot(b)[1] - foot[1]) < 1.0), None)
        if box is not None and any(_box_iou([float(v) for v in box[:4]], sb) >= POSTURE_MATCH_IOU for sb in standing):
            kept.append(foot)
    return kept


class _InvOut(tuple):
    """(frame, violation_detected, count) - unpacks exactly like before - plus `.issue`:
    'no_targets' (ABSENT: no invigilator), 'static_person' (STATIC: not moving) or ''. The unified detector
    puts it in the saved file name so the Detections page can filter by it."""
    issue = ""


def _inv_out(frame, violation, count, issue):
    out = _InvOut((frame, violation, count))
    out.issue = issue
    return out


def detect_invigilator_batch(items, results=None):
    """Run true batch inference."""
    if results is None:
        model = get_yolo_model()
        frames = [item['frame'].copy() for item in items]
        results = model(frames, conf=CONF_THRESHOLD, verbose=False)

    outputs = []
    for item, result in zip(items, results):
        frame = item['frame'].copy()
        person_boxes, person_confs, extra_boxes, _, soft_boxes = _split_person_boxes(result.boxes, frame_size=(frame.shape[1], frame.shape[0]))

        device_id = item.get('device_id', 'unknown')
        camera_no = item.get('ch_no', 'unknown')
        key = f"{device_id}_{camera_no}"

        with _state_lock:
            if key not in trackers:
                trackers[key] = _new_tracker(key)
            tracker = trackers[key]

        absent, static_violation, static_feet = tracker.update(person_boxes, frame, extra_boxes, soft_boxes)
        static_feet = posture_gate(static_feet, list(person_boxes) + list(extra_boxes), item.get('posture'))
        static_violation = bool(static_feet)
        violation_detected = absent or static_violation
        status = "ABSENT" if absent else ("STATIC" if static_violation else "Active")

        _draw_person_boxes(frame, person_boxes, person_confs, extra_boxes, static_violation, static_feet)

        # Status overlay
        overlay = frame.copy()
        cv2.rectangle(overlay, (10, 10), (450, 110), (0, 0, 0), -1)
        cv2.addWeighted(frame, 0.7, overlay, 0.3, 0, frame)
        status_color = (0, 0, 255) if violation_detected else (0, 255, 0)
        cv2.putText(frame, f"Invigilator: {status}", (20, 38),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, status_color, 2)
        cv2.putText(frame, f"Standing persons: {len(person_boxes)}", (20, 65),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.putText(frame, f"CH: {camera_no}", (20, 90),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

        outputs.append(_inv_out(frame, violation_detected, 1 if violation_detected else 0,
                                "no_targets" if absent else ("static_person" if static_violation else "")))

    return outputs
