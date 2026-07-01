#!/usr/bin/env python3
"""
Manual test runner for RTSP camera classification.

Fetches all active RTSP cameras for exam_id=20, runs Pass-1 (accessibility)
and Pass-2 (VLM classification), persists results to DB, and prints a summary.

Usage:
    python test_vision.py

Override exam_id at runtime:
    EXAM_ID=25 python test_vision.py
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from typing import List, Optional

# ---------------------------------------------------------------------------
# Path setup — allow running from any directory
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# ---------------------------------------------------------------------------
# Import shared pipeline components from the main classification module.
# This reuses all the Camview auth, frame-capture, VLM, and DB-persist logic
# without duplicating it.
# ---------------------------------------------------------------------------
from vision_classification_rtsp import (
    _check_rtsp_camera,
    _process_camera,
    _preauth_camview_servers,
    _make_connector,
    _bulk_persist,
    _bulk_persist_status,
    _ensure_vllm_running,
    _shutdown_vllm,
    MAX_WORKERS_NETWORK,
    MAX_WORKERS_VLM,
    _THREAD_POOL,
    LOG_DIR,
)

try:
    from utils.db_client import get_rtsp_cameras
except ImportError:
    from db_client import get_rtsp_cameras  # type: ignore

import aiohttp
from tqdm.asyncio import tqdm as atqdm

# ===========================================================================
# Config
# ===========================================================================
EXAM_ID: int = int(os.getenv("EXAM_ID", "20"))

# ===========================================================================
# Logging
# ===========================================================================
os.makedirs(LOG_DIR, exist_ok=True)
_log_file = os.path.join(
    LOG_DIR, f"test_vision_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(_log_file), logging.StreamHandler()],
)
logger = logging.getLogger("test_vision")

# ===========================================================================
# Pipeline
# ===========================================================================

async def run(exam_id: int) -> None:
    logger.info("=" * 70)
    logger.info(f"Test Vision — RTSP | exam_id={exam_id}")
    logger.info("=" * 70)

    cameras: List[dict] = get_rtsp_cameras(exam_id) or []
    if not cameras:
        logger.warning(f"No active RTSP cameras found for exam_id={exam_id}")
        return

    logger.info(f"Loaded {len(cameras)} RTSP cameras")

    runtime = {"exam_id": exam_id, "link_type": "RTSP"}

    sem_net = asyncio.Semaphore(MAX_WORKERS_NETWORK)
    sem_vlm = asyncio.Semaphore(MAX_WORKERS_VLM)

    accessible: List[dict] = []
    failed:     List[dict] = []

    async with aiohttp.ClientSession(connector=_make_connector()) as session:
        loop = asyncio.get_event_loop()

        # ── Pre-auth: warm up Camview tokens before the Pass-1 storm ─────────
        await loop.run_in_executor(_THREAD_POOL, _preauth_camview_servers, cameras)

        # ── Pass-1: accessibility check ───────────────────────────────────────
        logger.info(f"Pass-1 | {len(cameras)} cameras | {MAX_WORKERS_NETWORK} concurrent")
        p1_coros = [_check_rtsp_camera(c, sem_net) for c in cameras]
        p1_start = time.time()

        async for fut in atqdm(
            asyncio.as_completed(p1_coros),
            total=len(p1_coros),
            desc="Pass-1 accessibility",
        ):
            res = await fut
            if res["accessible"]:
                accessible.append(res)
            else:
                failed.append({
                    "camera_id": res["camera_id"],
                    "cam":       res.get("cam", {}),
                    "reason":    res["reason"],
                })

        logger.info(
            f"Pass-1 done {time.time()-p1_start:.1f}s | "
            f"accessible={len(accessible)}  failed={len(failed)}"
        )

        # Persist Pass-1 statuses immediately
        statuses_p1: List[dict] = []
        for x in accessible:
            statuses_p1.append({
                "camera_id": x["camera_id"],
                "device_id": str(x["cam"].get("device_id") or ""),
                "camera_no": str(x["cam"].get("camera_no") or ""),
                "status":    "Accessible",
            })
        for x in failed:
            cam = x.get("cam", {})
            statuses_p1.append({
                "camera_id": x["camera_id"],
                "device_id": str(cam.get("device_id") or ""),
                "camera_no": str(cam.get("camera_no") or ""),
                "status":    "Not Accessible",
            })
        _bulk_persist_status(runtime, statuses_p1)

        if not accessible:
            logger.warning("No accessible cameras — skipping Pass-2")
            _print_summary(exam_id, cameras, accessible, failed, [], 0)
            return

        # ── Pre-auth refresh: tokens can expire while Pass-1 runs ─────────────
        logger.info("[Camview] Pre-auth refresh before Pass-2 (force=True)…")
        await loop.run_in_executor(_THREAD_POOL, _preauth_camview_servers, cameras, True)

        # ── Pass-2: frame capture + VLM classification ────────────────────────
        logger.info(
            f"Pass-2 | {len(accessible)} cameras | {MAX_WORKERS_VLM} concurrent vLLM"
        )
        p2_coros = [
            _process_camera(r, session, sem_vlm, is_rtsp=True)
            for r in accessible
        ]
        classifications: List[dict] = []
        p2_start = time.time()

        async for fut in atqdm(
            asyncio.as_completed(p2_coros),
            total=len(p2_coros),
            desc="Pass-2 classifying",
        ):
            res = await fut
            classifications.append({
                "camera_id":      res["camera_id"],
                "device_id":      res.get("device_id", ""),
                "camera_no":      res.get("camera_no", ""),
                "camera_name":    res.get("camera_name"),
                "classification": res["classification"],
                "status":         res.get("status"),
                "detail":         res.get("detail"),
                "frame_path":     res.get("frame_path"),
            })
            if res.get("status") != "Accessible":
                failed.append({
                    "camera_id": res["camera_id"],
                    "reason":    res.get("detail"),
                })

        logger.info(f"Pass-2 done {time.time()-p2_start:.1f}s")

    # Revert Pass-2 failures in DB (they were marked Accessible after Pass-1)
    pass2_failed_statuses = [
        {
            "camera_id": c["camera_id"],
            "device_id": str(c.get("device_id") or ""),
            "camera_no": str(c.get("camera_no") or ""),
            "status":    "Not Accessible",
        }
        for c in classifications
        if c.get("status") != "Accessible"
    ]
    if pass2_failed_statuses:
        logger.info(
            f"Pass-2: {len(pass2_failed_statuses)} cameras failed after Pass-1 success "
            f"— reverting DB status to Not Accessible"
        )
        _bulk_persist_status(runtime, pass2_failed_statuses)

    # Bulk persist classifications
    updated = _bulk_persist(runtime, classifications)

    _print_summary(exam_id, cameras, accessible, failed, classifications, updated)

    # Write JSON results next to this script for easy inspection
    out_file = os.path.join(
        _HERE, f"test_vision_results_{exam_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    try:
        with open(out_file, "w") as fh:
            json.dump(classifications, fh, indent=2, default=str)
        logger.info(f"Results written → {out_file}")
        print(f"\nResults JSON → {out_file}")
    except Exception as e:
        logger.warning(f"Could not write results file: {e}")


def _print_summary(
    exam_id: int,
    cameras: List[dict],
    accessible: List[dict],
    failed: List[dict],
    classifications: List[dict],
    updated: int,
) -> None:
    classified_ok  = sum(1 for c in classifications if c.get("status") == "Accessible")
    uncertain      = sum(
        1 for c in classifications
        if c.get("status") == "Accessible"
        and "uncertain" in (c.get("classification") or "").lower()
    )
    not_accessible = len(cameras) - len(accessible) + sum(
        1 for c in classifications if c.get("status") != "Accessible"
    )

    print(f"""
{'='*70}
TEST VISION SUMMARY  —  exam_id={exam_id}
{'='*70}
Total cameras loaded  : {len(cameras)}
Pass-1 accessible     : {len(accessible)}
Pass-1 failed         : {len(cameras) - len(accessible)}
Pass-2 classified     : {classified_ok}
  — of which Uncertain: {uncertain}
Pass-2 failed         : {sum(1 for c in classifications if c.get('status') != 'Accessible')}
DB rows updated       : {updated}
Log                   : {_log_file}
{'='*70}""")

    if classifications:
        print("\nClassification breakdown:")
        counts: dict = {}
        for c in classifications:
            lbl = c.get("classification") or "Unknown"
            counts[lbl] = counts.get(lbl, 0) + 1
        for lbl, cnt in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"  {lbl:<30} {cnt}")


# ===========================================================================
# Entry point
# ===========================================================================

def _handle_signal(signum, frame):
    logger.warning(f"Signal {signum} received — shutting down vLLM")
    _shutdown_vllm(f"signal {signum}")
    sys.exit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    vllm_proc = _ensure_vllm_running()
    try:
        asyncio.run(run(EXAM_ID))
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        _shutdown_vllm("test_vision exit")
