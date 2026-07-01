import cv2

video = "videos/NVR_ch19_main_20231206120001_20231206130000.dav"

cap = cv2.VideoCapture(video)

fps = cap.get(cv2.CAP_PROP_FPS)
frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)

duration_sec = frame_count / fps if fps > 0 else 0

print("FPS:", fps)
print("Total Frames:", frame_count)
print("Duration (seconds):", duration_sec)
print("Duration (minutes):", duration_sec / 60)

cap.release()
