"""
Worker entrypoint.

NEW STACK: the worker is a HEADLESS agent — it exposes NO HTTP API and NO
socket.io. All dashboard APIs moved to the backend; the worker only polls the
backend for assignments and runs detection (app/poller.py). The identical image
runs on every host; per-host differences are env only.

  WORKER_MODE=agent (default) → headless poller loop, no server.
  WORKER_MODE=api              → legacy FastAPI (app/main.py); deprecated, kept
                                 only for transition/debug. Do not use in prod.
"""
import os
import asyncio


def _run_agent():
    # Fleet page settings (incl. the camera split) first: the modules below read their
    # configuration when imported.
    from app.fleet_config import apply as apply_fleet_settings
    apply_fleet_settings(log=lambda m: print(m, flush=True))

    from app.poller import poller_service
    from app.fleet_agent import fleet_agent
    from app.lifecycle import lifecycle_reconciler
    from app.logger import logger

    # Prometheus: a read-only /metrics (GPU stats) on METRICS_PORT when set. The
    # Fleet page's /fleet/prometheus/sd lists it as a scrape target, so a new
    # server is monitored without editing prometheus.yml. Not an API: scrape only.
    metrics_port = int(os.getenv("METRICS_PORT", "0") or 0)
    if metrics_port:
        try:
            from prometheus_client import start_http_server
            from app.metrics import registry
            start_http_server(metrics_port, registry=registry)
            logger.info(f"Prometheus /metrics on :{metrics_port}")
        except Exception as exc:  # metrics must never stop detection
            logger.warning(f"Prometheus /metrics not started on :{metrics_port}: {exc}")

    async def _main():
        logger.info("Headless worker agent starting (poller + fleet registry + lifecycle; no HTTP API / socket.io)")
        # Register/heartbeat first so the backend sees the worker even if it has
        # no assignments to poll (plan §4.1 — the fleet is self-describing).
        await fleet_agent.start()
        await poller_service.start()
        # Backend is the stop authority; this converges local processes to it.
        await lifecycle_reconciler.start()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await lifecycle_reconciler.stop()
            await poller_service.stop()
            await fleet_agent.stop()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


def _run_legacy_api():
    import uvicorn
    from app.config import HTTP_PORT
    reload_enabled = os.getenv("UVICORN_RELOAD", "false").lower() == "true"
    uvicorn.run("app.main:app", host="0.0.0.0", port=HTTP_PORT, reload=reload_enabled)


if __name__ == "__main__":
    if os.getenv("WORKER_MODE", "agent").lower() == "api":
        _run_legacy_api()
    else:
        _run_agent()
