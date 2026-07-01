import os
import shutil
import re
import sys
from datetime import datetime

SOURCE_ROOT = "/data/cctv_dev/cctv/results/"
DEST_ROOT = "/data/cctv_dev/cctv-analytics-system/results/proof_of_work"

# VALID_FOLDERS = [
#     "blind_spot_detection_results",       # ✅ naya
#     "camera_inversion_detection_results",
#     "camera_tampering_detection_results",
#     "crowd_detection_results",
#     "entry_exit_detection_results",
#     "phone_detection_results",
#     "proximity_detection_results",
#     "server_room_detection_results",
#     "zero_person_detection",              # ✅ naya
# ]
VALID_FOLDERS = [
    "proximity_detection",       # ✅ naya
    "entry_exit_detection",
    "crowd_detection_biometric",
    "crowd_detection",
    "invigilator_detection",
    "strongroom_detection",
    "qp_box_missing",            # ✅ naya
]

SHIFTS = ["shift1"]

timestamp_pattern = re.compile(r'(\d{8})_(\d{6})')

# ✅ Valid filename pattern
VALID_FILENAME_PATTERN = re.compile(
    r'^(violation|EntryExit|tampering|server|phone|crowd|Inversion|blind|zero)'
    r'_ch\d+_\d{8}_\d{6}(\w*)\.jpg$',
    re.IGNORECASE
)

def is_valid_filename(filename):
    return bool(VALID_FILENAME_PATTERN.match(filename))

def get_shift(time_str):
    t = datetime.strptime(time_str, "%H%M%S").time()
    if datetime.strptime("09:00:00", "%H:%M:%S").time() <= t <= datetime.strptime("18:00:00", "%H:%M:%S").time():
        return "shift1"
    # if datetime.strptime("12:30:00", "%H:%M:%S").time() <= t <= datetime.strptime("14:00:00", "%H:%M:%S").time():
    #     return "shift2"
    # if datetime.strptime("16:00:00", "%H:%M:%S").time() <= t <= datetime.strptime("17:30:00", "%H:%M:%S").time():
    #     return "shift3"
    return None

def create_base_structure(today_folder):
    for shift in SHIFTS:
        for folder in VALID_FOLDERS:
            os.makedirs(os.path.join(today_folder, shift, folder), exist_ok=True)

def main():
    if len(sys.argv) > 1:
        date_input = sys.argv[1]  # format: YYYY-MM-DD
        dt = datetime.strptime(date_input, "%Y-%m-%d")
    else:
        dt = datetime.now()

    today_date = dt.strftime("%Y-%m-%d")
    today_str = dt.strftime("%Y%m%d")
    today_folder = os.path.join(DEST_ROOT, today_date)
    create_base_structure(today_folder)

    skipped = 0
    copied = 0

    for folder in VALID_FOLDERS:
        src_folder = os.path.join(SOURCE_ROOT, folder)
        if not os.path.exists(src_folder):
            continue
        print(f"Processing {folder}")

        for root, dirs, files in os.walk(src_folder):
            for file in files:
                if not file.lower().endswith((".jpg", ".jpeg", ".png")):
                    continue

                # ✅ Skip files that don't match the valid naming pattern
                if not is_valid_filename(file):
                    print(f"  ⏭ Skipped (invalid format): {file}")
                    skipped += 1
                    continue

                match = timestamp_pattern.search(file)
                if not match:
                    continue

                date_str = match.group(1)
                time_str = match.group(2)

                if date_str != today_str:
                    continue

                shift = get_shift(time_str)
                if shift is None:
                    continue

                src = os.path.join(root, file)
                dst_folder = os.path.join(today_folder, shift, folder)
                os.makedirs(dst_folder, exist_ok=True)
                dst = os.path.join(dst_folder, file)
                shutil.copy2(src, dst)
                copied += 1

    print(f"\n✅ Proof of work created for today")
    print(f"   📁 Copied : {copied} files")
    print(f"   ⏭ Skipped: {skipped} files (invalid format)")

if __name__ == "__main__":
    main()