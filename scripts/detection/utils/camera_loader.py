"""
Camera Loader for CCTV Pipeline
=================================
Unified camera loading — replaces Excel-based approach.
Fetches pending executions → queries DB → returns camera list.

NOTE: This loader is used by DETECTION WORKERS and loads ONLY accessible cameras.
Vision classification scripts use get_hls_cameras() / get_rtsp_cameras() directly.
"""

import logging
from datetime import date

from utils.db_client import get_hls_cameras_accessible, get_rtsp_cameras_accessible
from utils.api_client import get_pending_executions

logger = logging.getLogger(__name__)


# ================= ANALYTIC -> CLASSIFICATION MAPPING =================
# This ensures each script only processes relevant cameras, per the NTA SOP.
# Updated 2026-06-30 for the HLS exam (exam_id=27) DB classification values:
#   Computer Lab · Server Room · Entry · Outside Area · Parking Lot ·
#   Outdoor Scene · Uncertain · NULL
# RTSP values (Inside Classroom · Entry/Exit · Strong Room · Biometric) are kept
# in the lists too, so the same map works for both link types.
#
# Matching is case-insensitive SUBSTRING (see _camera_matches_script): each token
# below must be a substring of the camera's classification. e.g. token "entry"
# matches both "Entry" and "Entry/Exit"; "computer lab" matches "Computer Lab".
# "Uncertain"/"Uncertain/Obstructed" cameras are NOT routed to room-specific
# analytics — only the "all" scripts (tamper/inversion). Cameras with a blank /
# NULL classification fall through to True (processed by every analytic).
#
# SOP "Camera Type" → DB classification tokens:
#   Inside Lab     -> "computer lab", "inside classroom"
#   Server Room    -> "server room", "strong room"
#   Entry / Exit   -> "entry"                (matches "Entry" and "Entry/Exit")
#   Outside        -> "outside area", "outdoor scene", "parking lot"
SCRIPT_CLASSIFICATION_MAP = {
    # 1. Intruder detection in exam lab and server room -> Inside Lab / Server Room
    "intruder":        ["computer lab", "inside classroom", "server room", "strong room"],

    # 3. Crowd — >10 people at entry gate -> Outside / Entry Exit
    "crowd":           ["entry", "outside area", "outdoor scene", "parking lot"],
    "crowd_biometric": ["entry", "outside area", "outdoor scene", "parking lot", "biometric"],

    # 4. Invigilator not moving in exam room -> Inside Lab
    "invigilator":     ["computer lab", "inside classroom"],

    # 5. Two or more people in close proximity -> Inside Lab
    "proximity":       ["computer lab", "inside classroom"],

    # 6. Server-room threshold -> Server Room
    "server":          ["server room", "strong room"],

    # 7. Mobile phone detection -> Inside Lab + Inside Server Room
    "phone":           ["computer lab", "inside classroom", "server room", "strong room"],

    # 8. Movement at entry / exit during examination -> Entry/Exit
    "entry_exit":      ["entry"],

    # 9. Camera feed is inverted -> Inside Lab / Server Room / Outside (all)
    "inversion":       ["all"],

    # 10. 0 candidates in a room -> Inside Lab
    "crowd_zero":      ["computer lab", "inside classroom"],

    # 11. Less than 5 candidates in a room -> Inside Lab
    "crowd_five":      ["computer lab", "inside classroom"],

    # 12. Blind spot detected inside lab -> Inside Lab
    "blind_spot":      ["computer lab", "inside classroom"],

    # Camera tampering -> all cameras
    "tamper":          ["all"],

    # Strong room / QP-box not visible -> Strong Room / Server Room
    "strongroom":      ["strong room", "server room"],
    "qp_box":          ["strong room", "server room"],
}

def _camera_matches_script(classification, script_code, custom_classifications=None):
    """Check if a camera's classification matches the requested script_code.

    If custom_classifications is provided (from DB assignment), use that instead
    of the hardcoded SCRIPT_CLASSIFICATION_MAP.
    """
    if not classification:
        return True  # Default to including if unknown

    # Use user-defined classifications if set
    allowed = custom_classifications if custom_classifications else SCRIPT_CLASSIFICATION_MAP.get(script_code, [])

    if not allowed:
        return True

    allowed_lower = [a.lower() for a in allowed]
    if "all" in allowed_lower or "all cameras" in allowed_lower:
        return True

    cls_lower = classification.lower()
    return any(scene in cls_lower for scene in allowed_lower)

def load_cameras_for_execution(execution: dict) -> list:
    """
    Given a pending execution dict, query the appropriate DB table
    and return a list of camera config dicts ready for workers.
    
    IMPORTANT: This function loads ONLY accessible cameras (status = 'Accessible').
    Vision classification scripts should use get_hls_cameras() / get_rtsp_cameras() directly.

    Each returned dict contains:
        - cctv_url:       str   (streaming URL)
        - camera_id:      str   (ch_code for HLS, device_id for RTSP)
        - classification: str   (e.g. 'Computer Lab', 'Entry/Exit')
        - link_type:      str   ('HLS' or 'RTSP')
        - script_code:    str   (e.g. 'crowd', 'server', 'entry_exit')
        - camera_name:    str
        - centre_name:    str
    """
    script_code = execution.get("script_code", "unknown")
    link_type   = execution.get("link_type", "").upper()
    exam_id     = execution.get("exam_id")
    shift_id    = execution.get("shift_id")
    custom_cls  = execution.get("camera_classifications") or (execution.get("execution_metadata") or {}).get("camera_classifications")

    today = date.today().isoformat()

    cameras = []

    if link_type == "HLS":
        rows = get_hls_cameras_accessible(exam_id, shift_id, today)
        for row in rows:
            ch_code = str(row.get("ch_code", "")).strip()
            classification = str(row.get("classification", "")).strip()
            
            if not ch_code or not row.get("cctv_url"):
                continue
            
            if not _camera_matches_script(classification, script_code, custom_cls):
                continue

            cameras.append({
                "cctv_url":       row["cctv_url"],
                "camera_id":      ch_code,
                "ch_no":          ch_code,   # Added to ensure workers get the channel code
                "camera_id_pk":   str(row.get("id", "")),   # DB primary key — always unique
                "classification": str(row.get("classification", "")).strip(),
                "link_type":      "HLS",
                "script_code":    script_code,
                "camera_name":    str(row.get("camera_name", "")).strip(),
                "centre_name":    str(row.get("centre_name", "")).strip(),
                "tc_code":        str(row.get("tc_code", "")).strip(),
                "exam_id":        exam_id,      # ← ADD exam_id to camera config
                "shift_id":       shift_id,     # ← ADD shift_id to camera config
            })

    elif link_type == "RTSP":
        rows = get_rtsp_cameras_accessible(exam_id)
        for row in rows:
            device_id = str(row.get("device_id", "")).strip()
            classification = str(row.get("classification", "")).strip()
            
            if not device_id or not row.get("cctv_url"):
                continue

            if not _camera_matches_script(classification, script_code, custom_cls):
                continue

            # id is PRIMARY KEY — guaranteed unique per row
            db_id     = str(row.get("id", "")).strip()
            
            raw_cam_no = row.get("camera_no")
            if raw_cam_no is None or str(raw_cam_no).strip().lower() in ("none", "null", ""):
                camera_no = "1"
            else:
                camera_no = str(raw_cam_no).strip()

            cameras.append({
                "cctv_url":       row["cctv_url"],
                "camera_id":      device_id,
                "camera_id_pk":   db_id,           # ← PRIMARY KEY (unique) — use for file naming
                "camera_no":      camera_no,
                "classification": str(row.get("classification", "")).strip(),
                "link_type":      "RTSP",
                "script_code":    script_code,
                "camera_name":    str(row.get("camera_name", "")).strip(),
                "centre_name":    str(row.get("centre_name", "")).strip(),
                "tc_code":        str(row.get("tc_code", "")).strip(),
                "exam_id":        exam_id,      # ← ADD exam_id to camera config
                "shift_id":       shift_id,     # ← ADD shift_id to camera config
            })
    else:
        logger.error(f"[CameraLoader] Unknown link_type: {link_type}")
        return []

    logger.info(
        f"[CameraLoader] Loaded {len(cameras)} ACCESSIBLE cameras "
        f"(script={script_code}, link_type={link_type}, "
        f"exam_id={exam_id}, shift_id={shift_id})"
    )
    return cameras


def load_all_pending_cameras(api_url: str = None, token: str = None) -> list:
    """
    Convenience: fetch all pending executions and load cameras for each.

    Returns list of (execution_dict, cameras_list) tuples.
    """
    executions = get_pending_executions(api_url, token)
    results = []

    for ex in executions:
        cameras = load_cameras_for_execution(ex)
        if cameras:
            results.append((ex, cameras))
            logger.info(
                f"[CameraLoader] Execution '{ex['script_code']}' → "
                f"{len(cameras)} cameras ({ex['link_type']})"
            )
        else:
            logger.warning(
                f"[CameraLoader] No cameras for execution "
                f"'{ex['script_code']}' (exam_id={ex['exam_id']})"
            )

    return results


# ============================================================
# 🧪  STANDALONE TEST
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    results = load_all_pending_cameras()
    for ex, cams in results:
        print(f"\n=== {ex['script_code']} ({ex['link_type']}) ===")
        for cam in cams[:3]:
            print(f"  camera_id={cam['camera_id']}  "
                  f"classification={cam['classification']}  "
                  f"url={str(cam['cctv_url'])[:50]}...")