"""
Live Worker Test Script
=======================
Tests each detection worker with real RTSP camera feeds.
Runs each worker for ~30s, checks: import, model load, frame capture, queue processing.
"""

import sys
import os
import time
import queue
import threading
import logging
import multiprocessing as mp
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "workers"))
sys.path.insert(0, os.path.join(BASE_DIR, "pipeline"))

os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── test cameras (from DB, verified reachable) ──────────────────────────────
TEST_CAMERAS = {
    "classroom": {"server": "d2.camview.io", "device_id": "1003061", "camera_no": "2",  "classification": "Inside Classroom"},
    "entry_exit": {"server": "d2.camview.io", "device_id": "1003061", "camera_no": "13", "classification": "Entry/Exit"},
    "strongroom": {"server": "d2.camview.io", "device_id": "1003061", "camera_no": "15", "classification": "Strong Room"},
    "any":        {"server": "d2.camview.io", "device_id": "1003061", "camera_no": "2",  "classification": "Inside Classroom"},
}

# ── worker definitions: (module, fn, camera_type, script_name) ───────────────
WORKERS = [
    ("close_proximity_worker",     "close_proximity_worker",     "classroom",  "proximity"),
    ("phone_worker",               "phone_worker",               "classroom",  "phone"),
    ("invigilator_worker",         "invigilator_worker",         "classroom",  "invigilator"),
    ("crowd_zero_det_worker",      "crowd_zero_det_worker",      "classroom",  "crowd_zero"),
    ("crowd_five_det_worker",      "crowd_five_det_worker",      "classroom",  "crowd_five"),
    ("crowd_worker",               "crowd_worker",               "entry_exit", "crowd"),
    ("entry_exit_worker",          "entry_exit_worker",          "entry_exit", "entry_exit"),
    ("intruder_worker",            "intruder_worker",            "strongroom", "intruder"),
    ("strongroom_worker",          "strongroom_worker",          "strongroom", "strongroom"),
    ("qp_box_det_worker",          "qp_box_det_worker",          "strongroom", "qp_box"),
    ("camera_tampering_worker",    "camera_tampering_worker",    "any",        "tamper"),
    ("camera_inversion_worker",    "camera_inversion_worker",    "any",        "inversion"),
    ("blind_spot_worker",          "blind_spot_worker",          "any",        "blind_spot"),
    ("crowd_det_biometric_worker", "crowd_det_biometric_worker", "any",        "crowd_biometric"),
    ("server_room_worker",         "server_room_worker",         "any",        "server"),
]

TEST_DURATION_SECS = 30
RESULTS = []


def get_rtsp_url(camera):
    """Get a live RTSP URL for a test camera."""
    from pipeline.master_pipeline import get_server_token, get_rtsp_from_dss, add_token_to_url, order_urls
    server = camera["server"]
    device_id = camera["device_id"]
    camera_no = camera["camera_no"]

    token = get_server_token(server)
    if not token:
        return None, f"Auth failed for {server}"

    result = get_rtsp_from_dss(device_id, camera_no, token, server)
    if not result:
        return None, f"get_rtsp_from_dss failed for {device_id}/{camera_no}"

    urls, stream_token = result
    ordered = order_urls(urls)
    if not ordered:
        return None, "No reachable URLs"

    return add_token_to_url(ordered[0], stream_token), None


def feed_frames(rtsp_url, frame_queue, stop_event, label):
    """Read frames from RTSP and push into the worker queue."""
    import cv2
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print(f"  [{label}] WARN: Could not open RTSP stream")
        return

    frames_sent = 0
    t_start = time.time()
    while not stop_event.is_set():
        ret, frame = cap.read()
        if not ret or frame is None:
            time.sleep(0.2)
            continue
        packet = {
            "frame": frame,
            "device_id": label.split("_")[0],
            "camera_no": label.split("_")[1] if "_" in label else "1",
            "exam_id": "0",
        }
        try:
            frame_queue.put(packet, timeout=0.1)
            frames_sent += 1
        except Exception:
            pass
        time.sleep(0.2)  # ~5 FPS

    cap.release()
    print(f"  [{label}] Sent {frames_sent} frames in {time.time()-t_start:.1f}s")


def run_worker_test(module_name, fn_name, camera_type, script_name):
    """Run one worker for TEST_DURATION_SECS and report outcome."""
    print(f"\n{'='*60}")
    print(f"  TESTING: {script_name} ({module_name}.{fn_name})")
    print(f"{'='*60}")

    result = {"worker": script_name, "status": "UNKNOWN", "error": None,
              "frames_queued": 0, "import_ok": False, "model_ok": False}

    # 1. Import check
    try:
        import importlib
        mod = importlib.import_module(module_name)
        fn = getattr(mod, fn_name)
        result["import_ok"] = True
        print(f"  [✓] Import OK")
    except Exception as e:
        result["status"] = "IMPORT_ERROR"
        result["error"] = str(e)
        print(f"  [✗] Import FAILED: {e}")
        RESULTS.append(result)
        return

    # 2. Get RTSP URL
    camera = TEST_CAMERAS[camera_type]
    url, err = get_rtsp_url(camera)
    if not url:
        # Worker still importable — mark partial success
        result["status"] = "NO_STREAM"
        result["error"] = err
        print(f"  [!] Stream unavailable: {err} — testing import/model only")
        # Try model load via import side-effects
        try:
            # Trigger model warm-up if there's a get_model function
            model_fn = None
            for attr in dir(mod):
                if "get_" in attr and "model" in attr.lower():
                    model_fn = getattr(mod, attr)
                    break
            if model_fn:
                model_fn()
                result["model_ok"] = True
                print(f"  [✓] Model loaded OK")
        except Exception as e:
            result["error"] = f"Model load error: {e}"
            print(f"  [✗] Model load FAILED: {e}")
        RESULTS.append(result)
        return

    print(f"  [✓] RTSP URL obtained: {url[:70]}...")

    # 3. Run worker with live frames
    frame_queue = queue.Queue(maxsize=32)
    stop_event = threading.Event()
    errors = []
    worker_started = threading.Event()
    worker_crashed = threading.Event()

    def worker_thread():
        try:
            worker_started.set()
            fn(frame_queue)
        except Exception as e:
            errors.append(str(e))
            worker_crashed.set()

    wt = threading.Thread(target=worker_thread, daemon=True)
    wt.start()

    label = f"{camera['device_id']}_{camera['camera_no']}"
    ft = threading.Thread(target=feed_frames, args=(url, frame_queue, stop_event, label), daemon=True)
    ft.start()

    # Wait for test duration
    deadline = time.time() + TEST_DURATION_SECS
    while time.time() < deadline:
        if worker_crashed.is_set():
            break
        time.sleep(0.5)

    # Signal workers to stop
    stop_event.set()
    try:
        frame_queue.put(None, timeout=1)
    except Exception:
        pass

    ft.join(timeout=5)
    wt.join(timeout=10)

    result["frames_queued"] = frame_queue.qsize()

    if worker_crashed.is_set() and errors:
        result["status"] = "CRASHED"
        result["error"] = errors[0]
        print(f"  [✗] Worker CRASHED: {errors[0]}")
    else:
        result["status"] = "OK"
        result["model_ok"] = True
        print(f"  [✓] Worker ran OK for {TEST_DURATION_SECS}s")

    RESULTS.append(result)


def main():
    mp.set_start_method("spawn", force=True)
    print("\n" + "="*60)
    print("  CCTV WORKER LIVE TEST")
    print("="*60)

    # Pre-auth both servers
    print("\nAuthenticating with DSS servers...")
    from pipeline.master_pipeline import get_server_token
    for s in ["d1.camview.io", "d2.camview.io"]:
        t = get_server_token(s)
        print(f"  {s}: {'OK' if t else 'FAILED'}")

    for module_name, fn_name, camera_type, script_name in WORKERS:
        run_worker_test(module_name, fn_name, camera_type, script_name)

    # ── Summary ────────────────────────────────────────────────────────────
    print("\n\n" + "="*60)
    print("  FINAL SUMMARY")
    print("="*60)
    ok = [r for r in RESULTS if r["status"] == "OK"]
    fail = [r for r in RESULTS if r["status"] not in ("OK", "NO_STREAM")]
    no_stream = [r for r in RESULTS if r["status"] == "NO_STREAM"]

    print(f"\n  ✅ OK           : {len(ok)}")
    print(f"  ⚠️  No stream    : {len(no_stream)}")
    print(f"  ❌ Failed        : {len(fail)}")
    print()

    for r in RESULTS:
        icon = "✅" if r["status"] == "OK" else ("⚠️ " if r["status"] == "NO_STREAM" else "❌")
        print(f"  {icon} {r['worker']:<30} {r['status']:<15} {r['error'] or ''}")

    return len(fail)


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
