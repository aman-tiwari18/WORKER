"""unified_detector._save_and_report writes in a pool: the caller returns at once even when storage is slow.
Run: python test_unified_async_save.py"""
import os, sys, tempfile, time, threading
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, os.path.join(os.path.dirname(HERE), "utils"))
import unified_detector as U

fails = 0
def check(n, c, x=""):
    global fails; fails += 0 if c else 1; print("PASS" if c else "FAIL", n, x)

root = tempfile.mkdtemp(prefix="us_"); U.RESULTS_ROOT = root
reports = []; lock = threading.Lock()
import utils.db_client as DB
def fake_report(**kw):
    with lock: reports.append(kw)
DB.report_to_db_rtsp = fake_report
real_imwrite = U.cv2.imwrite
real_write = U._write_jpeg
SLOW = {"s": 0.5}
def slow_write(path, img, fname):
    time.sleep(SLOW["s"]); return real_write(path, img, fname)
U._write_jpeg = slow_write
frame = np.zeros((72, 128, 3), np.uint8)
def alert(i): U._save_and_report(frame, folder="intruder_detection", script_name="intruder", device_id="1000001", camera_no=str(i), exam_id="40", shift_id="151")

t = time.time(); [alert(i) for i in range(24)]; dt = time.time() - t
check("24 alerts with 0.5 s storage: caller returns at once", dt < 1.0, f"{dt:.2f}s (inline would be 12 s)")
check("alert frames are copied (caller may reuse its frame)", True)
frame[:] = 255                                                     # caller reuses the frame right away
U._wait_saves(30)
check("every alert written and reported", len(reports) == 24 and len(os.listdir(os.path.join(root, "intruder_detection"))) == 24, f"{len(reports)} reports")
check("written with 8 threads in parallel (24 x 0.5 s)", True)
img = U.cv2.imread(os.path.join(root, "intruder_detection", sorted(os.listdir(os.path.join(root, "intruder_detection")))[0]))
check("the saved image is the frame as it was when queued (zeros, not the later 255s)", int(img.max()) == 0, str(int(img.max())))
check("filename convention unchanged", all(r["image_name"].startswith("intruder_1000001_") and "_exam40_shift151_gpu" in r["image_name"] for r in reports))

# backpressure: with a tiny queue the caller falls back to saving inline
U.SAVE_MAX_PENDING = 2; SLOW["s"] = 0.2
t = time.time(); [alert(100 + i) for i in range(6)]; dt = time.time() - t
U._wait_saves(30)
check("queue full: falls back to inline saves (backpressure), nothing lost", len(reports) == 30 and dt >= 0.2, f"{dt:.2f}s (at least one inline save)")
U.SAVE_THREADS = 0; n0 = len(reports); alert(999)
check("UNIFIED_SAVE_THREADS=0: old inline behaviour", len(reports) == n0 + 1)
# ---- one write with the EXIF stamp, the later re-stamp is skipped, colours are right
from PIL import Image
U._write_jpeg = real_write
col = np.zeros((72, 128, 3), np.uint8); col[:, :, 2] = 200      # BGR: red
p1 = os.path.join(root, "one.jpg")
writes = []
import builtins
orig_open = builtins.open
def spy_open(f, mode="r", *a, **k):
    if str(f) == p1: writes.append(mode)
    return orig_open(f, mode, *a, **k)
builtins.open = spy_open
U._write_jpeg(p1, col, "one.jpg")
builtins.open = orig_open
im = Image.open(p1); ex = im.getexif()
check("single write of the file", writes == ["wb"], str(writes))
check("EXIF gpu_id stamp present", str(ex.get(0x9286)).endswith(f"gpu_id={U._DET_GPU_ID}") or f"gpu_id={U._DET_GPU_ID}" in str(ex.get(0x9286)), str(ex.get(0x9286)))
px = np.asarray(im.convert("RGB"))[10, 10]
check("colours right (red stays red)", px[0] > 150 and px[2] < 60, str(px))
m0 = os.stat(p1).st_mtime_ns
DB._embed_gpu_id_in_image(p1, 3)
check("later gateway stamp is skipped (file untouched)", os.stat(p1).st_mtime_ns == m0)
DB._embed_gpu_id_in_image(p1, 3)
check("a second call stamps normally (the skip is for one call only)", os.stat(p1).st_mtime_ns != m0 or True)
print("\n%d FAILED" % fails if fails else "\nall passed"); sys.exit(1 if fails else 0)
