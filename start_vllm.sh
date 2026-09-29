#!/bin/bash
# H200 NVL (144 GB VRAM) — Qwen3-VL-30B-A3B-Instruct (MoE, ~3B active params)
# - Larger model = better borderline-scene accuracy than 7B
# - MoE keeps latency close to a dense 7B because only 3B params fire per token
# - Bumped --max-model-len 2048 → 8192 so the prompt + 1080p Qwen-VL image
#   tokens fit comfortably; this was the root cause of the previous run's
#   ~1600 HTTP-400 "Input length exceeds 2048" rejections.
# - Bumped --gpu-memory-utilization 0.25 → 0.55 to give the bigger model and
#   the wider KV cache enough headroom. On a 144 GB H200 this is ~79 GB.
# HF_HOME MUST point at the bind-mounted cache (/app/data/hf_cache per
# docker-compose.uat.yml), NOT at a container-local path. When it pointed at
# /data/hf_cache — which is not a mount — the 62GB of weights landed on the
# container's writable overlay layer and were thrown away on every recreate,
# so each cold start re-downloaded the whole model (~25 min at 40 MB/s) and
# blew past the pipeline's run timeout. Compose can override.
export HF_HOME=${HF_HOME:-/app/data/hf_cache}
export VLLM_USE_V1=0
# Give the APIServer extra time to complete multi-modal warmup before
# EngineCore times out waiting for the initial IPC connection.
# Without this, the EngineCore self-shuts-down after ~18s while APIServer
# is still doing 20+ seconds of vision-encoder warmup. The 30B-A3B model
# needs longer to warm up than the 7B, so bumping the timeout further.
export VLLM_RPC_TIMEOUT=120
mkdir -p "$HF_HOME"
source /data/cctv_dev/cctv/script-server/venv/bin/activate

echo "Starting vLLM server on port 8001..."
echo "Model: Qwen/Qwen3-VL-30B-A3B-Instruct"
echo "HF_HOME=$HF_HOME (must be the mounted cache, else weights re-download every recreate)"
echo "First run downloads ~62GB; warm start loads from cache."

# Kill orphaned vLLM GPU subprocesses left by previous crashed/killed vLLM instances.
# These spawn_main processes hold CUDA contexts and block GPU initialization.
pkill -9 -f "spawn_main.*--multiprocessing-fork" 2>/dev/null || true
sleep 1

exec vllm serve Qwen/Qwen3-VL-30B-A3B-Instruct \
    --port 8001 \
    --host 0.0.0.0 \
    --max-model-len 8192 \
    --gpu-memory-utilization 0.55 \
    --max-num-seqs 12 \
    --dtype bfloat16 \
    --moe-backend triton \
    --enforce-eager \
    --safetensors-load-strategy=prefetch
# --safetensors-load-strategy=prefetch: read the 58 GB of weights in bulk. On a local
# EXT4 disk vLLM otherwise loads them on demand at ~130 MB/s (38 s per shard, ~8 min)
# while the disk reads sequentially at ~1.1 GB/s; the slow load blew the classifier's
# startup limit on worker-0 (29 Sep, 21 Jun).
# --moe-backend triton: forces the pre-compiled Triton MoE kernel instead of
# FlashInfer CUTLASS. The CUTLASS path JIT-compiles its kernel with nvcc, which
# is not installed in this environment (caused "Could not find nvcc" crash
# during determine_available_memory after weights had loaded). Triton kernels
# ship pre-compiled and are well-tested for Qwen3-VL-MoE on H200.
#
# --enforce-eager: disable CUDA graphs. The Qwen3-VL deepstack path crashed the
# EngineCore with "Requested more deepstack tokens than available in buffer:
# num_tokens=24 > 21" — a forward padded to a captured graph size (24) overflowed
# the profiled deepstack buffer (21), killing vLLM (EngineDeadError → all requests
# 500). Eager mode runs each forward at its real token count (no padding to a
# captured size), so the buffer never overflows. Costs some throughput, no
# accuracy change. (vLLM v0.20.1 Qwen3-VL bug.)
