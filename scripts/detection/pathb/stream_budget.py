"""Stream budgets and a circuit breaker for stream sources.

Replaces the round-robin's "6 streams per DSS server" semaphore with two limits:
  - per server  : a DSS server (d11.camview.io …) or an HLS origin — its relay
                  bandwidth budget (~300 full-quality streams per DSS server is the
                  current estimate; confirm with the ramp test).
  - per device  : one exam-centre recorder (RTSP device_id) — its internet upload.
Both are per worker process; divide the per-server number across workers that
share a server until a fleet-wide broker exists.
"""
import threading
import time
from collections import defaultdict


class StreamBudget:
    def __init__(self, per_server, per_device):
        self.per_server = int(per_server)
        self.per_device = int(per_device)
        self._server = defaultdict(int)
        self._device = defaultdict(int)
        self._lock = threading.Lock()

    def try_acquire(self, server, device=None):
        """Non-blocking. True if a slot on both the server and the device was taken."""
        with self._lock:
            if self.per_server > 0 and self._server[server] >= self.per_server:
                return False
            if device is not None and self.per_device > 0 and self._device[device] >= self.per_device:
                return False
            self._server[server] += 1
            if device is not None:
                self._device[device] += 1
            return True

    def release(self, server, device=None):
        with self._lock:
            if self._server[server] > 0:
                self._server[server] -= 1
            if device is not None and self._device[device] > 0:
                self._device[device] -= 1

    def in_use(self, server):
        with self._lock:
            return self._server[server]


class CircuitBreaker:
    """Stop using a server after repeated failures, for a cool-off period.

    Default cool-off is 300 s — the DSS login lockout — so we never hammer a
    locked or down server.
    """

    def __init__(self, fail_threshold=5, cooloff_s=300.0, clock=time.monotonic):
        self.fail_threshold = int(fail_threshold)
        self.cooloff_s = float(cooloff_s)
        self._clock = clock
        self._fails = defaultdict(int)
        self._open_until = {}
        self._lock = threading.Lock()

    def is_open(self, key):
        with self._lock:
            until = self._open_until.get(key)
            if until is None:
                return False
            if self._clock() >= until:
                del self._open_until[key]
                self._fails[key] = 0
                return False
            return True

    def record_failure(self, key):
        with self._lock:
            self._fails[key] += 1
            if self._fails[key] >= self.fail_threshold:
                self._open_until[key] = self._clock() + self.cooloff_s
                return True
            return False

    def record_success(self, key):
        with self._lock:
            self._fails[key] = 0

    def open_keys(self):
        now = self._clock()
        with self._lock:
            return [k for k, t in self._open_until.items() if t > now]
