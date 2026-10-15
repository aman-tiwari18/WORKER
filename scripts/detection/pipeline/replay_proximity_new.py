"""
Replay recorded camera clips through the unified worker's proximity analytic, the way the live session
pipeline feeds it (a frame, then the confirming frame 10 s later), and list what would be reported.
Nothing is written to the database; the images go to --out.

  python replay_proximity_new.py --clips '/tmp/rec7_all/*.mp4' --step 60 --confirm 10 --out /tmp/replay
"""
import argparse
import glob
import os
import queue
import re
import sys
import threading
import time

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "utils"))

CLIP = re.compile(r"(?P<dev>\d+)_(?P<cam>\d+)_(?P<d>\d{8})_(?P<t>\d{6})\.mp4$")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", required=True)
    ap.add_argument("--step", type=float, default=60, help="seconds between a camera's samples")
    ap.add_argument("--confirm", type=float, default=10, help="seconds from a sample to its confirming frame")
    ap.add_argument("--max-seconds", type=float, default=600)
    ap.add_argument("--cooldown", type=int, default=120)
    ap.add_argument("--out", default="/tmp/replay_proximity")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    os.environ["PROXIMITY_ENGINE"] = "new"
    import unified_detector as u
    import proximity_repeat
    import tempfile
    proximity_repeat.REPEAT_DIR = tempfile.mkdtemp(prefix="prox_replay_")

    reported = []

    def capture(frame, *, folder, script_name, device_id, camera_no, exam_id="0", shift_id="0",
                source_type="RTSP", violation_count=None, violation_chain=None, issue=""):
        name = f"{device_id}_{camera_no}_{len(reported) + 1:03d}_count{violation_count}.jpg"
        cv2.imwrite(os.path.join(args.out, name), frame)
        reported.append({"camera": f"{device_id}_{camera_no}", "count": violation_count, "chain": violation_chain, "file": name})

    u._save_and_report = capture
    u.COOLDOWNS["proximity"] = args.cooldown
    q = queue.Queue()
    worker = threading.Thread(target=u.unified_worker, args=(q, ["proximity"]), daemon=True)
    worker.start()

    clips = sorted(glob.glob(args.clips))
    sent = {}
    t0 = time.time()
    for clip in clips:
        m = CLIP.search(clip)
        if not m:
            continue
        cap = cv2.VideoCapture(clip)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        dur = min(args.max_seconds, total / fps if total else 0)
        base = time.mktime(time.strptime(m["d"] + m["t"], "%Y%m%d%H%M%S"))
        cam = f"{m['dev']}_{m['cam']}"

        def grab(sec):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(sec * fps))
            ok, fr = cap.read()
            return fr if ok else None

        t = 0.0
        while t + args.confirm < dur:
            for off in (0.0, args.confirm):
                fr = grab(t + off)
                if fr is None:
                    continue
                q.put({"frame": fr, "device_id": m["dev"], "ch_no": m["cam"], "camera_no": m["cam"], "camera_id_pk": "",
                       "exam_id": "40", "shift_id": "151", "source_type": "RTSP", "enabled_scripts": ["proximity"],
                       "captured_at": base + t + off, "zoom": 1280})
                sent[cam] = sent.get(cam, 0) + 1
            t += args.step
        cap.release()
    q.put(None)
    worker.join(timeout=3600)
    print(f"replayed {sum(sent.values())} frames from {len(sent)} cameras in {time.time() - t0:.0f}s")
    by = {}
    for r in reported:
        by.setdefault(r["camera"], []).append(r["count"])
    print(f"reported {len(reported)} alerts on {len(by)} cameras:", {k: v for k, v in sorted(by.items())})


if __name__ == "__main__":
    main()
