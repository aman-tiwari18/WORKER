# Scaling Plan — 80,000 Cameras on 20× H200 (4,000 cameras / GPU)

> **Goal:** Process **80,000 cameras** across **20 H200 GPUs** = **4,000 cameras per GPU**,
> with **every camera getting a turn** (no starvation), instead of the current behaviour
> where only ~24 cameras run and the rest are starved.

---

## 1. The Problem (current state)

The current RTSP pipeline (`script-server/scripts/detection/pipeline/master_pipeline.py`)
uses a **"1 camera = 1 permanent process, continuous 5 FPS streaming"** model:

- For every camera a thread is launched, but only **`MAX_CONCURRENT = 24`** producers run at once
  (semaphore at `master_pipeline.py:1094`).
- A producer for a **stable camera** enters a `while not STOP` frame loop and **never releases its
  semaphore slot** for the whole shift (`master_pipeline.py:955`).
- An **unstable camera** releases its slot only after `MAX_CONSECUTIVE_FAILURES = 3` (`master_pipeline.py:862`).

### Consequence — starvation
Once 24 stable cameras grab the 24 slots, they hold them forever. The remaining cameras' threads
sit blocked on `sem.acquire()` and their producers **never launch**. With 4,000 cameras on a GPU,
**3,976 cameras would never get a turn.**

### Why simply raising `MAX_CONCURRENT` to 4000 does NOT work
| Limit | Now | At 4,000 |
|---|---|---|
| 1 `mp.Process` per camera | 24 processes | 4,000 processes → RAM / context-switch death |
| Slot held forever | by design | permanent starvation |
| Continuous 5 FPS | 24×5 = 120 fps | 4,000×5 = **20,000 fps** decode+inference → impossible |

**The model itself must change, not the constant.**

---

## 2. The Core Shift: Continuous Streaming → Rotating Snapshot Sampling

We stop keeping 4,000 live streams open. Instead, every camera is **sampled in a round-robin
rotation** at a target interval. This is how large-scale CCTV analytics systems operate, and it
guarantees fairness (every camera gets its turn) while keeping the GPU at a manageable load.

**Target revisit interval: ~10–15s** (using ~12s for sizing).
Acceptable for exam-monitoring analytics (crowd / intruder / phone / invigilator / server-room),
which do not need 5 FPS continuous coverage.

---

## 3. Target Architecture (per GPU worker)

```
        RoundRobin camera queue (4,000 cameras, DVR-aware)
                         |
                         v
   Bounded warm-connection capture pool (≈300–500 workers)
   - pull camera -> warm RTSP/DSS connection -> grab 1–3 frame burst
   - return camera to TAIL of queue (fair rotation, no starvation)
   - per-DVR concurrency throttle
                         |
                         v
              GPU batch queue (frames from many cameras)
                         |
                         v
       Batched inference (YOLO, 32–64 frames per forward pass)  ← NVDEC decode
                         |
                         v
   detection hit? --> trigger VLM classification (vLLM/Ollama) only on hits
                         |
                         v
        results -> MySQL (batched/async writes)
        images  -> NFS (only on hits + periodic proof-of-work)
```

### Four core changes
1. **Persistent producer → bounded capture pool + round-robin queue.**
   Fixed pool (~300–500 capture workers), all cameras in a round-robin queue. Each worker grabs a
   snapshot/burst then returns the camera to the tail. **Fairness guaranteed — starvation gone.**
2. **GPU batched inference.** Collect 32–64 frames across cameras into one forward pass. This is the
   throughput multiplier; inference stops being the bottleneck.
3. **Sampling interval, not continuous.** Each camera every ~12s instead of 5 FPS.
4. **Warm connection pool + DSS token reuse.** Never 4,000 simultaneous connections; rotate cameras
   over a bounded warm pool, amortise connect cost over a short burst.

---

## 4. The Real Bottleneck: Connection / Decode (NOT the GPU)

- **GPU inference:** 4,000 @ 12s ≈ **333 fps batched YOLO** — trivial for H200. Not the bottleneck.
- **RTSP/DSS connection + decode** is the hard part:
  - DSS auth + token + RTSP negotiation ≈ 0.5–3s per camera.
  - Fresh-connect-per-snapshot → ~500 connections in-flight constantly → kills network/CPU/DVR.
  - **NVDEC (hardware decode) is effectively mandatory** at this scale; CPU decode cannot sustain 333 fps/GPU.

### Connection strategy decision
| Strategy | Pros | Cons |
|---|---|---|
| **A. Fresh connect per snapshot** | simple, stateless, fair | connect cost every time, hammers DVRs |
| **B. Warm pool + camera rotation** *(recommended)* | amortises connect cost, DVR-friendly | more state to manage |

**Recommended: B**, because the DVRs are already fragile (see DVR circuit-breaker / d11–d16 outage
history). Fresh-connect spam would make it worse.

---

## 5. Fleet / System-Level Concerns (80k / 20)

```
Fleet snapshot throughput = 80,000 / 12 ≈ 6,700 snapshots/sec (all GPUs combined)
```

### 5.1 DVR-aware sharding (biggest factor)
80k cameras span hundreds–thousands of DVRs. The `camera_distributor` must shard by **DVR awareness**,
not just camera count:
- **DVR affinity:** keep one DVR's cameras on the same worker (share auth/token, reuse connections) —
  OR deliberately spread to avoid overloading a single DVR. *(Decision pending — depends on DVR count.)*
- **Per-DVR concurrency cap:** max N concurrent connections per DVR; throttle the rest. Natural
  extension of the existing circuit-breaker idea.

### 5.2 Central MySQL
20 workers polling is fine. Watch **detection-event write rate**:
- Sparse events (typical for exam monitoring) → fine.
- Writing a row per snapshot → ~6,700 writes/sec → needs batched/async writes.

### 5.3 NFS / image storage
Save **only detection hits + periodic proof-of-work**, never every frame, or NFS will be overwhelmed.

---

## 6. Reuse vs Replace (impact on existing code)

### Keep (reuse)
- GPU worker processes + batched queue model (`master_pipeline.py:1057-1079`)
- DSS auth / token pre-fetch / `get_server_token` (`master_pipeline.py:1043-1052`)
- `order_urls`, `probe_tcp_port`, frame validation helpers
- `camera_distributor` sharding (extended to be DVR-aware; per-worker count now up to 4,000)

### Replace
- `MAX_CONCURRENT = 24` semaphore + **1-producer-per-camera-forever** → **round-robin scheduler +
  bounded warm pool**
- Continuous `while not STOP` 5 FPS loop (`master_pipeline.py:955`) → **snapshot/burst grab then yield**
- Per-camera `mp.Process` → capture **thread / async pool** (4,000 processes is infeasible)

---

## 7. Honest Risk Assessment

- 4,000/GPU is **plausible but aggressive** — connection/decode bound, needs heavy tuning
  (warm-pool size, burst length, NVDEC offload, DVR throttle). **Not a config change — a real rewrite.**
- Tradeoff accepted: **continuous detection is lost.** An event occurring entirely between two
  snapshots (~12s gap) may be missed. Acceptable for static-ish exam events. If any analytic needs
  real-time (e.g. live invigilator tracking), give those cameras a **separate high-priority continuous lane**.
- Per-GPU 4,000 must be **validated by prototype**, not assumed.

---

## 8. Open Decisions (needed before finalising sizing)

1. **NVDEC (hardware decode) available/allowed?** Almost mandatory for 4,000/GPU.
2. **How many DVRs do the 80k cameras span?** Drives sharding strategy + per-DVR throttle + warm-pool size.
3. **Detection event sparsity** — row per snapshot, or only on hits? Drives MySQL/NFS sizing.
4. **Fleet size confirmation** — current memory shows 28 GPUs / 7 hosts; this plan targets 20. Which is canonical?
5. **Any analytic that genuinely needs continuous (non-sampled) coverage?** → priority-lane design.

---

## 9. Suggested Phased Implementation (high level)

1. **Phase 0 — Prototype & validate:** round-robin scheduler + warm pool + batched NVDEC inference on
   **one GPU**, ramp 100 → 500 → 1,000 → 4,000 cameras, measure achievable revisit interval & decode ceiling.
2. **Phase 1 — DVR-aware sharding:** extend `camera_distributor` with DVR affinity + per-DVR concurrency caps.
3. **Phase 2 — Write path:** batched/async MySQL writes; NFS save-on-hit policy.
4. **Phase 3 — Priority lanes (if needed):** continuous lane for real-time analytics, rotation for the rest.
5. **Phase 4 — Fleet rollout:** roll the new pipeline across all 20 GPUs, monitor via existing Prometheus.

---

*Status: design / discussion. No code written yet. Targets above (4,000/GPU, ~12s revisit) are
working assumptions to be validated in Phase 0.*
