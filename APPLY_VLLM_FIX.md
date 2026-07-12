# Quick Guide: Apply vLLM Fix

## TL;DR - What to Do Now

```bash
cd /cctv_dev/cctv/script_server/script-server
./rebuild.sh --build
```

Wait 10-15 minutes, then check:

```bash
docker exec cctv_script_server_worker_worker-3 cat /app/logs/vllm_startup.log
```

## What Was Fixed

1. **vLLM logging** - Now logs to `/app/logs/vllm_startup.log` (was `/dev/null`)
2. **Deep GEMM patch** - Handles missing deep_gemm module gracefully
3. **Memory reduced** - 35GB instead of 50GB (leaves room for detection models)
4. **Better error messages** - Shows last 20 lines if crash occurs

## Why Rebuild is Required

`start_vllm.sh` and `patch_vllm_deep_gemm.py` are **COPIED into the Docker image** during build.

Changes to these files require `./rebuild.sh --build` to take effect.

## Rebuild Modes

| Mode | Command | Time | When to Use |
|------|---------|------|-------------|
| **Fast** | `./rebuild.sh` | 5s | Code changes only |
| **Build** | `./rebuild.sh --build` | 10-15min | start_vllm.sh, requirements.txt |
| **Force** | `./rebuild.sh --force` | 20-30min | Dockerfile, clean slate |

## After Rebuild

### 1. Check vLLM Started

```bash
docker exec cctv_script_server_worker_worker-3 cat /app/logs/vllm_startup.log | tail -30
```

Look for:
```
✅ vLLM deep_gemm patch complete!
Starting vLLM server...
Uvicorn running on http://0.0.0.0:8001
```

### 2. Check GPU Memory

```bash
nvidia-smi
```

Expected: ~35GB for vLLM (was 50GB before)

### 3. Test Classification

```bash
docker logs -f cctv_script_server_worker_worker-3
```

Watch for:
```
INFO - Temporary vLLM server successfully started!
INFO - Worker worker-3: Assigned 1007/4029 cameras
```

## If vLLM Still Crashes

### Get the Error

```bash
docker exec cctv_script_server_worker_worker-3 cat /app/logs/vllm_startup.log
```

### Common Fixes

**Out of Memory?**
Edit `start_vllm.sh`, change:
```bash
--gpu-memory-utilization 0.25  # Try 0.20 or 0.15
```
Then: `./rebuild.sh --build`

**Patch Not Applied?**
```bash
docker exec cctv_script_server_worker_worker-3 python3 /app/patch_vllm_deep_gemm.py
```

**Model Download Failed?**
Check internet connection and HuggingFace access.

## Files Changed

- ✅ `scripts/detection/vision_classification_rtsp.py` - Logging fix
- ✅ `patch_vllm_deep_gemm.py` - NEW - Patch script
- ✅ `start_vllm.sh` - Apply patch, reduce memory
- ✅ `rebuild.sh` - Documentation
- ✅ `app/config.py` - LOGS_DIR (already fixed)

## Questions?

See `VLLM_FIX_COMPLETE.md` for detailed explanation.

---

**Ready to apply?** Run: `./rebuild.sh --build`
