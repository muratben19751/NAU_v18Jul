"""In-memory session state for the loop runner.

Holds the current iteration list, running flag, and status string. No
persistence — the web app is stateless across restarts on purpose.

Wiki References
---------------
Bkz: [[cache]]

In-memory single-source-of-truth like Nautilus's [[cache]]; no persistence, empty on restart.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class IterationResult:
    id: int
    strategy: str
    params: dict
    metrics: dict
    equity_curve: list[float]
    rationale: str
    error: str | None
    timestamp: datetime
    equity_dates: list[str] = field(default_factory=list)
    trades: list[dict] = field(
        default_factory=list
    )  # [{entry_time, exit_time, entry_price, exit_price, side, pnl}]
    bars_info: dict = field(
        default_factory=dict
    )  # {symbol, category, interval, n_bars}


@dataclass
class AppState:
    iterations: list[IterationResult] = field(default_factory=list)
    best: IterationResult | None = None
    running: bool = False
    stop_requested: bool = False
    thread_started: bool = False
    last_status: str = "idle"
    lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, r: IterationResult) -> None:
        with self.lock:
            self.iterations.append(r)
            if r.error is None:
                pnl = r.metrics.get("pnl", float("-inf"))
                best_pnl = (
                    self.best.metrics.get("pnl", float("-inf"))
                    if self.best
                    else float("-inf")
                )
                if pnl > best_pnl:
                    self.best = r

    def snapshot(
        self,
    ) -> tuple[list[IterationResult], IterationResult | None, bool, str]:
        with self.lock:
            return list(self.iterations), self.best, self.running, self.last_status

    def set_status(self, s: str) -> None:
        with self.lock:
            self.last_status = s


_STATE = AppState()


def get_state() -> AppState:
    return _STATE
