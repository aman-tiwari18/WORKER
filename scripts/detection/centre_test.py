"""
Live test of one or more analytics on the cameras of specific exam centres.

Runs the real production pipeline (pipeline/master_pipeline.py) restricted to the chosen
centre(s) and camera types. Detections are reported exactly like production
(camera_report_rtsp) and show on the Detections page, tagged with EXAM_ID / SHIFT_ID below.

WHERE IT RUNS: run this script from the main server (worker-0); it starts the test on
RUN_ON (worker-1 by default) over SSH, inside that worker's container. A hand-started
test on worker-0 itself cannot load the monitor model, so worker-0 is not the default.

EDIT THE CENTRE HERE  (or pass --tc-code on the command line, which wins):
"""
# Centre (TC) codes to test — one or many. Edit this list, or use --tc-code on the command line.
TC_CODES = [
    "300415",
    "300413",    # one line per centre; put a # in front of a line to skip that centre
    # "50901",
]
ANALYTICS = ["monitor_tamper"]   # analytics to run — or use --analytics
CAMERA_TYPES = ["computer lab", "inside classroom"]   # camera types — or use --classes; "all" = every type
EXAM_ID, SHIFT_ID = "42", "157"  # reports are tagged with these — or use --exam / --shift
MAX_MINUTES = 30                 # auto-stop after this long — or use --max-minutes
RUN_ON = "worker-1"              # where the test runs — or use --on ; "local" = this machine

# Workers this server can start the test on (key-based SSH, no password).
WORKERS = {
    "worker-1": ("root@192.168.19.43", "cctv_script_server_worker_worker-1"),
    "worker-2": ("root@192.168.19.42", "cctv_script_server_worker_worker-2"),
    "worker-3": ("root@192.168.19.41", "cctv_script_server_worker_worker-3"),
}

HELP_EXAMPLES = """
Run from the main server, in script-server/ :

  venv/bin/python scripts/detection/centre_test.py --dry-run     # list the cameras, start nothing
  venv/bin/python scripts/detection/centre_test.py               # START the test on worker-1
  venv/bin/python scripts/detection/centre_test.py --status      # is it running? cameras / alerts so far
  venv/bin/python scripts/detection/centre_test.py --logs        # follow the alerts live (Ctrl-C to leave)
  venv/bin/python scripts/detection/centre_test.py --stop        # stop it cleanly

  # other centres / analytics / worker:
  venv/bin/python scripts/detection/centre_test.py --tc-code 300409,351603 --dry-run
  venv/bin/python scripts/detection/centre_test.py --tc-code 50901 --analytics intruder --classes "computer lab" "server room"
  venv/bin/python scripts/detection/centre_test.py --on worker-2
"""

import argparse
import os
import shlex
import signal
import subprocess
import sys
import threading
import time

SCRIPT_SERVER = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PID_FILE = os.path.join(SCRIPT_SERVER, "scripts", "logs", "centre_test.pid")
LOG_FILE = os.path.join(SCRIPT_SERVER, "scripts", "logs", "centre_test.log")
REMOTE_SCRIPT = "scripts/detection/centre_test.py"      # path inside the worker container (/app)
WATCH = "TEST|MonitorTamper.*(MONITOR_|fed)|saved|FAILED|unavailable"


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__.split("EDIT THE CENTRE HERE")[0].strip(),
        epilog=HELP_EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tc-code", nargs="+", default=TC_CODES, metavar="CODE",
                   help=f"one or more centre codes, space or comma separated (default {TC_CODES})")
    p.add_argument("--analytics", nargs="+", default=ANALYTICS, metavar="NAME", help=f"analytics to run (default {ANALYTICS})")
    p.add_argument("--classes", nargs="+", default=CAMERA_TYPES, metavar="TYPE",
                   help='camera types (classification values, case-insensitive); "all" = no filter')
    p.add_argument("--exam", default=EXAM_ID, help=f"exam id the reports are tagged with (default {EXAM_ID})")
    p.add_argument("--shift", default=SHIFT_ID, help=f"shift id the reports are tagged with (default {SHIFT_ID})")
    p.add_argument("--max-minutes", type=float, default=float(os.environ.get("TEST_MAX_MINUTES", MAX_MINUTES)),
                   help=f"auto-stop after N minutes (default {MAX_MINUTES})")
    p.add_argument("--dry-run", action="store_true", help="only list the cameras it would run on; start nothing")
    p.add_argument("--on", default=RUN_ON, choices=[*WORKERS, "local"], help=f"where to run (default {RUN_ON})")
    p.add_argument("--status", action="store_true", help="show whether the test is running and what it has done")
    p.add_argument("--stop", action="store_true", help="stop the running test cleanly")
    p.add_argument("--logs", action="store_true", help="follow the test's alerts live")
    return p.parse_args()


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _running_pid():
    try:
        pid = int(open(PID_FILE).read().strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def local_status():
    pid = _running_pid()
    if not os.path.exists(LOG_FILE):
        print("[TEST] no test has run here yet")
        return
    lines = open(LOG_FILE, errors="replace").read().splitlines()
    last = lambda key: {l.rsplit("→ ", 1)[-1].strip() for l in lines if key in l}
    fed = {l.split("[MonitorTamper] ", 1)[1].split(":", 1)[0] for l in lines if "frames/s" in l and "[MonitorTamper] " in l}
    up = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], capture_output=True, text=True).stdout.strip() if pid else ""
    print(f"[TEST] {'RUNNING pid=' + str(pid) + ' up ' + up if pid else 'NOT running (last run shown below)'}")
    for l in lines:
        if l.startswith("[TEST]") and ("camera(s)" in l or "centre " in l and " -> " in l):
            print("  " + l.split(" -> ")[0])
    model = "FAILED to load" if any("FAILED to load" in l for l in lines) and not any("loaded for" in l for l in lines) \
        else ("loaded" if any("loaded for" in l for l in lines) else "loading...")
    print(f"  model: {model} | cameras connected: {len(last('Connected successfully'))} | analysed: {len(fed)} "
          f"| alerts saved: {sum('saved →' in l for l in lines)}")
    for l in [l for l in lines if "saved →" in l][-3:]:
        print("  " + l.split("saved → ")[-1].rsplit("/", 1)[-1])


def local_stop():
    pid = _running_pid()
    if not pid:
        print("[TEST] nothing is running")
        return
    os.kill(pid, signal.SIGTERM)          # normal stop: the pipeline closes its camera streams
    for _ in range(40):
        if not _alive(pid):
            break
        time.sleep(0.5)
    print(f"[TEST] stopped pid={pid}" if not _alive(pid) else f"[TEST] pid={pid} still shutting down")


def remote_main(args):
    """Run the requested action on a worker: copy this script into its container, then call it there."""
    host, ctr = WORKERS[args.on]
    ssh = ["ssh", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host]   # -q: no login banner
    inside = lambda cmd: f"docker exec {ctr} sh -c {shlex.quote('cd /app && ' + cmd)}"
    py = f"python3 -u {REMOTE_SCRIPT} --on local"

    push = subprocess.run([*ssh, f"docker exec -i {ctr} sh -c 'cat > /app/{REMOTE_SCRIPT}'"], stdin=open(__file__, "rb"))
    if push.returncode != 0:
        sys.exit(f"[TEST] cannot reach {args.on} ({host}) — is the worker up?")

    if args.logs:
        print(f"[TEST] following {args.on} — Ctrl-C to leave (the test keeps running)", flush=True)
        os.execvp("ssh", ["ssh", "-q", "-t", host, f"docker exec {ctr} tail -n 20 -f /app/scripts/logs/centre_test.log "
                                             f"| grep --line-buffered -E {shlex.quote(WATCH)}"])
    if args.status or args.stop:
        sys.exit(subprocess.run([*ssh, inside(f"{py} {'--status' if args.status else '--stop'}")]).returncode)

    opts = " ".join(["--tc-code", *map(shlex.quote, args.tc_code), "--analytics", *map(shlex.quote, args.analytics),
                     "--classes", *map(shlex.quote, args.classes), "--exam", shlex.quote(str(args.exam)),
                     "--shift", shlex.quote(str(args.shift)), "--max-minutes", str(args.max_minutes)])
    if args.dry_run:
        sys.exit(subprocess.run([*ssh, inside(f"{py} {opts} --dry-run 2>&1 | grep -E '^\\[TEST\\]'")]).returncode)

    busy = subprocess.run([*ssh, inside(f"{py} --status")], capture_output=True, text=True).stdout
    if "RUNNING pid=" in busy:
        sys.exit(f"[TEST] a test is already running on {args.on} — use --status, or --stop first\n{busy}")
    subprocess.run([*ssh, f"docker exec -d {ctr} sh -c "
                          + shlex.quote(f"cd /app && {py} {opts} > scripts/logs/centre_test.log 2>&1")], check=True)
    print(f"[TEST] started on {args.on}; waiting for it to come up...", flush=True)
    for _ in range(20):
        time.sleep(3)
        out = subprocess.run([*ssh, inside(f"{py} --status")], capture_output=True, text=True).stdout
        if "model: loaded" in out or "FAILED" in out or "NOT running" in out:
            break
    print(out.strip())
    me = f"venv/bin/python {os.path.relpath(__file__, SCRIPT_SERVER)}" + ("" if args.on == RUN_ON else f" --on {args.on}")
    print(f"[TEST] check: {me} --status   |   alerts live: {me} --logs   |   stop: {me} --stop")


def load_env():
    os.chdir(SCRIPT_SERVER)
    # Workers in Docker get their settings from the container environment (no .env file).
    if os.path.exists(".env"):
        for line in open(".env"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def main():
    args = parse_args()
    if args.on != "local":
        return remote_main(args)
    if args.status:
        return local_status()
    if args.stop:
        return local_stop()
    if args.logs:
        os.execvp("sh", ["sh", "-c", f"tail -n 20 -f {shlex.quote(LOG_FILE)} | grep --line-buffered -E {shlex.quote(WATCH)}"])
    # "300409 351603" and "300409,351603" both work.
    tc_codes = {c.strip() for raw in args.tc_code for c in str(raw).split(",") if c.strip()}
    classes = {c.strip().lower() for c in args.classes}
    any_class = "all" in classes

    load_env()
    # The test runs entirely on one host: no 1-of-N worker split.
    os.environ.update({"EXAM_ID": str(args.exam), "SHIFT_ID": str(args.shift), "LINK_TYPE": "RTSP",
                       "DISTRIBUTION_MODE": "single", "TOTAL_WORKERS": "1"})
    sys.path.insert(0, os.path.join(SCRIPT_SERVER, "scripts/detection"))
    sys.path.insert(0, SCRIPT_SERVER)

    import pipeline.master_pipeline as mp

    original_load = mp.load_cameras_for_execution

    def load_selected(execution):
        cams = [c for c in original_load(execution)
                if str(c.get("tc_code", "")).strip() in tc_codes
                and (any_class or str(c.get("classification", "")).strip().lower() in classes)]
        per_centre = {}
        for c in cams:
            per_centre.setdefault(str(c.get("tc_code", "")).strip(), []).append(
                f"{c.get('camera_no')}:{c.get('camera_name', '')}")
        print(f"[TEST] {execution['script_code']}: {len(cams)} camera(s), centre(s) {sorted(tc_codes)}, "
              f"types {'ALL' if any_class else sorted(classes)}", flush=True)
        for tc, names in sorted(per_centre.items()):
            print(f"[TEST]   centre {tc}: {len(names)} -> {sorted(names)}", flush=True)
        missing = tc_codes - set(per_centre)
        if missing:
            print(f"[TEST]   no accessible cameras found for centre(s): {sorted(missing)} "
                  f"(check the code, exam {args.exam}, camera types, or that they are Accessible)", flush=True)
        return cams

    mp.load_cameras_for_execution = load_selected

    if args.dry_run:
        for code in args.analytics:
            load_selected({"exam_id": str(args.exam), "shift_id": str(args.shift),
                           "link_type": "RTSP", "script_code": code})
        return

    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    open(PID_FILE, "w").write(str(os.getpid()))
    print(f"[TEST] FIRED pid={os.getpid()} analytics={args.analytics} centre(s)={sorted(tc_codes)} "
          f"exam={args.exam} shift={args.shift} max={args.max_minutes:g} min", flush=True)
    threading.Timer(args.max_minutes * 60, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    sys.argv = ["master_pipeline.py", "--analytics", *args.analytics]
    try:
        mp.main()
    finally:
        try:
            os.remove(PID_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    main()
