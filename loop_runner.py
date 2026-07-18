"""Background loop with two modes.

- `agent`: LLM (or fallback) proposes new strategy parameters each iter.
- `catalog`: cycles through user-saved ComposedStrategy specs.

Wiki References
---------------
Bkz: [[crash_only_design]]

Idempotent per-iteration reset (state resettable, engine yeniden yaratılır); [[crash_only_design]]'ın webapp'e yansıması: her iterasyon yeni process gibi davranır.
"""

from __future__ import annotations

import time
import traceback
from datetime import UTC, datetime

import pandas as pd

from agent import propose_strategy
from composer import load_catalog
from sandbox import run_backtest_guarded, run_legacy_backtest_guarded
from state import AppState, IterationResult

SLEEP_BETWEEN_ITER = 5.0


def _try_log(
    spec_or_name,
    result,
    bars_df: pd.DataFrame,
    symbol: str = "BTCUSDT",
    category: str = "linear",
    interval: str = "1",
    elapsed_sec: float | None = None,
) -> None:
    """Best-effort append to backtest_log.jsonl for the /reports page."""
    try:
        from composer import ComposedStrategySpec
        from web.routes.backtest import _log_backtest

        if isinstance(spec_or_name, ComposedStrategySpec):
            spec = spec_or_name
            instrument_kind = "Bybit"
        else:
            # Legacy agent mode — wrap bare strategy name in a minimal spec
            spec = ComposedStrategySpec(
                id=f"loop-{result.id}",
                name=str(spec_or_name),
                description="",
                blocks=[],
                trade_size=0.1,
            )
            instrument_kind = "Bybit"

        n_bars = len(bars_df) if bars_df is not None else 0
        bars_info = {
            "symbol": symbol,
            "category": category,
            "interval": interval,
            "n_bars": n_bars,
        }
        _log_backtest(spec, result, instrument_kind, bars_info, elapsed_sec=elapsed_sec)
    except Exception:
        pass


def run_loop(
    state: AppState,
    bars_df: pd.DataFrame,
    mode: str = "agent",
    symbol: str = "BTCUSDT",
    category: str = "linear",
    interval: str = "1",
) -> None:
    state.running = True
    state.stop_requested = False
    state.set_status(f"running ({mode})")
    catalog_idx = 0
    try:
        while not state.stop_requested:
            iter_id = len(state.iterations) + 1

            if mode == "catalog":
                catalog = load_catalog()
                if not catalog:
                    state.set_status("catalog empty, waiting")
                    time.sleep(SLEEP_BETWEEN_ITER)
                    continue
                spec = catalog[catalog_idx % len(catalog)]
                catalog_idx += 1
                state.set_status(f"iter {iter_id}: composed {spec.name}")
                _bt_t0 = time.perf_counter()
                try:
                    # Killable child: Nautilus backtest GIL'i tutar; loop
                    # thread'inde in-process koşarsa sunucu donar (agent bug'ı).
                    r = run_backtest_guarded(
                        spec,
                        bars_df,
                        {
                            "symbol": symbol,
                            "category": category,
                            "interval": interval,
                        },
                        iteration_id=iter_id,
                        rationale=f"catalog cycle #{catalog_idx}",
                        force_subprocess=True,
                    )
                except Exception as e:
                    r = IterationResult(
                        id=iter_id,
                        strategy=f"composed:{spec.name}",
                        params={"spec_id": spec.id},
                        metrics={},
                        equity_curve=[],
                        rationale="",
                        error=f"crash: {e}\n{traceback.format_exc()}",
                        timestamp=datetime.now(UTC),
                    )
                state.append(r)
                _try_log(
                    spec,
                    r,
                    bars_df,
                    symbol=symbol,
                    category=category,
                    interval=interval,
                    elapsed_sec=time.perf_counter() - _bt_t0,
                )
            else:
                state.set_status(f"iter {iter_id}: proposing")
                try:
                    proposal = propose_strategy(state.iterations)
                except Exception as e:
                    r = IterationResult(
                        id=iter_id,
                        strategy="?",
                        params={},
                        metrics={},
                        equity_curve=[],
                        rationale="",
                        error=f"propose failed: {e}",
                        timestamp=datetime.now(UTC),
                    )
                    state.append(r)
                    time.sleep(SLEEP_BETWEEN_ITER)
                    continue

                if state.stop_requested:
                    break

                state.set_status(
                    f"iter {iter_id}: backtesting {proposal.get('strategy')}"
                )
                try:
                    r = run_legacy_backtest_guarded(
                        strategy_name=proposal["strategy"],
                        params=proposal["params"],
                        bars_df=bars_df,
                        iteration_id=iter_id,
                        rationale=proposal.get("rationale", ""),
                    )
                except Exception as e:
                    r = IterationResult(
                        id=iter_id,
                        strategy=proposal.get("strategy", "?"),
                        params=proposal.get("params", {}),
                        metrics={},
                        equity_curve=[],
                        rationale=proposal.get("rationale", ""),
                        error=f"backtest crash: {e}\n{traceback.format_exc()}",
                        timestamp=datetime.now(UTC),
                    )
                state.append(r)
                _try_log(
                    proposal.get("strategy", "?"),
                    r,
                    bars_df,
                    symbol=symbol,
                    category=category,
                    interval=interval,
                )

            state.set_status(f"iter {iter_id}: done")

            for _ in range(int(SLEEP_BETWEEN_ITER * 10)):
                if state.stop_requested:
                    break
                time.sleep(0.1)
    finally:
        state.running = False
        state.set_status("stopped")
