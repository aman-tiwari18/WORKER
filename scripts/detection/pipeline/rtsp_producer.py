import cv2
import queue
import threading
import logging

def rtsp_frame_producer(rtsp_url, frame_queue, stop_event):
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)

    if not cap.isOpened():
        logging.error(f"Failed to open RTSP: {rtsp_url}")
        return

    logging.info(f"🎥 RTSP started: {rtsp_url}")

    while not stop_event.is_set():
        ret, frame = cap.read()
        if not ret:
            logging.warning("RTSP frame read failed")
            break

        if not frame_queue.full():
            frame_queue.put(frame.copy())

    cap.release()
    logging.info("🎬 RTSP producer stopped")
