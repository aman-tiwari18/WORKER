"""Run the existing analytics rules on Plan B detections, off the video thread.

Uses unified_detector's registry (EVALUATORS: rule + consecutive-hit/cooldown
gate + save-and-report), so reporting behaviour is identical to Path A.

Each camera is pinned to one worker thread (hash of its key): rules keep
per-camera state (tracks, count windows, cooldowns), so a camera's frames must be
evaluated in order and never by two threads at once.
"""
import importlib
import logging
import os
import queue
import threading
import time
import zlib

import cv2

from pathb.boxes import build_result

log = logging.getLogger(__name__)


class EvaluatorPool:
    def __init__(self, manifest, workers=8, queue_size=64, dry_run_dir=None):
        """dry_run_dir: run the rules and save annotated hits there instead of
        reporting to the DB (video tests — no DB rows, no SMS)."""
        import unified_detector  # rule registry; importing loads no model
        self._ud = unified_detector
        self._evaluators = unified_detector.EVALUATORS
        self.dry_run_dir = dry_run_dir
        if dry_run_dir:
            os.makedirs(dry_run_dir, exist_ok=True)
        self.manifest = manifest
        self._queues = [queue.Queue(maxsize=queue_size) for _ in range(max(1, workers))]
        self._states = {}
        self._lock = threading.Lock()
        self.stats = {"submitted": 0, "evaluated": 0, "dropped": 0, "errors": 0, "hits": 0}
        self._threads = [
            threading.Thread(target=self._loop, args=(q,), daemon=True, name=f"PathB-eval-{i}")
            for i, q in enumerate(self._queues)
        ]

    def start(self):
        for t in self._threads:
            t.start()

    def stop(self, timeout=10.0):
        for q in self._queues:
            try:
                q.put(None, timeout=1.0)
            except queue.Full:
                pass
        for t in self._threads:
            t.join(timeout=timeout)

    def _count(self, key, n=1):
        with self._lock:
            self.stats[key] += n

    def submit(self, packet):
        """Non-blocking. packet: {key, cfg, frame, detections, source_type}. False = dropped."""
        q = self._queues[zlib.crc32(packet["key"].encode()) % len(self._queues)]
        try:
            q.put_nowait(packet)
            self._count("submitted")
            return True
        except queue.Full:
            self._count("dropped")
            return False

    def _state(self, key, script):
        with self._lock:
            return self._states.setdefault((key, script), {})

    def _loop(self, q):
        while True:
            pkt = q.get()
            if pkt is None:
                return
            try:
                self._evaluate(pkt)
                self._count("evaluated")
            except Exception as exc:
                self._count("errors")
                log.error("[PathB] evaluation failed for %s: %s", pkt.get("key"), exc)

    def _evaluate(self, pkt):
        cfg, frame = pkt["cfg"], pkt["frame"]
        cam_no = cfg.get("camera_no") or cfg.get("ch_no") or "1"
        dev = cfg.get("camera_id") or cfg.get("device_id") or ""
        item = {
            "frame": frame,
            "camera_no": cam_no,
            "ch_no": cam_no,
            "device_id": dev,
            "camera_id": dev,
            "exam_id": cfg.get("exam_id", "0"),
            "shift_id": cfg.get("shift_id", "0"),
            "source_type": pkt.get("source_type", "RTSP"),
            "tc_code": str(cfg.get("tc_code") or cfg.get("TC_CODE") or ""),
        }
        for script in cfg.get("enabled_scripts", []):
            if script not in self._evaluators:
                continue
            result = build_result(pkt["detections"], script, self.manifest, frame.shape)
            state = self._state(pkt["key"], script)
            try:
                if self.dry_run_dir:
                    self._dry_eval(script, result, item, state, pkt["key"])
                else:
                    self._evaluators[script](result, item, state)
            except Exception as exc:
                self._count("errors")
                log.error("[PathB] rule %s failed for %s: %s", script, pkt["key"], exc)

    def _dry_eval(self, script, result, item, state, key):
        """Same rule + consecutive-hit/cooldown gate as production, but save the
        annotated hit to disk instead of _save_and_report (DB row + SMS path)."""
        module, fn_name, _ = self._ud.ANALYTIC_SPEC[script]
        fn = getattr(importlib.import_module(module), fn_name)
        annotated, detected = fn([item], results=[result])[0][:2]
        if not detected:
            state["consec"] = 0
            return
        if not self._ud._passed_gate(state, script, time.time()):
            return
        self._count("hits")
        name = f"{script}_{key}_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
        cv2.imwrite(os.path.join(self.dry_run_dir, name), annotated)
