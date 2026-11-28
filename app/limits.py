"""Load limit for heavy products: at most `parallel` calls at once, `queue_max` more waiting up to `wait_s` seconds
for a slot; beyond that Busy (the routes turn it into a free 429)."""
import threading
from contextlib import contextmanager


class Busy(Exception):
    """No slot freed up in time, or the queue is full."""


class Limit:
    def __init__(self, parallel: int, queue_max: int, wait_s: float):
        self.queue_max, self.wait_s = queue_max, wait_s
        self._slots = threading.BoundedSemaphore(parallel)
        self._lock = threading.Lock()
        self._waiting = 0

    @contextmanager
    def slot(self):
        if not self._slots.acquire(blocking=False):
            with self._lock:
                if self._waiting >= self.queue_max:
                    raise Busy
                self._waiting += 1
            try:
                got = self._slots.acquire(timeout=self.wait_s)
            finally:
                with self._lock:
                    self._waiting -= 1
            if not got:
                raise Busy
        try:
            yield
        finally:
            self._slots.release()
