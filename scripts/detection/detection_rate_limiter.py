"""
Detection Rate Limiter
========================
Controls save frequency per camera to avoid overwhelming storage.
Tracks last-save timestamps per camera_id and enforces max saves per minute.
"""

import time
import threading
import logging

logger = logging.getLogger(__name__)


class DetectionRateLimiter:
    """
    Rate limiter for detection saves.
    Enforces max_per_minute saves per camera.
    """

    def __init__(self, analytic_name: str, max_per_minute: int = 25):
        self.analytic_name = analytic_name
        self.max_per_minute = max_per_minute
        self.window = 60.0  # 1 minute window
        self._timestamps = {}  # camera_id -> list of save timestamps
        self._lock = threading.Lock()

    def should_save_detection(self, camera_id: str) -> tuple:
        """
        Check if a detection should be saved for this camera.

        Args:
            camera_id: Unique camera identifier

        Returns:
            (should_save: bool, saves_in_window: int, max_allowed: int)
        """
        now = time.time()

        with self._lock:
            if camera_id not in self._timestamps:
                self._timestamps[camera_id] = []

            # Clean old timestamps outside the window
            self._timestamps[camera_id] = [
                ts for ts in self._timestamps[camera_id]
                if now - ts < self.window
            ]

            saves_in_window = len(self._timestamps[camera_id])

            if saves_in_window < self.max_per_minute:
                self._timestamps[camera_id].append(now)
                return True, saves_in_window + 1, self.max_per_minute
            else:
                logger.debug(
                    f"[RateLimiter:{self.analytic_name}] "
                    f"Rate limit hit for {camera_id} "
                    f"({saves_in_window}/{self.max_per_minute} per min)"
                )
                return False, saves_in_window, self.max_per_minute


# ============================================================
# 🏭  FACTORY — one limiter per analytic type
# ============================================================
_limiters = {}
_factory_lock = threading.Lock()


def get_rate_limiter(analytic_name: str, max_per_minute: int = 25) -> DetectionRateLimiter:
    """
    Get or create a rate limiter for the given analytic.

    Args:
        analytic_name: e.g. 'crowd', 'server_room', 'phone'
        max_per_minute: max saves per minute per camera

    Returns:
        DetectionRateLimiter instance
    """
    with _factory_lock:
        if analytic_name not in _limiters:
            _limiters[analytic_name] = DetectionRateLimiter(
                analytic_name, max_per_minute
            )
        return _limiters[analytic_name]
