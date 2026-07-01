# cctv-worker (data plane / "hands")

ONE uniform script-server image for **every** GPU host (LAN single-GPU and the
big vLLM 8-GPU hosts alike). Per-host differences are **config only** (env).
Based on the `main` superset lineage (has `unified_detector.py`, the runaway-fixed
`script_runner.py`, `vision_classification_neet.py`, `start_vllm.sh`).

## Canonical design (see plan §9.1)
- **Detection:** shared YOLO pass via `scripts/detection/unified_detector.py` + `master_pipeline.py`.
- **Classification:** vLLM `Qwen/Qwen3-VL-30B-A3B-Instruct` on `:8001`, self-launched by
  `scripts/detection/vision_classification_neet.py` (`VISION_VLLM_URL`). **ollama is dropped.**
- **Capability-gated** by env: `ENABLE_VLLM`, `ENABLE_DETECTION`, `WORKER_INDEX`,
  `CUDA_VISIBLE_DEVICES`.

## Still TODO (later increments, not in scaffold)
- Merge w5/6 vLLM stability patches (`patch_vllm_deep_gemm.py`, Dockerfile vllm pin).
- Replace local poll+filter+timeout with the backend⇄worker contract (`app/agent.py`).
- One reconciled `Dockerfile` + `requirements.txt`.

> Copied read-only from the running tree. No worker host was modified.
