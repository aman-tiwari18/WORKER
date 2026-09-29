"""
One-off live test: Crowd Detection (Entry/Exit) + Crowd Detection (Biometric)
==============================================================================
Runs both HLS workers for ~2 minutes against REAL cameras from exam
"RE: UPCISB - 2026" (exam_id=38), Shift 2 (shift_id=148), today's date
(2026-09-19 — camera_loader.load_cameras_for_execution always uses today).

Safety (this is a TEST, not a production run):
  - SAVE_FOLDER for both workers is redirected to a dedicated test folder
    (results/tcs_detections/_test_crowd_entryexit_biometric/) instead of the
    real crowd_detection / crowd_detection_biometric folders.
  - utils.db_client.report_to_db_hls is monkeypatched to a no-op so this test
    does NOT insert rows into camera_report_hls or trigger SMS alerts.
  - Camera count is capped (SAMPLE_PER_SCRIPT) since Shift 2 is live in
    production right now — this test intentionally does not open the full
    ~127/~183 matching cameras to avoid extra GPU/stream load during a live
    exam window.
"""
import os
import sys
import time
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "workers"))

EXAM_ID = 38          # RE: UPCISB - 2026
SHIFT_ID = 148         # Shift 2
TEST_DURATION_SECS = 120
SAMPLE_PER_SCRIPT = 4  # small sample — Shift 2 is live in prod right now

TEST_ROOT = os.path.join(BASE_DIR, "results", "tcs_detections", "_test_crowd_entryexit_biometric")
CROWD_TEST_FOLDER = os.path.join(TEST_ROOT, "crowd_entry_exit")
BIOMETRIC_TEST_FOLDER = os.path.join(TEST_ROOT, "crowd_biometric")
os.makedirs(CROWD_TEST_FOLDER, exist_ok=True)
os.makedirs(BIOMETRIC_TEST_FOLDER, exist_ok=True)


def main():
    from utils.camera_loader import load_cameras_for_execution
    import utils.db_client as db_client

    # Neutralize DB reporting for this test run (no fake rows, no SMS).
    db_client.report_to_db_hls = lambda *a, **kw: None
    db_client.report_to_db_rtsp = lambda *a, **kw: None

    import workers.crowd_worker as crowd_worker_mod
    import workers.crowd_det_biometric_worker as crowd_bio_worker_mod

    # Redirect save folders to the isolated test directory.
    crowd_worker_mod.HLS_SAVE_FOLDER = CROWD_TEST_FOLDER
    crowd_bio_worker_mod.HLS_SAVE_FOLDER = BIOMETRIC_TEST_FOLDER

    crowd_cams = load_cameras_for_execution({
        "script_code": "crowd", "link_type": "HLS",
        "exam_id": EXAM_ID, "shift_id": SHIFT_ID,
    })[:SAMPLE_PER_SCRIPT]

    bio_cams = load_cameras_for_execution({
        "script_code": "crowd_biometric", "link_type": "HLS",
        "exam_id": EXAM_ID, "shift_id": SHIFT_ID,
    })[:SAMPLE_PER_SCRIPT]

    print(f"[TEST] Crowd (Entry/Exit) cameras: {[c['camera_id'] for c in crowd_cams]}")
    print(f"[TEST] Crowd (Biometric) cameras : {[c['camera_id'] for c in bio_cams]}")

    if not crowd_cams and not bio_cams:
        print("[TEST] No matching cameras found for either script — aborting.")
        return

    stop_event = threading.Event()

    threads = []
    if crowd_cams:
        t1 = threading.Thread(
            target=crowd_worker_mod.crowd_worker_hls,
            args=(crowd_cams, stop_event),
            daemon=True,
        )
        threads.append(t1)
    if bio_cams:
        t2 = threading.Thread(
            target=crowd_bio_worker_mod.crowd_det_biometric_worker_hls,
            args=(bio_cams, stop_event),
            daemon=True,
        )
        threads.append(t2)

    print(f"[TEST] Starting {len(threads)} worker(s) for {TEST_DURATION_SECS}s ...")
    for t in threads:
        t.start()

    time.sleep(TEST_DURATION_SECS)

    print("[TEST] Duration elapsed — stopping workers ...")
    stop_event.set()
    for t in threads:
        t.join(timeout=15)

    print("\n[TEST] Done. Saved files:")
    for folder, label in [(CROWD_TEST_FOLDER, "Crowd (Entry/Exit)"), (BIOMETRIC_TEST_FOLDER, "Crowd (Biometric)")]:
        files = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
        print(f"  {label}: {len(files)} file(s) in {folder}")
        for f in files:
            print(f"    - {f}")


if __name__ == "__main__":
    main()
