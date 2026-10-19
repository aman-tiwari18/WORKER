"""
fleet_config.py — worker settings managed on the Fleet page (Servers -> Worker settings).

At start, before any module reads its configuration, the worker asks the backend for the
settings of its server (the all-servers ones, overridden by its own) and puts them into
os.environ. Detection scripts are started as subprocesses and inherit them. So a setting
changed on the page reaches EVERY worker — the main server too, which has no Fleet agent —
on its next restart; the page's Restart / Update / camera-split switch triggers that.

Includes COORDINATION_MODE (hash | lease), the fleet-wide camera split.

A backend that cannot be reached never stops the worker: it starts with its .env values.
Names the installer sets per worker (WORKER_ID, WORKER_INDEX, API_KEY, ...) are refused by
the backend when saved, and skipped here as well.
"""
import json
import os
import time
import urllib.parse
import urllib.request

PER_WORKER = {
    "API_KEY", "MAIN_BACKEND_URL", "WORKER_ID", "WORKER_NAME", "WORKER_INDEX", "TOTAL_WORKERS",
    "METRICS_PORT", "WORKER_HOST_IP", "WORKER_HOSTNAME", "VISION_READY", "ENABLE_VISION", "ENABLE_VLLM",
    "FLEET_NETWORK", "HF_TOKEN",
}


def apply(log=print, attempts=3, timeout=5.0):
    """Fetch and apply this worker's Fleet settings once per process tree. Returns what changed."""
    if os.environ.get("FLEET_CONFIG_APPLIED") == "1":
        return {}
    base = os.environ.get("MAIN_BACKEND_URL", "").strip(" '\"`{}").rstrip("/")
    worker_id = os.environ.get("WORKER_ID", "")
    if not base or not worker_id:
        return {}
    url = f"{base}/fleet/worker-config?worker_id={urllib.parse.quote(worker_id)}"
    req = urllib.request.Request(url, headers={"X-API-Key": os.environ.get("API_KEY", "")})
    settings = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                settings = (json.loads(r.read().decode() or "{}").get("data") or {}).get("settings") or {}
            break
        except Exception as e:                 # backend down / old backend: start with .env
            if i == attempts - 1:
                log(f"fleet settings: not loaded ({e}); starting with the .env values")
                return {}
            time.sleep(2)
    changed = {}
    for k, v in settings.items():
        if k in PER_WORKER or not isinstance(v, str):
            continue
        if os.environ.get(k) != v:
            changed[k] = (os.environ.get(k), v)
            os.environ[k] = v
    os.environ["FLEET_CONFIG_APPLIED"] = "1"
    if changed:
        log("fleet settings applied: " + ", ".join(f"{k}={new} (was {old if old is not None else 'unset'})"
                                                     for k, (old, new) in sorted(changed.items())))
    else:
        log(f"fleet settings: {len(settings)} loaded, all already in effect")
    return changed
