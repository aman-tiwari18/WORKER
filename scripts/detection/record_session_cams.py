"""
Record a sample of an exam's cameras in parallel, for later offline tests.

ffmpeg stream copy (no re-encode, negligible CPU), one ffmpeg per camera, cut into chunks.
Uses the detection pipeline's Camview login (token server or CAMVIEW_AUTH_*), and asks for a
fresh stream link on every reconnect, so it runs inside the worker container next to a session.

  python record_session_cams.py --exam 40 --shift 152 --date 2026-10-07 --count 20 --minutes 25
        [--stream sub|main] [--cls "Inside Lab"] [--segment 600]

Cameras: the exam's accessible cameras scheduled for that date + shift, of the given camera
types, spread over as many centres as possible. Each opens one extra stream on its DVR.
Output: /app/data/recordings/<date>_exam<E>_shift<S>_<stream>/<device>_<cam>/<device>_<cam>_YYYYmmdd_HHMMSS.mp4
plus cameras.json (which cameras, their centre and type).
"""
import argparse
import json
import os
import random
import signal
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    p = argparse.ArgumentParser(description="Record a sample of an exam's cameras in parallel")
    p.add_argument("--exam", required=True)
    p.add_argument("--shift", required=True, help="exam_shifts.id")
    p.add_argument("--date", required=True, help="YYYY-MM-DD (centre schedule)")
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--camera-list", default="", help="JSON file [{camera_id, camera_no}, ...]: record exactly these cameras")
    p.add_argument("--no-schedule", action="store_true", help="do not limit the cameras to the centres scheduled for --date/--shift")
    p.add_argument("--minutes", type=float, required=True)
    p.add_argument("--stream", choices=("sub", "main"), default="sub")
    p.add_argument("--cls", nargs="+", default=["Inside Lab"], help="camera types (assignment names)")
    p.add_argument("--segment", type=int, default=600, help="chunk length in seconds")
    p.add_argument("--out", default="/app/data/recordings")
    args = p.parse_args()

    # the pipeline reads these at import
    os.environ["DSS_STREAM_TYPE"] = "2" if args.stream == "sub" else "1"
    os.environ["SHIFT_ID"], os.environ["ASSIGNMENT_DATE"] = str(args.shift), args.date
    if args.no_schedule:
        del os.environ["ASSIGNMENT_DATE"]          # the loader then applies no centre schedule
    for d in (HERE, os.path.join(HERE, "pipeline")):
        sys.path.insert(0, d)
    import master_pipeline as mp_
    import record_rtsp_chunks as rec
    from utils.camera_loader import _camera_matches_script

    pool = mp_._session_camera_pool(str(args.exam), str(args.shift))
    cams = [c for c in pool.values() if _camera_matches_script(c["classification"], None, args.cls)]
    by_centre = {}
    for c in sorted(cams, key=lambda c: (str(c.get("tc_code")), c["camera_id"], c["camera_no"])):
        by_centre.setdefault(str(c.get("tc_code")), []).append(c)
    rnd = random.Random(f"{args.exam}-{args.shift}-{args.date}")
    for lst in by_centre.values():
        rnd.shuffle(lst)
    if args.camera_list:
        want = {(str(x["camera_id"]), str(x["camera_no"])) for x in json.load(open(args.camera_list))}
        listed = [c for c in pool.values() if (c["camera_id"], c["camera_no"]) in want]
        missing = len(want) - len(listed)
        if missing:
            rec.logger.warning(f"{missing} camera(s) of the list are not in the exam's camera list and are skipped")
        by_centre = {"list": listed}
        args.count = len(listed)
    picked, centres = [], sorted(by_centre)
    rnd.shuffle(centres)
    while len(picked) < args.count and any(by_centre.values()):
        for tc in centres:                                  # one camera per centre per round
            if by_centre[tc] and len(picked) < args.count:
                picked.append(by_centre[tc].pop())
    if not picked:
        rec.logger.error("no cameras match — nothing to record")
        return

    out_root = os.path.join(args.out, f"{args.date}_exam{args.exam}_shift{args.shift}_{args.stream}")
    os.makedirs(out_root, exist_ok=True)
    with open(os.path.join(out_root, "cameras.json"), "w") as f:
        json.dump([{k: c.get(k) for k in ("camera_id", "camera_no", "classification", "tc_code",
                                          "centre_name", "camera_name")} for c in picked], f, indent=1)
    deadline = time.time() + args.minutes * 60
    rec.logger.info(f"recording {len(picked)} cameras from {len({c.get('tc_code') for c in picked})} centres "
                    f"({args.stream} stream) for {args.minutes:g} min -> {out_root}")

    signal.signal(signal.SIGINT, lambda *_: rec.STOP.set())
    signal.signal(signal.SIGTERM, lambda *_: rec.STOP.set())

    def record(cam):
        dev, ch = cam["camera_id"], cam["camera_no"]
        label = f"{dev}_{ch}"
        server = cam["cctv_url"]
        if "://" in server:
            server = mp_.urllib.parse.urlparse(server).netloc
        out_dir = os.path.join(out_root, label)
        os.makedirs(out_dir, exist_ok=True)
        pattern = os.path.join(out_dir, f"{label}_%Y%m%d_%H%M%S.mp4")
        while not rec.STOP.is_set() and time.time() < deadline:
            token = mp_.get_server_token(server)
            res = mp_.get_rtsp_from_dss(dev, ch, token, server) if token else None
            if not res:
                time.sleep(rec.RETRY_DELAY)
                continue
            urls, stream_token = res
            for url in [mp_.add_token_to_url(u, stream_token) for u in mp_.order_urls(urls)]:
                if rec.STOP.is_set() or time.time() >= deadline or mp_.is_private_ip_url(url):
                    continue
                cmd = rec.build_ffmpeg_cmd(url, pattern, args.segment, False)
                i = cmd.index("-timeout")               # ffmpeg 4.x: -timeout means "listen"; -stimeout = socket timeout (µs)
                cmd[i] = "-stimeout"
                ran = rec.run_ffmpeg(cmd, label, max_seconds=deadline - time.time())
                if ran > 30:
                    break                                   # it recorded: get a fresh link next time
            time.sleep(min(rec.RETRY_DELAY, max(0, deadline - time.time())))

    threads = [threading.Thread(target=record, args=(c,), daemon=True) for c in picked]
    for t in threads:
        t.start()
        time.sleep(0.2)
    for t in threads:
        t.join()
    rec.logger.info(f"recording finished -> {out_root}")


if __name__ == "__main__":
    main()
