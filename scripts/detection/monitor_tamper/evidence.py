"""Culprit evidence: which frames show the person who moved the monitor.

The watcher decides *whether* a monitor was tampered with; by the time it is
sure (a desk empty for several seconds), the person who carried the monitor
has often walked away and someone else is standing there. So every camera
keeps its recent frames (`FrameBuffer`), and when an alert is due,
`select_evidence` looks back through them for the frames where that monitor
was being carried.

Memory: frames are kept as JPEGs (quality 85, full size), about 250 KB each at
1570x882 (measured on the test clips), so 60 s at 5 fps is about 75 MB per camera,
about 1.9 GB for 25. Encoding takes 3-4 ms per frame in the service loop.

Vendored from monitor_alerts_deploy_v2/monitor_alerts/evidence.py. Changes for the
pipeline, which samples each camera in bursts every few seconds instead of a
steady 5 fps: the buffer keeps the last `seconds` by time (capped at seconds * fps
frames), and "visibly empty" needs the empty run to last EMPTY_CONFIRM_SECONDS in
time, not a fixed number of frames.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np

from .core import Box, Detection, FrameDetections, Params, box_size, centre_distance, coverage, overlap
from .home_map import HomeMap, HomeSpot
from .watcher import Event

EPS = 1e-6  # frame times are float sums of 1/fps; compare them with a little slack
SPOT_EVENTS = {"MONITOR_MISSING", "MONITOR_GONE"}   # anchored on the home spot the monitor left
MOVE_EVENTS = {"MONITOR_OFF_HOME", "MONITOR_NEW", "MONITOR_CARRIED"}   # anchored on a monitor seen away from home
EMPTY_CONFIRM_SECONDS = 1.0  # a desk counts as visibly empty once it stays so this long (one missed person box is not enough)


@dataclass(frozen=True)
class BufferedFrame:
    t: float
    jpeg: bytes
    dets: FrameDetections


class FrameBuffer:
    """The last `seconds` of processed frames of one camera, oldest first."""

    def __init__(self, seconds: float, fps: float, jpeg_quality: int = 85):
        self.seconds = seconds
        # Bounds memory if a camera is fed faster than `fps`.
        self._frames: deque[BufferedFrame] = deque(maxlen=max(1, round(seconds * fps)))
        self._params = [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]

    def push(self, t: float, frame: np.ndarray, dets: FrameDetections) -> None:
        ok, jpeg = cv2.imencode(".jpg", frame, self._params)
        if not ok:
            raise ValueError("could not JPEG-encode the frame")
        self._frames.append(BufferedFrame(t, jpeg.tobytes(), dets))
        # Sparse sampling fills the deque slowly: drop by age, not only by count.
        while self._frames and t - self._frames[0].t > self.seconds + EPS:
            self._frames.popleft()

    def window(self, t0: float, t1: float) -> list[BufferedFrame]:
        return [f for f in self._frames if t0 - EPS <= f.t <= t1 + EPS]

    def clear(self) -> None:
        self._frames.clear()

    def __len__(self) -> int:
        return len(self._frames)


def decode(jpeg: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)


@dataclass
class EvidenceFrame:
    t: float
    jpeg: bytes
    monitor_box: Box | None  # None when the monitor is not visible (last_at_desk)
    culprit_box: Box
    score: float


@dataclass
class Evidence:
    basis: str                                # carrying | last_at_desk | none
    frames: list[EvidenceFrame]               # time order
    path: list[tuple[float, Box, Box | None]] # (t, monitor or desk box, culprit box), for the replay video
    span: tuple[float, float] | None          # first to last evidence time
    desk_box: Box | None                      # the home spot involved, if any


NO_EVIDENCE = Evidence("none", [], [], None, None)  # shared: never mutate


def select_evidence(event: Event, buffer: FrameBuffer, home_map: HomeMap, params: Params) -> Evidence:
    """Pick the frames that show who moved the monitor behind `event` (spec §6)."""
    entries = buffer.window(event.timestamp - params.evidence_lookback_seconds,
                            event.timestamp + params.evidence_delay_seconds)
    if not entries:
        return NO_EVIDENCE
    if event.type in SPOT_EVENTS and event.missing_homes:
        # The watcher lists every long-empty spot, including desks emptied in earlier
        # incidents; prefer a real carry, then a desk whose monitor was seen leaving
        # in this window, the most recent first.
        scored = [_spot_evidence(entries, event.timestamp, spot, home_map, params) for spot in event.missing_homes]
        return max(scored, key=lambda s: s[0])[1]
    if event.type in MOVE_EVENTS and event.off_home_monitors:
        return _move_evidence(entries, event, home_map, params)
    return NO_EVIDENCE


def _spot_evidence(entries: list[BufferedFrame], trigger: float, spot: HomeSpot, home_map: HomeMap,
                   p: Params) -> tuple[tuple, Evidence]:
    """(ranking key, evidence) for a monitor that left `spot`."""
    left, seed, seen = _find_left(entries, trigger, spot, p)
    path = _trace(entries, left, seed, +1, spot.id, home_map, p)
    carries, culprits = _carry_frames(entries, path, spot.box, home_map.frame_size[0], p, home=spot.box)
    # one frame of "monitor inside a person box" can be a detector glitch: trust it from min_carry_frames
    if len(carries) >= p.min_carry_frames:
        rows = [(entries[i].t, det.box, culprits.get(i)) for i, det in path]
        evidence, score = _carrying(carries, rows, spot.box, p), sum(f.score for f in carries)
    else:
        evidence, score = _last_at_desk(entries, left, trigger, spot, p), 0.0
    left_at = entries[left].t if seen else float("-inf")
    return (score, seen, left_at, evidence.basis != "none"), evidence


def _last_at_desk(entries: list[BufferedFrame], left: int, trigger: float, spot: HomeSpot, p: Params) -> Evidence:
    """No frame shows the monitor being carried (it was hidden against the body, say):
    blame whoever stood over the desk last before it became visibly empty, not whoever
    happens to be there when the alert fires. Only that last visit counts: earlier
    visits, separated by the desk standing clear, are other people's."""
    empty = _visibly_empty_from(entries, left, _trigger_index(entries, trigger), spot, p)
    candidates, gap = [], 0
    for e in reversed(entries[:empty + 1]):
        covering = [(coverage(q.box, spot.box), q) for q in e.dets.persons]
        covering = [(c, q) for c, q in covering if c >= p.occlude_cover]
        if covering:
            c, person = max(covering, key=lambda x: x[0])
            candidates.append(EvidenceFrame(e.t, e.jpeg, None, person.box, c * person.score))
            gap = 0
        elif candidates:
            gap += 1
            if gap > p.link_gap_frames:
                break
    candidates.reverse()
    picked: list[EvidenceFrame] = []
    for f in reversed(candidates):  # the latest frames: the person who was there just before it emptied
        if len(picked) == p.evidence_frames:
            break
        if all(abs(f.t - q.t) >= p.evidence_min_spacing_seconds - EPS for q in picked):
            picked.append(f)
    if not picked:
        return NO_EVIDENCE
    picked.reverse()
    span = (picked[0].t, picked[-1].t)
    rows = [(f.t, spot.box, f.culprit_box) for f in candidates if span[0] - EPS <= f.t <= span[1] + EPS]
    return Evidence("last_at_desk", picked, rows, span, spot.box)


def _visibly_empty_from(entries: list[BufferedFrame], left: int, end: int, spot: HomeSpot, p: Params) -> int:
    """First entry after `left` from which the spot stays neither matched nor covered for
    EMPTY_CONFIRM_SECONDS; `end` if that never happens.

    At a steady `fps` this is the same run of round(fps * EMPTY_CONFIRM_SECONDS) frames
    as v2; on sparse bursts it waits for a later visit instead of two frames 40 ms apart."""
    span = EMPTY_CONFIRM_SECONDS - 1.0 / p.fps
    run_start = None
    for i in range(left + 1, end + 1):
        dets = entries[i].dets
        empty = (not any(overlap(m.box, spot.box) >= p.match_overlap for m in dets.monitors)
                 and not any(coverage(q.box, spot.box) >= p.occlude_cover for q in dets.persons))
        if not empty:
            run_start = None
            continue
        if run_start is None:
            run_start = i
        if i > run_start and entries[i].t - entries[run_start].t >= span - EPS:
            return run_start
    return end


def _move_evidence(entries: list[BufferedFrame], event: Event, home_map: HomeMap, p: Params) -> Evidence:
    """A monitor seen moving away from home: follow it backwards and forwards from the trigger."""
    ti = _trigger_index(entries, event.timestamp)
    person = event.nearest_person
    seed = (max(event.off_home_monitors, key=lambda m: overlap(m.box, person.box)) if person
            else event.off_home_monitors[0])
    path = {i: det for i, det in _trace(entries, ti, seed, -1, None, home_map, p)}
    path.update(_trace(entries, ti, seed, +1, None, home_map, p))
    path = sorted(path.items())
    carries, culprits = _carry_frames(entries, path, path[0][1].box, home_map.frame_size[0], p)
    if len(carries) >= p.min_carry_frames:
        rows = [(entries[i].t, det.box, culprits.get(i)) for i, det in path]
        return _carrying(carries, rows, None, p)
    if person is None:
        return NO_EVIDENCE
    t = entries[ti].t
    return Evidence("last_at_desk", [EvidenceFrame(t, entries[ti].jpeg, None, person.box, person.score)],
                    [(t, seed.box, person.box)], (t, t), None)


def _trigger_index(entries: list[BufferedFrame], trigger: float) -> int:
    """The last entry at or before the trigger."""
    before = [i for i, e in enumerate(entries) if e.t <= trigger + EPS]
    return before[-1] if before else 0


def _find_left(entries: list[BufferedFrame], trigger: float, spot: HomeSpot,
               p: Params) -> tuple[int, Detection, bool]:
    """The last entry (going back from the trigger) with the monitor still on its spot, that
    monitor, and True. If it is never seen there: the oldest entry, the spot itself, False."""
    for i in range(_trigger_index(entries, trigger), -1, -1):
        on_spot = [m for m in entries[i].dets.monitors if overlap(m.box, spot.box) >= p.match_overlap]
        if on_spot:
            return i, max(on_spot, key=lambda m: overlap(m.box, spot.box)), True
    return 0, Detection(spot.box, 0.0), False


def _trace(entries: list[BufferedFrame], start: int, seed: Detection, step: int, own_spot_id: int | None,
           home_map: HomeMap, p: Params) -> list[tuple[int, Detection]]:
    """Follow one monitor from `seed` through the entries in direction `step` (+1 or -1).

    Each entry links the detection nearest the last linked box, within one box size
    per frame since that link, so the search widens while the monitor is hidden
    behind a body. Monitors sitting on other home spots are never linked: a carried
    monitor passing a desk must not jump onto that desk's monitor."""
    path, last, missed = [(start, seed)], seed, 0
    i = start + step
    while 0 <= i < len(entries) and missed <= p.link_gap_frames:
        radius = (1 + missed) * box_size(last.box)
        candidates = [m for m in entries[i].dets.monitors
                      if not _on_other_spot(m.box, own_spot_id, home_map, p)
                      and centre_distance(m.box, last.box) <= radius]
        if candidates:
            last = min(candidates, key=lambda m: centre_distance(m.box, last.box))
            path.append((i, last))
            missed = 0
        else:
            missed += 1
        i += step
    return sorted(path, key=lambda x: x[0])


def _on_other_spot(box: Box, own_spot_id: int | None, home_map: HomeMap, p: Params) -> bool:
    return any(overlap(box, s.box) >= p.match_overlap for s in home_map.spots if s.id != own_spot_id)


def _carry_frames(entries: list[BufferedFrame], path: list[tuple[int, Detection]], reference: Box,
                  frame_w: int, p: Params, home: Box | None = None) -> tuple[list[EvidenceFrame], dict[int, Box]]:
    """Path entries where the monitor has moved away from `reference` and sits inside a person box.
    A monitor still on its `home` spot (as the watcher counts it) is not carried, however much
    its box jitters. Returns those frames and each carry entry's culprit box by entry index."""
    min_move = p.jitter_tolerance * frame_w
    frames, culprits = [], {}
    for i, det in path:
        if centre_distance(det.box, reference) < min_move:
            continue
        if home is not None and overlap(det.box, home) >= p.match_overlap:
            continue
        held = [(coverage(person.box, det.box), person) for person in entries[i].dets.persons]
        held = [(share, person) for share, person in held if share >= p.carry_overlap]
        if not held:
            continue
        share, person = max(held, key=lambda h: h[0])
        culprits[i] = person.box
        frames.append(EvidenceFrame(entries[i].t, entries[i].jpeg, det.box, person.box,
                                    share * det.score * person.score))
    return frames, culprits


def _carrying(carries: list[EvidenceFrame], rows: list[tuple[float, Box, Box | None]], desk: Box | None,
              p: Params) -> Evidence:
    span = (min(f.t for f in carries), max(f.t for f in carries))
    return Evidence("carrying", _pick(carries, p.evidence_frames, p.evidence_min_spacing_seconds), rows, span, desk)


def _pick(frames: list[EvidenceFrame], n: int, spacing: float) -> list[EvidenceFrame]:
    """The best-scoring frames, at most `n`, no two closer than `spacing` seconds; in time order."""
    picked: list[EvidenceFrame] = []
    for f in sorted(frames, key=lambda f: (-f.score, f.t)):
        if len(picked) == n:
            break
        if all(abs(f.t - q.t) >= spacing - EPS for q in picked):
            picked.append(f)
    return sorted(picked, key=lambda f: f.t)
