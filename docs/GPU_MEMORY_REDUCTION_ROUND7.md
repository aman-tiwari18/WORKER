# GPU Memory Reduction — Round 7 (branch `memory_leak_k`)

**Goal:** reduce the ~4956 MiB per-process GPU memory baseline shown in `nvidia-smi --query-compute-apps` for the per-analytic `master_pipeline.py` detection processes.

**Result observed after deploy:** drop to ~252 MiB per process — far better than the projected 2000–2500 MiB target. (Must still be validated by confirming detections are actively running, not just that memory is low.)

This document explains *why* each problem existed and *what* the change does, so anyone touching detection code later understands the constraint.

---

## Root causes of the 4956 MiB baseline

The high per-process baseline was the sum of **four compounding problems**. Earlier rounds that only added `del results / del frames / del overlay / del batch_results` could not move the nvidia-smi number because none of those `del`s touched any of these four root causes.

### 1. Ultralytics' internal predictor cache pinned every batch

Every call to `model(frames, ...)` quietly stored the resulting list at `model.predictor.results` so Ultralytics' own helpers (`.show()`, `.save()`, etc.) could access it later.

- Our code never read `model.predictor.results`, but Ultralytics kept it alive between inference calls.
- Each cached `Results` object holds:
  - `boxes.data` — a GPU tensor of all detections
  - `orig_img` — a numpy reference to the input frame (~6 MB at 1080p)
- With `GPU_BATCH_SIZE = 64`, that's ~64 frames + 64 detection tensors held between calls.
- Approximate pin: **~500–700 MB per process** (host RAM via `orig_img` + GPU memory via detection tensors).
- `del results` in our code only dropped *our local reference* — Ultralytics still held its own. **That is why every earlier `del`-based round did nothing visible.**

### 2. FP32 was the structural floor

`yolov10m.pt` in FP32 = ~80 MB weights, + batch=64 activations, + cuDNN workspace ≈ **~3 GB just to run inference**.

No amount of memory cleanup can reduce this — only a precision change (FP16) or batch-size reduction can.

### 3. PyTorch's CUDA caching allocator hid real usage

PyTorch reserves GPU memory in large blocks and reuses them. Once reserved, that memory stays attached to the process until `torch.cuda.empty_cache()` is called.

- `nvidia-smi` reports *reserved* memory, not actual allocations.
- Even if every tensor was freed, the allocator kept the blocks, so the reported number didn't drop.
- This is why Round 2's `del`-based work was invisible in nvidia-smi even where it *did* release memory.

### 4. CUDA allocator fragmentation inflated the reported number further

The default `max_split_size_mb` causes the allocator to hold fragmented blocks that can't be coalesced. Reported usage is higher than the actual working set.

---

## Changes applied (28 files: 14 cores + 14 workers + run.py)

### Change 1 — Allocator configuration

**File:** [script-server/run.py](../run.py)

```python
import os
os.environ.setdefault(
    "PYTORCH_CUDA_ALLOC_CONF",
    "expandable_segments:True,max_split_size_mb:256",
)

import uvicorn
from app.config import HTTP_PORT
```

**Why this fixes problem #4:** `expandable_segments:True` tells the allocator to commit physical memory only as needed (instead of grabbing large reserved chunks). `max_split_size_mb:256` lets larger allocations be split into reusable chunks, reducing fragmentation.

**Inheritance:** spawned `mp.Process` subprocesses (per-analytic detection workers spawned by FastAPI) inherit the parent's env vars, so all `master_pipeline.py --analytics X` processes get this automatically.

**Risk:** zero — official PyTorch flag.

### Change 2 — FP16 + `torch.inference_mode()` + clear Ultralytics cache

**Files (14):** 13 detection cores + [camera_inversion_worker.py](../scripts/detection/workers/camera_inversion_worker.py).

The 13 cores:
- [crowd_five_det.py](../scripts/detection/crowd_five_det.py)
- [phone_det_complete.py](../scripts/detection/phone_det_complete.py)
- [qp_box_det.py](../scripts/detection/qp_box_det.py)
- [invigilator_tracking.py](../scripts/detection/invigilator_tracking.py)
- [blind_spot_det.py](../scripts/detection/blind_spot_det.py)
- [crowd_zero_det.py](../scripts/detection/crowd_zero_det.py)
- [close_proximity.py](../scripts/detection/close_proximity.py)
- [Crowd_det_complete.py](../scripts/detection/Crowd_det_complete.py)
- [Crowd_det_biometric.py](../scripts/detection/Crowd_det_biometric.py)
- [Entry_exit_det.py](../scripts/detection/Entry_exit_det.py)
- [Intruder_det.py](../scripts/detection/Intruder_det.py)
- [Server_room_det.py](../scripts/detection/Server_room_det.py)
- [StrongRoom_det.py](../scripts/detection/StrongRoom_det.py)

Every `model(frames, ...)` (and single-frame `model(frame, ...)`) call was replaced with:

```python
with torch.inference_mode():
    results = model(frames, conf=CONF_THRESHOLD, verbose=False, half=True)
    if model.predictor is not None:
        model.predictor.results = None
```

Three things combined here (they belong together at every call site):

| Piece | What it does | Fixes |
|---|---|---|
| `torch.inference_mode()` | Disables PyTorch autograd machinery; smaller activation buffers, freed eagerly. Standard PyTorch inference pattern. | General activation overhead (~200–400 MB) |
| `half=True` | Ultralytics converts model weights to FP16 on first call and keeps them there. Halves weights + activations + cuDNN workspace. | **Problem #2** (FP32 floor) — biggest single lever (~1.5–2.0 GB) |
| `model.predictor.results = None` | Releases Ultralytics' internal pin on the previous batch's `Results` (GPU detection tensors + numpy `orig_img` references). | **Problem #1** (Ultralytics cache) — ~500–700 MB per process |

**Risk profile:**
- `inference_mode`: zero. We never train these models.
- `half=True`: low. Confidence values shift by typically <0.01 — well below all our thresholds (0.25–0.50). Ultralytics officially supports FP16; widely used in production YOLO. Most workers also require `CONSEC_THRESHOLD = 2–5` consecutive violation frames, which absorbs any single-frame flicker at threshold boundaries.
- `predictor.results = None`: zero. Internal cache, never read by our code; the `if predictor is not None` guard handles a future Ultralytics API change.

### Change 3 — Periodic `torch.cuda.empty_cache()` in GPU batch worker loops

**Files (14):** all `*_worker.py` files in [scripts/detection/workers/](../scripts/detection/workers/) **except [camera_tampering_worker.py](../scripts/detection/workers/camera_tampering_worker.py)** (it's pure CPU OpenCV, no torch, no GPU).

Pattern added to each `_gpu_batch_worker(...)`:

```python
def _gpu_batch_worker(stop_event, save_folder, cooldown_time=...):
    ...
    log_counter = 0
    batch_count = 0      # NEW

    while not stop_event.is_set():
        ...
        # ... existing batch fill + inference + result processing ...

        del batch
        batch_count += 1                                              # NEW
        if batch_count % 30 == 0 and torch.cuda.is_available():       # NEW
            torch.cuda.empty_cache()                                  # NEW

    logger.info("🔴 [...] GPU worker stopped")
```

**Why this fixes problem #3:** returns reserved-but-unused GPU memory to the OS so nvidia-smi reflects the *actual* drop. Without this, Changes 1–2 still reduce real usage but the nvidia-smi number stays inflated because the caching allocator keeps blocks reserved.

**Why every 30 batches and not every batch:** `empty_cache()` is a single-ms operation, but calling it per batch wastes throughput. Once every ~30 batches (~every few seconds in steady state) is enough to keep nvidia-smi honest without measurable performance impact.

**Placement note:** the increment is at the end of the while loop body, AFTER `del batch`. For `blind_spot_worker.py` and `camera_inversion_worker.py` the structure has `del batch` inside try/except, so the increment goes immediately after the try/except block, still inside the while.

**Skipped:** `camera_tampering_worker.py` — uses only OpenCV (brightness, blur, edge density); doesn't import torch.

---

## Expected vs. observed outcome

| Source | Expected reduction | Cumulative target |
|---|---|---|
| `PYTORCH_CUDA_ALLOC_CONF` | ~300–600 MiB | — |
| FP16 (`half=True`) | ~1.5–2.0 GB | the single biggest lever |
| `model.predictor.results = None` | ~500–700 MB | — |
| `torch.inference_mode()` | ~200–400 MB | — |
| Periodic `empty_cache()` | ~300–500 MiB (makes the rest visible) | — |
| **Combined target** | **~4956 → ~2000–2500 MiB** | ~50–60% reduction |

**Observed after deploy:** ~252 MiB per process — far below target.

This is consistent with `expandable_segments:True` only reporting committed pages (not reserved virtual address space) in nvidia-smi, combined with `empty_cache()` flushing the allocator just before the snapshot. **Verify detections are actually running** (saves appearing, GPU utilization >0%) before celebrating — 252 MiB is also the size of a CUDA context with no active inference, which could indicate silent worker failure.

---

## What was explicitly NOT changed

These were considered and rejected as either ineffective or out of scope:

- **`del results / del frames / del overlay / del batch_results / del inference_items / del packet`** — Round 2 added these; they didn't move nvidia-smi because Python GC already releases function-local refs at scope end, and the *actual* tensor pin was inside Ultralytics, not our code.
- **Reducing `GPU_BATCH_SIZE` 64 → 32** — separate throughput trade-off, not in mentor scope.
- **`gc.collect()`** — slow blunt instrument; doesn't help with CUDA-side memory.
- **FastAPI cache changes from Round 4** — user reverted these deliberately on the prior branch.
- **Detection thresholds, save logic, DB hooks, scene-filter rules, HLS readers, RTSP producer, frame rates, classification gates, queue sizes** — none touched.

---

## Verified non-issues (do NOT chase these again)

These were audited and confirmed NOT to be sources of growth:

| Suspect | Verdict |
|---|---|
| DB connection leak on early return in `report_to_db_hls/rtsp` | ❌ REFUTED — `return False` is inside the `try:` block, so `finally: conn.close()` always runs. |
| `mp.Queue` not `.close()`-ed on teardown | ⚠️ Negligible — Python cleans these on process exit. |
| `keepalive_threads` dict | ⚠️ Bounded by # of unique DSS servers (~5–10). |
| Per-camera state dicts (`camera_states`, `last_save_times`, `trackers`) | ✅ Bounded by physical camera count (stable). |
| `result.boxes` Box refs held in `person_boxes` list | ✅ Released at end of each loop iteration. |
| Producer-side frame retention | ✅ Already fixed in Round 1 (`frame.copy()` + `del frame` in `finally`). |

---

## Verification

### Static (Mac, before push)
```bash
python3 -m py_compile script-server/run.py
python3 -m py_compile script-server/scripts/detection/*.py
python3 -m py_compile script-server/scripts/detection/workers/*.py
```
All 28 edited files compile clean.

### Runtime (Linux VM, after pull + pm2 restart)
```bash
# Watch per-process memory drop and verify it's stable
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv -l 30

# Confirm detections are actually firing (not just memory-low because workers crashed)
ls -lat script-server/scripts/detection/results/*/ | head -30
pm2 logs --lines 100 | grep -E "SAVED|GPU worker|Batch inference error|YOLO model"

# Trigger known scenarios
#  - Empty out a lab camera view → wait 45s → check invigilator_detection folder
#  - Show a phone → check phone_detection folder
```

Detection sanity check: saved images should look visually identical to before this change. FP16 produces minor confidence-value differences but boxes, labels, and status text are unchanged.

---

## Rollback

All changes are additive (one env var, function-internal wraps, one counter per worker). To revert:

```bash
git revert <Round 7 commit>
```

Or to revert *only* FP16 (the one change with a non-zero behavior risk), do a search-and-replace of `, half=True)` → `)` across the 14 files in Change 2. The other Round 7 changes are pure memory management with no behavior impact.

---

## If memory grows back later

Stop applying speculative fixes. The next step is **measurement, not guessing**:

```python
# Add at the top of each batch iteration in one worker:
import torch
if torch.cuda.is_available():
    allocated = torch.cuda.memory_allocated() / 1e9
    reserved  = torch.cuda.memory_reserved()  / 1e9
    logger.info(f"[Worker] batch={len(batch)} alloc={allocated:.2f}GB reserved={reserved:.2f}GB")
```

Capture 10+ minutes of data. The actual growing component will be obvious — then fix that one thing rather than guessing at another round of cleanups.
