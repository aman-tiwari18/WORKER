# CCTV Vision/Detection Fleet — Architecture, Data Flow & Scaling Guide

> Single source of truth for how the distributed CCTV analytics system is structured,
> how work and data flow across the VMs and GPUs, and how it scales from 1 host to
> 7 hosts / 28 GPUs. Includes current bottlenecks, what must be done, and optimization
> suggestions.
>
> Last updated: 2026-05-31. Reflects the live fleet after the 28-worker distribution fix.

---

## 0. TL;DR

- The system classifies & runs detections on **tens of thousands of exam-hall CCTV cameras** by pulling one frame per camera over RTSP and running a vision model (Qwen2.5-VL-7B on vLLM) plus a suite of YOLO/heuristic detectors.
- Work is spread across **7 hosts / 28× H200 (140 GB) GPUs**. **Each GPU = one independent "worker"** (its own container, poller, vLLM). There is **no central dispatcher** — every worker deterministically hashes the camera list and processes only its slice.
- A **single token-coordinator** on the main host solves the "DVRs allow one session per user" problem by holding one auth session per DVR and handing tokens to all 28 workers.
- The bottleneck is **DVR I/O (RTSP capture + per-DVR session/stream limits)**, *not* GPU compute. The GPUs are heavily over-provisioned for the inference load.

---

## 1. Hardware & Host Topology

```
                                  ┌──────────────────────────────────────────────┐
                                  │  MAIN / worker-0   gpu-h200-5  192.168.19.30   │
                                  │  1× H200 (bare-metal python under pm2)         │
                                  │  Hosts the shared services:                    │
                                  │   • MySQL  (cctv_monitoring, :3306)            │
                                  │   • Node backend  cctv-backend-node (:3000)   │
                                  │   • FastAPI/poller cctv-backend-fastapi(run.py)│
                                  │   • Camview token coordinator (:8090)         │
                                  │   • Prometheus + NFS server                    │
                                  └───────────────┬───────────────────────────────┘
                  private LAN 192.168.19.0/24     │
        ┌──────────────────┬────────────────────┬─┴──────────────────┐
   ┌────┴─────┐      ┌──────┴────┐        ┌──────┴────┐               │
   │ worker-1 │      │ worker-2  │        │ worker-3  │               │  (the "original" 4 GPUs:
   │ .19.43   │      │ .19.42    │        │ .19.41    │               │   1 GPU each, Docker)
   │ 1× H200  │      │ 1× H200   │        │ 1× H200   │               │
   └──────────┘      └───────────┘        └───────────┘               │
                                                                      │
   ════════ NEW 8-GPU VMs (added 2026-05-28/29) — remote, NAT'd ══════╪═════════
   reached over the public internet at ONE IP 103.42.51.250 via SSH tunnels
                                                                      │
   ┌────────────────────┐   ┌────────────────────┐   ┌───────────────┴────┐
   │ worker-4  :2222    │   │ worker-5  :2223    │   │ worker-6  :2224    │
   │ innmi1srh2-p002    │   │ innmi1srh2-p008    │   │ innmi1srh2-p010    │
   │ 8× H200            │   │ 8× H200            │   │ 8× H200            │
   │ Docker, 8 ctrs     │   │ Docker, 8 ctrs     │   │ Docker, 8 ctrs     │
   └────────────────────┘   └────────────────────┘   └────────────────────┘

   GPU census:  worker-0..3 = 4×1 = 4   +   worker-4..6 = 3×8 = 24   =   28 H200 GPUs
```

| Logical | Access | GPUs | Run mode | Repo path |
|---|---|---|---|---|
| worker-0 / main | `192.168.19.30` (local) | 1 | bare python, pm2 `cctv-backend-fastapi` | `/data/cctv_dev/cctv/script-server` |
| worker-1 | `root@192.168.19.43` (`iitk@2026`) | 1 | Docker | `/cctv_dev/cctv/script-server` |
| worker-2 | `root@192.168.19.42` | 1 | Docker | `/cctv_dev/cctv/script_server/script-server` |
| worker-3 | `root@192.168.19.41` | 1 | Docker | `/cctv_dev/cctv/script_server/script-server` |
| worker-4 | `iitkanpur.admin@103.42.51.250 -p 2222` | 8 | Docker ×8 | `/home/iitkanpur.admin/cdis/script_server/script-server` |
| worker-5 | `… -p 2223` | 8 | Docker ×8 | same |
| worker-6 | `… -p 2224` | 8 | Docker ×8 | same |

---

## 2. Network Architecture

The 4 original hosts sit on a **private LAN** and talk directly to the main host's MySQL/API/token server. The 3 new 8-GPU VMs are **remote** (behind one public IP, NAT'd onto SSH ports 2222/2223/2224) and reach the shared services through **persistent SSH tunnels** (systemd units `cctv-worker-{4,5,6}-tunnel.service` on the main host).

```
  REMOTE 8-GPU VM (worker-N)                SSH tunnel               MAIN HOST 192.168.19.30
 ┌───────────────────────┐                                         ┌──────────────────────┐
 │ poller/scripts use:   │                                         │                      │
 │  localhost:13306  ─────┼──reverse -R──►  main MySQL :3306        │  MySQL :3306         │
 │  localhost:13000  ─────┼──reverse -R──►  main API   :3000        │  Node API :3000      │
 │  localhost:18090  ─────┼──reverse -R──►  main token :8090        │  token coord :8090   │
 │                       │                                         │                      │
 │  8× /metrics :80xx  ◄──┼──forward -L───  main :9000-9007 (w4)    │  Prometheus :9090    │
 │                       │                  main :9100-9107 (w5)    │  scrapes these       │
 │                       │                  main :9200-9207 (w6)    │                      │
 └───────────────────────┘                                         └──────────────────────┘
```

**Why tunnels:** the remote GPU farm has no private-LAN route to the main host. The reverse-forwards let remote workers use the *same* MySQL/API/token endpoints as LAN workers (just via `localhost:13xxx`), so application code is identical everywhere. The forward-maps let the main host's Prometheus scrape each remote GPU's metrics.

**Caveat (current):** the per-GPU `/metrics` scrape is flaky on the 8-GPU boxes (nvidia-smi contention across 8 containers) — monitoring only, doesn't affect detection.

---

## 3. Software Stack per Node

Every GPU runs a **self-contained worker**. On the 8-GPU VMs there are 8 such workers (one container pinned to each physical GPU).

```
  ONE WORKER (= one GPU = one container, except worker-0 which is bare python)
  ┌────────────────────────────────────────────────────────────────────┐
  │  script-server (FastAPI app, run.py)                                 │
  │   ├─ HTTP server  :8000   → /health, /metrics, /gpu-monitor/stats    │
  │   ├─ Poller (every 30s)   → asks backend "any assignment due?"        │
  │   │                         filters cameras by hash → spawns script   │
  │   └─ script_runner         → runs detection script as a subprocess    │
  │                                                                       │
  │  Detection subprocess (one of):                                       │
  │   • vision_classification_rtsp.py   (Scene Classification, vLLM)      │
  │   • master_pipeline.py + <detector> (phone, crowd, proximity, …)     │
  │                                                                       │
  │  vLLM server (Qwen/Qwen2.5-VL-7B-Instruct)  :8001  ← started on demand│
  │  Env that defines this worker's identity & slice:                     │
  │   WORKER_ID, WORKER_INDEX, TOTAL_WORKERS, CUDA_VISIBLE_DEVICES=0       │
  └────────────────────────────────────────────────────────────────────┘
```

- **Container code path** is `/app`. Bind mounts: NFS results `/mnt/nfs/cctv_images → /app/scripts/detection/results`, models, HuggingFace cache, and **per-worker logs** `scripts/logs/worker-N-gpuM`. App code itself is *baked into the image* (not bind-mounted).
- **Detection scripts run as fresh subprocesses**, so script-level code changes take effect on the next run without a container restart. Only `app/` changes require a restart.
- **GPU pinning:** each 8-GPU-VM container gets `device_ids: ["N"]` (reserves physical GPU N) and `CUDA_VISIBLE_DEVICES: "0"` (so inside the container it's always "GPU 0"). This makes the per-GPU container config uniform.

---

## 4. Camera Distribution — How Work is Split (no central dispatcher)

Every worker pulls the **same** full camera list from the shared DB and keeps only the cameras that hash to its index. Pure, deterministic, stateless:

```python
# app/camera_distributor.py
assigned_worker = crc32(camera_id) % TOTAL_WORKERS
process_it      = (assigned_worker == WORKER_INDEX % TOTAL_WORKERS)
```

```
        full camera list (identical on every worker, from MySQL)
                              │
              camera_id ─► crc32(id) % 28
                              │
  ┌──────┬──────┬──────┬──────┴───┬───────────┬───────────┐
 idx0   idx1   idx2   idx3  …    idx11        idx19       idx27
 (w0)   (w1)   (w2)   (w3)     (w4-gpu7)    (w5-gpu7)   (w6-gpu7)
```

**The index map (must be unique 0..N-1, same TOTAL_WORKERS everywhere):**

| Worker | Indices | TOTAL_WORKERS |
|---|---|---|
| worker-0 / 1 / 2 / 3 | 0, 1, 2, 3 | 28 |
| worker-4 gpu0-7 | 4 – 11 | 28 |
| worker-5 gpu0-7 | 12 – 19 | 28 |
| worker-6 gpu0-7 | 20 – 27 | 28 |

> **Failure mode this design is sensitive to (and the bug we just fixed):** if any worker has a different `TOTAL_WORKERS`, or two workers share an index, the partition stops being a clean partition → **some cameras get processed by 2+ workers and others by none** (silent coverage gaps). LAN/worker-0 derive their index from the `worker-N` name via regex; the 8-GPU VMs set `WORKER_INDEX` explicitly per service in `docker-compose.worker-N.yml`.

**This is count-balanced, NOT DVR-aware** — see §8/§9, this is the main scaling limitation.

---

## 5. End-to-End Workflow & Data Flow

### 5.1 Scheduling (who decides what runs)

```
 Operator configures exams/shifts ─► shift_script_assignments (PENDING rows, per shift×script)
                                          │
 Node scheduler (cctv_backend/services/scheduler.js)
   • expands due assignments → script_executions rows (PENDING)   ← created here
   • exposes "due assignments" to workers via the API
```

### 5.2 Per-worker run lifecycle

```
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ POLLER (each worker, every 30s)                                            │
  │   GET backend /assignments?window=now → assignment 8852 (shift 122, RTSP)  │
  └───────────────┬──────────────────────────────────────────────────────────┘
                  ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ FILTER: keep cameras where crc32(id)%28 == my WORKER_INDEX                  │
  └───────────────┬──────────────────────────────────────────────────────────┘
                  ▼
  ┌──────────── PASS-1  (capture — I/O bound, GPU idle) ───────────────────────┐
  │ for each of my cameras:                                                    │
  │   token = GET token-coord :8090 ?server=dXX.camview.io   (shared session)   │
  │   StartVideo(token) → RTSP URL                                              │
  │   FFmpeg grab 1 frame   ── limited by semaphore (≈6 concurrent / worker) ──│
  │   write frame → /mnt/nfs/cctv_images/.../assignment_8852/<cam>.jpg (NFS)    │
  │   on 401 → StopVideo, refresh token, retry                                  │
  └───────────────┬──────────────────────────────────────────────────────────┘
                  ▼
  ┌──────────── PASS-2  (classify — GPU bound, fast) ──────────────────────────┐
  │ vLLM Qwen2.5-VL-7B, ~32 concurrent → label each captured frame             │
  │   (Entry / Computer Lab / Outside Area / Server Room / …)                   │
  └───────────────┬──────────────────────────────────────────────────────────┘
                  ▼
  ┌──────────── PERSIST ───────────────────────────────────────────────────────┐
  │ bulk UPDATE cameras + INSERT history → MySQL (shared)                       │
  │ frames already on NFS → served by main host to the dashboard                │
  │ vLLM shut down to free VRAM                                                 │
  └────────────────────────────────────────────────────────────────────────────┘
```

### 5.3 Data flow summary (where each kind of data lives)

```
  Camera list / config ............ MySQL on main  (all workers read)
  DVR auth tokens ................. token coordinator :8090 (1 session/DVR, shared)
  Captured frames ................. NFS /mnt/nfs/cctv_images (written by workers, read by main/dashboard)
  Classification results / history. MySQL on main  (workers write)
  Run scheduling .................. shift_script_assignments + script_executions (MySQL)
  GPU/health metrics .............. each worker :8000/metrics → Prometheus on main
```

### 5.4 Detection-suite scripts (beyond vision)

The same poller/worker model runs ~10-16 detectors via `master_pipeline.py`, each ending in `MASTER PIPELINE COMPLETED`:
Server Room, Crowd (Entry/Exit), Intruder, Entry/Exit, Proximity, Phone, Invigilator, Camera Inversion, Tampering, Blind Spot, Zero Person, Crowd<5, Strong Room, QP Box, Biometric Crowd, plus **vision** Scene Classification. Default frequency 5 min (vision 60 min).

---

## 6. The DVR / Token Coordination Problem (critical design constraint)

Camview DVRs (`dNN.camview.io`) allow **only ONE active session per username**. With N workers each authenticating as `system`, every new login **invalidates** the previous worker's token → constant 401s → near-zero capture. Solution:

```
  worker-1 ┐
  worker-2 ┤   GET :8090 /token?server=d15      ┌──────────────────────────┐
  worker-3 ┼──────────────────────────────────► │ TOKEN COORDINATOR (worker-0)│
  worker-4 ┤   (LAN direct, remote via tunnel)  │  • ONE auth session / DVR   │
  …        ┤                                     │  • keepalive threads        │
  worker-6 ┘                                     │  • negative-cache on failure│
                                                 └──────────────────────────┘
                                                          │ one login per DVR
                                                          ▼
                                              d11 d14 d15 d16 … .camview.io
```

This is a **single point of truth and a single point of failure**, and it currently only knows a handful of DVRs. At 50k-camera scale it must scale to many DVRs and ideally lease *stream* slots (see §9).

**DVR health is external and variable.** As of 2026-05-31: d14/d15 healthy; **d11/d16 down (HTTP 503 backend outage)**; d1 auth-broken (wrong creds); d2 DNS removed. DVR health, not GPU count, dominates the accessible-camera rate.

---

## 7. Scaling Story: 1 → 7 VMs / 1 → 28 GPUs

### 7.1 How the system grew

```
  Phase 1 (single node):     worker-0 only. 1 GPU. Runs everything (DB, API, scripts).
                             DISTRIBUTION_MODE=single → processes ALL cameras itself.

  Phase 2 (LAN scale-out):   + worker-1/2/3 (1 GPU each, Docker on private LAN).
                             TOTAL_WORKERS=4, hash split 0-3. NFS + shared MySQL introduced.
                             Token coordinator added (DVR single-session problem appears at N>1).

  Phase 3 (multi-GPU boxes): + worker-4/5/6 (8 GPUs each, remote, SSH-tunneled).
                             Key change: one container PER GPU, identity via WORKER_INDEX env
                             (camera_distributor honors WORKER_INDEX because worker-4-gpu5
                              has no unique trailing int).  TOTAL_WORKERS → 28.
                             Prometheus extended with per-GPU tunnel targets.
```

### 7.2 What had to change at each scaling step (and why)

| Concern | 1 node | 4 nodes (LAN) | 7 nodes / 28 GPUs |
|---|---|---|---|
| **Work split** | none (single) | `crc32%4` by `worker-N` regex | `crc32%28`, explicit `WORKER_INDEX` per GPU container |
| **Camera list** | local | shared MySQL | shared MySQL via tunnel for remote VMs |
| **DVR auth** | direct login | **token coordinator** (1 session/DVR) | same coordinator, now serving 28 clients across tunnels |
| **Frame storage** | local disk | **NFS** (`/mnt/nfs/cctv_images`) | NFS, remote VMs write over tunnel-routed mount/path |
| **Identity** | implicit | `WORKER_ID=worker-N` → index N | `WORKER_INDEX` env (names aren't unique-int) |
| **Connectivity** | localhost | private LAN | **SSH reverse/forward tunnels** (no LAN route) |
| **GPU pinning** | n/a | n/a | `device_ids:[N]` + `CUDA_VISIBLE_DEVICES=0` per container |
| **Monitoring** | local | Prometheus scrape on LAN | Prometheus via tunnel-forwarded per-GPU ports |

### 7.3 Theoretical capacity at 28 GPUs

- 50,000 cameras / 28 workers ≈ **1,786 cameras per GPU per cycle**.
- vLLM Pass-2: ~130 frames in ~25 s on one H200 → a single GPU can classify **thousands of frames/min**. Inference is *not* the limit.
- Pass-1 capture at ~6 concurrent streams and ~3 s/frame → 1,786 × 3 / 6 ≈ **15 min/cycle/worker** — **capture-bound**, and ultimately bounded by **per-DVR concurrent-stream caps**, not by GPUs.

**Conclusion: 28 GPUs is over-provisioned for inference. The binding constraint is DVR I/O.**

---

## 8. Current Bottlenecks & Risks

1. **Distribution is count-balanced, not DVR-aware.** 28 workers each open up to ~6 streams; many can hit the *same* DVR at once and exceed its session/stream cap → 401/503 storms. This is the #1 scaling blocker for 50k cameras.
2. **Token coordinator is a SPOF** and only knows a few DVRs; no stream-slot leasing.
3. **No per-DVR circuit breaker** — a down DVR (d11/d16) is retried per-camera, wasting ~50 min of Pass-1.
4. **Execution status tracking is broken** — `script_executions` rows are created PENDING and never updated (no `ScriptExecution.update()` anywhere). Dashboard cannot show real success/failure.
5. **Metrics flaky on 8-GPU boxes** — nvidia-smi contention; Prometheus sees a partial view of the 24 new GPUs.
6. **Worker registry hardcodes worker-1/2/3** (`app/main.py`) — FleetView health layer doesn't list worker-4/5/6.
7. **DVR outages are external** — only 2 of 6 DVRs healthy; caps real coverage regardless of compute.

---

## 9. What Needs to be Done (prioritized)

**P0 — make all 28 GPUs do correct, non-overlapping work** ✅ *(done 2026-05-31)*
Consistent `TOTAL_WORKERS=28`, unique `WORKER_INDEX 0-27`, all containers healthy.

**P1 — DVR-aware sharding + global per-DVR stream budget.**
Shard primarily by **DVR host** (not just camera-id hash) and enforce a **fleet-wide** concurrent-stream budget per DVR (the token coordinator is the natural place to lease stream slots). Today `_RTSP_CAP_SEM` is *per worker* (6) → 28×6 can all land on one DVR.

**P2 — Per-DVR circuit breaker.** Detect a DVR returning 503/timeout once, mark it down, skip its cameras for the cycle. Cuts wasted Pass-1 from ~50 min to seconds when a DVR is out.

**P3 — Fix execution status write-back.** On assignment status update, reconcile the matching `script_executions` rows (started/completed/failed). Backend change + restart; needs testing to avoid mis-marking.

**P4 — Token coordinator scaling/HA.** Connection pooling for 28 clients, full DVR inventory + per-DVR creds + per-DVR stream caps, and a restart/failover story.

**P5 — Monitoring completeness.** Raise `GPU_STATS_CACHE_TTL` + Prometheus `scrape_timeout`; make `app/main.py` worker registry data-driven to include all 28 GPUs in FleetView.

**External — DVR recovery.** Escalate d11/d16 (503 backend outage), d1 (creds), d2 (DNS) to DVR operators. No code fix helps.

---

## 10. Optimization Suggestions

- **Decouple capture from GPU.** Since capture (DVR I/O) is the limiter and GPUs idle during Pass-1, run more capture concurrency per box (bounded only by DVR caps) and/or dedicate some processes purely to capture, feeding a shared frame queue that GPU workers drain. This raises utilization without more GPUs.
- **Right-size GPU usage.** One H200 can serve several workers' inference. Consider fewer vLLM instances with higher batch, freeing VRAM/processes — or co-locate detection + vision on a GPU since neither saturates it.
- **Run during live DVR windows.** Camview streams are only live during the exam window; scheduling capture inside that window is the single biggest lever on accessible-camera count.
- **Stream-slot leasing via the coordinator.** Turn the token server into a *capacity broker*: a worker requests "a stream slot on d15"; the broker grants up to the DVR's cap fleet-wide. This makes adding workers safe (no DVR overload).
- **Adaptive backoff + circuit breaker per DVR** (see P2) to stop hammering locked/down DVRs (DVR lockout is 300 s; backoff must exceed it).
- **NFS write pressure.** 28 workers writing frames to one NFS server can bottleneck; consider sharded/striped storage or local-write + async upload.
- **Config-driven fleet.** Replace hardcoded worker IPs and per-VM compose copies with a generated inventory (one source of truth for index map + TOTAL_WORKERS) to prevent the split-brain class of bug.
- **Health-gated pickup.** Have workers self-report readiness (vLLM up, GPU free, DVR reachable) before claiming a shift, so a half-initialized box doesn't silently take a shard it can't process.

---

## Appendix A — Reconfiguration cheat-sheet

- **8-GPU VM** (change `TOTAL_WORKERS` / `WORKER_INDEX`): edit `docker-compose.worker-N.yml` (one `TOTAL_WORKERS:` + per-service `WORKER_INDEX:`), then `sudo docker compose -f docker-compose.worker-N.yml up -d` (explicit `container_name` → recreates in place).
- **LAN worker**: edit `<dir>/.env` (`TOTAL_WORKERS=`), `docker compose -f docker-compose.worker.yml up -d`.
- **worker-0 (main)**: edit `/data/cctv_dev/cctv/script-server/.env` (loaded via `load_dotenv()`), `pm2 restart cctv-backend-fastapi --update-env`.

## Appendix B — Index map (keep unique 0..N-1, same TOTAL on all)

```
worker-0=0  worker-1=1  worker-2=2  worker-3=3
worker-4 gpu0-7 = 4..11   worker-5 gpu0-7 = 12..19   worker-6 gpu0-7 = 20..27
TOTAL_WORKERS = 28 everywhere
```
