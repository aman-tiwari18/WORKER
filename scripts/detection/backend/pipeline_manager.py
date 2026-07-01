import subprocess
import sys
import os
import signal

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PIPELINE = os.path.join(PROJECT_ROOT, "pipeline", "master_pipeline.py")

processes = {}          # analytics -> Popen
stopped_analytics = set()

ALL_ANALYTICS = [
    "server",
    "crowd",
    "intruder",
    "entry_exit",
    "proximity",
    "phone",
    "invigilator",
    "tamper",
    "inversion",
    "crowd_zero",
    "crowd_five",
    "blind_spot",
    "strongroom",
]

def start_all():
    return start_analytics(ALL_ANALYTICS)

def stop_all():
    return stop_analytics(list(processes.keys()))


def _kill_process_group(pid: int, sig=signal.SIGTERM):
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except Exception as e:
        print(f"Cleanup error killing process group {pid}: {e}")


def cleanup_old_pipelines():
    patterns = [
        "pipeline/master_pipeline.py",
        f"{sys.executable} -c from multiprocessing.spawn",
        f"{sys.executable} -c from multiprocessing.resource_tracker",
    ]

    # First try to kill any running master pipeline groups by PID.
    try:
        output = subprocess.check_output(
            ["pgrep", "-f", "pipeline/master_pipeline.py"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        for pid_str in output.split():
            try:
                _kill_process_group(int(pid_str), signal.SIGTERM)
            except ValueError:
                continue
    except subprocess.CalledProcessError:
        pass
    except Exception as e:
        print("Cleanup error finding old pipelines:", e)

    # Clean up any old pipeline and multiprocessing worker processes.
    for sig in [signal.SIGTERM, signal.SIGKILL]:
        for pattern in patterns:
            subprocess.call(["pkill", f"-{sig.value}", "-f", pattern])

    print("🧹 Old analytics pipelines cleaned")

def start_analytics(analytics):
    started = []
    for name in analytics:
        if name in processes and processes[name].poll() is None:
            continue

        cmd = [sys.executable, PIPELINE, "--analytics", name]
        p = subprocess.Popen(
            cmd,
            cwd=PROJECT_ROOT,
            preexec_fn=os.setsid
        )

        processes[name] = p
        stopped_analytics.discard(name)
        started.append(name)

    return started

def stop_analytics(analytics):
    stopped = []
    for name in analytics:
        p = processes.get(name)
        if p and p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                pass

            stopped.append(name)
            stopped_analytics.add(name)
            del processes[name]

    return stopped

def status_analytics():
    running = []
    stopped = list(stopped_analytics)

    for name, p in list(processes.items()):
        if p.poll() is None:
            running.append(name)
        else:
            stopped.append(name)
            stopped_analytics.add(name)
            del processes[name]

    return running, stopped

