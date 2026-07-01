# Pipeline Validation Report
**Date:** 15 June 2026 | **Prepared by:** Abhishek (Script-Server Team)

---

## Spreadsheet vs Actual Implementation

| # | Stage | Spreadsheet Claim | Actual (from code) | Verdict |
|---|-------|-------------------|---------------------|---------|
| 2 | Video Decoding | 1–5 ms, vCPUs | 1–5 ms via FFmpeg CPU decode ✅ | **Correct** |
| 3 | Preprocessing | ~2 hrs / 5,000 cameras, vCPUs | ~2 hrs for VLM classification pass; detection scripts have no separate preprocessing stage | **Mostly correct** — timing applies to classification only |
| 4 | Frame Scheduling | <10 ms, vCPUs | Hash routing is <1 ms ✅ but poller cycle is 30s | **Correct for routing**, polling adds latency |
| 5 | AI Inference | 10–30 ms, GPUs | YOLO detectors: 10–30 ms ✅; VLM (Qwen3-VL-30B): 2–10 sec | **Correct for YOLO**, VLM is 100x slower |
| 6 | Multi-model Analytics | 20–60 ms, GPUs | 15 YOLO workers run in parallel, 20–60 ms ✅ | **Correct** |
| 7 | Post-processing | <10 ms/event, vCPUs | Filtering/scoring <10 ms ✅; NFS image write adds 10–50 ms | **Slightly under** — NFS write not accounted |
| 8 | Event Generation | Real-time, vCPUs | File-scan watcher runs every 10s; Socket.IO emits after scan | **~10–15s latency**, not instant real-time |
| 9 | Storage / Logging | Near real-time, vCPUs + storage | NFS + MySQL writes are synchronous ✅ | **Correct** |
| 10 | Alerting | <1 sec, vCPUs | Depends on Stage 8 scan (10s) + Socket.IO emit | **~10–15s**, push to client is also manual |

> **CPU vs GPU split is correct across all stages.** The resource assignment (vCPUs for ingestion/scheduling/post-processing, GPUs for inference) matches the actual implementation.
