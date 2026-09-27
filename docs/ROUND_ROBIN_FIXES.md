# Round-Robin Capture — Fixes for RTSP and HLS

Status: proposed, not implemented. Date: 2026-09-27.
Scope: the current round-robin capture only (`scripts/detection/pipeline/master_pipeline.py`,
`scripts/detection/workers/hls_scheduler.py`). Plan B (DeepStream / TensorRT) is out of scope.

---

## 1. Where we are today (measured)

Evidence comes from the old monorepo's real exam runs (`/data/cctv_dev/cctv/script-server`):
RE-NEET 21-06-2026 for RTSP, RE-UPCISB 17–21-09-2026 for HLS, plus Prometheus for 18-09.

| | RTSP (RE-NEET, exam 25) | HLS (RE-UPCISB) |
|---|---|---|
| Slots configured | 256 (`RR_POOL_SIZE`) | 8 (`HLS_RR_POOL_SIZE`, prod `.env`) |
| Streams actually open at once | at most **42** (6 per DSS server × 7 servers); exam 20 = **24** (4 servers) | at most **8** |
| Throughput | 2.9 stream opens/s | — |
| Revisit per camera | **~128 s** (375 cameras) | **1–8 min** (9–230 cameras per job) |
| Visit success | not logged | 85–99 % |
| Frames sent to the model per visit | 2 | 4 |

What the machine is doing (18-09, Prometheus): GPU median 0 % / p90 5 %; each YOLO process 1.9–2.6 GB
VRAM; worker CPU p90 23 %; up to 54k context switches/s. **The GPU is idle — capture is the limit.**

### Why RTSP is capped at 24–42 streams

- The `6` in `MAX_STREAMS_PER_DVR` is **our own setting**, copied from the classification script.
  No DSS document or error sets it.
- In this code "DVR" means the **DSS server** (`d11.camview.io` …), which hosts ~100+ exam-centre
  recorders. The cap was meant per recorder but is applied per server.
- In 40 real exam logs DSS **never** returned a "too many streams" error. It returned login lockouts
  (3,942 — one session per username), blocked ports (2,505), `2084` "no authorization" (882),
  API read timeouts (508) and "MTS server is not ready" (2).
- Estimated DSS capacity (one server: 108 recorders / ~1,080 channels, DSS Pro V7):
  **~300 full-quality streams** (assumes ~600 Mbps forwarding, ~2 Mbps per 1080p H.265 stream)
  or **all ~1,080 on substream**. To be confirmed by the ramp test in §5.

### Cost of one stream (measured on a 24-core Xeon 6740P, worker OpenCV 4.13, 1080p H.265 ~2 Mbps)

| Action | CPU |
|---|---|
| Open stream + first picture | ~52 ms (plus 1–5 s of network waiting, no CPU) |
| Decode one frame (`grab()`) | 5.0 ms |
| Decode + colour convert + copy (`read()`) | 8.2 ms |
| Hand a 6.2 MB frame to the detector process (copy + pickle + unpickle) | 4.1 ms |
| Extra OS threads per open stream, OpenCV default | **+39** |
| Extra OS threads per open stream, `CAP_PROP_N_THREADS=1` | **+0** |
| Whole machine, max decoded frames/s | ~3,000 |

One round-robin RTSP visit ≈ 77 ms of CPU. **300 visits in flight ≈ 8 of 24 cores** — CPU is not the
limit once the thread fix (§2.1) is in.

---

## 2. RTSP fixes

### 2.1 One decoder thread per stream
- **Where:** `open_video_capture`, `master_pipeline.py:875`
  (`cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)`).
- **Change:** pass `[cv2.CAP_PROP_N_THREADS, 1]` as open parameters.
- **Why:** each open stream currently spawns ~39 decoder threads; 300 streams would mean ~11,700.
  With 1 thread the stream decodes on its own capture thread (~200 fps per core — more than enough
  for 2 frames per visit).
- **Verify:** thread count of the pipeline process (`ls /proc/<pid>/task | wc -l`) drops sharply;
  context switches/s drop in Prometheus.

### 2.2 Real stream budget instead of "6 per DSS server"
- **Where:** `MAX_STREAMS_PER_DVR` (`:223`), `_get_server_sem` (`:1340`), `sem_key` (`:1368`).
- **Change:** two limits, both env-configurable:
  - `MAX_STREAMS_PER_DSS` — per DSS server. Start at **50**, raise per §5 (target ~300).
  - `MAX_STREAMS_PER_DEVICE` — per recorder (`device_id`), **4–6**, protects each centre's upload.
  A visit must get a slot in both; release both in `finally`.
- **Why:** removes our own artificial cap while still protecting DSS and centre uplinks.
- **Expected:** 24–42 streams at once → up to ~300 per worker; revisit ~128 s → ~10 s for ~945 cameras.
- **Note:** the limit is still per worker process. With several workers on one DSS server the
  per-server number must be divided between them (a fleet-wide budget via the token coordinator is
  the follow-up, not part of this change).

### 2.3 Stop idle slots from spinning
- **Where:** `slot_loop` in `_run_round_robin` (`:1431–1449`).
- **Change:** when the camera's DSS server or recorder is full, wait for a slot with a short timeout
  (e.g. `sem.acquire(timeout=0.5)`) or skip ahead to a camera on another server, instead of the
  current skip + `sleep(0.02)` loop. Size `RR_POOL_SIZE` to roughly the total budget.
- **Why:** today ~214 of 256 slots loop without work, burning CPU and adding context switches.

### 2.4 Log what round-robin is doing
- **Where:** `_rr_capture_once` (`:1349`), `_run_round_robin` (`:1422`).
- **Change:** counters + a periodic progress line in the same format as HLS
  (`[RTSP RR] progress … covered x/y | visits ok/fail | dropped | live/peak | revisit`), including:
  - frames dropped because the unified queue was full — today `except queue_module.Full: pass`
    (`:1411`) drops them silently;
  - failures split by reason (DSS login, 2084, port blocked, timeout, open failed).
- **Why:** RTSP round-robin logs nothing today; the ramp test in §5 needs these numbers.

### 2.5 Circuit breaker per DSS server
- **Where:** `_rr_capture_once` / `get_rtsp_cached`.
- **Change:** after repeated failures from one server (503, login lockout, API timeouts), mark it down
  for a cool-off (≥ 300 s — the DSS lockout time) and skip its cameras until then.
- **Why:** a down or locked server is currently retried camera by camera, wasting slots and
  prolonging lockouts.

---

## 3. HLS fixes

### 3.1 Raise the pool and per-origin cap gradually
- **Where:** `.env` — `HLS_RR_POOL_SIZE`, `HLS_RR_MAX_STREAMS_PER_HOST` (currently 8 / 8).
- **Change:** 8 → 32 → 64 → 128, one step per exam shift, per §5.
- **Why:** the TCS origin has already held ~128 open streams without trouble; the 8 is our setting.
- **Expected:** revisit 1–8 min → well under a minute.

### 3.2 Hold streams open once the pool covers the cameras
- **Where:** already implemented — `master_pipeline.py:1764` switches to `_hls_unified_persistent`
  when cameras ≤ pool.
- **Change:** none in code; happens automatically once 3.1 raises the pool above a job's camera count.
- **Why:** removes the 3–10 s re-open (page fetch + `.m3u8` + segments + FFmpeg start) on every visit.
- **Caveat:** `grab()` **does decode** every frame (measured 5.0 of 8.2 ms); the code comment at
  `:1551` saying it skips decoding is wrong. A held 1080p stream costs full decode on the CPU — keep
  held streams within ~100 per 24-core worker until Plan B.

### 3.3 One decoder thread per stream
- **Where:** `open_hls_stream` in `tcs_hls_utils.py`.
- **Change:** same as 2.1.

### 3.4 Print pushed / dropped counts
- **Where:** `_hls_unified_stats` (counters exist, not printed) → add to the `[UnifiedHLS RR]`
  progress line.
- **Why:** shows whether the detector is keeping up as the pool grows.

---

## 4. Not in this change

- `USE_NVDEC=1` — **keep off.** The worker's OpenCV is built without GStreamer and the pipeline
  template is H.264-only while most streams are H.265, so it silently falls back to CPU.
- FP16 inference, shared HLS streams across analytic jobs, fleet-wide DSS budget, Plan B.

---

## 5. Rollout and ramp test

1. Ship 2.1–2.4 and 3.3–3.4 together (code), with the budget left at the current effective values.
2. During a live exam window, raise one knob at a time and hold each step ≥ 15 min:
   - RTSP `MAX_STREAMS_PER_DSS`: 50 → 100 → 200 → 300 (one DSS server first).
   - HLS `HLS_RR_POOL_SIZE` / `HLS_RR_MAX_STREAMS_PER_HOST`: 32 → 64 → 128.
3. Watch at each step: visit success %, DSS timeouts / "MTS server is not ready", frames dropped,
   revisit time, worker CPU and context switches, DSS server outgoing Mbps (ask the DSS admin for a
   screenshot during the step).
4. **Stop and step back one level** if visit success drops by more than ~5 points, DSS timeouts or
   "MTS not ready" climb, or dropped frames appear. That level is the real capacity for that server.
5. Ship 2.5 (circuit breaker) once the logs from step 3 show how failures cluster.
