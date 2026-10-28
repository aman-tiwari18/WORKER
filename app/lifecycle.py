"""
lifecycle.py — worker half of the lifecycle authority (plan §4.3).

The worker does not decide when to stop. It asks the backend
(GET /fleet/desired-state) what should be running and converges: anything the
backend answers STOP for is killed, whatever the local timer thinks.

This runs as its OWN loop, deliberately separate from the poller:

  - The poller's job is to START work. If it wedges, blocks on a slow DB, or
    crashes mid-cycle, nothing would enforce the stop — which is precisely the
    shape of the incident this exists to prevent.
  - Its interval is short (15 s) and independent of the 30 s poll, so a closed
    window is enforced promptly rather than at the next poll boundary.

The local timeout in script_runner is left in place as defence in depth. It is
no longer the authority — this loop is — but two independent stops are strictly
better than one for a failure this expensive.
"""
import asyncio
import os

import httpx

from app.config import MAIN_BACKEND_URL, API_KEY, WORKER_ID
from app.logger import logger
from app.script_runner import script_runner

RECONCILE_INTERVAL_S = int(os.getenv("LIFECYCLE_RECONCILE_INTERVAL_S", "15"))
LIFECYCLE_ENABLED = os.getenv("LIFECYCLE_ENABLED", "1") == "1"
REQUEST_TIMEOUT_S = float(os.getenv("LIFECYCLE_REQUEST_TIMEOUT_S", "10"))
_MAX_BACKOFF_S = 120


class LifecycleReconciler:
    def __init__(self):
        self.base_url = MAIN_BACKEND_URL.rstrip("/")
        self.headers = {"X-API-Key": API_KEY}
        self._task = None
        self._stopping = False

    def _running_assignments(self):
        """
        Map backend assignment_id → local execution key.

        A grouped detection job runs under a synthetic "grp-…" key that the
        backend has never heard of, so it must be resolved to its member
        assignment ids — otherwise grouped jobs (the exact kind that ran long in
        prod) would be invisible to the authority.
        """
        from app.poller import poller_service

        mapping = {}
        for exec_key in list(getattr(poller_service, "running_scripts", set())):
            members = poller_service.group_members.get(exec_key, [exec_key])
            # A shift session keeps the finished windows of its earlier runs / earlier analytics in its member
            # list, and the backend orders STOP for any member that is COMPLETED. 28 Oct 2026: a second run on
            # the same shift joined the session whose first run (assignment 9771) had completed -> the new run
            # was stopped 8 s after it started on every worker. Ask only about members that are not done
            # (if all are done, ask about all so the job is still stopped).
            done = ((getattr(poller_service, "sessions", {}) or {}).get(exec_key) or {}).get("status") or {}
            live = [m for m in members if done.get(str(m)) != "COMPLETED"]
            if live:
                members = live
            for mid in members:
                try:
                    mapping[int(mid)] = exec_key
                except (TypeError, ValueError):
                    continue  # not a backend assignment id
        return mapping

    async def _reconcile_once(self, client: httpx.AsyncClient) -> None:
        mapping = self._running_assignments()
        if not mapping:
            return

        resp = await client.post(
            f"{self.base_url}/fleet/desired-state",
            json={"worker_id": WORKER_ID, "assignment_ids": list(mapping.keys())},
            headers=self.headers,
            timeout=REQUEST_TIMEOUT_S,
        )
        resp.raise_for_status()
        payload = resp.json()

        # Kill each local execution at most once per pass, even if several of its
        # member assignments are told to stop.
        to_stop = {}
        for item in payload.get("desired", []):
            if str(item.get("action", "")).upper() != "STOP":
                continue
            exec_key = mapping.get(int(item["assignment_id"]))
            if exec_key is not None and exec_key not in to_stop:
                to_stop[exec_key] = item

        for exec_key, item in to_stop.items():
            logger.warning(
                f"⏹ lifecycle: backend orders STOP of {exec_key} "
                f"(assignment {item['assignment_id']}): {item.get('reason')}"
            )
            try:
                killed = await script_runner.cancel(exec_key)
                if killed:
                    logger.info(f"lifecycle: stopped {exec_key} on backend authority")
                else:
                    logger.info(f"lifecycle: {exec_key} was already gone")
            except Exception as exc:
                logger.error(f"lifecycle: failed to stop {exec_key}: {exc}")

    async def _loop(self) -> None:
        backoff = RECONCILE_INTERVAL_S
        async with httpx.AsyncClient() as client:
            while not self._stopping:
                try:
                    await self._reconcile_once(client)
                    backoff = RECONCILE_INTERVAL_S
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Backend unreachable: keep detection running (a network blip
                    # must not kill live exam monitoring) and retry. The local
                    # timeout in script_runner still bounds the process.
                    logger.warning(f"lifecycle: reconcile failed ({exc}); retrying in {backoff}s")
                    backoff = min(backoff * 2, _MAX_BACKOFF_S)
                await asyncio.sleep(backoff)

    async def start(self) -> None:
        if not LIFECYCLE_ENABLED:
            logger.info("lifecycle: reconciler disabled (LIFECYCLE_ENABLED=0)")
            return
        if self._task:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop())
        logger.info(f"lifecycle: reconciler started (every {RECONCILE_INTERVAL_S}s; backend is the stop authority)")

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
        logger.info("lifecycle: reconciler stopped")


lifecycle_reconciler = LifecycleReconciler()
