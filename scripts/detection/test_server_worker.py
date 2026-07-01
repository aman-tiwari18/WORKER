# test_server_worker.py
import multiprocessing as mp
import cv2
from server_room_worker import server_room_worker

def test():
    q = mp.Queue()
    cap = cv2.VideoCapture("NVR_ch19_main_20231206120001_20231206130000.dav")

    p = mp.Process(target=server_room_worker, args=(q,))
    p.start()

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        q.put(frame)

    q.put(None)
    p.join()

if __name__ == "__main__":
    test()
