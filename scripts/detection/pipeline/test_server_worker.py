# import multiprocessing as mp
# import cv2
# import os
# import sys

# # ===============================
# # Ensure project root in path
# # ===============================
# PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# if PROJECT_ROOT not in sys.path:
#     sys.path.insert(0, PROJECT_ROOT)

# # ===============================
# # Import worker correctly
# # ===============================
# from workers.server_room_worker import server_room_worker

# # ===============================
# # CONFIG
# # ===============================
# VIDEO_PATH = os.path.join(
#     PROJECT_ROOT,
#     "videos",
#     "NVR_ch19_main_20231206120001_20231206130000.dav"
# )

# QUEUE_SIZE = 5

# # ===============================
# # TEST FUNCTION
# # ===============================
# def test_server_worker():
#     print("🧪 Starting Server Room Worker Test")

#     q = mp.Queue(maxsize=QUEUE_SIZE)
#     cap = cv2.VideoCapture(VIDEO_PATH, cv2.CAP_FFMPEG)

#     if not cap.isOpened():
#         print("❌ Video open nahi ho raha")
#         return

#     worker = mp.Process(target=server_room_worker, args=(q,))
#     worker.start()

#     frame_count = 0

#     while True:
#         ret, frame = cap.read()
#         if not ret:
#             break

#         frame_count += 1
#         if frame_count % 5 != 0:
#             continue

#         if not q.full():
#             q.put(frame)

#     # stop worker
#     q.put(None)

#     worker.join()
#     cap.release()

#     print("✅ Server Room Worker Test Completed")

# # ===============================
# # MAIN
# # ===============================
# if __name__ == "__main__":
#     mp.set_start_method("spawn", force=True)
#     test_server_worker()









# test_server_worker.py
import multiprocessing as mp
import cv2
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)

from workers.server_room_worker import server_room_worker

# def test():
#     q = mp.Queue()
#     cap = cv2.VideoCapture("NVR_ch19_main_20231206120001_20231206130000.dav")

#     p = mp.Process(target=server_room_worker, args=(q,))
#     p.start()

#     while True:
#         ret, frame = cap.read()
#         if not ret:
#             break
#         q.put(frame)

#     q.put(None)
#     p.join()

# if __name__ == "__main__":
#     test()

VIDEO = os.path.join(
    PROJECT_ROOT,
    "videos",
    "NVR_ch19_main_20231206120001_20231206130000.dav"
)


def test():
    q = mp.Queue(maxsize=5)
    cap = cv2.VideoCapture(VIDEO, cv2.CAP_FFMPEG)

    p = mp.Process(target=server_room_worker, args=(q,))
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
