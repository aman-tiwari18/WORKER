"""Per-camera anomaly logic: person + monitor boxes in, tamper alerts out.

Pure logic (no I/O, no GPU, no threads) so it can be unit-tested with synthetic
boxes and reused by any system that already has detections.

Each home spot is in one of three states every frame:
  OK        a monitor is detected at the spot
  OCCLUDED  no monitor, but a person is standing over the spot (normal invigilation)
  EMPTY     no monitor and nobody in front of it
A spot EMPTY for `missing_seconds`, after a person touched it recently, is an alert.

Monitors detected away from every home spot are followed frame to frame as
"off-home tracks". A track that sits still for `learn_seconds` is a monitor
that was hidden behind someone during capture: it becomes a home spot, so it is
protected like the others. A track that stays MOVED away from where it first appeared
(for `offhome_min_frames` frames in a row) while a person touches it is a
monitor being carried or lifted - an alert.

Vendored from monitor_alerts_deploy_v2/monitor_alerts/watcher.py. Changes for the
pipeline, which samples each camera sparsely (bursts every few seconds) instead of
a steady 5 fps: "missing" also needs `missing_min_obs` EMPTY frames in a row,
"still long enough to learn" counts frames actually seen, not seconds * fps, and
with `touch_cover` a person standing at a still-visible monitor counts as a touch
(sparse frames rarely catch the moment the person fully hides it).
"""
from __future__ import annotations

from dataclasses import dataclass

from .core import Box, Detection, FrameDetections, Params, box_size, centre_distance, coverage, iou, overlap
from .home_map import HomeMap, HomeSpot

OK, OCCLUDED, EMPTY = "OK", "OCCLUDED", "EMPTY"


@dataclass
class Event:
    type: str  # MONITOR_MISSING | MONITOR_OFF_HOME | VIEW_CHANGED
    camera_id: str
    timestamp: float
    missing_homes: list[HomeSpot]
    off_home_monitors: list[Detection]
    persons: list[Detection]
    nearest_person: Detection | None = None  # the person most likely involved (simple logic)


@dataclass
class _Spot:
    home: HomeSpot
    state: str = OK
    empty_since: float | None = None
    empty_obs: int = 0               # consecutive frames EMPTY
    last_touch: float | None = None  # last time a person stood over this spot
    toucher: Detection | None = None # ...and that person
    alerted: bool = False            # already part of an alert; cleared when the monitor returns
    learned: bool = False            # added after capture (was hidden behind someone then)

    def empty_for(self, t: float) -> float:
        return t - self.empty_since if self.state == EMPTY and self.empty_since is not None else 0.0

    def long_empty(self, t: float, p: Params) -> bool:
        return self.empty_for(t) >= p.missing_seconds and self.empty_obs >= p.missing_min_obs


@dataclass
class _Track:
    """One off-home monitor followed across frames."""
    start: Box                       # where it was first seen (for "carried")
    anchor: Box                      # where it has been sitting since it last moved (for "learned")
    still_since: float
    still_since_frame: int           # watcher frame number at still_since
    det: Detection                   # latest detection
    last_seen: float
    still_hits: int = 1              # detections since it settled at `anchor`
    moved_streak: int = 0            # consecutive frames it has been away from `start`
    touched_at: float | None = None  # last time a person box overlapped it
    toucher: Detection | None = None # ...and that person
    alerted: bool = False            # alerted at least once
    alerted_at: float | None = None  # last alert; a still-HELD monitor re-alerts every repeat_held_seconds



class Watcher:
    def __init__(self, home_map: HomeMap, params: Params):
        self.camera_id = home_map.camera_id
        self.p = params
        # Box jitter of small, far monitors can exceed half their size; a move must
        # also be larger than this many pixels to count.
        self.min_move_px = params.jitter_tolerance * home_map.frame_size[0]
        self.spots = [_Spot(h) for h in home_map.spots]
        self.tracks: list[_Track] = []
        self.last_t: float | None = None
        self.frame_no = 0
        self.last_alert_t: float | None = None

    def update(self, dets: FrameDetections, t: float) -> list[Event]:
        self.frame_no += 1
        self._reset_if_gap(t)
        self._classify_spots(dets, t)
        self._forget_stale_learned_spots(t)
        self._update_tracks(dets, t)
        self.last_t = t
        event = None if self._cooling_down(t) else self._check_events(dets, t)
        if event is None:
            return []
        self._start_incident(t)
        return [event]

    def spot_states(self) -> list[tuple[HomeSpot, str, bool]]:
        """(spot, OK/OCCLUDED/EMPTY, learned?) for every spot - for drawing and debugging."""
        return [(s.home, s.state, s.learned) for s in self.spots]

    def state_counts(self) -> dict[str, int]:
        counts = {OK: 0, OCCLUDED: 0, EMPTY: 0}
        for s in self.spots:
            counts[s.state] += 1
        return counts

    # --- per-frame steps -------------------------------------------------------

    def _reset_if_gap(self, t: float) -> None:
        # After a reconnect or stall we don't know what happened in between, so
        # a gap must never count as "empty for N seconds" or as a recent touch.
        if self.last_t is not None and t - self.last_t > self.p.gap_reset_seconds:
            for s in self.spots:
                s.empty_since = None
                s.empty_obs = 0
                s.last_touch = None
            for tr in self.tracks:
                tr.touched_at = None

    def _classify_spots(self, dets: FrameDetections, t: float) -> None:
        for s in self.spots:
            if any(overlap(m.box, s.home.box) >= self.p.match_overlap for m in dets.monitors):
                s.state, s.empty_since, s.empty_obs, s.alerted = OK, None, 0, False
                if self.p.touch_cover is not None:
                    toucher = max(dets.persons, key=lambda p: coverage(p.box, s.home.box), default=None)
                    if toucher is not None and coverage(toucher.box, s.home.box) >= self.p.touch_cover:
                        s.last_touch, s.toucher = t, toucher
            elif (person := self._covering_person(s.home.box, dets)) is not None:
                s.state, s.empty_since, s.empty_obs, s.last_touch, s.toucher = OCCLUDED, None, 0, t, person
            else:
                s.state = EMPTY
                s.empty_obs += 1
                if s.empty_since is None:
                    s.empty_since = t

    def _forget_stale_learned_spots(self, t: float) -> None:
        # A learned "monitor" that stays gone was probably something passing
        # through (a laptop, a monitor on a trolley); don't keep it forever.
        self.spots = [s for s in self.spots
                      if not (s.learned and s.empty_for(t) > self.p.stray_forget_seconds)]

    def _update_tracks(self, dets: FrameDetections, t: float) -> None:
        self.tracks = [tr for tr in self.tracks if t - tr.last_seen <= self.p.stray_forget_seconds]
        unmatched = list(self.tracks)
        for det in self._off_home(dets):
            # Link to the nearest track within one box size (a walking person
            # moves a carried monitor less than that between frames at 5 fps).
            near = [tr for tr in unmatched if centre_distance(tr.det.box, det.box) <= box_size(tr.det.box)]
            tr = min(near, key=lambda tr: centre_distance(tr.det.box, det.box), default=None)
            if tr is None:
                tr = _Track(start=det.box, anchor=det.box, still_since=t, still_since_frame=self.frame_no,
                            det=det, last_seen=t)
                self.tracks.append(tr)
            else:
                unmatched.remove(tr)
                tr.det, tr.last_seen = det, t
                if self._moved(tr.anchor, det.box):
                    tr.anchor, tr.still_since, tr.still_hits = det.box, t, 1  # moved: start settling again
                    tr.still_since_frame = self.frame_no
                else:
                    tr.still_hits += 1
            # Carried monitors stay displaced; box jitter and jumps to a
            # neighbouring monitor flip back and forth and reset the streak.
            tr.moved_streak = tr.moved_streak + 1 if self._moved(tr.start, det.box) else 0
            touching = [p for p in dets.persons if iou(p.box, det.box) > 0]
            if touching:
                tr.touched_at, tr.toucher = t, max(touching, key=lambda p: iou(p.box, det.box))
        self._learn_still_tracks(t)

    def _learn_still_tracks(self, t: float) -> None:
        # Also applies to alerted tracks: a monitor carried to a new desk and left
        # there becomes that desk's home spot, so taking it again alerts again.
        for tr in list(self.tracks):
            still_for = t - tr.still_since
            # Same bar as home capture: seen in most frames, not just now and then.
            seen_enough = tr.still_hits / (self.frame_no - tr.still_since_frame + 1) >= self.p.capture_min_presence
            # Never while a person is holding it: someone standing still with a monitor in
            # their hands must keep alerting, not have it turned into a desk.
            if tr.last_seen == t and tr.touched_at != t and still_for >= self.p.learn_seconds and seen_enough:
                next_id = max((s.home.id for s in self.spots), default=-1) + 1
                self.spots.append(_Spot(HomeSpot(next_id, tr.det.box), learned=True))
                self.tracks.remove(tr)

    def _cooling_down(self, t: float) -> bool:
        return self.last_alert_t is not None and t - self.last_alert_t < self.p.cooldown_seconds

    def _check_events(self, dets: FrameDetections, t: float) -> Event | None:
        view_changed = self._view_changed_event(dets, t)
        if view_changed:
            return view_changed
        return self._missing_event(dets, t) or self._off_home_event(dets, t)

    def _view_changed_event(self, dets: FrameDetections, t: float) -> Event | None:
        # Most spots empty at once = camera covered, knocked or lights off.
        # No person gate: whoever covers the lens is usually not in the frame.
        # Only captured spots count: learned ones are less certain to be monitors.
        captured = [s for s in self.spots if not s.learned]
        long_empty = [s for s in captured if s.long_empty(t, self.p)]
        if not captured or len(long_empty) / len(captured) <= self.p.view_changed_frac:
            return None
        if all(s.alerted for s in long_empty):
            return None
        return self._event("VIEW_CHANGED", dets, t)

    def _missing_event(self, dets: FrameDetections, t: float) -> Event | None:
        candidates = [s for s in self._long_empty(t) if not s.alerted]
        touched = any(s.last_touch is not None and t - s.last_touch <= self.p.person_window_seconds
                      for s in candidates)
        if not touched:
            return None
        event = self._event("MONITOR_MISSING", dets, t)
        touched_spots = [s for s in candidates if s.last_touch is not None]
        event.nearest_person = max(touched_spots, key=lambda s: s.last_touch).toucher
        return event

    def _off_home_event(self, dets: FrameDetections, t: float) -> Event | None:
        carried = [tr for tr in self.tracks
                   if tr.last_seen == t
                   and tr.moved_streak >= self.p.offhome_min_frames
                   and tr.touched_at is not None and t - tr.touched_at <= self.p.person_window_seconds
                   and (not tr.alerted
                        # Repeat while it is still being HELD: the monitor and a person
                        # overlapping it are both in this frame.
                        or (tr.touched_at == t and t - tr.alerted_at >= self.p.repeat_held_seconds))]
        if not carried:
            return None
        for tr in carried:
            tr.alerted, tr.alerted_at = True, t
        event = self._event("MONITOR_OFF_HOME", dets, t)
        event.off_home_monitors = [tr.det for tr in carried]
        event.nearest_person = max(carried, key=lambda tr: tr.touched_at).toucher
        return event

    def _start_incident(self, t: float) -> None:
        # Spots already empty belong to this incident; marking them stops a second
        # alert for the same theft once the cool-down ends. Spots someone is merely
        # standing in front of are left alone: if that monitor goes too, it alerts.
        self.last_alert_t = t
        for s in self.spots:
            if s.state == EMPTY:
                s.alerted = True

    # --- helpers ---------------------------------------------------------------

    def _moved(self, before: Box, now: Box) -> bool:
        needed = max(self.p.offhome_move_frac * box_size(now), self.min_move_px)
        return centre_distance(before, now) >= needed

    def _covering_person(self, box: Box, dets: FrameDetections) -> Detection | None:
        """The person hiding most of this spot, if any hides at least `occlude_cover` of it."""
        best = max(dets.persons, key=lambda p: coverage(p.box, box), default=None)
        return best if best is not None and coverage(best.box, box) >= self.p.occlude_cover else None

    def _long_empty(self, t: float) -> list[_Spot]:
        return [s for s in self.spots if s.long_empty(t, self.p)]

    def _off_home(self, dets: FrameDetections) -> list[Detection]:
        return [m for m in dets.monitors
                if all(overlap(m.box, s.home.box) < self.p.match_overlap for s in self.spots)]

    def _event(self, type_: str, dets: FrameDetections, t: float) -> Event:
        return Event(type_, self.camera_id, t, [s.home for s in self._long_empty(t)],
                     self._off_home(dets), list(dets.persons))
