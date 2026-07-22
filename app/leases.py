"""
leases.py — worker half of lease-based work ownership (plan §4.2).

Under the old scheme a worker assumed ownership from env vars
(crc32(camera) % TOTAL_WORKERS == WORKER_INDEX). Nothing verified that every
slice had an owner, so a down or mis-indexed worker meant its cameras were
processed by nobody — silently.

Here the worker *claims* shards from the backend and renews them while it works.
If it dies, its leases expire and another worker picks them up.

Rollout is deliberately flag-gated (plan §4.2 migration path):

    COORDINATION_MODE=hash   (default) — unchanged behaviour, leases unused
    COORDINATION_MODE=lease            — ownership comes from claimed shards

Default stays `hash` until lease coverage has been validated row-for-row against
hash mode on a real exam. Nothing here changes how cameras are processed today.
"""
import asyncio
import os

import httpx

from app.config import MAIN_BACKEND_URL, API_KEY, WORKER_ID
from app.logger import logger

COORDINATION_MODE = os.getenv("COORDINATION_MODE", "hash").lower()
MAX_SHARDS = int(os.getenv("LEASE_MAX_SHARDS", "16"))
REQUEST_TIMEOUT_S = float(os.getenv("LEASE_REQUEST_TIMEOUT_S", "10"))


class LeaseClient:
    """Claims and renews shard leases; renewal runs per assignment."""

    def __init__(self):
        self.base_url = MAIN_BACKEND_URL.rstrip("/")
        self.headers = {"X-API-Key": API_KEY}
        # assignment_id -> {"lease_ids": [...], "shards": set(), "shard_count": int}
        self._held = {}
        self._renew_task = None
        self._stopping = False
        self._renew_interval_s = 30

    # ── ownership view used by camera_distributor ───────────────────────────
    def owned(self, assignment_id):
        return self._held.get(int(assignment_id))

    def is_enabled(self) -> bool:
        return COORDINATION_MODE == "lease"

    # ── claim / release ─────────────────────────────────────────────────────
    async def claim(self, assignment_id: int, exam_id=None, shift_id=None, script_code=None) -> dict | None:
        """
        Open the assignment's shards (idempotent) and claim a batch of them.
        Returns the held record, or None if nothing could be claimed.
        """
        if not self.is_enabled():
            return None

        async with httpx.AsyncClient() as client:
            await client.post(
                f"{self.base_url}/fleet/leases/open",
                json={
                    "assignment_id": assignment_id,
                    "exam_id": exam_id,
                    "shift_id": shift_id,
                    "script_code": script_code,
                },
                headers=self.headers,
                timeout=REQUEST_TIMEOUT_S,
            )

            resp = await client.post(
                f"{self.base_url}/fleet/leases/claim",
                json={"worker_id": WORKER_ID, "assignment_id": assignment_id, "max": MAX_SHARDS},
                headers=self.headers,
                timeout=REQUEST_TIMEOUT_S,
            )
            resp.raise_for_status()
            data = resp.json().get("data") or {}

        claimed = data.get("claimed") or []
        if not claimed:
            logger.info(f"leases: no claimable shards for assignment {assignment_id} (other workers own them)")
            return None

        held = {
            "lease_ids": [c["lease_id"] for c in claimed],
            "shards": {int(c["shard_key"]) for c in claimed},
            "shard_count": int(claimed[0]["shard_count"]),
        }
        self._held[int(assignment_id)] = held

        # Renew well inside the TTL so a slow round-trip can't cost us the lease.
        ttl = int(data.get("lease_ttl_s") or 90)
        self._renew_interval_s = max(5, ttl // 3)

        logger.info(
            f"leases: claimed {len(held['shards'])}/{held['shard_count']} shards "
            f"for assignment {assignment_id}"
        )
        await self.start_renewing()
        return held

    async def release(self, assignment_id: int, state: str = "DONE") -> None:
        held = self._held.pop(int(assignment_id), None)
        if not held or not self.is_enabled():
            return
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{self.base_url}/fleet/leases/release",
                    json={"worker_id": WORKER_ID, "lease_ids": held["lease_ids"], "state": state},
                    headers=self.headers,
                    timeout=REQUEST_TIMEOUT_S,
                )
            logger.info(f"leases: released {len(held['lease_ids'])} shards for assignment {assignment_id} ({state})")
        except Exception as exc:
            # Not fatal: the lease simply expires and someone else claims it.
            logger.warning(f"leases: release failed for {assignment_id}: {exc}")

    # ── renewal ─────────────────────────────────────────────────────────────
    async def _renew_loop(self) -> None:
        async with httpx.AsyncClient() as client:
            while not self._stopping:
                await asyncio.sleep(self._renew_interval_s)
                for assignment_id, held in list(self._held.items()):
                    try:
                        resp = await client.post(
                            f"{self.base_url}/fleet/leases/renew",
                            json={"worker_id": WORKER_ID, "lease_ids": held["lease_ids"]},
                            headers=self.headers,
                            timeout=REQUEST_TIMEOUT_S,
                        )
                        resp.raise_for_status()
                        data = resp.json().get("data") or {}
                        lost = data.get("lost") or []
                        if lost:
                            # Another worker took these — stop claiming their
                            # cameras rather than double-processing them.
                            lost_set = set(lost)
                            held["lease_ids"] = [i for i in held["lease_ids"] if i not in lost_set]
                            logger.warning(
                                f"leases: lost {len(lost)} shard(s) of assignment {assignment_id} "
                                f"to another worker; shrinking our slice"
                            )
                    except Exception as exc:
                        logger.warning(f"leases: renew failed for {assignment_id}: {exc}")

    async def start_renewing(self) -> None:
        if self._renew_task or not self.is_enabled():
            return
        self._stopping = False
        self._renew_task = asyncio.create_task(self._renew_loop())
        logger.info(f"leases: renewal loop started (every {self._renew_interval_s}s)")

    async def stop(self) -> None:
        self._stopping = True
        if self._renew_task:
            self._renew_task.cancel()
            try:
                await self._renew_task
            except (asyncio.CancelledError, Exception):
                pass
            self._renew_task = None
        for assignment_id in list(self._held):
            await self.release(assignment_id, state="OPEN")


lease_client = LeaseClient()
