"""Run the full Plan B pipeline on recorded videos instead of live cameras.

Each video becomes one "camera" (file:// source). Uses the real DeepStream
decode, TensorRT models and analytics rules, in dry-run mode: annotated hits are
saved to --out and nothing is written to the DB or sent as SMS.

    cd scripts/detection && python -m pathb.tools.run_video_test \
        --video /path/to/videos --analytics proximity crowd_five phone \
        --seconds 300 --copies 8 --out /tmp/pathb_video_test

--copies N plays every video N times as separate cameras, to load-test the GPU
(e.g. 20 videos x 10 copies = 200 concurrent streams).

Needs the DeepStream container (deploy/deepstream/) with the engines built.
"""
import argparse
import glob
import logging
import os
import time

VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".h264", ".h265", ".dav")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", action="append", required=True, help="video file or folder (repeatable)")
    ap.add_argument("--analytics", nargs="+", default=["proximity", "crowd_five", "phone"])
    ap.add_argument("--seconds", type=float, default=300)
    ap.add_argument("--copies", type=int, default=1, help="play each video this many times")
    ap.add_argument("--out", default="/tmp/pathb_video_test")
    ap.add_argument("--no-loop", action="store_true", help="stop each video at its end instead of looping")
    ap.add_argument("--all-frames", action="store_true", help="decode every frame, not keyframes only")
    args = ap.parse_args()

    files = []
    for v in args.video:
        if os.path.isdir(v):
            files += sorted(p for p in glob.glob(os.path.join(v, "**", "*"), recursive=True)
                            if p.lower().endswith(VIDEO_EXT))
        else:
            files.append(v)
    if not files:
        raise SystemExit("no videos found")

    os.environ["PATHB_DRY_RUN_DIR"] = args.out
    os.environ.setdefault("PATHB_LOOP_FILES", "0" if args.no_loop else "1")
    os.environ.setdefault("PATHB_KEYFRAMES_ONLY", "0" if args.all_frames else "1")
    n = len(files) * args.copies
    os.environ.setdefault("PATHB_MAX_SOURCES_PER_PIPELINE", str(n))

    cams = []
    for copy in range(args.copies):
        for i, path in enumerate(files):
            for script in args.analytics:
                cams.append({"camera_id": f"video{i}", "ch_no": str(copy + 1), "script_code": script,
                             "cctv_url": "file://" + os.path.abspath(path),
                             "exam_id": "0", "shift_id": "0", "tc_code": ""})

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    logging.info("video test: %d videos x %d copies = %d cameras, analytics %s, %ds -> %s",
                 len(files), args.copies, n, args.analytics, args.seconds, args.out)

    from pathb.runner import run_pathb
    deadline = time.monotonic() + args.seconds
    run_pathb(cams, "RTSP", helpers=None, should_stop=lambda: time.monotonic() >= deadline)
    hits = [f for f in os.listdir(args.out) if f.endswith(".jpg")] if os.path.isdir(args.out) else []
    logging.info("done: %d annotated hits in %s", len(hits), args.out)


if __name__ == "__main__":
    main()
