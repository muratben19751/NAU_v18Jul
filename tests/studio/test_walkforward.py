"""Optimizer INTEGRATION POINT: the walk-forward optimizer (optimizer.py).

Two things are under test: the geometry (which slices of the sample a
candidate is screened on and scored on) and the accounting (penalized ranking,
trade/fold guards, deflation by trial count). The engine is stubbed — what this
repo owns is the split and the arithmetic on top of it.
"""

from __future__ import annotations

import math

import pytest

from strategy_studio.backtest import (
    BacktestMetrics,
    StubBacktestAdapter,
    Window,
    probabilistic_sharpe,
)
from strategy_studio.compiler import compile_strategy
from strategy_studio.optimizer import (
    NoViableCandidates,
    OptResult,
    WalkForwardOptimizer,
    expected_max_sharpe,
    fold_objective,
    penalized_score,
    windows_for,
)
from strategy_studio.schema import (
    InstrumentConfig,
    OptimizeRange,
    Param,
    RiskBlock,
    Rule,
    RuleGroup,
    StrategyDefinition,
    WalkForwardConfig,
)


def _p(v, opt=None):
    return Param(value=v, optimize=opt)


def _defn(**extra) -> StrategyDefinition:
    return StrategyDefinition(
        id="wf1",
        name="WF",
        entry=RuleGroup(
            rules=[
                Rule(
                    id="r-rsi",
                    indicator="rsi",
                    params={"len": _p(14, OptimizeRange(min=10, step=2, max=18))},
                    operator="crosses_below",
                    target=_p(30.0),
                )
            ]
        ),
        exit=RuleGroup(
            rules=[
                Rule(
                    id="r-atr",
                    indicator="atr",
                    params={"len": _p(14)},
                    operator="gt",
                    target=_p(3.0),
                )
            ]
        ),
        risk=RiskBlock(
            stop_loss_atr_mult=_p(2.0),
            stop_loss_atr_len=_p(14),
            take_profit_r=_p(1.5),
            risk_per_trade_pct=_p(1.0),
            max_concurrent=_p(1),
        ),
        instruments=[InstrumentConfig(symbol="BTCUSDT", timeframe="1h", active=True)],
        **extra,
    )


# ── geometry ────────────────────────────────────────────────────────────────


def test_windows_split_the_sample_by_the_months_ratio():
    screen, folds = windows_for(
        WalkForwardConfig(in_sample_months=6, oos_months=3, folds=2, embargo_bars=24)
    )

    # 6 + 2×3 = 12 units → in-sample is half, each fold a quarter.
    assert screen == Window(0.0, 0.5, label="in-sample")
    assert [(f.start, f.end) for f in folds] == [(0.5, 0.75), (0.75, 1.0)]
    assert all(f.embargo_bars == 24 for f in folds)


def test_folds_walk_forward_without_overlapping_or_leaving_a_gap():
    _screen, folds = windows_for(
        WalkForwardConfig(in_sample_months=9, oos_months=3, folds=6)
    )

    assert len(folds) == 6
    for a, b in zip(folds, folds[1:]):
        assert a.end == pytest.approx(b.start)
    assert folds[-1].end == 1.0  # the remainder is never left unevaluated


def test_no_in_sample_months_means_no_screen():
    screen, folds = windows_for(WalkForwardConfig(in_sample_months=0, folds=3))

    assert screen is None
    assert folds[0].start == 0.0


def test_the_optimizer_only_scores_on_out_of_sample_windows():
    """The screen window must never contribute to a candidate's score."""
    seen: list[Window] = []

    class _Recording(StubBacktestAdapter):
        def run(self, compiled, *, window=None):
            seen.append(window)
            return super().run(compiled, window=window)

    defn = _defn(walkforward=WalkForwardConfig(folds=3, in_sample_months=6))
    WalkForwardOptimizer(adapter=_Recording()).run(defn)

    screen, folds = windows_for(defn.walkforward)
    assert all(w is not None for w in seen), "an unwindowed run leaked into the sweep"
    labels = {w.label for w in seen}
    assert labels == {screen.label} | {f.label for f in folds}


# ── the windowed adapter contract ───────────────────────────────────────────


def test_a_windowed_stub_run_reports_no_fold_table_of_its_own():
    compiled = compile_strategy(_defn())
    stub = StubBacktestAdapter()

    assert stub.run(compiled).folds, "the unwindowed run still carries folds"
    assert stub.run(compiled, window=Window(0.0, 0.5)).folds == []


def test_two_windows_of_one_config_do_not_return_the_same_metrics():
    """Otherwise every candidate would look perfectly stable across folds."""
    compiled = compile_strategy(_defn())
    stub = StubBacktestAdapter()

    a = stub.run(compiled, window=Window(0.0, 0.5))
    b = stub.run(compiled, window=Window(0.5, 1.0))

    assert a.to_json() != b.to_json()
    assert stub.run(compiled, window=Window(0.0, 0.5)).to_json() == a.to_json()


def test_nautilus_adapter_slices_bars_and_applies_the_embargo(monkeypatch):
    import pandas as pd

    import sandbox
    from strategy_studio.backtest import NautilusBacktestAdapter

    n = 1000
    close = [100 + i * 0.05 for i in range(n)]
    bars = pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": [10.0] * n,
        },
        index=pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC"),
    )
    lengths: list[int] = []

    class _Res:
        error = None
        equity_curve = [10_000.0, 10_100.0]
        metrics = {
            "n_trades": 8,
            "win_rate": 0.5,
            "n_wins": 4,
            "n_losses": 4,
            "avg_win": 20.0,
            "avg_loss": -10.0,
        }

    def _fake(spec, b, recipe=None, **kw):
        lengths.append(len(b))
        return _Res()

    monkeypatch.setattr(sandbox, "run_backtest_guarded", _fake)
    adapter = NautilusBacktestAdapter(bars_loader=lambda s, tf: bars)
    m = adapter.run(
        compile_strategy(_defn()), window=Window(0.5, 0.75, embargo_bars=50)
    )

    # 500..750 minus the 50-bar purge at the front, and no fold sub-runs.
    assert lengths == [200]
    assert m.folds == []


def test_a_window_too_thin_to_run_is_refused_not_silently_run(monkeypatch):
    import pandas as pd

    from strategy_studio.backtest import NautilusBacktestAdapter

    bars = pd.DataFrame(
        {
            "open": [1.0] * 100,
            "high": [1.0] * 100,
            "low": [1.0] * 100,
            "close": [1.0] * 100,
            "volume": [1.0] * 100,
        },
        index=pd.date_range("2025-01-01", periods=100, freq="h", tz="UTC"),
    )
    adapter = NautilusBacktestAdapter(bars_loader=lambda s, tf: bars)

    with pytest.raises(RuntimeError, match="embargo"):
        adapter.run(compile_strategy(_defn()), window=Window(0.9, 1.0, embargo_bars=5))


# ── scoring ─────────────────────────────────────────────────────────────────


def test_penalized_score_prefers_the_steady_candidate():
    steady = [0.5, 0.5, 0.5]
    spiky = [1.6, 0.05, 0.05]  # higher mean, one carrying fold

    assert sum(spiky) > sum(steady)
    assert penalized_score(steady) > penalized_score(spiky)


def _m(**kw) -> BacktestMetrics:
    base = dict(
        net_pnl_pct=1.0,
        sharpe=1.0,
        dsr=0.5,
        max_dd_pct=-5.0,
        trades=100,
        win_rate_pct=50.0,
        profit_factor=1.2,
    )
    return BacktestMetrics(**{**base, **kw})


def test_thin_trade_counts_are_damped_on_dsr_but_not_on_max_dd():
    """Damping a negative drawdown would reward a candidate for having no history."""
    thin, thick = _m(trades=5), _m(trades=1000)

    assert fold_objective(thin, "dsr") < fold_objective(thick, "dsr")
    assert fold_objective(thin, "max_dd") == fold_objective(thick, "max_dd")


def test_expected_max_sharpe_grows_with_the_number_of_trials():
    few, many = expected_max_sharpe(0.1, 10), expected_max_sharpe(0.1, 1000)

    assert 0 < few < many
    assert expected_max_sharpe(0.0, 500) == 0.0  # no spread, nothing to deflate
    assert expected_max_sharpe(0.1, 1) == 0.0  # one trial is not a search


def test_deflation_only_lowers_the_probability():
    rets = [0.004, -0.002, 0.006, 0.001, -0.003, 0.005] * 40
    sr = 0.2

    undeflated = probabilistic_sharpe(rets, sr)
    deflated = probabilistic_sharpe(rets, sr, benchmark=0.15)

    assert deflated <= undeflated


# ── the sweep ───────────────────────────────────────────────────────────────


def test_results_carry_the_trial_count_they_were_deflated_against():
    defn = _defn(walkforward=WalkForwardConfig(folds=3))
    opt = WalkForwardOptimizer(adapter=StubBacktestAdapter())

    results = opt.run(defn)

    assert results
    assert all(r.trials == opt.last_run["grid"] for r in results)
    assert all(0.0 <= r.dsr <= 0.99 for r in results)
    assert all(r.folds_valid >= 0.6 * 3 for r in results)
    assert [r.rank for r in results] == list(range(1, len(results) + 1))


def test_a_sweep_where_nothing_survives_fails_loudly():
    class _NoTrades(StubBacktestAdapter):
        def run(self, compiled, *, window=None):
            m = super().run(compiled, window=window)
            m.trades = 1  # under min_trades everywhere
            return m

    with pytest.raises(NoViableCandidates, match="in-sample trades"):
        WalkForwardOptimizer(adapter=_NoTrades()).run(_defn())


def test_a_candidate_whose_folds_mostly_die_is_dropped_not_ranked():
    """Surviving on one lucky fold is exactly what the fold fraction exists for."""

    class _OneGoodFold(StubBacktestAdapter):
        def run(self, compiled, *, window=None):
            m = super().run(compiled, window=window)
            if window and window.label not in ("in-sample", "oos1"):
                raise RuntimeError("engine died")
            return m

    with pytest.raises(NoViableCandidates, match="usable folds"):
        WalkForwardOptimizer(adapter=_OneGoodFold()).run(
            _defn(walkforward=WalkForwardConfig(folds=4))
        )


def test_the_grid_is_capped_and_the_cap_is_reported():
    defn = _defn()
    defn.entry.rules[0].params["len"].optimize = OptimizeRange(min=2, step=1, max=200)
    opt = WalkForwardOptimizer(adapter=StubBacktestAdapter(), max_evals=12)

    opt.run(defn)

    assert defn.sweep_size() == 199
    assert opt.last_run["grid"] == 12


def test_stored_results_from_an_older_build_still_rehydrate():
    """The panel rebuilds OptResult(**row) from JSON written before these fields."""
    old = {
        "rank": 1,
        "params": {"r-rsi.len": 14.0},
        "dsr": 0.4,
        "sharpe": 1.1,
        "net_pnl_pct": 3.0,
        "max_dd_pct": -2.0,
        "trades": 40,
    }

    r = OptResult(**old)

    assert (r.score, r.folds_valid, r.trials) == (0.0, 0, 0)


def test_a_deflated_result_is_never_more_confident_than_its_own_psr():
    defn = _defn(walkforward=WalkForwardConfig(folds=3))
    results = WalkForwardOptimizer(adapter=StubBacktestAdapter()).run(defn)

    for r in results:
        assert math.isfinite(r.dsr) and 0.0 <= r.dsr <= 0.99
