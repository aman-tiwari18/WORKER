#!/usr/bin/env python3
"""
check_hls_links.py — are an exam's HLS camera links actually live right now?

Runs exactly the two steps the classification pipeline's Pass-1 does, so the
verdict here matches what vision_classification.py reports:

  1. GET the portal URL stored in camera_links_hls.cctv_url, follow redirects,
     and pull the .m3u8 out of the response  (failure -> "hls_extract_failed")
  2. GET that .m3u8 playlist                 (non-200 -> "http_<status>")

Reports the breakdown, per-centre counts, and — with -v — the full exchange for
a few cameras (portal status, resolved playlist URL, playlist status and body),
which is what tells an out-of-window feed apart from an auth/token problem.

Usage:
  python3 ops/check_hls_links.py --exam 33
  python3 ops/check_hls_links.py --exam 33 --shift 139 --limit 50 -v
  python3 ops/check_hls_links.py --exam 33 --all          # every camera
"""

import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import pymysql
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)

# Same .env the pipeline reads, so we hit the same database.
def _load_env():
    path = os.path.join(PROJECT_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()

DB = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "cctv_monitoring"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}

TIMEOUT = 10


def fetch_cameras(exam_id, shift_id, limit, take_all):
    sql = (
        "SELECT id, ch_code, tc_code, centre_name, city, camera_name, cctv_url, "
        "       classification, shift_id "
        "  FROM camera_links_hls "
        " WHERE exam_id = %s AND is_active = 1"
    )
    params = [exam_id]
    if shift_id:
        sql += " AND shift_id = %s"
        params.append(shift_id)
    sql += " ORDER BY tc_code, ch_code"
    if not take_all:
        sql += " LIMIT %s"
        params.append(limit)

    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def extract_m3u8(text):
    """Same three methods the pipeline uses."""
    m = re.findall(r'https?://[^\s<>"]+?\.m3u8[^\s<>"]*', text)
    if m:
        return m[0]
    if ".m3u8" in text:
        m = re.findall(r'(["\'])(https?://[^"\']+\.m3u8[^"\']*)(["\'])', text)
        if m:
            return m[0][1]
    return None


def check(cam):
    """Returns a dict describing what happened for one camera."""
    out = {
        "ch_code": cam["ch_code"],
        "tc_code": cam["tc_code"],
        "centre": cam["centre_name"],
        "portal_status": None,
        "portal_len": 0,
        "portal_snippet": "",
        "m3u8": None,
        "m3u8_status": None,
        "m3u8_snippet": "",
        "reason": None,
        "ok": False,
    }
    url = cam["cctv_url"]
    if not url:
        out["reason"] = "no_url"
        return out

    try:
        r = requests.get(url, timeout=TIMEOUT, allow_redirects=True, verify=False)
        out["portal_status"] = r.status_code
        out["portal_len"] = len(r.text or "")
        out["portal_snippet"] = " ".join((r.text or "")[:300].split())
        if r.status_code != 200:
            out["reason"] = f"portal_http_{r.status_code}"
            return out
        playlist = extract_m3u8(r.text or "")
    except Exception as exc:
        out["reason"] = f"portal_error:{type(exc).__name__}"
        return out

    if not playlist:
        out["reason"] = "hls_extract_failed"
        return out
    out["m3u8"] = playlist

    try:
        r2 = requests.get(playlist, timeout=TIMEOUT, verify=False)
        out["m3u8_status"] = r2.status_code
        out["m3u8_snippet"] = " ".join((r2.text or "")[:300].split())
        out["ok"] = r2.status_code == 200
        out["reason"] = "OK" if out["ok"] else f"http_{r2.status_code}"
    except Exception as exc:
        out["reason"] = f"m3u8_error:{type(exc).__name__}"
    return out


def main():
    ap = argparse.ArgumentParser(description="Check whether an exam's HLS camera links are live right now")
    ap.add_argument("--exam", type=int, required=True, help="exams.id")
    ap.add_argument("--shift", type=int, help="exam_shifts.id (default: all shifts of the exam)")
    ap.add_argument("--limit", type=int, default=25, help="how many cameras to test (default 25)")
    ap.add_argument("--all", action="store_true", help="test every camera, ignoring --limit")
    ap.add_argument("--workers", type=int, default=32, help="concurrent requests (default 32)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print the full exchange for a few cameras")
    args = ap.parse_args()

    cams = fetch_cameras(args.exam, args.shift, args.limit, args.all)
    if not cams:
        print(f"No active HLS cameras for exam {args.exam}" + (f" shift {args.shift}" if args.shift else ""))
        return 1

    scope = "all" if args.all else f"first {len(cams)}"
    print(f"Testing {len(cams)} cameras ({scope}) of exam {args.exam}"
          + (f", shift {args.shift}" if args.shift else "") + f" with {args.workers} workers\n")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(check, cams))

    reasons = Counter(r["reason"] for r in results)
    live = sum(1 for r in results if r["ok"])

    print("=" * 62)
    print(f"  LIVE (playlist 200) : {live} / {len(results)}")
    print("  Breakdown:")
    for reason, n in reasons.most_common():
        print(f"    {reason:<26} {n}")
    print("=" * 62)

    portal = Counter(r["portal_status"] for r in results)
    print("\nPortal URL responses:", dict(portal))
    extracted = sum(1 for r in results if r["m3u8"])
    print(f"Playlist URL extracted for {extracted}/{len(results)} cameras")
    m3u8 = Counter(r["m3u8_status"] for r in results if r["m3u8_status"] is not None)
    if m3u8:
        print("Playlist responses:", dict(m3u8))

    by_centre = defaultdict(lambda: [0, 0])
    for r in results:
        slot = by_centre[f"{r['tc_code']} {str(r['centre'])[:28]}"]
        slot[1] += 1
        if r["ok"]:
            slot[0] += 1
    print("\nPer centre (live/total):")
    for name, (ok, total) in sorted(by_centre.items()):
        print(f"  {name:<36} {ok}/{total}")

    if args.verbose:
        print("\n" + "=" * 62)
        print("  SAMPLE EXCHANGES")
        print("=" * 62)
        for r in results[:3]:
            print(f"\ncamera {r['ch_code']} ({r['centre']})")
            print(f"  portal  : HTTP {r['portal_status']}  ({r['portal_len']} bytes)")
            print(f"            {r['portal_snippet'][:220]}")
            print(f"  playlist: {r['m3u8']}")
            print(f"            HTTP {r['m3u8_status']}")
            if r["m3u8_snippet"]:
                print(f"            {r['m3u8_snippet'][:220]}")
            print(f"  verdict : {r['reason']}")

    print(f"\nVERDICT: {'links are LIVE' if live else 'links are NOT live right now'}"
          f" ({live}/{len(results)} playlists returned 200)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
