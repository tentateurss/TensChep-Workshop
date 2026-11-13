"""Progress reporting for a running plan: phase, X / Y, elapsed and an estimate of the time left.

The planner calls start() / step() at its existing loop boundaries; nothing here changes what is computed.
Listeners (the CLI's stderr printer, the server's NDJSON stream) receive plain dicts:
    {"phase": "calculating" | "simulating" | "done", "done": int, "total": int, "elapsed": float,
     "eta": float | None, "timeouts": int}
"eta" is the time left in the current phase, extrapolated from that phase's rate so far; it is None until the
phase has run for a moment and made some progress (an estimate from one step is noise)."""
from __future__ import annotations

import time
from typing import Callable

PHASES = ("calculating", "simulating", "done")


class Progress:
    def __init__(self, listener: Callable[[dict], None] | None = None, min_interval: float = 0.1):
        self.listener = listener
        self.min_interval = min_interval        # throttle for repeated events of the same phase
        self.t0 = time.monotonic()
        self.phase = "calculating"
        self.phase_t0 = self.t0
        self.done = self.total = 0
        self.timeouts = 0
        self.ends_by: float | None = None       # monotonic time by which the phase must end (a hard limit), if any
        self._last_emit = 0.0

    def start(self, phase: str, total: int = 0) -> None:
        """Enter `phase` (a no-op if already in it, apart from growing the total)."""
        if phase != self.phase:
            self.phase, self.phase_t0, self.done, self.total = phase, time.monotonic(), 0, 0
            self.ends_by = None
        self.total += total
        self.emit(force=True)

    def grow(self, n: int) -> None:
        first = not self.total               # the phase's first real count is always shown (no "0/0" before it)
        self.total += n
        self.emit(force=first)

    def step(self, n: int = 1) -> None:
        self.done += n
        self.total = max(self.total, self.done)
        self.emit()

    def timed_out(self) -> None:
        self.timeouts += 1
        self.emit(force=True)

    def finish(self) -> None:
        self.phase, self.done, self.total = "done", 1, 1
        self.emit(force=True)

    def snapshot(self) -> dict:
        now = time.monotonic()
        spent = now - self.phase_t0
        eta = None
        if self.phase != "done" and self.done and self.total > self.done and spent > 0.3:
            eta = spent / self.done * (self.total - self.done)
        if eta is not None and self.ends_by is not None:
            eta = max(0.0, min(eta, self.ends_by - now))      # never promise longer than the time limit allows
        return {"phase": self.phase, "done": self.done, "total": self.total, "elapsed": round(now - self.t0, 2),
                "eta": None if eta is None else round(eta, 1), "timeouts": self.timeouts}

    def emit(self, force: bool = False) -> None:
        if self.listener is None:
            return
        now = time.monotonic()
        if not force and now - self._last_emit < self.min_interval:
            return
        self._last_emit = now
        self.listener(self.snapshot())

