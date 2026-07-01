"""
Quick worker test — import, model load, and 15s live-frame run per worker.
Run with: python3 -u test_worker_quick.py [worker_name]
"""
import sys, os, time, queue, threading, importlib, traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "workers"))
sys.path.insert(0, os.path.join(BASE_DIR, "pipeline"))
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

import logging
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")

WORKERS = [
    ("close_proximity_worker",     "close_proximity_worker",     "d2.camview.io", "1003061", "2"),
    ("phone_worker",               "phone_worker",               "d2.camview.io", "1003061", "2"),
    ("invigilator_worker",         "invigilator_worker",         "d2.camview.io", "1003061", "2"),
    ("crowd_zero_det_worker",      "crowd_zero_det_worker",      "d2.camview.io", "1003061", "2"),
    ("crowd_five_det_worker",      "crowd_five_det_worker",      "d2.camview.io", "1003061", "2"),
    ("crowd_worker",               "crowd_worker",               "d2.camview.io", "1003061", "13"),
    ("entry_exit_worker",          "entry_exit_worker",          "d2.camview.io", "1003061", "13"),
    ("intruder_worker",            "intruder_worker",            "d2.camview.io", "1003061", "15"),
    ("strongroom_worker",          "strongroom_worker",          "d2.camview.io", "1003061", "15"),
    ("qp_box_det_worker",          "qp_box_det_worker",          "d2.camview.io", "1003061", "15"),
    ("camera_tampering_worker",    "camera_tampering_worker",    "d2.camview.io", "1003061", "2"),
    ("camera_inversion_worker",    "camera_inversion_worker",    "d2.camview.io", "1003061", "2"),
    ("blind_spot_worker",          "blind_spot_worker",          "d2.camview.io", "1003061", "2"),
    ("crowd_det_biometric_worker", "crowd_det_biometric_worker", "d2.camview.io", "1003061", "2"),
    ("server_room_worker",         "server_room_worker",         "d2.camview.io", "1003061", "2"),
]

RUN_SECS = 15
_token_cache = {}

def get_token(server):
    if server not in _token_cache:
        from pipeline.master_pipeline import get_server_token
        _token_cache[server] = get_server_token(server)
    return _token_cache[server]

def get_rtsp_url(server, device_id, camera_no):
    from pipeline.master_pipeline import get_server_token, get_rtsp_from_dss, add_token_to_url, order_urls
    token = get_token(server)
    if not token:
        return None
    result = get_rtsp_from_dss(device_id, camera_no, token, server)
    if not result:
        return None
    urls, stream_token = result
    ordered = order_urls(urls)
    return add_token_to_url(ordered[0], stream_token) if ordered else None

def test_one(module_name, fn_name, server, device_id, camera_no):
    print(f"\n{'─'*55}", flush=True)
    print(f"  {module_name}.{fn_name}", flush=True)
    print(f"  Camera: {server} {device_id} ch={camera_no}", flush=True)

    # Step 1: import
    try:
        mod = importlib.import_module(module_name)
        fn = getattr(mod, fn_name)
        print(f"  [✓] import OK", flush=True)
    except Exception as e:
        print(f"  [✗] IMPORT FAILED: {e}", flush=True)
        traceback.print_exc()
        return "IMPORT_ERROR"

    # Step 2: RTSP url
    url = get_rtsp_url(server, device_id, camera_no)
    if not url:
        print(f"  [!] No RTSP URL — skipping live test", flush=True)
        return "NO_STREAM"
    print(f"  [✓] URL: {url[:70]}...", flush=True)

    # Step 3: open capture
    import cv2
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print(f"  [✗] Could not open RTSP stream", flush=True)
        return "STREAM_ERROR"

    # Read first frame
    ret, frame = cap.read()
    if not ret or frame is None:
        print(f"  [✗] No frames from stream", flush=True)
        cap.release()
        return "STREAM_ERROR"
    print(f"  [✓] Frame OK: {frame.shape}", flush=True)

    # Step 4: run worker
    fq = queue.Queue(maxsize=16)
    stop = threading.Event()
    errors = []

    def worker_fn():
        try:
            fn(fq)
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")

    def feeder():
        local_cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        sent = 0
        while not stop.is_set():
            r, f = local_cap.read()
            if not r or f is None:
                time.sleep(0.3)
                continue
            pkt = {"frame": f, "device_id": device_id, "camera_no": camera_no, "exam_id": "0"}
            try:
                fq.put(pkt, timeout=0.1)
                sent += 1
            except queue.Full:
                pass
            time.sleep(0.2)
        local_cap.release()
        print(f"  [i] Feeder sent {sent} frames", flush=True)

    wt = threading.Thread(target=worker_fn, daemon=True)
    ft = threading.Thread(target=feeder, daemon=True)
    cap.release()

    wt.start()
    ft.start()
    t0 = time.time()

    while time.time() - t0 < RUN_SECS:
        if errors:
            break
        time.sleep(0.5)

    stop.set()
    try:
        fq.put(None, timeout=1)
    except Exception:
        pass
    ft.join(timeout=5)
    wt.join(timeout=8)

    if errors:
        print(f"  [✗] WORKER CRASHED: {errors[0]}", flush=True)
        return "CRASHED"
    else:
        print(f"  [✓] Worker ran {RUN_SECS}s OK", flush=True)
        return "OK"


def main():
    filter_name = sys.argv[1] if len(sys.argv) > 1 else None

    print("Pre-authing DSS servers...", flush=True)
    from pipeline.master_pipeline import get_server_token
    for s in ["d1.camview.io", "d2.camview.io"]:
        t = get_server_token(s)
        print(f"  {s}: {'OK' if t else 'FAILED'}", flush=True)

    results = {}
    for entry in WORKERS:
        module_name = entry[0]
        if filter_name and filter_name not in module_name:
            continue
        status = test_one(*entry)
        results[module_name] = status

    print(f"\n\n{'='*55}", flush=True)
    print("  SUMMARY", flush=True)
    print(f"{'='*55}", flush=True)
    for name, status in results.items():
        icon = "✅" if status == "OK" else ("⚠️ " if status == "NO_STREAM" else "❌")
        print(f"  {icon} {name:<35} {status}", flush=True)

    fails = [n for n, s in results.items() if s not in ("OK", "NO_STREAM")]
    print(f"\n  Failures: {len(fails)}/{len(results)}", flush=True)
    return len(fails)

if __name__ == "__main__":
    sys.exit(main())
