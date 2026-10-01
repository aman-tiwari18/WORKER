"""
fleet_agent.py — worker half of the fleet registry (plan §4.1).

The worker is outbound-only: it POSTs /fleet/register once on boot, then
/fleet/heartbeat on a fixed interval carrying its GPU/process snapshot. The
backend is the single source of truth for "who is alive"; nothing here listens
on a port, which is what lets the identical image run on every host.

Two properties that matter operationally:
  - Registration is idempotent (keyed on WORKER_ID), so a restart re-registers
    rather than creating a duplicate row.
  - A 404 from /heartbeat means the backend no longer knows this worker (its DB
    was wiped, or the reaper removed it), so the agent re-registers on the spot.

The loop never raises into the caller: a backend outage must not take down
detection. It just logs, backs off, and keeps trying.
"""
import asyncio
import os
import socket
import time

import httpx

from app.config import MAIN_BACKEND_URL, API_KEY, WORKER_ID, WORKER_NAME, TOTAL_WORKERS
from app.logger import logger

HEARTBEAT_INTERVAL_S = int(os.getenv("FLEET_HEARTBEAT_INTERVAL_S", "10"))
def _truthy(name, default):
    """Accept the usual truthy spellings for a boolean env var (Compose uses "1",
    Kubernetes manifests more often use "true") instead of a strict == "1"."""
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")
FLEET_ENABLED = _truthy("FLEET_ENABLED", "1")
REQUEST_TIMEOUT_S = float(os.getenv("FLEET_REQUEST_TIMEOUT_S", "10"))
_MAX_BACKOFF_S = 120


def _worker_index() -> int:
    try:
        return int(os.getenv("WORKER_INDEX", "0"))
    except ValueError:
        return 0


def _capabilities() -> dict:
    """What this worker can do — config, never code (plan §4.0 bucket 3)."""
    return {
        "detector": _truthy("ENABLE_DETECTION", "1"),
        "vllm": _truthy("ENABLE_VLLM", "0"),
        "nvdec": _truthy("USE_NVDEC", "0"),
        "unified_detector": _truthy("UNIFIED_DETECTOR", "0"),
        # How this worker splits cameras (Fleet page): "lease" = claimed shards,
        # rebalanced automatically; "hash" = static WORKER_INDEX/TOTAL_WORKERS split.
        "coordination_mode": os.getenv("COORDINATION_MODE", "hash").strip().lower() or "hash",
        # Prometheus scrape target (Fleet /prometheus/sd): host LAN IP + port
        "metrics_port": int(os.getenv("METRICS_PORT", "0") or 0) or None,
        "vision": os.getenv("ENABLE_VISION", "1").strip().lower() in ("1", "true", "yes"),
        # "remote" = outside the LAN (Fleet bootstrap --remote): metrics are pushed by the
        # edge-agent and relayed by the backend instead of being scraped directly.
        "network": os.getenv("FLEET_NETWORK", "lan").strip().lower() or "lan",
        "cuda_visible_devices": os.getenv("CUDA_VISIBLE_DEVICES", ""),
        "max_concurrent": os.getenv("MAX_CONCURRENT", ""),
    }


def _gpu_snapshot() -> dict:
    """
    The worker's own nvidia-smi view — the same payload the old worker FastAPI
    served at /gpu-monitor/local. The backend stores it verbatim and the GPU page
    reassembles the cluster from it, so no fan-out or SSH is needed.

    Cached/refreshed in a background thread by gpu_monitor, so this is cheap.
    """
    try:
        from app.gpu_monitor import get_local_gpu_stats_data
        return get_local_gpu_stats_data()
    except Exception as exc:  # nvidia-smi missing, no GPU, etc.
        logger.debug(f"fleet: GPU snapshot unavailable: {exc}")
        return {}


class FleetAgent:
    def __init__(self):
        self.base_url = MAIN_BACKEND_URL.rstrip("/")
        self.headers = {"X-API-Key": API_KEY}
        self.worker_id = WORKER_ID
        self._task = None
        self._registered = False
        self._stopping = False
        self._started_at = time.time()
        # RETIRED on the Fleet page: take no work, hold no leases, until resumed.
        self.retired = False

    # ── payloads ────────────────────────────────────────────────────────────
    def _register_payload(self) -> dict:
        gpu = _gpu_snapshot()
        gpus = gpu.get("gpus") or []
        first = gpus[0] if gpus else {}
        return {
            "worker_id": self.worker_id,
            "name": WORKER_NAME,
            "hostname": os.getenv("WORKER_HOSTNAME") or socket.gethostname(),
            # LAN IP of the host (set by the Fleet bootstrap) — Prometheus scrapes it
            "ip_address": os.getenv("WORKER_HOST_IP") or None,
            "gpu_index": first.get("gpu_id"),
            "gpu_name": first.get("name"),
            "gpu_uuid": first.get("uuid"),
            "capabilities": _capabilities(),
            "version": os.getenv("WORKER_VERSION") or os.getenv("IMAGE_TAG") or "dev",
            "worker_index": _worker_index(),
            "total_workers": TOTAL_WORKERS,
            "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
        }

    def _heartbeat_payload(self) -> dict:
        from app.poller import poller_service
        running = getattr(poller_service, "running_scripts", set())
        return {
            "worker_id": self.worker_id,
            "metrics": {
                "gpu": _gpu_snapshot(),
                "running_scripts": len(running),
                "running_assignment_ids": sorted(str(x) for x in running),
                "uptime_s": int(time.time() - self._started_at),
            },
        }

    # ── HTTP ────────────────────────────────────────────────────────────────
    async def register(self, client: httpx.AsyncClient) -> bool:
        resp = await client.post(
            f"{self.base_url}/fleet/register",
            json=self._register_payload(),
            headers=self.headers,
            timeout=REQUEST_TIMEOUT_S,
        )
        resp.raise_for_status()
        self._registered = True
        await self._apply_status(((resp.json() or {}).get("data") or {}).get("status"))
        logger.info(f"fleet: registered {self.worker_id} with backend (heartbeat every {HEARTBEAT_INTERVAL_S}s)")
        return True

    async def _beat(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            f"{self.base_url}/fleet/heartbeat",
            json=self._heartbeat_payload(),
            headers=self.headers,
            timeout=REQUEST_TIMEOUT_S,
        )
        if resp.status_code == 404:
            # Backend forgot us — re-register rather than heartbeat into a void.
            logger.warning("fleet: backend does not know this worker; re-registering")
            self._registered = False
            await self.register(client)
            return
        resp.raise_for_status()
        await self._apply_status(((resp.json() or {}).get("data") or {}).get("status"))

    async def _apply_status(self, status) -> None:
        """React to an operator action on the Fleet page. RETIRED: stop everything
        this worker runs and hand its camera shards back, then idle (the container
        may keep restarting; it stays idle). Anything else: normal operation."""
        retired = str(status or "").upper() == "RETIRED"
        if retired == self.retired:
            return
        self.retired = retired
        if not retired:
            logger.info("fleet: resumed from the Fleet page — taking work again")
            return
        logger.warning("fleet: RETIRED from the Fleet page — stopping all work and releasing leases")
        try:
            from app.poller import poller_service
            from app.script_runner import script_runner
            for key in list(getattr(poller_service, "running_scripts", set())):
                try:
                    await script_runner.cancel(key)   # run_assignment's finally releases the lease
                except Exception as exc:
                    logger.warning(f"fleet: could not stop {key} on retire: {exc}")
            for task in list(getattr(poller_service, "scheduled_scripts", {}).values()):
                task.cancel()
            poller_service.scheduled_scripts.clear()
        except Exception as exc:
            logger.error(f"fleet: retire cleanup failed: {exc}")

    # ── loop ────────────────────────────────────────────────────────────────
    async def _loop(self) -> None:
        backoff = HEARTBEAT_INTERVAL_S
        async with httpx.AsyncClient() as client:
            while not self._stopping:
                try:
                    if not self._registered:
                        await self.register(client)
                    else:
                        await self._beat(client)
                    backoff = HEARTBEAT_INTERVAL_S
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # A backend outage must never stop detection — log and retry.
                    logger.warning(f"fleet: heartbeat/register failed ({exc}); retrying in {backoff}s")
                    backoff = min(backoff * 2, _MAX_BACKOFF_S)

                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise

    async def start(self) -> None:
        if not FLEET_ENABLED:
            logger.info("fleet: agent disabled (FLEET_ENABLED=0)")
            return
        if self._task:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop())
        logger.info(f"fleet: agent started for {self.worker_id} → {self.base_url}")

    async def stop(self) -> None:
        self._stopping = True
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        logger.info("fleet: agent stopped")


fleet_agent = FleetAgent()
