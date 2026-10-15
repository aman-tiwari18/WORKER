"""
Run Proximity_New_Script on camera clips and report each confirmed close pair as a detection.

  python proximity_new_runner.py --clips /app/data/recordings/<dir> --exam 40 --shift 152 \
         [--mode closeness|relative] [--max-seconds 120] [--shard 0/3] [--no-report]

Clips are the recorder's files, named <device>_<camera>_<YYYYmmdd>_<HHMMSS>.mp4 (the stamp is the
clip's start time). For every clip the script's own pipeline runs (detector + pose + tracking +
closeness rules + invigilator filter + temporal confirmation); each confirmed pair becomes ONE saved
image (the annotated frame, the pair in red) and one row in camera_report_rtsp as detection type
proximity_detection, so it shows on the Detections page as "Close Proximity" with the time it was
filmed. At most one report per camera per --cooldown seconds, like the other analytics.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
for d in (HERE, os.path.join(HERE, "utils")):
    if d not in sys.path:
        sys.path.insert(0, d)

CLIP_RE = re.compile(r"^(?P<dev>\d+)_(?P<cam>\d+)_(?P<d>\d{8})_(?P<t>\d{6})\.mp4$")
FOLDER = "proximity_detection"


def make_config(P, models_dir, mode, fps):
    """Same settings Proximity_New_Script.main() applies for --closeness / --relative."""
    cfg = P.Config()
    cfg.yolo_weights = os.path.join(models_dir, "yolo11m.pt")
    cfg.pose_weights = os.path.join(models_dir, "yolo11m-pose.pt")
    if fps:
        cfg.sample_fps = fps
    if mode == "relative":
        cfg.relative_proximity = True
        cfg.proximity_m = cfg.rel_thresh
        cfg.gather_m = cfg.rel_gather_thresh
        cfg.row_aware = False
        cfg.require_interaction = True
        cfg.invig_mobility_std = 1e9
        cfg.autocalib = None
    else:
        cfg.closeness_mode = True
        cfg.proximity_m = cfg.close_nd
        cfg.gather_m = cfg.close_nd
        cfg.row_aware = False
        cfg.require_interaction = False
        cfg.exclude_invigilators = True
        cfg.invig_mobility_std = 1e9
        cfg.draw_red_only = True
        cfg.min_event_s = 1.0
        cfg.persist_frames = 2
        cfg.autocalib = None
    cfg.draw = True
    return cfg


def trim(src, dst, seconds):
    """First `seconds` of a clip, stream copy (fast). Returns the path to analyse."""
    if not seconds:
        return src
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-t", str(seconds),
                        "-c", "copy", dst], capture_output=True, text=True)
    return dst if r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0 else src


def annotated_frame(video, index):
    cap = cv2.VideoCapture(video)
    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def banner(frame, lines):
    """Status text in the corner, like the other analytics' alert images."""
    h, w = frame.shape[:2]
    scale = max(0.5, w / 1280.0)
    pad = int(8 * scale)
    top = int(30 * scale)            # the script's own status line sits in the first rows: start below it
    box_w = int(min(w - 2 * pad, 560 * scale))
    box_h = int((34 * len(lines) + 14) * scale)
    over = frame.copy()
    cv2.rectangle(over, (pad, top), (pad + box_w, top + box_h), (0, 0, 0), -1)
    cv2.addWeighted(frame, 0.65, over, 0.35, 0, frame)
    for i, (text, color) in enumerate(lines):
        cv2.putText(frame, text, (pad + int(10 * scale), top + int((30 + 34 * i) * scale)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8 * scale if i == 0 else 0.6 * scale, color, 2 if i == 0 else 1)
    return frame


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clips", required=True, help="folder (searched recursively) or one .mp4")
    ap.add_argument("--exam", required=True)
    ap.add_argument("--shift", required=True)
    ap.add_argument("--mode", choices=("closeness", "relative"), default="closeness")
    ap.add_argument("--max-seconds", type=float, default=0, help="analyse only the first N seconds of each clip")
    ap.add_argument("--fps", type=float, default=0, help="frames per second analysed (default: the script's 3)")
    ap.add_argument("--cooldown", type=float, default=120, help="seconds between reports of one camera")
    ap.add_argument("--shard", default="0/1", help="i/n: take every n-th clip, starting at i")
    ap.add_argument("--models-dir", default=os.environ.get("MODELS_DIR", "/app/models"))
    ap.add_argument("--script", default=os.path.join(HERE, "Proximity_New_Script.py"))
    ap.add_argument("--work", default="/tmp/proximity_new_work")
    ap.add_argument("--gpu", default=os.environ.get("WORKER_INDEX", "0"), help="gpu number written into the file name")
    ap.add_argument("--no-report", action="store_true", help="write no database row (and save no image unless --save-dir is given)")
    ap.add_argument("--save-dir", default="", help="with --no-report: save each flagged image here, for review")
    args = ap.parse_args()

    os.environ["EXAM_ID"], os.environ["SHIFT_ID"] = str(args.exam), str(args.shift)
    sys.path.insert(0, os.path.dirname(os.path.abspath(args.script)))
    import importlib
    P = importlib.import_module(os.path.splitext(os.path.basename(args.script))[0])

    report = None
    if not args.no_report:
        from utils.db_client import report_to_db_rtsp as report
    results_dir = os.path.join(os.environ.get("RESULTS_DIR", os.path.join(HERE, "results")), FOLDER)
    os.makedirs(results_dir, exist_ok=True)
    os.makedirs(args.work, exist_ok=True)

    clips = [args.clips] if args.clips.endswith(".mp4") else sorted(glob.glob(os.path.join(args.clips, "**", "*.mp4"), recursive=True))
    clips = [c for c in clips if CLIP_RE.match(os.path.basename(c))]
    i, n = (int(x) for x in args.shard.split("/"))
    clips = clips[i::n]
    print(f"[proximity-new] {len(clips)} clip(s), mode {args.mode}, shard {args.shard}", flush=True)

    cfg = make_config(P, args.models_dir, args.mode, args.fps)
    detector = P.PersonDetector(cfg)
    last_report, summary = {}, []
    for ci, clip in enumerate(clips, 1):
        m = CLIP_RE.match(os.path.basename(clip))
        dev, cam = m["dev"], m["cam"]
        start = datetime.strptime(m["d"] + m["t"], "%Y%m%d%H%M%S")
        t0 = time.time()
        try:
            src = trim(clip, os.path.join(args.work, os.path.basename(clip)), args.max_seconds)
            out_dir = os.path.join(args.work, "out")
            res, out_json = P.run_video(src, cfg, out_dir, detector=detector)
        except Exception as e:
            print(f"[{ci}/{len(clips)}] {dev}_{cam}: FAILED {e}", flush=True)
            summary.append({"camera": f"{dev}_{cam}", "clip": clip, "error": str(e)})
            continue
        events = res["confirmed_pair_violations"]
        per_frame = json.load(open(out_json))["per_frame"]
        cap = cv2.VideoCapture(src)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        cap.release()
        step = max(1, int(round(fps / cfg.sample_fps)))
        out_vid = os.path.join(out_dir, os.path.splitext(os.path.basename(src))[0] + "_proximity.mp4")
        reported = []
        for ev in sorted(events, key=lambda e: e["first_s"]):
            when = start + timedelta(seconds=ev["first_s"])
            if last_report.get((dev, cam)) and (when - last_report[(dev, cam)]).total_seconds() < args.cooldown:
                continue
            mid = (ev["first_s"] + ev["last_s"]) / 2.0
            rec = min((f for f in per_frame if any(p["ids"] == ev["ids"] for p in f["pairs"])),
                      key=lambda f: abs(f["time_s"] - mid), default=None)
            frame = annotated_frame(out_vid, rec["frame"] // step) if rec else None
            if frame is None:
                continue
            frame = banner(frame, [("PROXIMITY VIOLATION", (0, 0, 255)),
                                   (f"Close for {ev['last_s'] - ev['first_s']:.0f} s - distance {ev['min_dist_m']:.1f}", (255, 255, 255)),
                                   (f"{dev} - Cam {cam}", (255, 255, 255))])
            fname = f"proximity_{dev}_{cam}_exam{args.exam}_shift{args.shift}_gpu{args.gpu}_{when:%Y%m%d_%H%M%S}_{uuid.uuid4()}.jpg"
            if report is not None:
                cv2.imwrite(os.path.join(results_dir, fname), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
                ok = report(device_id=dev, camera_no=cam, image_name=fname, folder=FOLDER)
            else:
                ok = None
                if args.save_dir:
                    os.makedirs(args.save_dir, exist_ok=True)
                    cv2.imwrite(os.path.join(args.save_dir, fname), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            last_report[(dev, cam)] = when
            reported.append({"at": when.strftime("%H:%M:%S"), "pair": ev["ids"], "secs": round(ev["last_s"] - ev["first_s"], 1),
                             "dist": ev["min_dist_m"], "file": fname, "db_ok": ok})
        for tmp in (out_vid, src if src != clip else None):
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        print(f"[{ci}/{len(clips)}] {dev}_{cam} {os.path.basename(clip)}: {res['frames_analyzed']} frames, "
              f"{len(events)} confirmed pair(s), {len(reported)} reported, {time.time() - t0:.0f}s", flush=True)
        summary.append({"camera": f"{dev}_{cam}", "clip": os.path.basename(clip), "frames": res["frames_analyzed"],
                        "confirmed_pairs": len(events), "reported": reported, "seconds": round(time.time() - t0, 1)})
    out = os.path.join(args.work, f"summary_{args.shard.replace('/', 'of')}.json")
    json.dump(summary, open(out, "w"), indent=1)
    print(f"[proximity-new] done: {sum(len(s.get('reported', [])) for s in summary)} reported -> {out}", flush=True)


if __name__ == "__main__":
    main()
