"""
GPU Monitor Module
==================
GPU monitoring functionality integrated into main FastAPI server.
No separate port needed - all endpoints available on main HTTP_PORT.

Returns:
- GPU utilization (%)
- Memory usage
- Running processes (detection scripts)
- Camera counts per process
"""

import os
import re
import subprocess
import psutil
import threading
import time
from pathlib import Path
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

# ── nvidia-smi result cache ──────────────────────────────────────────────
# get_local_gpu_stats_data() fires 4 nvidia-smi subprocesses, each taking
# 100-800ms. Without caching, every /gpu-monitor/stats poll (every 10s
# per browser tab) and every /metrics scrape blocks for seconds. Worse,
# the async /gpu-monitor/stats endpoint used to call this synchronously,
# freezing the event loop for the duration and stalling every other request.
#
# Cache so concurrent callers share one nvidia-smi pass. Under fleet-wide
# scraping (Prometheus + dashboard polling across many workers, and 8
# containers on the multi-GPU box), simultaneous nvidia-smi calls serialise
# on the NVIDIA driver mutex and each collection slows to several seconds.
# A longer TTL keeps collections infrequent. Tunable via env (no rebuild).
_GPU_CACHE_TTL_SECONDS = float(os.getenv("GPU_STATS_CACHE_TTL", "10"))
_gpu_cache: dict = {"data": None, "ts": 0.0}
_gpu_cache_lock = threading.Lock()


def get_gpu_stats():
    """Get GPU utilization and memory using nvidia-smi"""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,"
                "temperature.gpu,driver_version,pcie.link.gen.current",
                "--format=csv,noheader,nounits"
            ],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode != 0:
            return []

        gpus = []
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue

            parts = [p.strip() for p in line.split(',')]
            if len(parts) >= 6:
                gpu = {
                    "gpu_id": int(parts[0]),
                    "name": parts[1],
                    "utilization": int(parts[2]),
                    "memory_used": int(parts[3]),
                    "memory_total": int(parts[4]),
                    "temperature": int(parts[5]),
                }
                if len(parts) >= 7:
                    gpu["driver_version"] = parts[6]
                if len(parts) >= 8:
                    try:
                        gpu["pcie_gen"] = int(parts[7])
                    except ValueError:
                        gpu["pcie_gen"] = None
                gpus.append(gpu)

        return gpus

    except Exception as e:
        logger.error(f"Error getting GPU stats: {e}")
        return []


def get_cuda_version() -> str:
    """Return CUDA toolkit version (e.g. '12.4') by parsing the nvidia-smi header."""
    try:
        result = subprocess.run(
            ["nvidia-smi"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            m = re.search(r"CUDA Version:\s*([\d.]+)", result.stdout)
            if m:
                return m.group(1)
    except Exception as exc:
        logger.debug(f"CUDA version probe failed: {exc}")
    return ""


def get_gpu_processes():
    """Get processes running on each GPU using nvidia-smi"""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_bus_id,pid,process_name,used_memory",
                "--format=csv,noheader"
            ],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode != 0:
            return {}
        
        # Map GPU bus ID to GPU index
        bus_to_index = {}
        bus_result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,gpu_bus_id", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        for line in bus_result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(',')]
            if len(parts) >= 2:
                bus_to_index[parts[1]] = int(parts[0])
        
        # Parse processes
        gpu_processes = {}
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            
            parts = [p.strip() for p in line.split(',')]
            if len(parts) >= 4:
                bus_id = parts[0]
                gpu_id = bus_to_index.get(bus_id, 0)
                pid = int(parts[1])
                process_name = parts[2]
                memory_mb = int(parts[3].split()[0]) if parts[3] else 0
                
                if gpu_id not in gpu_processes:
                    gpu_processes[gpu_id] = []
                
                # Get process details
                try:
                    proc = psutil.Process(pid)
                    cmdline = ' '.join(proc.cmdline())

                    # Extra fields for the UI: who launched it, how long it's been running
                    try:
                        username = proc.username()
                    except Exception:
                        username = ""
                    try:
                        runtime_seconds = int(datetime.now().timestamp() - proc.create_time())
                    except Exception:
                        runtime_seconds = 0
                    try:
                        status = proc.status()  # 'running', 'sleeping', etc.
                    except Exception:
                        status = "unknown"

                    # Extract script name and type
                    script_type = "unknown"
                    script_name = process_name
                    
                    if "master_pipeline" in cmdline:
                        script_type = "master"
                        script_name = "Master Pipeline"
                    elif "worker" in cmdline:
                        # Extract worker type
                        if "server_room_worker" in cmdline:
                            script_type = "server_room"
                            script_name = "Server Room Worker"
                        elif "crowd_worker" in cmdline:
                            script_type = "crowd"
                            script_name = "Crowd Detection Worker"
                        elif "proximity_worker" in cmdline:
                            script_type = "proximity"
                            script_name = "Proximity Worker"
                        else:
                            script_type = "worker"
                            script_name = "Detection Worker"
                    elif "vision_classification" in cmdline:
                        script_type = "classification"
                        script_name = "Vision Classification"
                    
                    gpu_processes[gpu_id].append({
                        "pid": pid,
                        "name": script_name,
                        "type": script_type,
                        "memory_mb": memory_mb,
                        "cmdline": cmdline[:200],  # Truncate long commands
                        "username": username,
                        "runtime_seconds": runtime_seconds,
                        "status": status,
                    })

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    gpu_processes[gpu_id].append({
                        "pid": pid,
                        "name": process_name,
                        "type": "unknown",
                        "memory_mb": memory_mb,
                        "cmdline": "",
                        "username": "",
                        "runtime_seconds": 0,
                        "status": "unknown",
                    })
        
        return gpu_processes
    
    except Exception as e:
        logger.error(f"Error getting GPU processes: {e}")
        return {}


def get_camera_counts():
    """
    Get camera counts for running detection processes.
    Reads from a shared state file created by detection scripts.
    """
    camera_counts = {}
    
    # Path to shared state file (created by detection scripts)
    state_file = Path("/tmp/detection_camera_counts.json")
    
    try:
        if state_file.exists():
            import json
            with open(state_file, 'r') as f:
                camera_counts = json.load(f)
            return camera_counts
    except Exception as e:
        logger.debug(f"Error reading camera counts from state file: {e}")
    
    # Fallback: Try to get from process info
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'environ']):
            try:
                cmdline = ' '.join(proc.info['cmdline'] or [])
                
                # Check if it's a detection process
                if any(keyword in cmdline for keyword in ['master_pipeline', 'worker', 'detection']):
                    pid = proc.info['pid']
                    
                    # Try to get camera count from environment variable
                    env = proc.info.get('environ', {})
                    if env and 'CAMERA_COUNT' in env:
                        camera_counts[pid] = int(env['CAMERA_COUNT'])
            
            except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
                continue
    except Exception as e:
        logger.debug(f"Error getting camera counts from processes: {e}")
    
    return camera_counts


def _compute_local_gpu_stats_data():
    """Actual nvidia-smi/psutil work — runs 4 subprocess calls."""
    from app.config import WORKER_ID, WORKER_NAME

    gpus = get_gpu_stats()
    gpu_processes = get_gpu_processes()
    camera_counts = get_camera_counts()

    for gpu_id, processes in gpu_processes.items():
        for proc in processes:
            proc["camera_count"] = camera_counts.get(proc["pid"], 0)

    return {
        "success": True,
        "server_id": WORKER_ID,
        "server_name": WORKER_NAME,
        "timestamp": datetime.now().isoformat(),
        "cuda_version": get_cuda_version(),
        "gpus": gpus,
        "processes": gpu_processes,
        "total_gpus": len(gpus),
        "total_processes": sum(len(procs) for procs in gpu_processes.values()),
    }


_gpu_refresher_started = False
_gpu_refresher_lock = threading.Lock()


def _gpu_refresh_loop():
    """Background daemon: refresh the GPU stats cache on a fixed interval.

    nvidia-smi runs here ONCE per interval regardless of how many HTTP
    callers (Prometheus scrapes, dashboard polls, peer workers) ask for
    stats. Request handlers never run nvidia-smi or wait on a lock, so they
    return instantly and the threadpool/event loop never stalls under burst
    load — the failure mode that previously hung /gpu-stats and /metrics for
    30s and exhausted the worker threadpool.
    """
    while True:
        try:
            data = _compute_local_gpu_stats_data()
            _gpu_cache["data"] = data
            _gpu_cache["ts"] = time.monotonic()
        except Exception as exc:
            logger.error(f"GPU stats background refresh failed: {exc}")
        time.sleep(_GPU_CACHE_TTL_SECONDS)


def _ensure_refresher_started():
    global _gpu_refresher_started
    if _gpu_refresher_started:
        return
    with _gpu_refresher_lock:
        if _gpu_refresher_started:
            return
        threading.Thread(
            target=_gpu_refresh_loop, daemon=True, name="gpu-stats-refresher"
        ).start()
        _gpu_refresher_started = True


def get_local_gpu_stats_data():
    """
    Return the most recently collected GPU stats. Non-blocking.

    A background daemon (started on first call) refreshes the cache every
    GPU_STATS_CACHE_TTL seconds. Callers always get the cached snapshot
    instantly — they never spawn nvidia-smi or block on a lock, so high
    concurrency (12 workers worth of Prometheus + dashboard traffic) can't
    stall the event loop or exhaust the threadpool.
    """
    _ensure_refresher_started()

    if _gpu_cache["data"] is None:
        # Cold start: the background thread hasn't produced data yet. Do one
        # synchronous collection so the very first request has something.
        with _gpu_cache_lock:
            if _gpu_cache["data"] is None:
                try:
                    _gpu_cache["data"] = _compute_local_gpu_stats_data()
                    _gpu_cache["ts"] = time.monotonic()
                except Exception as exc:
                    logger.error(f"Initial GPU stats collection failed: {exc}")
                    from app.config import WORKER_ID, WORKER_NAME
                    return {
                        "server_id": WORKER_ID,
                        "server_name": WORKER_NAME,
                        "gpus": [],
                        "processes": {},
                        "total_gpus": 0,
                        "total_processes": 0,
                    }

    return _gpu_cache["data"]
