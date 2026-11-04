"""Uptime UP -> Cameras tab (RTSP only, the live run's own shift and date; earlier shifts are never touched).

A camera found UP that already has a label (anything but empty / plain "Uncertain"):
  * camera_links_rtsp: status = 'Accessible', frame_path = this check's frame. Never the other way:
    a DOWN check changes nothing here.
  * camera_classification_rtsp (what the Cameras tab counts for the shift's classification run):
      - its row of this shift + date that holds only a failed capture ('Uncertain', no frame) gets the
        camera's label and this frame;
      - no row of this shift + date yet: one is added with the camera's label and this frame, timed at the
        run's newest row so it falls inside that run (no row for the shift + date at all: nothing added —
        classification has not run for it).
"""
from __future__ import annotations

_LABELLED = ("cl.classification IS NOT NULL AND TRIM(cl.classification) <> '' "
             "AND LOWER(TRIM(cl.classification)) <> 'uncertain'")

LINK_SQL = f"""
UPDATE camera_links_rtsp cl SET cl.status = 'Accessible', cl.frame_path = %s
WHERE cl.id = %s AND {_LABELLED}
"""

FILL_FAILED_SQL = f"""
UPDATE camera_classification_rtsp h JOIN camera_links_rtsp cl ON cl.id = h.camera_id
SET h.classification = cl.classification, h.frame_path = %s
WHERE h.camera_id = %s AND h.shift_id = %s AND h.exam_date = %s
  AND h.classification = 'Uncertain' AND (h.frame_path IS NULL OR h.frame_path = '') AND {_LABELLED}
"""

ADD_HISTORY_SQL = f"""
INSERT INTO camera_classification_rtsp (camera_id, shift_id, exam_date, frame_path, classification, created_at)
SELECT cl.id, %s, %s, %s, cl.classification, r.newest
FROM camera_links_rtsp cl
JOIN (SELECT MAX(created_at) AS newest FROM camera_classification_rtsp
      WHERE shift_id = %s AND exam_date = %s) r ON r.newest IS NOT NULL
WHERE cl.id = %s AND {_LABELLED}
  AND NOT EXISTS (SELECT 1 FROM camera_classification_rtsp h
                  WHERE h.camera_id = cl.id AND h.shift_id = %s AND h.exam_date = %s)
"""


def apply(cur, rec: dict) -> int:
    """rec: camera_row_id, shift_id, exam_date, frame_path. Safe to repeat: history rows are only filled / added once."""
    cid, sid, day, frame = rec["camera_row_id"], rec["shift_id"], rec["exam_date"], rec["frame_path"]
    n = cur.execute(LINK_SQL, (frame, cid))
    n += cur.execute(FILL_FAILED_SQL, (frame, cid, sid, day))
    n += cur.execute(ADD_HISTORY_SQL, (sid, day, frame, sid, day, cid, sid, day))
    return n


def frame_name(exam_id: int, exam_date, shift_id: int, camera_key: str) -> str:
    """Path under the classification frames root; one file per camera + shift, overwritten with the newest."""
    from .capture import frame_rel_path
    return "uptime/" + frame_rel_path(exam_id, exam_date, shift_id, camera_key)
