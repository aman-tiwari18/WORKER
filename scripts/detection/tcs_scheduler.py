"""
TCS Scheduler
==============
Frame-processing scheduler, concurrency control, and worker pool
for TCS HLS streams.
"""

import os
import threading
import queue
import time
from contextlib import contextmanager


def frame_should_process(frame_count: int, every_n: int = 5) -> bool:
    """
    Return True if this frame should be processed.

    Args:
        frame_count: Current frame number (1-indexed)
        every_n: Process every Nth frame

    Returns:
        True if frame_count is a multiple of every_n
    """
    return frame_count % every_n == 0


def get_sample_every(env_key: str = "SAMPLE_EVERY", default: int = 5) -> int:
    """Read frame sampling interval from environment variable."""
    return int(os.environ.get(env_key, str(default)))


def get_max_frames_per_run(env_key: str = "MAX_FRAMES", default=0):
    """
    Read max frames per camera run from environment variable.
    0 or None means unlimited (run until stream ends or stop event).
    """
    val = os.environ.get(env_key, str(default) if default is not None else "0")
    result = int(val)
    return result if result > 0 else None


def get_max_concurrency(env_key: str = "MAX_CONCURRENCY", default=None):
    """
    Read max concurrent camera workers from environment variable.
    None means unlimited.
    """
    val = os.environ.get(env_key)
    if val is not None:
        return int(val)
    return default


def get_bool(env_key: str, default: bool = False) -> bool:
    """Read a boolean from environment variable (true/1/yes → True)."""
    val = os.environ.get(env_key, str(default)).lower()
    return val in ('true', '1', 'yes')


def get_positive_float(env_key: str, default: float = 1.0) -> float:
    """Read a positive float from environment variable."""
    val = float(os.environ.get(env_key, str(default)))
    return max(0.0, val)


# ── Concurrency control ─────────────────────────────
_semaphores = {}
_sem_lock = threading.Lock()


@contextmanager
def camera_slot(group: str, max_concurrent=None):
    """
    Context manager for limiting concurrent camera workers.

    Usage:
        with camera_slot("crowd", max_concurrent=10):
            process_stream(...)

    If max_concurrent is None, no limit is applied.
    """
    if max_concurrent is None:
        yield
        return

    with _sem_lock:
        if group not in _semaphores:
            _semaphores[group] = threading.Semaphore(int(max_concurrent))

    sem = _semaphores[group]
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


# ── Worker Pool ──────────────────────────────────────

class CameraWorkerPool:
    """
    Thread pool for camera workers with cooldown between task starts.

    Usage:
        pool = CameraWorkerPool(worker_count=8, cooldown_sec=0.5)
        pool.start()
        for cam in cameras:
            pool.submit(process_fn, cam, df)
        pool.wait_forever()
        pool.stop()
    """

    def __init__(self, worker_count: int = 8, cooldown_sec: float = 0.5):
        self.worker_count = worker_count
        self.cooldown_sec = cooldown_sec
        self._queue = queue.Queue()
        self._threads = []
        self._stop = threading.Event()

    def start(self):
        """Start the worker threads."""
        for i in range(self.worker_count):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"CameraPool-{i}",
                daemon=True
            )
            t.start()
            self._threads.append(t)

    def submit(self, fn, *args, **kwargs):
        """Submit a task to the pool."""
        self._queue.put((fn, args, kwargs))
        if self.cooldown_sec > 0:
            time.sleep(self.cooldown_sec)

    def _worker_loop(self):
        while not self._stop.is_set():
            try:
                fn, args, kwargs = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                fn(*args, **kwargs)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"[CameraPool] Task error: {e}")
            finally:
                self._queue.task_done()

    def wait_forever(self):
        """Block until all tasks are done or KeyboardInterrupt."""
        try:
            self._queue.join()
        except KeyboardInterrupt:
            self._stop.set()

    def stop(self):
        """Signal all workers to stop."""
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)


