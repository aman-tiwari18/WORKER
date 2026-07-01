"""
GPU Memory Guard
================
Limits per-process GPU memory fraction to prevent any single worker from
consuming all VRAM on the H200. Called once at worker startup.

With 7 analytics workers + 1 vLLM process, each worker is capped at 12%
of total VRAM (143GB * 0.12 ≈ 17GB per worker). The YOLO model itself
uses ~3-4GB, leaving overhead for batch frame inference.

Set GPU_MEMORY_FRACTION env var to override (0.0–1.0).
"""
import os
import logging

logger = logging.getLogger(__name__)

# Default: 12% of total VRAM per worker (~17GB on H200 143GB)
# This safely allows 7 YOLO workers + vLLM without running out of VRAM
_DEFAULT_FRACTION = 0.12


def apply_gpu_memory_limit(fraction: float = None) -> None:
    """
    Apply a per-process GPU memory fraction limit.
    Must be called BEFORE any CUDA tensors are allocated.

    Args:
        fraction: Float 0.0-1.0. Defaults to GPU_MEMORY_FRACTION env var or 0.12.
    """
    try:
        import torch
        if not torch.cuda.is_available():
            return

        if fraction is None:
            fraction = float(os.environ.get("GPU_MEMORY_FRACTION", _DEFAULT_FRACTION))

        # Clamp to valid range
        fraction = max(0.05, min(1.0, fraction))

        torch.cuda.set_per_process_memory_fraction(fraction)
        total_vram_mb = torch.cuda.get_device_properties(0).total_memory / (1024 ** 2)
        allowed_mb = total_vram_mb * fraction
        logger.info(
            f"[GPUGuard] Per-process VRAM limit: {fraction:.0%} of "
            f"{total_vram_mb:.0f}MB = {allowed_mb:.0f}MB"
        )
    except Exception as e:
        # Don't crash the worker — just warn
        logger.warning(f"[GPUGuard] Could not set GPU memory fraction: {e}")
