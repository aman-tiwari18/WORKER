"""
leases.py — worker half of lease-based work ownership (plan §4.2).

Under the old scheme a worker assumed ownership from env vars
(crc32(camera) % TOTAL_WORKERS == WORKER_INDEX). Nothing verified that every
slice had an owner, so a down or mis-indexed worker meant its cameras were
processed by nobody — silently, and adding a server meant editing every worker.

Here each assignment's cameras are cut into shards (64 by default) and the backend
*balances* them across the live workers, like a consumer group:

  * On start and every LEASE_BALANCE_INTERVAL_S the worker calls
    /fleet/leases/balance. The backend works out a fair share (shards / live lease
    workers) and this worker releases what it holds above it or claims unowned
    shards up to it. A drained or retired worker's share is 0.
  * If this worker's shard set changes, the poller relaunches that assignment with
    the new shards (the pipeline reads them once, at launch).
  * Held shards are renewed every TTL/3; a dead worker's shards expire (or are
    freed at once by the reaper) and the others claim them on their next balance.
  * A run that ends hands its shards back as OPEN — never DONE: detection
    assignments re-run every cycle, and DONE shards are never claimed again.

Flag-gated: COORDINATION_MODE=hash (default) leaves all of this unused. Every
worker of a fleet must use the same mode — a mixed fleet double-processes some
cameras and drops others.
"""
import asyncio
import os

import httpx

from app.config import MAIN_BACKEND_URL, API_KEY, WORKER_ID
from app.logger import logger

COORDINATION_MODE = os.getenv("COORDINATION_MODE", "hash").lower()
REQUEST_TIMEOUT_S = float(os.getenv("LEASE_REQUEST_TIMEOUT_S", "10"))
BALANCE_INTERVAL_S = float(os.getenv("LEASE_BALANCE_INTERVAL_S", "60"))


class LeaseClient:
    """Balances, renews and releases shard leases for the assignments this worker runs."""

    def __init__(self):
        self.base_url = MAIN_BACKEND_URL.rstrip("/")
        self.headers = {"X-API-Key": API_KEY}
        # backend assignment id -> {"lease_ids": [...], "shards": {int}, "shard_count": int}
        self._held = {}
        # backend assignment id -> assignment meta, for assignments we run or wait on
        self._watched = {}
        self._renew_task = None
        self._balance_task = None
        self._stopping = False
        self._renew_interval_s = 30
        # async callback(assignment_id) — set by the poller: relaunch with new shards
        self.on_change = None

    def owned(self, assignment_id):
        return self._held.get(int(assignment_id))

    def is_enabled(self) -> bool:
        return COORDINATION_MODE == "lease"

    # ── balance (claim / shed) ───────────────────────────────────────────────
    async def _balance(self, client, assignment_id: int, meta: dict) -> dict:
        """One balance round-trip. Returns the backend's answer (held, refused, ...)."""
        await client.post(
            f"{self.base_url}/fleet/leases/open",
            json={"assignment_id": assignment_id, "exam_id": meta.get("exam_id"),
                  "shift_id": meta.get("shift_id"), "script_code": meta.get("script_code")},
            headers=self.headers, timeout=REQUEST_TIMEOUT_S,
        )
        resp = await client.post(
            f"{self.base_url}/fleet/leases/balance",
            json={"worker_id": WORKER_ID, "assignment_id": assignment_id},
            headers=self.headers, timeout=REQUEST_TIMEOUT_S,
        )
        resp.raise_for_status()
        return resp.json().get("data") or {}

    def _store(self, assignment_id: int, data: dict) -> set:
        held = data.get("held") or []
        rec = {
            "lease_ids": [h["lease_id"] for h in held],
            "shards": {int(h["shard_key"]) for h in held},
            "shard_count": int(data.get("shard_count") or (held[0]["shard_count"] if held else 64)),
        }
        ttl = int(data.get("lease_ttl_s") or 90)
        self._renew_interval_s = max(5, ttl // 3)
        if rec["lease_ids"]:
            self._held[assignment_id] = rec
        else:
            self._held.pop(assignment_id, None)
        return rec["shards"]

    async def claim(self, assignment_id: int, exam_id=None, shift_id=None, script_code=None) -> dict | None:
        """
        Take this worker's fair share of the assignment's shards.

        Returns {"shards", "shard_count", "lease_ids"}; {"refused": status} when the
        worker is drained/retired; or {"shards": set()} when there is nothing to take
        yet (the caller must NOT run then — the balance loop relaunches once shards
        free up). None in hash mode.
        """
        if not self.is_enabled():
            return None
        assignment_id = int(assignment_id)
        meta = {"exam_id": exam_id, "shift_id": shift_id, "script_code": script_code}
        self._watched[assignment_id] = meta
        async with httpx.AsyncClient() as client:
            data = await self._balance(client, assignment_id, meta)

        if data.get("refused"):
            logger.info(f"leases: assignment {assignment_id} refused — worker is {data['refused']}")
            self._held.pop(assignment_id, None)
            return {"refused": data["refused"]}

        shards = self._store(assignment_id, data)
        held = self._held.get(assignment_id) or {"lease_ids": [], "shards": set(),
                                                 "shard_count": int(data.get("shard_count") or 64)}
        logger.info(
            f"leases: assignment {assignment_id}: hold {len(shards)}/{held['shard_count']} shards "
            f"(fair share {data.get('target')}, {data.get('workers')} lease worker(s))"
        )
        await self.start()
        return held

    async def release(self, assignment_id: int, state: str = "OPEN", keep_watch: bool = False) -> None:
        """Hand this assignment's shards back (OPEN = claimable again at once)."""
        assignment_id = int(assignment_id)
        held = self._held.pop(assignment_id, None)
        if not keep_watch:
            self._watched.pop(assignment_id, None)
        if not held or not self.is_enabled():
            return
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{self.base_url}/fleet/leases/release",
                    json={"worker_id": WORKER_ID, "lease_ids": held["lease_ids"], "state": state},
                    headers=self.headers, timeout=REQUEST_TIMEOUT_S,
                )
            logger.info(f"leases: released {len(held['lease_ids'])} shards of assignment {assignment_id} ({state})")
        except Exception as exc:
            # Not fatal: the lease expires and another worker claims it.
            logger.warning(f"leases: release failed for {assignment_id}: {exc}")

    # ── background loops ────────────────────────────────────────────────────
    async def _renew_loop(self) -> None:
        async with httpx.AsyncClient() as client:
            while not self._stopping:
                await asyncio.sleep(self._renew_interval_s)
                for assignment_id, held in list(self._held.items()):
                    try:
                        resp = await client.post(
                            f"{self.base_url}/fleet/leases/renew",
                            json={"worker_id": WORKER_ID, "lease_ids": held["lease_ids"]},
                            headers=self.headers, timeout=REQUEST_TIMEOUT_S,
                        )
                        resp.raise_for_status()
                        lost = set((resp.json().get("data") or {}).get("lost") or [])
                        if lost:
                            # Another worker holds them now; the next balance relaunches
                            # us with the shrunk set rather than double-processing.
                            held["lease_ids"] = [i for i in held["lease_ids"] if i not in lost]
                            logger.warning(f"leases: lost {len(lost)} shard(s) of assignment {assignment_id}")
                    except Exception as exc:
                        logger.warning(f"leases: renew failed for {assignment_id}: {exc}")

    async def _balance_loop(self) -> None:
        async with httpx.AsyncClient() as client:
            while not self._stopping:
                await asyncio.sleep(BALANCE_INTERVAL_S)
                for assignment_id, meta in list(self._watched.items()):
                    before = set((self._held.get(assignment_id) or {}).get("shards") or ())
                    try:
                        data = await self._balance(client, assignment_id, meta)
                    except Exception as exc:
                        logger.warning(f"leases: balance failed for {assignment_id}: {exc}")
                        continue
                    after = set() if data.get("refused") else self._store(assignment_id, data)
                    if data.get("refused"):
                        self._held.pop(assignment_id, None)
                    if after != before:
                        logger.info(
                            f"leases: assignment {assignment_id} rebalanced {len(before)} → {len(after)} "
                            f"shards (fair share {data.get('target')}, {data.get('workers')} worker(s))"
                        )
                        if self.on_change:
                            try:
                                await self.on_change(assignment_id)
                            except Exception as exc:
                                logger.error(f"leases: relaunch after rebalance failed for {assignment_id}: {exc}")

    async def start(self) -> None:
        if not self.is_enabled():
            return
        self._stopping = False
        if not self._renew_task:
            self._renew_task = asyncio.create_task(self._renew_loop())
        if not self._balance_task:
            self._balance_task = asyncio.create_task(self._balance_loop())
            logger.info(f"leases: balancing every {BALANCE_INTERVAL_S:.0f}s, renewing every {self._renew_interval_s}s")

    # kept for callers of the old API
    start_renewing = start

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._renew_task, self._balance_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._renew_task = self._balance_task = None
        for assignment_id in list(self._held):
            await self.release(assignment_id, state="OPEN")


lease_client = LeaseClient()
