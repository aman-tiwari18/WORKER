import subprocess
import sys
import os

VIDEO = "NVR_ch19_main_20231206120001_20231206130000.dav"

SCRIPTS = [
    "Crowd_det_complete.py",
    "close_proximity.py",
    "Entry_exit_det.py",
    "Intruder_det.py",
    "invigilator_tracking.py",
    "phone_det_complete.py",
    "Server_room_det.py"
]

processes = []

print("🚀 Launching all analytics scripts...\n")

for script in SCRIPTS:
    print(f"▶ Starting {script}")
    p = subprocess.Popen(
        [sys.executable, script, VIDEO]
    )
    processes.append(p)

for p in processes:
    p.wait()

print("✅ All scripts finished")
