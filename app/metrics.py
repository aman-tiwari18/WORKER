"""
Prometheus metrics for GPU monitoring.

Exposes a /metrics scrape target. Reuses get_local_gpu_stats_data() so the
data path stays identical to the existing /gpu-stats JSON endpoint.

Design: a custom Collector that pulls fresh nvidia-smi data on every scrape.
No background task, no stale labels — Prometheus drives the cadence.
"""

from prometheus_client import CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from app.config import WORKER_ID, WORKER_NAME
from app.gpu_monitor import get_local_gpu_stats_data
from app.logger import logger


class GPUCollector(Collector):
    def collect(self):
        try:
            data = get_local_gpu_stats_data()
        except Exception as exc:
            logger.error(f"GPUCollector failed to fetch stats: {exc}")
            return

        server_id = data.get("server_id") or WORKER_ID
        server_name = data.get("server_name") or WORKER_NAME

        util = GaugeMetricFamily(
            "gpu_utilization_percent",
            "GPU utilization (0-100) from nvidia-smi",
            labels=["server_id", "server_name", "gpu_id", "gpu_name"],
        )
        mem_used = GaugeMetricFamily(
            "gpu_memory_used_mb",
            "GPU memory used in MiB",
            labels=["server_id", "gpu_id"],
        )
        mem_total = GaugeMetricFamily(
            "gpu_memory_total_mb",
            "GPU memory total in MiB",
            labels=["server_id", "gpu_id"],
        )
        temp = GaugeMetricFamily(
            "gpu_temperature_celsius",
            "GPU temperature in Celsius",
            labels=["server_id", "gpu_id"],
        )
        proc_mem = GaugeMetricFamily(
            "gpu_process_memory_mb",
            "Per-process GPU memory in MiB",
            labels=["server_id", "gpu_id", "pid", "script_type", "script_name"],
        )
        proc_cameras = GaugeMetricFamily(
            "gpu_process_camera_count",
            "Cameras handled by a detection process",
            labels=["server_id", "gpu_id", "pid", "script_type"],
        )
        gpu_total = GaugeMetricFamily(
            "gpu_count",
            "Number of GPUs visible on this server",
            labels=["server_id"],
        )
        proc_total = GaugeMetricFamily(
            "gpu_process_count",
            "Number of detection processes running on GPUs",
            labels=["server_id"],
        )

        for g in data.get("gpus", []):
            gpu_id = str(g.get("gpu_id"))
            util.add_metric(
                [server_id, server_name, gpu_id, g.get("name", "")],
                float(g.get("utilization", 0)),
            )
            mem_used.add_metric([server_id, gpu_id], float(g.get("memory_used", 0)))
            mem_total.add_metric([server_id, gpu_id], float(g.get("memory_total", 0)))
            temp.add_metric([server_id, gpu_id], float(g.get("temperature", 0)))

        for gpu_id, procs in (data.get("processes") or {}).items():
            for p in procs:
                labels = [
                    server_id,
                    str(gpu_id),
                    str(p.get("pid", "")),
                    p.get("type", "unknown"),
                    p.get("name", ""),
                ]
                proc_mem.add_metric(labels, float(p.get("memory_mb", 0)))
                proc_cameras.add_metric(
                    [server_id, str(gpu_id), str(p.get("pid", "")), p.get("type", "unknown")],
                    float(p.get("camera_count", 0)),
                )

        gpu_total.add_metric([server_id], float(data.get("total_gpus", 0)))
        proc_total.add_metric([server_id], float(data.get("total_processes", 0)))

        yield util
        yield mem_used
        yield mem_total
        yield temp
        yield proc_mem
        yield proc_cameras
        yield gpu_total
        yield proc_total


registry = CollectorRegistry()
registry.register(GPUCollector())


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(registry), CONTENT_TYPE_LATEST
