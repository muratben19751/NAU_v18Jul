"""Dependency-free shared constants — every module can read it without circular imports.

- ``STARTING_CASH``: the shared starting cash for backtest.py and composer.py
  (kept here so it comes from a single source; a duplicated constant could silently diverge).
- ``NO_WINDOW_FLAGS``: the ``creationflags`` value that prevents subprocesses on
  Windows from opening a console window.
- ``stamp_buy_hold_benchmark``/``benchmark_and_excess``: the buy-and-hold
  benchmark/excess-return formula, shared so backtest_robustness.py,
  parallel_exec.py, and web/routes/agent_backtest.py can't independently
  re-derive it into three (or four) silently-drifting copies.
- ``env_float``/``env_int``: env-override parsing (DeepR 2026-08-09 [ORTA]),
  character-for-character identical in backtest_robustness.py,
  parallel_exec.py, and wfo_optimizer.py before this — a bugfix to one would
  silently not reach the other two.
"""

from __future__ import annotations

import math
import os
import subprocess

STARTING_CASH = 10_000.0


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# On Windows, when a CONSOLE application (claude CLI, bash/gunzip/awk) is launched,
# a terminal window opens and closes on every call — even if the server runs
# consoleless via pythonw, because a consoleless parent creates a NEW window for a
# console-bearing child. CREATE_NO_WINDOW never creates the console; `startupinfo`/`windowsHide`
# is ignored by Windows Terminal on this machine, so this is the only reliable way.
# On POSIX it MUST be 0 — subprocess rejects non-zero creationflags there.
NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def benchmark_and_excess(
    first: float, last: float, pnl_pct: float
) -> tuple[float, float] | None:
    """Core buy-and-hold benchmark/excess-return formula. None if not computable."""
    if not math.isfinite(first) or not math.isfinite(last) or first <= 0:
        return None
    benchmark = last / first - 1.0
    return benchmark, float(pnl_pct) - benchmark


def stamp_buy_hold_benchmark(
    metrics: dict | None, bars, *, label: str | None = None
) -> None:
    """Attach buy-and-hold benchmark/excess-return fields to ``metrics``, in place.

    ``bars`` is a DataFrame with a "close" column spanning the window being
    scored. A missing/degenerate window (fewer than 2 rows, non-finite or
    non-positive first close) leaves ``metrics`` untouched rather than
    stamping a misleading value.
    """
    if not metrics or bars is None or len(bars) < 2 or "close" not in bars:
        return
    try:
        first = float(bars["close"].iloc[0])
        last = float(bars["close"].iloc[-1])
        pnl_pct = metrics.get("pnl_pct")
        if pnl_pct is None:
            pnl_pct = float(metrics.get("pnl") or 0.0) / STARTING_CASH
        result = benchmark_and_excess(first, last, float(pnl_pct))
    except (TypeError, ValueError, KeyError, IndexError):
        return
    if result is None:
        return
    benchmark, excess = result
    metrics["benchmark_return_fraction"] = benchmark
    metrics["excess_return_fraction"] = excess
    # Legacy aliases keep archived viewers and old callers readable.
    metrics["benchmark_return_pct"] = benchmark
    metrics["excess_pnl_pct"] = excess
    # The strategy return is net of its simulated costs, while this simple
    # close-to-close reference has no trading costs — keep that contract
    # explicit so downstream ranking/reporting cannot label it net alpha.
    metrics["benchmark_cost_basis"] = "gross_buy_and_hold_no_costs"
    metrics["strategy_return_cost_basis"] = "net_simulated_costs"
    if label is not None:
        metrics["benchmark"] = label
