# Scaling Path A — GStreamer + NVDEC + Round-Robin (~800 cameras / GPU)

> **Target:** ~800 *concurrent* live streams per H200, and via round-robin rotation,
> coverage of **all cameras assigned to a worker** (several thousand, sampled).
> **No model retraining. No TensorRT conversion. YOLO (`yolov10m.pt` + ultralytics) stays as-is.**
> This is the **interim / fast** path. For 3–4k/GPU see `SCALING_PATH_B_DEEPSTREAM_TENSORRT_4000.md`.

---

## 0. TL;DR

Three independent changes, all required together:

1. **Unified detector** — run YOLO **once** per frame, feed boxes to many cheap rule-evaluators
   (kills the ~10× redundant inference). *Pure Python, no infra, shippable today.*
2. **NVDEC decode via GStreamer** — move H.264/H.265 decode off the CPU onto the GPU's
   hardware decoder (removes the wall that pins us at `MAX_CONCURRENT=24`).
3. **Round-robin scheduler** — a bounded pool of ~800 live capture slots; cameras rotate
   through it so every assigned camera gets a turn (fairness, no starvation).

YOLO model code is **unchanged**. Only the capture layer and the worker/dispatch layer change.

---

## 1. Why all three are required (they fix different bottlenecks)

| Bottleneck | Symptom | Fixed by |
|---|---|---|
| CPU software decode | caps at ~24 streams | **NVDEC + GStreamer** |
| Same model run ~10× per frame | GPU drowns even after decode is fixed | **Unified detector** |
| Only N cameras ever get a slot | rest starve | **Round-robin** |

**Critical:** NVDEC alone does NOT get you to 800. Without the unified detector, the GPU
must do `800 × 10 = 8,000` inferences and chokes (~80–120 real cameras). Without round-robin,
only the live set is ever processed. **All three, or it falls short.**

---

## 1b. Issue → Fix (history & resolution)

Each ceiling we hit, what caused it, and how this design resolves it (status as of writing):

| # | Issue (symptom) | Root cause | Fix in this design | Status |
|---|---|---|---|---|
| 1 | Only ~24 cameras run; rest starve | `MAX_CONCURRENT=24` + 1-producer-per-camera holds a slot for the whole shift (`master_pipeline.py:955`,`:1094`) | round-robin queue + bounded pool — every camera returns to the tail | planned |
| 2 | Raising the constant doesn't help | 1 `mp.Process`/cam → RAM/context-switch death; CPU software decode ~0.5–1 core/stream | bounded pool (not per-cam process) + **NVDEC** decode on GPU | planned |
| 3 | Even after decode fix, GPU drowns | **same `yolov10m` run ~10×/frame** (one per analytic; all load identical model — confirmed) | **unified detector**: 1 inference/frame → boxes fanned to cheap rule-evaluators | planned (shippable today, no infra) |
| 4 | Over-sampling waste | capture at 5 fps while cooldowns are 60–360s → ~600 inferences between two saved events | FPS set by detection latency (~1–2 fps); cooldown left fixed (client) | planned |
| 5 | Rotation could hammer DSS/DVRs on every revisit | per-camera **StartVideo + port-probe is not cached**; fired on every connect | cache StartVideo result + port-probe per camera/host; per-DSS rate cap (see §4 Auth) | **open — must fix before round-robin** |
| 6 | Server re-login storm under rotation | — | **already solved**: server token pre-authed once/server, cached 5 min, reused by all 3,000 cams (`master_pipeline.py:488`,`:1043`) | ✅ done |

> Items 1–4 are the core rewrite (unified detector first, then NVDEC, then round-robin).
> Item 5 is the **one auth gap** rotation introduces; item 6 confirms server-level auth is
> already safe.

---

## 2. The Unified Detector (foundation — shared with Path B)

### Current state (the waste)
- Every detection script loads the **identical** model — `models/yolov10m.pt`
  (`Crowd_det_complete.py:18`, `close_proximity.py:18`, `Intruder_det.py:17`, … all 13).
- Camera→script mapping is classification-driven (`utils/camera_loader.py:23`
  `SCRIPT_CLASSIFICATION_MAP`). An **inside-lab** camera matches ~10 scripts.
- Each script is its own worker process with its own queue (`master_pipeline.py:1061-1079`)
  and the camera's frame is fanned to all of them (`master_pipeline.py:1110-1114`).
- Net: one lab frame → **~10 separate `yolov10m(frame)` passes** producing the same boxes.

### The fix
YOLO returns *all* objects in a frame; every analytic is just a rule over those boxes:
- `crowd` = `count(person) >= 10`  ·  `crowd_zero` = `== 0`  ·  `crowd_five` = `< 5`
- `intruder` = `> 0`  ·  `server` = `>= threshold`  ·  `entry_exit` = `>= 2`
- `proximity` = pairwise distance between the same person boxes
- `qp_box` = `box.cls in [24,25,26,28,73,84]`  ·  `phone` = cell-phone class
- (all currently `int(box.cls[0]) == 0` person filters — confirmed in code)

So: **run the model once, apply many rules.**

```python
# ONE model pass per frame
boxes = detector(frame)                       # the only GPU-heavy step

persons = [b for b in boxes if int(b.cls[0]) == 0 and b.conf[0] >= CONF]
phones  = [b for b in boxes if int(b.cls[0]) == 67]
bags    = [b for b in boxes if int(b.cls[0]) in (24,25,26,28,73,84)]

# per-camera enabled set — SAME config as today (classification map / DB)
if "crowd"     in cam.enabled: evaluate_crowd(persons, item)
if "proximity" in cam.enabled: evaluate_proximity(persons, item)
if "intruder"  in cam.enabled: evaluate_intruder(persons, item)
# ... disabled analytics are simply skipped — costs nothing, full flexibility preserved
```

### Flexibility is preserved
The on/off decision moves from *"spawn a worker process"* to *"call this rule or not"* —
driven by the **same** per-camera config. You can enable any subset per camera, instantly,
with zero process overhead.

### Migration (incremental, low-risk)
1. Add `detect(frame) -> boxes` (one shared model instance).
2. Convert each script's core function from "load model + run + decide" to "**accept boxes** + decide".
   Delete only the 2 model lines; **keep all thresholds, debounce (`last_save_times`), DB
   reporting, `cv2` overlays, image-on-hit.**
3. Convert the **person-counting family first** (crowd, crowd_zero, crowd_five, intruder,
   server, entry_exit) — nearly identical, mechanical.
4. Then proximity, phone, qp_box (different class filters, same boxes).
5. Then trackers (invigilator, entry_exit-crossing) — add **one** shared tracker on the boxes.
6. Move image-quality checks (inversion, tamper, blind_spot) to **classical CV (no GPU)**.
7. Old per-script paths stay runnable until each is migrated → diff outputs to verify identical rows.

**Effort:** person-counting family in ~1 day; full set + orchestration ~1–2 weeks.
**Gain alone (still CPU decode):** ~24 → ~80–120 cameras/GPU.

---

## 3. NVDEC decode via GStreamer

### What it does
NVDEC is a dedicated hardware decoder block on the H200 (separate from CUDA cores).
GStreamer's `nvv4l2decoder` (or FFmpeg `h264_cuvid`) decodes the RTSP stream on the GPU,
so the CPU only handles network + demux. This removes the per-stream ~0.5–1 CPU core cost
that forces `MAX_CONCURRENT=24`.

**Verified on host:** H200 + driver 580; `ffmpeg -decoders` lists `h264_cuvid`, `hevc_cuvid`.

### The build gotcha (the real work item)
`pip install opencv-python` ships its **own** bundled FFmpeg that usually lacks cuvid, so
`cv2.VideoCapture` may not engage NVDEC even with env hints. Pick ONE:

| Option | How | Notes |
|---|---|---|
| **A. OpenCV + GStreamer backend** *(recommended)* | build/installed OpenCV `WITH_GSTREAMER=ON`; open a `gst-launch` pipeline string via `cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)` | clean, frames as numpy; can keep on GPU later |
| B. OpenCV linked to system FFmpeg w/ cuvid | custom OpenCV build against system ffmpeg | heavier build |
| C. FFmpeg subprocess pipe w/ `h264_cuvid` | spawn ffmpeg, read raw frames from stdout | simplest to prove, less elegant |

> **Action item before coding:** on a *worker*, run
> `python -c "import cv2; print('GStreamer' in cv2.getBuildInformation())"` and
> `gst-inspect-1.0 nvv4l2decoder`. This decides Option A vs B/C and whether NVDEC is
> same-week or a container-build task.

### Code change (localized)
Replace `open_video_capture()` (`master_pipeline.py:788`) with a GStreamer pipeline. Example:

```python
def open_video_capture(stream_url, latency_ms=200):
    pipeline = (
        f'rtspsrc location="{stream_url}" protocols=tcp latency={latency_ms} ! '
        f'rtph264depay ! h264parse ! nvv4l2decoder ! '
        f'nvvidconv ! video/x-raw,format=BGRx ! '
        f'videoconvert ! video/x-raw,format=BGR ! '
        f'appsink drop=true max-buffers=1 sync=false'
    )
    return cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
```

Everything downstream (`read_frame_from_capture`, the dispatch to rule-evaluators) is unchanged.
**YOLO/ultralytics untouched** — `detector(frame)` still receives a numpy BGR frame.

### Optional zero-copy (later)
Keep the frame on the GPU and pass a CUDA tensor to ultralytics (it accepts `torch` tensors on
`cuda`). Saves the device→host copy. Not required for 800; nice for headroom.

---

## 4. Round-robin scheduler (coverage for all assigned cameras)

### Concept
Maintain a fixed pool of ~800 capture slots and a queue of all assigned cameras. A slot grabs a
camera, captures a short burst (1–3 frames) or holds a continuous lane, then returns the camera to
the **tail** of the queue. Fairness guaranteed; no starvation.

```
all assigned cameras (e.g. 2,400)  →  [ round-robin queue ]
                                              │
                              ┌───────────────┴───────────────┐
                              ▼  pool of ~800 capture slots    ▼
                        slot: pull cam → NVDEC grab → return to tail
                              │
                              ▼
                     shared detector (1× YOLO, batched 32–64)
                              │
                              ▼
                     per-camera enabled rule-evaluators
```

### How it works — waves (worked example: 3,000 cameras, 800 slots)

Think of it as **800 cameras processed in parallel at a time**, in waves. Each slot pulls a
camera, grabs a frame, runs the shared detector, and returns the camera to the **tail** of the
queue.

```
3,000 cameras in queue.  800 live slots.

Wave 1: cameras    1– 800   (camera #1 is here)  ─┐
Wave 2: cameras  801–1600                         │  each wave takes S seconds
Wave 3: cameras 1601–2400                         │  (S = grab + inference per visit)
Wave 4: cameras 2401–3000  + wave 1 returns      ─┘
```

### Revisit formula
```
revisit interval = (total cameras / pool size) × service time per visit (S)
                 = (3000 / 800) × S
                 = 3.75 × S
```
`S` = time a camera occupies a slot (frame grab + YOLO). With a warm/reused connection `S ≈ 1–1.5s`:

| S | Camera #1 returns every |
|---|---|
| 1.0s | **~3.75 s** |
| 1.5s | **~5.6 s** |

So with 3,000 cameras on one GPU, **each camera is looked at roughly every 4–6 seconds.**
- **N ≤ 800** → run continuous, every camera always live (no rotation).
- **N = 2,400** → revisit ≈ `(2400/800) × 1.5s ≈ 4.5s`.
- **N = 3,000** → revisit ≈ `(3000/800) × 1.5s ≈ 5.6s`.

### Two separate clocks: FPS/revisit vs cooldown (do NOT confuse them)

| Clock | Controls | Set by |
|---|---|---|
| **FPS / revisit** | how often we *look at* a camera (capture + YOLO) | us (capacity) |
| **Cooldown** | how often we *report/save* from that camera | **client — FIXED, never changed** |

Cooldown definition (client semantics): *after camera N reports a detection, the next report
from camera N is suppressed until the cooldown elapses.* Looking ≠ reporting — we look many
times, we report rarely.

**How they interact (the key result):** revisit (~5s) ≪ cooldown (60–360s). So within one
cooldown window we look at a camera ~20–30 times:
- first look that sees a violation → reports, starts cooldown;
- the next ~25 looks see it but report nothing (suppressed);
- after the cooldown, the next look still seeing it → reports again.

**Therefore the cooldown is never the bottleneck** — round-robin revisit is far faster than any
client cooldown, so onset is caught within ~5s and re-reporting honors the client's fixed cooldown
exactly. The two clocks work together cleanly; we never modify the cooldown.

### Lanes (important for exam use)
A ~5s revisit is fine for crowd / intruder / server / occupancy (a person physically present
doesn't vanish in 5s). It is **not** fine for:
- **Brief events** (phone, visible ~5s) → at one look per ~5s you may catch it only once or miss it.
- **Tracking** (invigilator, entry_exit-crossing) → a tracker needs *consecutive* frames; it
  cannot be rotated at all.

So split into lanes (both share the **one** detector):
- **Continuous lane** — a subset of the 800 slots held permanently for phone / invigilator /
  entry_exit-crossing cameras, looked at at 2–3 fps, never rotated.
- **Rotation lane** — the remaining slots round-robin everything else.

```
200 tracking/phone cameras → continuous lane (held, 2–3 fps)
600 slots                  → round-robin the other 2,800 cameras
                             revisit = (2800/600) × S ≈ 4.7 × S ≈ 5–7 s
```

### FPS per camera (decoupled from cooldown)

FPS is set by **detection latency** (how fast we must catch onset), NOT by the client cooldown.
A person physically present doesn't vanish in under a second, so **1 fps catches onset** for most
analytics. Per-camera FPS = the **max** of its enabled analytics (one inference serves them all).

Client cooldowns (fixed inputs — for reference only, they do NOT set FPS):

| Script | Cooldown (s) | Lane / FPS |
|---|---|---|
| Intruder | 0 | default, 1 fps |
| Server_room | 60 | default, 1 fps |
| phone | 90 | **brief event** → continuous lane, 2 fps |
| Crowd / crowd_five / proximity / entry_exit / biometric | 120 | 1 fps (crossing → 2 fps) |
| StrongRoom / blind_spot | 180 | 1 fps / CV off-GPU |
| qp_box | 240 | 1 fps |
| crowd_zero | 360 | 1 fps |
| invigilator | none (tracking) | **continuous lane, 2–3 fps** |

| Lane | FPS | Why |
|---|---|---|
| Default (counting/occupancy/intruder) | **1 fps** | onset within 1s; cooldowns are 60–360s |
| Brief events (phone, proximity, entry_exit-crossing) | **2 fps** | phone visible ~5s → ~10 chances |
| Tracking (invigilator) | **2–3 fps** | motion continuity |
| Image quality (inversion/tamper/blind_spot) | ~0.2 fps | classical CV, off-GPU |

**Planning number: ~2 fps average per camera.**
```
800 cameras × 2 fps = 1,600 inferences/sec   ← fits a ~2,000–3,000/s H200 budget (benchmark to confirm)
```
(vs today: 5 fps × ~10 analytics = ~50 inf/cam/sec — unsustainable.)

> **NOTE on cooldown:** cooldown is **client-defined and fixed** — never tuned for capacity. It
> only governs report cadence. Since revisit (~5s) ≪ cooldown (60–360s), no special "backoff" is
> required; the cooldown is honored naturally by the existing `last_save_times` debounce in each
> script.

### Scheduler changes (`master_pipeline.py`)
- Replace `MAX_CONCURRENT=24` semaphore + 1-producer-per-camera-forever
  (`master_pipeline.py:955`, `:1094`) with a **bounded pool + round-robin queue**.
- Add a **per-DVR connection cap** (fragile DVRs — d11/d16 history; don't concentrate 800
  connections on a few DVRs).
- Add **aggressive per-grab timeouts** so a dead DVR can't tie up a slot (head-of-line blocking).
- Reuse DSS token pre-auth per server (already at `master_pipeline.py:1043`).
- Track **per-camera actual revisit interval** + alarm on SLA breach (don't trust the average).

### Authentication under round-robin (token-reuse analysis)

There are **two distinct auth artifacts** in the DSS flow — they behave very differently under
rotation:

| Artifact | Code | Scope | TTL | Cached? |
|---|---|---|---|---|
| **Server auth token** | `get_server_token` (`master_pipeline.py:488`) | per DSS server, shared by ALL its cameras | `TOKEN_EXPIRY_SECONDS = 300` | ✅ disk + memory (`server_tokens.json`), process-safe, negative-cached on failure |
| **StartVideo result** (RTSP URL + per-stream token) | `get_rtsp_from_dss` (`master_pipeline.py:650`) | **per camera, per connection** | unknown (401 ⇒ refetch) | ❌ **not cached — a fresh `requests.post` every connect** |

**The good part (your intuition is correct for the server token):**
The server login token is **pre-authed once per unique server** at pipeline start
(`master_pipeline.py:1043`) and reused from cache for 5 minutes. So the **next 800 — and all
3,000 — cameras reuse the same server token**; round-robin does **NOT** cause a re-login storm.
A background refresh every <300s keeps it warm. ✅

**The real risk (NOT handled today):**
The per-camera **`StartVideo` call is made on every (re)connection** and is **not cached**, plus
it does a **TCP port probe (2s timeout) per returned URL**. In the *continuous* model this fires
**once per camera per shift** — negligible. In **round-robin, every revisit is a reconnect**, so:

```
StartVideo calls/sec ≈ cameras / revisit  =  3000 / 5s  ≈  600 calls/sec against DSS
```

That can **overload the DSS API / DVRs** (the d11/d16 fragility) — it's the "fresh-connect-per-
snapshot hammers the DVR" problem from `SCALING_4000_CAMS_PER_GPU.md §4`. **So auth is NOT
"perfectly handled" for round-robin out of the box** — the *server token* is, but the
*StartVideo + port-probe* per-visit cost is not.

**How it gets fixed (required for Path A round-robin):**
1. **Cache the StartVideo result (URL + stream token) per camera** for its validity window and
   reuse on re-pull (refetch only on 401/stream failure). *Open item: measure the StartVideo
   URL/stream-token TTL — this single number decides how much DSS load drops.*
2. **Bound StartVideo to slot-entry, not per-frame**, and add a **per-DSS/per-DVR rate cap +
   circuit breaker** so a server is never hit faster than it can serve. With a warm pool the rate
   is ~`pool/service ≈ 800/1.5s ≈ 530/s` worst case → cap it.
3. **Cache port-probe results per host** (a port open 5s ago is still open) — do not re-probe
   every visit; this also shrinks service time `S` (faster revisit).
4. **Keep server-token reuse as-is** — already correct; just keep the background refresh alive.

> **Net:** server-level auth is already reused correctly across all 3,000 cameras. The work item
> is the **per-camera StartVideo + port-probe**, which must be cached / rate-capped before
> round-robin is safe — otherwise the DSS/DVRs, not the GPU, become the bottleneck.

### Revisit time — how long until ONE camera comes back around

**Definitions (don't confuse them):**

| Term | Meaning | Value |
|---|---|---|
| **Single visit `S`** | time to service ONE camera once (StartVideo + RTSP setup + keyframe wait + grab + infer) | ~1.5 s healthy |
| **Revisit (full cycle)** | time for ALL N cameras to each get one turn → camera #1's next turn | what matters |

> Note on the 2s TCP probe: `probe_tcp_port(timeout=2.0)` is a **worst-case timeout for
> *unreachable* ports**, not the normal cost. A healthy TCP handshake is ~1–50 ms. The 2s only
> applies to dead/blocked cameras (mitigate: cache probe-per-host + circuit breaker).

**Revisit math:**
```
revisit = N / throughput
  Pool-bound:  throughput = pool / S = 800 / 1.5s ≈ 533 cameras/sec
  DSS-bound:   throughput = R_dss   (StartVideo calls/sec a DSS server can mint)
  revisit = N / min(pool-bound, DSS-bound throughput)
```
The 800 slots run **in parallel** (capture is I/O-bound on network waits), so the cycle is
`N/pool × S`, NOT `N × S`. The DSS is the only thing that drags it out, by capping throughput
below the pool's 533/sec.

**Revisit for 3,000 cameras (camera #1's next turn):**

| Scenario | Throughput | **Revisit** |
|---|---|---|
| **Continuous @ ≤800/GPU** (no rotation) | n/a | sample interval, ~**0.5 s** @ 2 fps; no StartVideo at all |
| **Cached URL** / pool-bound | 533 cam/s | `3000/533` ≈ **5.6 s** |
| **Uncached, spread over ~10 DSS servers** | ~500 cam/s | ≈ **6–15 s** |
| **Uncached, one DSS @ 100/s** | 100 cam/s | `3000/100` = **30 s** |
| **Uncached, one DSS @ 50/s** (typical) | 50 cam/s | `3000/50` = **60 s** |
| **Uncached, one slow DSS @ 20/s** | 20 cam/s | `3000/20` = **150 s (2.5 min)** |

**Key driver: cameras-per-DSS-server, not caching alone.** The DSS StartVideo limit is
**per server**. Spread 3,000 cameras across many DVRs/DSS → per-server load drops → revisit stays
~6–15s even uncached. Concentrate them on one DSS → ~60s and you need caching or DVR-aware sharding.

**Caching the URL removes the DSS-bound regime (→ ~5.6s) but NOT the keyframe wait** — you still
reopen the RTSP stream per visit (~1s). Only a *warm/held* connection avoids that, which = the
continuous mode (capped at ~800 cameras).

**Two unknowns that turn this into an exact number** (measure on a worker):
1. **StartVideo throughput per DSS server** (`R_dss`) — fire N concurrent StartVideo, find calls/sec before it slows/errors.
2. **How many DSS servers** the cameras span (→ DVR-aware sharding decision).

---

## 5. End-to-end architecture (Path A)

```
RTSP/DSS (N assigned cams)
        │  DSS auth/token reused per server (existing get_server_token)
        ▼
Round-robin queue ──► bounded pool (~800 slots)
        │
        ▼  NVDEC decode (GStreamer nvv4l2decoder) — frame as numpy/GPU
Shared detector: yolov10m + ultralytics, ONE pass, batched 32–64 frames
        │
        ├─ persons / phones / bags  (filtered from same boxes)
        ▼
Per-camera enabled rule-evaluators (crowd, proximity, intruder, server, ...) — CPU, cheap
        │
        ├─ tracker (invigilator, entry_exit-crossing) — continuous lane
        ├─ image-quality (inversion/tamper/blind_spot) — classical CV, no GPU
        ▼
hit? ─► VLM (vision_classification) gated on trigger only
        ▼
batched/async DB writes  +  image saved only on hit
```

---

## 6. Capacity reasoning

| Stage | Budget at 800 cams | Notes |
|---|---|---|
| Decode | NVDEC, ~800 streams sampled | H200 has multiple NVDEC engines; no session cap on datacenter cards |
| Inference | 800 × **1** YOLO pass, batched | unified detector; FP16 ultralytics on H200 handles it easily |
| Network | 800 × ~3 Mbps ≈ 2.4 Gbps | needs 10GbE NIC headroom |
| RAM | pool processes/threads, bounded | not 1-proc-per-camera; bounded pool |

Without unified detector this becomes `800 × 10` inferences → GPU-bound → ~80–120 real cams.

---

## 7. Risks / gotchas

- **OpenCV build for GStreamer/NVDEC** — the main work item; verify on a worker first.
- **RTSP keyframe wait** — snapshot grabs wait up to one GOP for an I-frame; budget it in revisit.
- **DVR fragility** — per-DVR cap + timeouts mandatory, or one bad DVR degrades the whole pool.
- **Blind windows in rotation lane** — brief events (phone) can be missed; keep them in the
  continuous lane.
- **Codec** — confirm DVRs send H.264/H.265 (NVDEC supports both); MJPEG also covered (`mjpeg_cuvid`).

---

## 8. Phased plan & time

| Phase | Work | Time |
|---|---|---|
| 0 | Unified detector — person-counting family, old paths intact, diff outputs | ~1 day |
| 1 | Unified detector — proximity/phone/qp_box + shared tracker + CV checks | ~1 week |
| 2 | Worker probe: OpenCV GStreamer/NVDEC availability; pick Option A/B/C | ~0.5 day |
| 3 | GStreamer NVDEC capture swap (`open_video_capture`); benchmark 50→200→500 | ~3–5 days |
| 4 | Round-robin scheduler + per-DVR cap + timeouts + revisit SLA metrics | ~1 week |
| 5 | Lanes (continuous vs rotation), VLM gating, async DB writes | ~3–5 days |
| **Total** | functional ~end of Phase 3; production ~5–6 weeks | |

**Shippable today:** Phase 0 (unified detector, person-counting family) — the 10× win, no infra.

---

## 9. Validation

- Phase 0/1: diff DB rows old-path vs unified for the same recorded frames → must match.
- Phase 3: measure cores-per-stream before/after NVDEC; confirm CPU no longer the wall.
- Phase 4: ramp cameras, measure achieved revisit interval per camera vs target.
- Confirm no missed events in continuous lane under load.

---

*Status: design. Targets (~800/GPU, revisit ~4.5s at 2,400 assigned) are working assumptions
to validate by benchmark. Model is NOT retrained or converted in this path.*
