"""Write an alert: event JSON + raw frame + annotated snapshot + evidence frames.

Vendored unchanged from monitor_alerts_deploy_v2/monitor_alerts/alerts.py.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .core import Box
from .evidence import NO_EVIDENCE, Evidence, decode
from .home_map import HomeMap
from .watcher import Event

# BGR colours for the annotated snapshot
RED, ORANGE, MAGENTA = (0, 0, 255), (0, 140, 255), (255, 0, 255)
HEADER_H = 28
BASIS_LABEL = {"carrying": "CARRYING", "last_at_desk": "LAST AT DESK", "none": ""}


def write_alert(event: Event, frame: np.ndarray, home_map: HomeMap, alerts_dir: Path,
                evidence: Evidence = NO_EVIDENCE) -> Path:
    when = datetime.fromtimestamp(event.timestamp).astimezone()
    out_dir = Path(alerts_dir) / event.camera_id / when.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{when.strftime('%H%M%S')}_{when.microsecond // 1000:03d}_{event.type}"
    raw_name, snapshot_name = f"{stem}_raw.jpg", f"{stem}_annotated.jpg"

    cv2.imwrite(str(out_dir / raw_name), frame)
    cv2.imwrite(str(out_dir / snapshot_name), draw_annotated(event, frame, home_map))

    h, w = frame.shape[:2]
    payload = {
        "event_id": str(uuid.uuid4()),
        "camera_id": event.camera_id,
        "type": event.type,
        "timestamp": when.isoformat(timespec="milliseconds"),
        "frame_size": [w, h],
        "missing_homes": [{"home_id": s.id, "box": _box(s.box)} for s in event.missing_homes],
        "off_home_monitors": [{"box": _box(d.box), "score": round(d.score, 3)} for d in event.off_home_monitors],
        "persons": [{"box": _box(d.box), "score": round(d.score, 3)} for d in event.persons],
        "nearest_person": ({"box": _box(event.nearest_person.box), "score": round(event.nearest_person.score, 3)}
                           if event.nearest_person else None),
        "raw_frame": raw_name,
        "snapshot": snapshot_name,
        "written_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "evidence": _write_evidence(event, evidence, out_dir, stem),
    }
    json_path = out_dir / f"{stem}.json"
    json_path.write_text(json.dumps(payload, indent=2))
    return json_path


def draw_annotated(event: Event, frame: np.ndarray, home_map: HomeMap) -> np.ndarray:
    # Only the anomaly: where monitors went missing (red) and monitors that moved
    # (orange). The person involved is shown in the evidence frames instead: at
    # trigger time the nearest person is often a bystander. All boxes are in the JSON.
    img = frame.copy()
    for s in event.missing_homes:
        _rect(img, s.box, RED, 3)
    for d in event.off_home_monitors:
        _rect(img, d.box, ORANGE, 3)
    when = datetime.fromtimestamp(event.timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    cv2.rectangle(img, (0, 0), (img.shape[1], HEADER_H), (0, 0, 0), -1)
    cv2.putText(img, f"{event.type}  {event.camera_id}  {when}", (6, HEADER_H - 9),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    return img


def draw_incident(frame: np.ndarray, monitor_box: Box | None, culprit_box: Box | None, desk_box: Box | None,
                  header: str) -> np.ndarray:
    """Only the incident: the desk it came from (red), the monitor (orange), the culprit (magenta)."""
    img = frame.copy()
    if desk_box is not None:
        _rect(img, desk_box, RED, 3)
    if monitor_box is not None:
        _rect(img, monitor_box, ORANGE, 3)
    if culprit_box is not None:
        _rect(img, culprit_box, MAGENTA, 4)
    cv2.rectangle(img, (0, 0), (img.shape[1], HEADER_H), (0, 0, 0), -1)
    cv2.putText(img, header, (6, HEADER_H - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    return img


def _write_evidence(event: Event, evidence: Evidence, out_dir: Path, stem: str) -> dict:
    frames = []
    for n, f in enumerate(evidence.frames, start=1):
        name = f"{stem}_evidence_{n}.jpg"
        when = _iso(f.t)
        header = f"{event.type}  {event.camera_id}  {when[:19].replace('T', ' ')}  {BASIS_LABEL[evidence.basis]}"
        cv2.imwrite(str(out_dir / name), draw_incident(decode(f.jpeg), f.monitor_box, f.culprit_box,
                                                       evidence.desk_box, header))
        frames.append({"file": name, "timestamp": when, "score": round(f.score, 3),
                       "monitor_box": _box(f.monitor_box) if f.monitor_box is not None else None,
                       "culprit_box": _box(f.culprit_box)})
    return {"basis": evidence.basis,
            "span": [_iso(t) for t in evidence.span] if evidence.span else None,
            "frames": frames}


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds")


def _rect(img: np.ndarray, box: Box, colour: tuple[int, int, int], thickness: int) -> None:
    x1, y1, x2, y2 = map(int, box)
    cv2.rectangle(img, (x1, y1), (x2, y2), colour, thickness)


def _box(box: Box) -> list[float]:
    return [round(v, 1) for v in box]
