# Scaling Path B — DeepStream + TensorRT (~3,000–4,000 cameras / GPU)

> **Target:** 3,000–4,000 cameras per H200 → the 80,000-cameras / 20-GPU goal.
> **The model is NOT retrained.** `yolov10m.pt` is **converted** (ONNX → TensorRT engine).
> Same weights, faster runtime format. This is the **full / production** path.
> Interim path: `SCALING_PATH_A_GSTREAMER_NVDEC_800.md`. Both share the unified detector.

---

## 0. TL;DR

To reach 3–4k/GPU you stack three multipliers on top of the unified detector:

1. **Unified detector** (prerequisite — same as Path A): one model pass per frame, not ~10.
2. **NVDEC decode + batching in VRAM** via DeepStream (`nvurisrcbin` → `nvstreammux`):
   frames never leave the GPU, hundreds batched into one tensor.
3. **TensorRT-optimized inference** (`nvinfer`): `yolov10m` compiled to an FP16/INT8 engine,
   ~3–5× faster than ultralytics PyTorch on the same GPU.

DeepStream is the industry-standard productized toolkit that implements exactly this.

---

## 1. What changes vs Path A

| | Path A (GStreamer) | Path B (DeepStream) |
|---|---|---|
| Decode | NVDEC via GStreamer | NVDEC via `nvurisrcbin` (built in) |
| Batching | manual, frames to RAM | `nvstreammux`, frames stay in VRAM |
| Inference | ultralytics `yolov10m.pt`, **unchanged** | `nvinfer` running a **TensorRT engine** (converted) |
| Process model | bounded pool + round-robin | **one process per GPU**, all streams in the graph |
| Results | Python rule-evaluators on boxes | **probe callback** reads metadata, then same rule-evaluators |
| Capacity | ~800 (+round-robin) | **~3,000–4,000** |
| Model work | none | **TensorRT conversion (no training)** |

---

## 2. The unified detector (prerequisite — see Path A §2)

Same change as Path A: run one detector, dispatch boxes to per-camera rule-evaluators.
**Without it, DeepStream would need 10 inference engines per camera (parallel-branch routing)
— the hardest part of a DeepStream build.** With it, you run **one** `nvinfer` (one TensorRT
engine) + one `nvtracker`, and the probe callback dispatches boxes to all enabled rules.
**The unified detector is what makes DeepStream straightforward instead of a routing nightmare.**

Do Path A's Phase 0/1 first. It is not throwaway — Path B sits directly on it.

---

## 3. TensorRT conversion (NOT training)

TensorRT is an inference **optimizer + runtime**. It compiles a trained model into a
hardware-specific engine (layer fusion, precision reduction, kernel autotuning).

```
yolov10m.pt  ──export──►  yolov10m.onnx  ──trtexec/builder──►  yolov10m_h200_fp16.engine
 (trained weights, unchanged)                    (one-time, ~minutes, per GPU arch)
```

- **Weights do not change.** FP16 = lower precision for speed (negligible accuracy loss).
- **INT8** is optional and gives more speed; it needs a small **calibration set** (a few hundred
  representative frames) — this is calibration, **still not training**.
- The engine is **GPU-arch-specific** (build on/for H200/Hopper). Rebuild if GPU model changes.
- Validate after conversion: run the engine and the original `.pt` on the same test frames,
  confirm detections/counts match within tolerance.

### Alternative: Triton (`nvinferserver`)
Instead of a raw TensorRT engine in `nvinfer`, DeepStream can call **Triton Inference Server**
(`nvinferserver`), which can serve ONNX/PyTorch/TensorRT. Use Triton if you want to serve the
model without converting to a raw engine, or share one model server across pipelines. Adds Triton
as infra. **For a single YOLO, direct TensorRT `nvinfer` is simpler; Triton is for multi-model /
multi-pipeline serving.**

---

## 4. Target architecture (per GPU — one process)

```
        RTSP/DSS URLs (authed via existing get_rtsp_from_dss)
                         │
   nvurisrcbin × N  ─────┤   (RTSP in + NVDEC decode + auto-reconnect, per camera)
                         ▼
                   nvstreammux            (batch frames from many cams in VRAM)
                         ▼
                   nvinfer (PGIE)         (yolov10m TensorRT engine — ONE pass, batched)
                         ▼
                   nvtracker             (shared tracker for invigilator / entry-exit)
                         ▼
                 probe callback           ← your dispatch + rule-evaluators live here
                  │
                  ├─ map source_id → (camera_id, ch_no, exam_id)
                  ├─ persons/phones/bags from batched metadata
                  ├─ per-camera enabled rules (crowd, proximity, intruder, server, ...)
                  ├─ image-quality (classical CV) — can stay off-GPU
                  └─ hit? → VLM (vLLM) gated on trigger
                         ▼
                 batched/async MySQL writes + image-on-hit (NFS)
```

### Mapping to current code
| Today | DeepStream element |
|---|---|
| `rtsp_producer` 1 `mp.Process`/cam (`master_pipeline.py:1120`) | `nvurisrcbin` source bin (all in one process) |
| `cv2.VideoCapture(CAP_FFMPEG)` CPU decode (`:799`) | `nvv4l2decoder` inside `nvurisrcbin` |
| `frame.copy()` + `mp.Queue` (`:983`,`:1067`) | `nvstreammux` (zero host copies) |
| `model(frames)` ultralytics (`Crowd_det_complete.py:122`) | `nvinfer` + TensorRT engine |
| per-script worker loop | **probe callback** dispatching to rule-evaluators |

### Reused unchanged
DSS auth/token (`get_server_token`, `get_rtsp_from_dss`), the rule-evaluators + thresholds +
debounce + DB report + image-save logic, `camera_distributor` sharding, Prometheus, frontend.

---

## 5. Multi-analytic handling (why unified detector matters here)

Your dominant cameras (inside-lab) match ~10 analytics, but **all 10 use `yolov10m`**. So:
- **One PGIE** (`nvinfer`, yolov10m engine) produces all boxes for all streams.
- The **probe callback** filters boxes (person/phone/bags) and applies each camera's enabled
  rules — exactly the Path A dispatch, but reading DeepStream metadata instead of a queue.
- No cascade of 10 engines, no `nvstreamdemux` parallel-branch routing, no 10× VRAM.

VLM analytics (`vision_classification`) are **never** an `nvinfer` stage — far too heavy.
They run **external, gated on a detector hit** (cheap YOLO triggers the VLM only on events).

---

## 6. Capacity reasoning (per H200)

| Stage | At ~4,000 cams (sampled ~12s) | Notes |
|---|---|---|
| Decode | ~4000/12 ≈ 333 fps NVDEC | well within H200's multiple NVDEC engines |
| Inference | ~333 fps batched, **1× TensorRT** YOLO | FP16/INT8 engine; batched 32–64; trivial |
| Network | 4000 × ~3 Mbps ≈ 12 Gbps/node | needs 25GbE; **real constraint** |
| MySQL | up to ~333 events/s/node if writing per snapshot | batch/async; write on hit, not per frame |
| NFS | images on hit only | never per frame |

- Continuous (no sampling) realistic: ~1,500–2,500/GPU.
- With ~12s round-robin sampling: ~4,000/GPU.
- **The real fleet limit is network + DVR fan-out, not GPU compute.**

---

## 7. The hard parts (where DeepStream time goes — none is training)

1. **pyds pipeline + probe callbacks** — Python bindings work but are less documented than C++;
   parsing batched metadata and mapping `source_id → camera_id` is the fiddliest code.
2. **TensorRT conversion + accuracy validation** — automated but must be verified per arch.
3. **DSS token integration** — feed authed RTSP URLs into `nvurisrcbin`; wire token refresh to
   its reconnect logic.
4. **Container/driver compat** — runs in `nvcr.io/nvidia/deepstream`; must match host driver/CUDA.
   Shared hosts (run other workloads) need care.
5. **Dynamic add/remove streams** — exam shifts start/stop; add/remove `nvurisrcbin` at runtime.
6. **Fault isolation** — one bad stream must not stall the shared pipeline (per-source EOS/watchdog).

---

## 8. Phased plan & time

| Phase | Work | Time |
|---|---|---|
| Pre | **Unified detector** (Path A Phase 0/1) — prerequisite | ~1–2 weeks |
| 0 | Spike: DeepStream container on one H200; `nvurisrcbin` vs a real DSS URL; decode 50 streams | ~3–5 days |
| 1 | TensorRT engine for `yolov10m`; validate accuracy vs `.pt` | ~2–3 days |
| 2 | Single-analytic pipeline (crowd): `nvurisrcbin→nvstreammux→nvinfer`; probe → existing crowd DB logic; ramp 100→500→1000 | ~2 weeks |
| 3 | Full dispatch (all rules in probe) + `nvtracker` + VLM gating + CV checks | ~2 weeks |
| 4 | DSS token refresh, dynamic add/remove streams, fault isolation, Prometheus, async MySQL/NFS | ~2 weeks |
| 5 | Integrate `camera_distributor` (DVR-aware sharding, per-DVR caps), fleet rollout across GPUs | ~1–2 weeks |
| **Total** | working single-analytic ~Phase 2; production-grade | **~7–9 weeks** (after unified detector) |

---

## 9. DVR-aware sharding (fleet level — 80k / 20 GPUs)

- `camera_distributor` shards by **DVR awareness**, not just count:
  - **DVR affinity** (share auth/token, reuse connections) vs **spread** (avoid overloading one DVR).
    Decision driven by DVR count — needs the actual DVR inventory.
  - **Per-DVR concurrency cap** — extends existing circuit-breaker idea.
- Reconcile fleet size: memory shows **28 GPUs / 7 hosts**; the 80k plan assumed 20. Use 28 canonical.

---

## 10. Risks / open decisions

- **4,000/GPU is aggressive** — validate by prototype (Phase 2), don't assume.
- **Network/DVR fan-out** is the true ceiling at fleet scale — size NICs and per-DVR caps.
- **Sampling vs continuous** — brief exam events (phone) missed in a ~12s gap → keep
  latency-sensitive analytics in a continuous priority lane.
- **INT8 accuracy** — validate against FP16/`.pt`; some analytics (small phone class) may
  prefer FP16.
- **Shared-host containers** — DeepStream driver/CUDA pinning vs other GPU workloads.

---

## 11. Industry-standard context

This path **is** the industry standard for thousands of cameras on GPUs:
**NVIDIA DeepStream + TensorRT (+ Triton for serving).** The underlying patterns —
hardware decode (NVDEC), decode-once → one detector → downstream rules, TensorRT/INT8 batched
inference, cascade/tiered models (cheap detector gates VLM), event-driven results bus
(Kafka/MQTT), motion-gated sampling — are what this architecture implements. Alternatives exist
for other hardware (Intel DL Streamer + OpenVINO; Frigate for small scale; AWS Panorama / Azure
for cloud), but for GPU-based fleet scale, DeepStream is the canonical answer.

---

*Status: design. The model is converted (FP16/INT8 TensorRT engine), never retrained.
Targets (3–4k/GPU) require Phase 2 prototype validation. Prerequisite: unified detector.*
