#!/bin/bash
# H200 NVL (144 GB VRAM) — Qwen3-VL-30B-A3B-Instruct (MoE, ~3B active params)
# Option C: latest-generation Qwen3 VL model with 8192 context and ~55% VRAM cap.
export HF_HOME=/app/data/hf_cache
export VLLM_USE_V1=0
# Disable flashinfer JIT sampler — nvcc not in runtime image, causes startup crash
export VLLM_USE_FLASHINFER_SAMPLER=0
# Give EngineCore extra time during vision-encoder warmup
export VLLM_RPC_TIMEOUT=120

mkdir -p /app/data/hf_cache
mkdir -p /app/scripts/logs

# Kill orphaned vLLM GPU subprocesses from previous crashed/killed instances.
# Only true orphans (parent died, reparented to PID 1) — a running detection
# pipeline's own spawn workers (unified detector, producers) must survive.
ps -eo pid=,ppid=,args= | awk '$2 == 1 && /spawn_main/ && /--multiprocessing-fork/ {print $1}' | xargs -r kill -9 2>/dev/null || true
sleep 1

echo "Starting vLLM server on port 8001..."
echo "Model: Qwen/Qwen3-VL-30B-A3B-Instruct"
echo "This will download ~62GB of weights the first time it runs."

# Apply vLLM deep_gemm patch
python3 /app/patch_vllm_deep_gemm.py 2>/dev/null || true

exec vllm serve Qwen/Qwen3-VL-30B-A3B-Instruct \
    --port 8001 \
    --host 0.0.0.0 \
    --max-model-len 8192 \
    --gpu-memory-utilization 0.55 \
    --max-num-seqs 12 \
    --dtype bfloat16
