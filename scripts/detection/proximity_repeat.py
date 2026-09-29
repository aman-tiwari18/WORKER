"""
Repeat counter for close-proximity violations.

When a camera reports a proximity violation, the boxes of every violating pair are
stored in a per-camera JSON file. On that camera's next reported violation (after
the cooldown), each new pair is compared with the stored pairs: if both persons
are at about the same spot as before, it is the same pair still sitting together
and its count goes up (Proximity Violation 1 -> 2 -> 3 ...); a pair not seen
before starts at 1. A pair missing from one report keeps its count for the rest
of the shift, so it continues if the same two persons are seen together again.

State lives in results/proximity_repeat/<session>/<camera>.json (session =
exam{E}_shift{S}), so a worker restart within the shift keeps the counts and a
new shift starts from 1.
"""

import json
import logging
import os
import re
import threading
import time

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPEAT_DIR = os.path.join(BASE_DIR, "results", "proximity_repeat")

# A person counts as "at the same spot" when the centre of their box moved by at
# most this fraction of the box height (seated students barely move; YOLO jitter
# is a few percent of the height).
SAME_SPOT_RATIO = 0.35
MAX_PAIRS_PER_CAMERA = 50   # oldest pairs are dropped beyond this

_lock = threading.Lock()
_cache = {}   # json path -> list of stored pairs


def _box(b):
    return [round(float(v), 1) for v in b[:4]]


def _same_spot(a, b):
    """True when boxes a and b are the same person at (about) the same place."""
    ax, ay = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
    bx, by = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    h = ((a[3] - a[1]) + (b[3] - b[1])) / 2
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 <= max(h, 1.0) * SAME_SPOT_RATIO


def _same_pair(new, old):
    a, b = new
    oa, ob = old["a"], old["b"]
    return (_same_spot(a, oa) and _same_spot(b, ob)) or (_same_spot(a, ob) and _same_spot(b, oa))


def _json_path(camera_key, exam_id, shift_id):
    session = f"exam{exam_id or 0}_shift{shift_id or 0}"
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(camera_key))
    return os.path.join(REPEAT_DIR, session, f"{safe}.json")


def _load(path):
    if path in _cache:
        return _cache[path]
    pairs = []
    try:
        with open(path) as f:
            pairs = json.load(f).get("pairs", [])
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error(f"[ProximityRepeat] could not read {path}: {e}")
    _cache[path] = pairs
    return pairs


def _save(path, camera_key, pairs):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"camera": camera_key, "updated_at": time.time(), "pairs": pairs}, f)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"[ProximityRepeat] could not write {path}: {e}")


def next_violation_count(camera_key, pairs, exam_id=None, shift_id=None):
    """Record a reported proximity violation and return its repeat count.

    camera_key: stable per-camera id (e.g. "rtsp_<device>_<camera_no>", "hls_ch<code>").
    pairs     : [(box_a, box_b), ...] — the [x1, y1, x2, y2] boxes of each violating pair.
    Returns the highest count among this report's pairs (1 = first time seen).
    """
    if not pairs:
        return 1
    path = _json_path(camera_key, exam_id, shift_id)
    now = time.time()
    with _lock:
        stored = _load(path)
        unmatched = list(stored)
        current = []
        for a, b in pairs:
            new = (_box(a), _box(b))
            match = next((old for old in unmatched if _same_pair(new, old)), None)
            if match is not None:
                unmatched.remove(match)
            count = match["count"] + 1 if match else 1
            current.append({"a": new[0], "b": new[1], "count": count, "last_seen": now})

        # Pairs seen now (with their latest position) + pairs not seen this time.
        merged = current + sorted(unmatched, key=lambda p: p.get("last_seen", 0), reverse=True)
        merged = merged[:MAX_PAIRS_PER_CAMERA]
        _cache[path] = merged
        _save(path, camera_key, merged)
        return max(p["count"] for p in current)
