import multiprocessing as mp
import cv2
import os
import sys

# ensure project root in path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)

from workers.entry_exit_worker import entry_exit_worker

VIDEO = os.path.join(
    PROJECT_ROOT,
    "videos",
    "NVR_ch19_main_20231206120001_20231206130000.dav"
)

def test():
    q = mp.Queue(maxsize=5)
    cap = cv2.VideoCapture(VIDEO, cv2.CAP_FFMPEG)

    p = mp.Process(target=entry_exit_worker, args=(q,))
    p.start()

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if not q.full():
            q.put(frame)

    q.put(None)
    p.join()
    cap.release()

if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    test()
