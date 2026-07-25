"""Backtest INTEGRATION POINT: to_nautilus + NautilusBacktestAdapter.

No market data and no engine here — the runner is stubbed out. What is under
test is the lowering (CompiledStrategy → composer spec) and the metric
mapping, i.e. the parts this repo owns.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from strategy_studio.backtest import (
    NautilusBacktestAdapter,
    StubBacktestAdapter,
    UnsupportedStrategy,
    to_nautilus,
)
from strategy_studio.compiler import compile_strategy
from strategy_studio.schema import (
    InstrumentConfig,
    Param,
    RegimeBranch,
    RiskBlock,
    Rule,
    RuleGroup,
    StrategyDefinition,
)


def _p(v):
    return Param(value=v)


def _risk():
    return RiskBlock(
        stop_loss_atr_mult=_p(2.0),
        stop_loss_atr_len=_p(14),
        take_profit_r=_p(1.5),
        risk_per_trade_pct=_p(1.0),
        max_concurrent=_p(1),  # >1 is refused: the engine holds one position
    )


def _defn(entry_rules, exit_rules, **extra) -> StrategyDefinition:
    return StrategyDefinition(
        id="t1",
        name="Test",
        entry=RuleGroup(rules=entry_rules),
        exit=RuleGroup(rules=exit_rules),
        risk=_risk(),
        instruments=[InstrumentConfig(symbol="BTCUSDT", timeframe="1h", active=True)],
        **extra,
    )


def _rsi_rule(op="crosses_below", target=30.0):
    return Rule(
        id="r-rsi",
        indicator="rsi",
        params={"len": _p(14)},
        operator=op,
        target=_p(target),
    )


def _atr_exit():
    return Rule(
        id="r-atr",
        indicator="atr",
        params={"len": _p(14)},
        operator="gt",
        target=_p(3.0),
    )


# ── lowering ────────────────────────────────────────────────────────────────


def test_lowers_to_a_spec_the_composer_accepts():
    spec = to_nautilus(compile_strategy(_defn([_rsi_rule()], [_atr_exit()])))

    assert spec.validate() is None, spec.validate()
    kinds = [(b.type, b.role) for b in spec.blocks]
    assert ("rsi_threshold", "entry") in kinds
    assert ("atr_stop", "exit") in kinds


def test_rsi_params_and_direction_carry_over():
    spec = to_nautilus(
        compile_strategy(
            _defn([_rsi_rule(op="crosses_above", target=70.0)], [_atr_exit()])
        )
    )

    rsi = next(b for b in spec.blocks if b.type == "rsi_threshold")
    assert rsi.params == {"period": 14, "threshold": 70.0, "cross": "above"}


def test_risk_translation_stop_tp_and_sizing():
    spec = to_nautilus(compile_strategy(_defn([_rsi_rule()], [_atr_exit()])))

    assert spec.use_bracket is True
    assert (spec.sl_type, spec.sl_value) == ("atr", 2.0)
    assert spec.atr_period == 14
    # take_profit_r=1.5 R, and 1 R is the 2.0×ATR stop → 3.0 ATR
    assert (spec.tp_type, spec.tp_value) == ("atr", 3.0)
    # 1.0% risk over a 2.0×ATR stop → 0.5% per 1×ATR
    assert spec.trade_size_mode == "atr_target"
    assert spec.trade_size_atr_risk == pytest.approx(0.5)


def test_match_all_becomes_AND_logic():
    defn = _defn(
        [
            _rsi_rule(),
            Rule(
                id="r-adx",
                indicator="adx",
                params={"len": _p(14)},
                operator="gt",
                target=_p(25.0),
            ),
        ],
        [_atr_exit()],
    )
    defn.entry.match = "all"

    assert to_nautilus(compile_strategy(defn)).entry_logic == "AND"


# ── refusals ────────────────────────────────────────────────────────────────


def test_regime_branch_is_refused_not_dropped():
    defn = _defn(
        [_rsi_rule()],
        [_atr_exit()],
        regime=RegimeBranch(
            conditions=RuleGroup(
                rules=[
                    Rule(
                        id="r-reg",
                        indicator="adx",
                        params={"len": _p(14)},
                        operator="gt",
                        target=_p(20.0),
                    )
                ]
            )
        ),
    )

    with pytest.raises(UnsupportedStrategy) as e:
        to_nautilus(compile_strategy(defn))
    assert any("regime" in r for r in e.value.reasons)


def test_indicator_without_an_engine_block_names_the_rule():
    defn = _defn(
        [
            Rule(
                id="r-fund",
                indicator="funding_z",
                params={"lookback": _p(96)},
                operator="gt",
                target=_p(1.5),
            )
        ],
        [_atr_exit()],
    )

    with pytest.raises(UnsupportedStrategy) as e:
        to_nautilus(compile_strategy(defn))
    assert any("r-fund" in r and "funding_z" in r for r in e.value.reasons)


def test_every_refusal_is_reported_at_once():
    """One failed run should list all the blockers, not just the first."""
    defn = _defn(
        [
            Rule(
                id="r-fund",
                indicator="funding_z",
                params={"lookback": _p(96)},
                operator="gt",
                target=_p(1.5),
            ),
            Rule(
                id="r-nw",
                indicator="nadaraya_watson",
                params={"bandwidth": _p(8), "mult": _p(3)},
                operator="crosses_above",
                target=_p(1.0),
            ),
        ],
        [_atr_exit()],
    )

    with pytest.raises(UnsupportedStrategy) as e:
        to_nautilus(compile_strategy(defn))
    assert len(e.value.reasons) >= 2
    assert any("r-fund" in r for r in e.value.reasons)
    assert any("r-nw" in r for r in e.value.reasons)


def test_adx_below_threshold_is_refused_rather_than_inverted():
    defn = _defn(
        [
            Rule(
                id="r-adx",
                indicator="adx",
                params={"len": _p(14)},
                operator="lt",
                target=_p(20.0),
            )
        ],
        [_atr_exit()],
    )

    with pytest.raises(UnsupportedStrategy) as e:
        to_nautilus(compile_strategy(defn))
    assert any("r-adx" in r for r in e.value.reasons)


# ── adapter.run over a stubbed runner ───────────────────────────────────────


@pytest.fixture()
def fake_bars():
    n = 400
    close = [100 + i * 0.05 for i in range(n)]
    return pd.DataFrame(
        {
            "open": close,
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "volume": [10.0] * n,
        },
        index=pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC"),
    )


class _FakeResult:
    error = None

    def __init__(self, curve, **overrides):
        self.equity_curve = curve
        self.metrics = {
            "n_trades": 10,
            "win_rate": 0.6,
            "n_wins": 6,
            "n_losses": 4,
            "avg_win": 30.0,
            "avg_loss": -10.0,
            **overrides,
        }


def test_run_maps_runner_output_onto_studio_metrics(monkeypatch, fake_bars):
    import sandbox

    calls = []

    # The adapter runs the engine through the sandbox child (force_subprocess),
    # never run_composed_backtest in-process — patching the engine directly
    # would not reach a child anyway.
    def _fake_run(spec, bars, recipe, **kw):
        calls.append(len(bars))
        step = 10_000 / 100
        return _FakeResult([10_000 + step * i for i in range(101)])

    monkeypatch.setattr(sandbox, "run_backtest_guarded", _fake_run)

    adapter = NautilusBacktestAdapter(bars_loader=lambda s, tf: fake_bars)
    compiled = compile_strategy(_defn([_rsi_rule()], [_atr_exit()]))
    m = adapter.run(compiled)

    assert m.trades == 10
    assert m.win_rate_pct == pytest.approx(60.0)
    # gross win 6×30 over gross loss 4×10
    assert m.profit_factor == pytest.approx(4.5)
    assert m.net_pnl_pct == pytest.approx(100.0)  # curve doubles
    assert m.equity_curve[0] == 1.0  # normalized for the sparkline
    assert 0.0 <= m.dsr <= 0.99
    assert m.max_dd_pct <= 0.0  # studio reports dd negative
    # one full run + one per walk-forward fold
    assert len(calls) == 1 + compiled.walkforward["folds"]
    assert m.folds and len(m.folds) <= compiled.walkforward["folds"]


def test_runner_error_surfaces_instead_of_returning_fake_metrics(
    monkeypatch, fake_bars
):
    import sandbox

    class _Err:
        error = "no fills"
        equity_curve: list = []
        metrics: dict = {}

    monkeypatch.setattr(
        sandbox, "run_backtest_guarded", lambda spec, bars, recipe, **kw: _Err()
    )

    adapter = NautilusBacktestAdapter(bars_loader=lambda s, tf: fake_bars)
    with pytest.raises(RuntimeError, match="no fills"):
        adapter.run(compile_strategy(_defn([_rsi_rule()], [_atr_exit()])))


# ── the stub stays the default path ─────────────────────────────────────────


def test_stub_adapter_is_untouched_and_still_deterministic():
    compiled = compile_strategy(_defn([_rsi_rule()], [_atr_exit()]))
    stub = StubBacktestAdapter()

    assert stub.run(compiled).to_json() == stub.run(compiled).to_json()


def test_default_adapter_is_the_stub():
    from web.routes import strategy_studio

    assert isinstance(strategy_studio.ADAPTER, StubBacktestAdapter)
    assert isinstance(strategy_studio.TRIAL_ADAPTER, StubBacktestAdapter)


# ── single-run and fan-out engines are selected independently ───────────────


def test_single_run_switch_does_not_drag_the_sweep_along(monkeypatch):
    """STUDIO_BACKTEST=nautilus must not make sweeps/AI trials hit the engine."""
    from web.routes import strategy_studio

    monkeypatch.setenv("STUDIO_BACKTEST", "nautilus")
    monkeypatch.delenv("STUDIO_BACKTEST_OPT", raising=False)

    assert isinstance(
        strategy_studio._adapter_for("STUDIO_BACKTEST"), NautilusBacktestAdapter
    )
    assert isinstance(
        strategy_studio._adapter_for("STUDIO_BACKTEST_OPT"), StubBacktestAdapter
    )


def test_sweep_switch_selects_the_real_engine(monkeypatch):
    from web.routes import strategy_studio

    monkeypatch.setenv("STUDIO_BACKTEST_OPT", "nautilus")

    assert isinstance(
        strategy_studio._adapter_for("STUDIO_BACKTEST_OPT"), NautilusBacktestAdapter
    )


def test_optimizer_runs_on_the_trial_adapter_not_the_single_run_one():
    """The fan-out consumers share TRIAL_ADAPTER, keeping ADAPTER unshared."""
    from web.routes import strategy_studio

    assert strategy_studio.OPTIMIZER.adapter is strategy_studio.TRIAL_ADAPTER
    assert strategy_studio.OPTIMIZER.adapter is not strategy_studio.ADAPTER


# ── metrics the engine reports as NaN / from the broken MTM series ──────────


def test_no_winners_does_not_produce_a_nan_profit_factor(monkeypatch, fake_bars):
    """avg_win is NaN when nothing won — NaN is truthy, so `or 0.0` misses it.

    A NaN reaches the store as bare `NaN` in the metrics JSON, which the
    browser's JSON.parse rejects outright.
    """
    import sandbox

    monkeypatch.setattr(
        sandbox,
        "run_backtest_guarded",
        lambda spec, bars, recipe=None, **kw: _FakeResult(
            [10_000, 9_900],
            n_trades=1,
            win_rate=0.0,
            n_wins=0,
            n_losses=1,
            avg_win=float("nan"),
            avg_loss=-34.4,
        ),
    )

    adapter = NautilusBacktestAdapter(bars_loader=lambda s, tf: fake_bars)
    m = adapter.run(compile_strategy(_defn([_rsi_rule()], [_atr_exit()])))

    assert not math.isnan(m.profit_factor)
    assert "NaN" not in m.to_json()


def test_curve_and_drawdown_come_from_the_bar_level_mtm_series(monkeypatch, fake_bars):
    """The realized curve only moves on trade closes; MTM moves every bar."""
    import sandbox

    monkeypatch.setattr(
        sandbox,
        "run_backtest_guarded",
        lambda spec, bars, recipe=None, **kw: _FakeResult(
            [10_000, 10_100],  # realized: two points, never dips
            equity_curve_mtm=[
                ("t0", 10_000.0),
                ("t1", 9_700.0),
                ("t2", 10_100.0),
            ],
            max_dd=-0.03,
            sharpe=6.02,
            sharpe_per_trade=0.51,
        ),
    )

    adapter = NautilusBacktestAdapter(bars_loader=lambda s, tf: fake_bars)
    m = adapter.run(compile_strategy(_defn([_rsi_rule()], [_atr_exit()])))

    assert len(m.equity_curve) == 3, "sparkline fell back to the realized curve"
    assert m.max_dd_pct == pytest.approx(-3.0), "dip only the MTM curve sees"


def test_sharpe_is_per_trade_not_bar_frequency(monkeypatch, fake_bars):
    """Bar-frequency Sharpe counts flat bars as zero returns and inflates.

    The studio ranks strategies (optimizer objective, deploy gate), so it uses
    the per-trade ratio — otherwise rarely-trading strategies win on exposure
    rather than on edge.
    """
    import sandbox

    monkeypatch.setattr(
        sandbox,
        "run_backtest_guarded",
        lambda spec, bars, recipe=None, **kw: _FakeResult(
            [10_000, 10_100],
            equity_curve_mtm=[("t0", 10_000.0), ("t1", 10_100.0)],
            sharpe=6.02,
            sharpe_per_trade=0.51,
        ),
    )

    adapter = NautilusBacktestAdapter(bars_loader=lambda s, tf: fake_bars)
    m = adapter.run(compile_strategy(_defn([_rsi_rule()], [_atr_exit()])))

    assert m.sharpe == 0.51
    assert m.sharpe != 6.02
