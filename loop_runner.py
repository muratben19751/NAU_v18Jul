"""Background loop with two modes.

- `agent`: LLM (or fallback) proposes new strategy parameters each iter.
- `catalog`: cycles through user-saved ComposedStrategy specs.

Wiki References
---------------
Bkz: [[crash_only_design]], [[nau_deepr_toplu_sertlestirme_2026_08]]

Idempotent per-iteration reset (state resettable, engine recreated); [[crash_only_design]]'s reflection onto the webapp: each iteration behaves like a new process.

`bars_df` is re-fetched at the top of every iteration (2026-08-08) instead of
staying pinned to the single frame `/loop/start` was called with — see
[[nau_deepr_toplu_sertlestirme_2026_08]] and the `webapp_module_map` row for
this file.
"""

from __future__ import annotations

import logging
import time
import traceback
from datetime import UTC, datetime

import pandas as pd

from agent import propose_strategy
from composer import load_catalog
from data import load_bybit_bars
from sandbox import run_backtest_guarded, run_legacy_backtest_guarded
from state import AppState, IterationResult

log = logging.getLogger(__name__)

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

        # DeepR 2026-08-11 [ORTA]: burası `web.routes.backtest._log_backtest`
        # idi — çalışma zamanı katmanı, bir HTTP route modülünün ALT ÇİZGİLİ
        # yüzeyinden fonksiyon çekiyordu. O ad zaten `web.shared.log_backtest`'in
        # takma adıydı; doğru olan kaynağı çağırmak.
        from web.shared import log_backtest as _log_backtest

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
        # Stamp the log key back onto the iteration so the dashboard row can
        # link its tear sheet without re-matching on metrics.
        result.log_ts = _log_backtest(
            spec, result, instrument_kind, bars_info, elapsed_sec=elapsed_sec
        )
    except Exception:
        # log I/O failure must not stop the loop — but it must not vanish
        # without a trace either (DeepR 2026-08-09 [ORTA]: sibling code path
        # web/routes/backtest.py's run() worker had the same bare `pass`,
        # fixed 2026-08-08; this one wasn't). A persistently failing log
        # write (disk full, permission error) had no observable symptom
        # beyond /reports quietly staying empty.
        log.warning(
            "loop iteration %s: _log_backtest failed",
            getattr(result, "id", "?"),
            exc_info=True,
        )


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

            # Refresh market data every iteration. `bars_df` used to be the
            # ONE snapshot captured when /loop/start was clicked (or, worse,
            # whatever lifespan() loaded at server boot) and never touched
            # again — a loop left running for days kept backtesting the exact
            # same stale window forever, unlike every other data path in the
            # app (backtest/robustness/lab/chart/reports), which all re-fetch
            # fresh bars per request. On a fetch failure, keep the previous
            # bars_df (a stale-but-working iteration beats killing the loop).
            try:
                bars_df = load_bybit_bars(
                    symbol=symbol, interval=interval, category=category
                )
            except Exception as e:
                log.warning(
                    "loop iter %d: bars refresh failed (%s), reusing prior snapshot",
                    iter_id,
                    e,
                )

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
                    # Killable child: Nautilus backtest holds the GIL; if it
                    # runs in-process on the loop thread the server freezes (agent bug).
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
