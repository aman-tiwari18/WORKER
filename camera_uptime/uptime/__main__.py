"""CLI:
  python -m uptime run                                  the service (pm2 runs this)
  python -m uptime dry-run [--at "YYYY-MM-DD HH:MM"]     live runs + roster counts, touches no camera
  python -m uptime check-one --camera-id N              one RTSP check (camera_links_rtsp.id), no DB writes
  python -m uptime check-one --exam-id E --ch-code X    one HLS check, no DB writes
  python -m uptime rebuild-summary --run-id N           recompute a run's summary and down periods from its raw checks
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime

from . import config


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level, logging.INFO), stream=sys.stdout,
                        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_dry_run(s, at: str | None) -> int:
    from .db import Database
    from .roster import in_scope, load_run_roster, merge_rosters
    from .schedule import load_live_windows
    from .service import now_ist

    db = Database(s)
    now = datetime.strptime(at, "%Y-%m-%d %H:%M") if at else now_ist()
    wins = load_live_windows(db, now, s.start_hold_min, s.max_window_hours)
    print(f"At {now:%Y-%m-%d %H:%M} IST: {len(wins)} live run(s) (start hold {s.start_hold_min} min)")
    if s.shard_count > 1:
        print(f"Shard {s.shard_index} of {s.shard_count} ({s.worker_id or 'no worker id'}): this instance's cameras only")
    elif s.shard_auto:
        print("Auto sharding: counts below are for the whole exam (each live instance takes its part)")
    rosters = {}
    for w in wins:
        cams = {k: c for k, c in (load_run_roster(db, w) or {}).items() if in_scope(c, s)}
        rosters[w.key] = cams
        print(f"\n  exam {w.exam_id} '{w.exam_name}' [{w.link_type}] {w.exam_date} shift {w.shift_id} "
              f"'{w.shift_name}' {w.shift_start}-{w.shift_end}  window {w.window_start:%H:%M}-{w.window_end:%H:%M}")
        _print_lanes(cams)
    if len(wins) > 1:
        print("\n  union (a camera in several runs is checked once):")
        _print_lanes(merge_rosters(rosters))
    return 0


def _print_lanes(cams) -> None:
    per_lane, dvrs = Counter(), {}
    for c in cams.values():
        per_lane[c.lane] += 1
        dvrs.setdefault(c.lane, set()).add(c.dvr_key)
    total_dvrs = sum(len(v) for v in dvrs.values())
    print(f"    {len(cams)} cameras on {total_dvrs} DVRs/centres")
    for lane in sorted(per_lane):
        print(f"      {lane:<22} {per_lane[lane]:>5} cameras  {len(dvrs[lane]):>4} DVRs/centres")


def cmd_check_one(s, camera_id: int | None, exam_id: int | None, ch_code: str | None, save: bool) -> int:
    from .camview import Camview, TokenClient
    from .capture import Ffmpeg, save_frame
    from .checker import Checker
    from .db import Database
    from .hls import HlsClient, TokenBucket
    from .roster import hls_camera, rtsp_camera

    db = Database(s)
    if camera_id:
        rows = db.query("SELECT id, device_id, camera_no, camera_name, centre_name, tc_code, city, state, cctv_url "
                        "FROM camera_links_rtsp WHERE id = %s", (camera_id,))
        cam = rtsp_camera(rows[0], None) if rows else None
    elif exam_id and ch_code:
        rows = db.query("SELECT id, ch_code, camera_name, centre_name, tc_code, city, state, cctv_url "
                        "FROM camera_links_hls WHERE exam_id = %s AND ch_code = %s ORDER BY id DESC LIMIT 1",
                        (exam_id, ch_code))
        cam = hls_camera(rows[0], None) if rows else None
    else:
        print("give --camera-id, or --exam-id with --ch-code", file=sys.stderr)
        return 2
    if cam is None:
        print("camera not found", file=sys.stderr)
        return 1
    tokens = TokenClient(s.token_server)
    bucket = TokenBucket(s.hls_req_per_s)
    checker = Checker(s, Camview(tokens, s.rtsp_stream_type, s.camview_api_timeout_s, s.stop_video),
                      HlsClient(bucket, s.hls_portal_timeout_s), Ffmpeg(width=s.frame_width), bucket)
    res = checker.check(cam)
    out = {k: v for k, v in res.__dict__.items() if k != "jpeg"}
    out.update(camera_key=cam.key, camera_name=cam.members[0].camera_name, frame_bytes=len(res.jpeg or b""))
    if save and res.jpeg:
        rel = f"_check_one/{cam.key.replace('|', '_').replace('/', '_')}.jpg"
        if save_frame(s.frames_root, rel, res.jpeg):
            out["saved_to"] = f"{s.frames_root}/{rel}"
    print(json.dumps(out, indent=2, default=str))
    return 0 if res.result == "UP" else 3


REBUILD_SQL = """
UPDATE camera_uptime_summary s
LEFT JOIN (
  SELECT camera_key, COUNT(*) AS total, SUM(result = 'UP') AS up, SUM(result = 'DOWN') AS down,
         SUM(result = 'UNKNOWN') AS unk, SUM(frame_blank = 1) AS blank, MIN(checked_at) AS first_c,
         MAX(checked_at) AS last_c, MIN(IF(result = 'UP', checked_at, NULL)) AS first_up,
         MAX(IF(result = 'UP', checked_at, NULL)) AS last_up, MAX(IF(result = 'DOWN', checked_at, NULL)) AS last_down
    FROM camera_uptime_checks WHERE run_id = %s GROUP BY camera_key) c ON c.camera_key = s.camera_key
SET s.checks_total = COALESCE(c.total, 0), s.checks_up = COALESCE(c.up, 0), s.checks_down = COALESCE(c.down, 0),
    s.checks_unknown = COALESCE(c.unk, 0), s.checks_blank = COALESCE(c.blank, 0),
    s.first_checked_at = c.first_c, s.last_checked_at = c.last_c, s.first_up_at = c.first_up,
    s.last_up_at = c.last_up, s.last_down_at = c.last_down,
    s.last_result = COALESCE((SELECT x.result FROM camera_uptime_checks x WHERE x.run_id = s.run_id
                               AND x.camera_key = s.camera_key ORDER BY x.checked_at DESC, x.id DESC LIMIT 1), 'NOT_CHECKED'),
    s.last_reason = (SELECT x.reason FROM camera_uptime_checks x WHERE x.run_id = s.run_id
                      AND x.camera_key = s.camera_key ORDER BY x.checked_at DESC, x.id DESC LIMIT 1)
WHERE s.run_id = %s
"""


def cmd_rebuild(s, run_id: int) -> int:
    from . import downtime, runs
    from .db import Database

    db = Database(s)
    n = db.execute(REBUILD_SQL, (run_id, run_id))
    runs.update_counters(db, run_id)
    d = downtime.rebuild(db, run_id)
    print(f"run {run_id}: {n} summary rows recomputed, {d} cameras with down periods")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="uptime", description="Camera uptime checker")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run")
    d = sub.add_parser("dry-run")
    d.add_argument("--at", help='IST time "YYYY-MM-DD HH:MM" (default: now)')
    c = sub.add_parser("check-one")
    c.add_argument("--camera-id", type=int)
    c.add_argument("--exam-id", type=int)
    c.add_argument("--ch-code")
    c.add_argument("--save", action="store_true", help="save the frame under FRAMES_ROOT/_check_one/")
    r = sub.add_parser("rebuild-summary")
    r.add_argument("--run-id", type=int, required=True)
    args = ap.parse_args(argv)

    s = config.load()
    _setup_logging(s.log_level)
    cmd = args.cmd or "run"
    if cmd == "dry-run":
        return cmd_dry_run(s, args.at)
    if cmd == "check-one":
        return cmd_check_one(s, args.camera_id, args.exam_id, args.ch_code, args.save)
    if cmd == "rebuild-summary":
        return cmd_rebuild(s, args.run_id)
    from .service import Service

    return Service(s).run_forever()


if __name__ == "__main__":
    sys.exit(main())
