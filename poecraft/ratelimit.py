"""Per-client request limits for the public server: one token bucket per (client, endpoint class), in memory.

A bucket holds up to `burst` tokens and refills at `rate` tokens per second; each request takes one. Memory stays
bounded: idle buckets are dropped once the table grows past `max_keys`. Single-process only - with several server
processes each keeps its own counts (put a shared limiter in the reverse proxy then).
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, rate: float, burst: float, max_keys: int = 50_000):
        self.rate, self.burst, self.max_keys = float(rate), float(burst), max_keys
        self._buckets: dict = {}
        self._lock = threading.Lock()

    def allow(self, key, now: float | None = None) -> tuple[bool, float]:
        """(allowed, seconds until the next request would be allowed)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                allowed, wait = True, 0.0
            else:
                self._buckets[key] = (tokens, now)
                allowed, wait = False, (1.0 - tokens) / self.rate if self.rate > 0 else float("inf")
            if len(self._buckets) > self.max_keys:
                self._sweep(now)
            return allowed, wait

    def _sweep(self, now: float) -> None:
        full = self.burst / self.rate if self.rate > 0 else float("inf")    # time for an empty bucket to refill
        for k in [k for k, (_, last) in self._buckets.items() if now - last > full]:
            del self._buckets[k]
