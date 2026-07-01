import multiprocessing as mp
import cv2
import os
import sys

# ===============================
# Ensure project root is in path
# ===============================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ===============================
# Import worker
# ===============================
from workers.crowd_worker import crowd_worker

# ===============================
# CONFIG
# ===============================
VIDEO_PATH = os.path.join(
    PROJECT_ROOT,
    "videos",
    "NVR_ch19_main_20231206120001_20231206130000.dav"
)

QUEUE_SIZE = 5

# ===============================
# TEST FUNCTION
# ===============================
def test_crowd_worker():
    print("🧪 Starting Crowd Worker Test")

    q = mp.Queue(maxsize=QUEUE_SIZE)

    cap = cv2.VideoCapture(VIDEO_PATH, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print("❌ Video open nahi ho raha")
        return

    worker = mp.Process(target=crowd_worker, args=(q,))
    worker.start()

    frame_count = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_count += 1

        # throttle (same as production idea)
        if frame_count % 5 != 0:
            continue

        if not q.full():
            q.put(frame)

    # stop signal
    q.put(None)

    worker.join()
    cap.release()

    print("✅ Crowd Worker Test Completed")

# ===============================
# MAIN
# ===============================
if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    test_crowd_worker()
