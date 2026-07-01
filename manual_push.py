# # # import os
# # # import sys
# # # import shutil
# # # import re

# # # # ================= DB REPORTING =================
# # # # Import centralized reporter from utils/db_client (single source of truth)
# # # _utils_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "detection", "utils")
# # # if _utils_path not in sys.path:
# # #     sys.path.insert(0, _utils_path)
# # # from db_client import report_to_db_hls  # noqa: E402

# # # # ================= CONFIGURATION =================
# # # # 1. Full path to your pre-formatted image file on this machine
# # # SOURCE_IMAGE_PATH = "/data/cctv_dev/cctv/proximity_ch85721_exam9_20260416_111001_f7a3c2d1-8b4e-4f9a-a1c5-3d7e6b2f0e94.jpg"

# # # # 2. Detection type folder — used in DB image URL and detection_type column
# # # DETECTION_FOLDER = "proximity_detection"

# # # # 3. Destination save folder on this server
# # # SAVE_FOLDER = os.path.join(
# # #     os.path.dirname(os.path.abspath(__file__)),
# # #     "scripts", "detection", "results", "tcs_detections", "proximity_detection"
# # # )

# # # # 4. Exam metadata — set via environment or edit defaults here for testing
# # # os.environ.setdefault("EXAM_ID", "9")
# # # os.environ.setdefault("SHIFT_ID", "68")
# # # # ==================================================

# # # os.makedirs(SAVE_FOLDER, exist_ok=True)

# # # filename  = os.path.basename(SOURCE_IMAGE_PATH)
# # # dest_path = os.path.join(SAVE_FOLDER, filename)

# # # match   = re.search(r"_ch(\d+)_", filename)
# # # ch_code = match.group(1) if match else "unknown"

# # # print("--- Manual Proximity Detection Push ---")
# # # print(f"Moving : {filename}")
# # # print(f"To     : {SAVE_FOLDER}")
# # # print("----------------------------------------")

# # # try:
# # #     if os.path.exists(SOURCE_IMAGE_PATH):
# # #         shutil.copy(SOURCE_IMAGE_PATH, dest_path)
# # #         print("Image successfully copied to results folder.")

# # #         success = report_to_db_hls(ch_code=ch_code, image_name=filename, folder=DETECTION_FOLDER)

# # #         if success:
# # #             print("SUCCESS: Record added to database.")
# # #         else:
# # #             print("FAILED DB: Check db_reports.log for details.")
# # #     else:
# # #         print(f"ERROR: Source image not found at {SOURCE_IMAGE_PATH}")

# # # except Exception as e:
# # #     print(f"FATAL ERROR: {e}")



import os
import sys
import shutil
import re

# ================= DB REPORTING =================
_utils_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "detection", "utils")
if _utils_path not in sys.path:
    sys.path.insert(0, _utils_path)
from db_client import report_to_db_hls  # noqa: E402

# ================= CONFIGURATION =================
# 1. Full path to your pre-formatted image file on this machine
#    Expected format: phone_detection_ch{ch_no}_exam{exam_id}_{timestamp}_{uuid}.jpg
SOURCE_IMAGE_PATH = "/data/cctv_dev/cctv/phone_detection_ch88720_exam11_20260421_112010_f7a3c2d1-8b4e-4f9a-a1c5-3d7e6b2f0e99.jpg"

# 2. Detection type folder — used in DB image URL and detection_type column
DETECTION_FOLDER = "phone_detection"

# 3. Destination save folder on this server
SAVE_FOLDER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),     
    "scripts", "detection", "results", "tcs_detections", "phone_detection"
)

# 4. Exam metadata — set via environment or edit defaults here for testing
os.environ.setdefault("EXAM_ID", "11")
os.environ.setdefault("SHIFT_ID", "70")
# ==================================================

os.makedirs(SAVE_FOLDER, exist_ok=True)

filename  = os.path.basename(SOURCE_IMAGE_PATH)
dest_path = os.path.join(SAVE_FOLDER, filename)

match   = re.search(r"_ch(\d+)_", filename)
ch_code = match.group(1) if match else "unknown"

print("--- Manual Phone Detection Push ---")
print(f"File   : {filename}")
print(f"ch_code: {ch_code}")
print(f"To     : {SAVE_FOLDER}")
print("------------------------------------")

try:
    if os.path.exists(SOURCE_IMAGE_PATH):
        shutil.copy(SOURCE_IMAGE_PATH, dest_path)
        print("Image successfully copied to results folder.")

        success = report_to_db_hls(ch_code=ch_code, image_name=filename, folder=DETECTION_FOLDER)

        if success:
            print("SUCCESS: Record added to database.")
        else:
            print("FAILED DB: Check db_reports.log for details.")
    else:
        print(f"ERROR: Source image not found at {SOURCE_IMAGE_PATH}")

except Exception as e:
    print(f"FATAL ERROR: {e}")



# import os
# import sys
# import shutil
# import re

# # ================= DB REPORTING =================
# _utils_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "detection", "utils")
# if _utils_path not in sys.path:
#     sys.path.insert(0, _utils_path)
# from db_client import report_to_db_rtsp

# # ================= CONFIGURATION =================
# # Expected format: blind_spot_[device_id]_[camera_no]_exam[exam_id]_[timestamp]_[uuid].jpg
# SOURCE_IMAGE_PATH = "/data/cctv_dev/cctv/blind_1000036_21_exam7_20260416_225252_a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d.jpg"

# DETECTION_FOLDER = "blind_spot_detection"

# SAVE_FOLDER = os.path.join(
#     os.path.dirname(os.path.abspath(__file__)),
#     "scripts", "detection", "results", "blind_spot_detection"
# )

# # Exam metadata
# os.environ.setdefault("EXAM_ID", "3")
# os.environ.setdefault("SHIFT_ID", "58")
# # ==================================================

# os.makedirs(SAVE_FOLDER, exist_ok=True)

# filename  = os.path.basename(SOURCE_IMAGE_PATH)
# dest_path = os.path.join(SAVE_FOLDER, filename)

# # Extract device_id and camera_no
# match = re.search(r"_([^_]+)_(\d+)_exam\d+", filename)
# if match:
#     device_id = match.group(1)
#     camera_no = match.group(2)
# else:
#     # simple fallback
#     parts = filename.split('_')
#     print("WARNING: Could not parse properly. Using fallback splits.")
#     device_id = parts[2] if len(parts) > 2 else "unknown"
#     camera_no = parts[3] if len(parts) > 3 else "unknown"

# print("--- Manual Blind Spot (RTSP) Detection Push ---")
# print(f"File     : {filename}")
# print(f"Device ID: {device_id}")
# print(f"Camera No: {camera_no}")
# print(f"To       : {SAVE_FOLDER}")
# print("-------------------------------------------------")

# try:
#     # Create dummy image if it doesn't exist for easy testing
#     if not os.path.exists(SOURCE_IMAGE_PATH):
#         print(f"Source image not found, creating a dummy file at {SOURCE_IMAGE_PATH} for testing")
#         os.makedirs(os.path.dirname(SOURCE_IMAGE_PATH), exist_ok=True)
#         with open(SOURCE_IMAGE_PATH, 'wb') as f:
#             f.write(b'dummy image content')

#     shutil.copy(SOURCE_IMAGE_PATH, dest_path)
#     print("Image successfully copied to results folder.")

#     success = report_to_db_rtsp(device_id=device_id, camera_no=camera_no, image_name=filename, folder=DETECTION_FOLDER)

#     if success:
#         print("SUCCESS: Record added to camera_report_rtsp database.")
#     else:
#         print("FAILED DB: Check db_reports.log for details.")

# except Exception as e:
#     print(f"FATAL ERROR: {e}")


# import os
# import sys
# import shutil
# import re

# # ================= DB REPORTING =================
# _utils_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "detection", "utils")
# if _utils_path not in sys.path:
#     sys.path.insert(0, _utils_path)
# from db_client import report_to_db_rtsp

# # ================= CONFIGURATION =================
# # Expected format: server_room_[device_id]_[camera_no]_exam[exam_id]_[timestamp]_[uuid].jpg
# SOURCE_IMAGE_PATH = "/data/cctv_dev/cctv/server_1000036_21_exam7_20260416_225252_a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d.jpg"

# DETECTION_FOLDER = "server_room_detection"

# SAVE_FOLDER = os.path.join(
#     os.path.dirname(os.path.abspath(__file__)),
#     "scripts", "detection", "results", "server_room_detection"
# )

# # Exam metadata
# os.environ.setdefault("EXAM_ID", "3")
# os.environ.setdefault("SHIFT_ID", "58")
# # ==================================================

# os.makedirs(SAVE_FOLDER, exist_ok=True)

# filename  = os.path.basename(SOURCE_IMAGE_PATH)
# dest_path = os.path.join(SAVE_FOLDER, filename)

# # Extract device_id and camera_no
# match = re.search(r"_([^_]+)_(\d+)_exam\d+", filename)
# if match:
#     device_id = match.group(1)
#     camera_no = match.group(2)
# else:
#     # simple fallback
#     parts = filename.split('_')
#     print("WARNING: Could not parse properly. Using fallback splits.")
#     device_id = parts[2] if len(parts) > 2 else "unknown"
#     camera_no = parts[3] if len(parts) > 3 else "unknown"

# print("--- Manual Server Room (RTSP) Detection Push ---")
# print(f"File     : {filename}")
# print(f"Device ID: {device_id}")
# print(f"Camera No: {camera_no}")
# print(f"To       : {SAVE_FOLDER}")
# print("--------------------------------------------------")

# try:
#     # Create dummy image if it doesn't exist for easy testing
#     if not os.path.exists(SOURCE_IMAGE_PATH):
#         print(f"Source image not found, creating a dummy file at {SOURCE_IMAGE_PATH} for testing")
#         os.makedirs(os.path.dirname(SOURCE_IMAGE_PATH), exist_ok=True)
#         with open(SOURCE_IMAGE_PATH, 'wb') as f:
#             f.write(b'dummy image content')

#     shutil.copy(SOURCE_IMAGE_PATH, dest_path)
#     print("Image successfully copied to results folder.")

#     success = report_to_db_rtsp(device_id=device_id, camera_no=camera_no, image_name=filename, folder=DETECTION_FOLDER)

#     if success:
#         print("SUCCESS: Record added to camera_report_rtsp database.")
#     else:
#         print("FAILED DB: Check db_reports.log for details.")

# except Exception as e:
#     print(f"FATAL ERROR: {e}")
