"""Faz 2 — robustness methodology: Monte Carlo, WFO, runner parity."""

from types import SimpleNamespace

import pytest

import wfo_optimizer as W
from backtest import comparable_metrics
from backtest_robustness import run_monte_carlo
from composer import ComposedStrategySpec, SignalBlock


def _spec(**over):
    kw = dict(
        id="t",
        name="t",
        description="",
        blocks=[
            SignalBlock(
                type="ma_cross",
                role="entry",
                params={"fast": 10, "slow": 30, "direction": "up"},
            ),
            SignalBlock(
                type="atr_stop", role="exit", params={"period": 14, "mult": 3.0}
            ),
        ],
    )
    kw.update(over)
    return ComposedStrategySpec(**kw)


# ---------------------------------------------------------------------------
# Monte Carlo — must no longer be degenerate
# ---------------------------------------------------------------------------


class TestMonteCarloBootstrap:
    _TRADES = [{"pnl": p} for p in [100, -50, 30, -10, 200, -80, 60, -20, 150, -40]]

    @pytest.mark.parametrize("method", ["iid_bootstrap", "block_bootstrap"])
    def test_final_band_and_winrate_vary(self, method):
        r = run_monte_carlo(
            self._TRADES, n_sims=500, starting_cash=10_000, method=method
        )
        assert r["p95_final"] - r["p5_final"] > 0, "final PnL band is degenerate"
        assert r["win_rate_std"] > 0, "win_rate_std is 0 (permutation degeneracy)"
        # median should sit near the original realized final
        assert abs(r["median_final"] - r["original_final"]) < 2_000
        assert r["method"] == method

    def test_max_dd_distribution_varies(self):
        r = run_monte_carlo(self._TRADES, n_sims=500, starting_cash=10_000)
        assert r["max_dd_p50"] != r["max_dd_p95"]

    def test_empty_trades_returns_error(self):
        assert "error" in run_monte_carlo([], n_sims=10)


# ---------------------------------------------------------------------------
# WFO — real parameter search
# ---------------------------------------------------------------------------


class TestParamSpace:
    def test_numeric_dims_from_registry(self):
        space = W.build_param_space(
            _spec(use_bracket=True, sl_value=2.0, tp_type="off")
        )
        labels = {d["label"] for d in space}
        assert labels == {
            "ma_cross[0].fast",
            "ma_cross[0].slow",
            "atr_stop[1].period",
            "atr_stop[1].mult",
            "sl_value",  # only because use_bracket=True
        }

    def test_spec_dim_gated_on_feature(self):
        # No bracket → sl_value/tp_value absent; no trend filter → trend_ema absent.
        space = W.build_param_space(_spec(use_bracket=False, trend_filter=False))
        labels = {d["label"] for d in space}
        assert "sl_value" not in labels
        assert "tp_value" not in labels
        assert "trend_ema_period" not in labels

    def test_tp_and_trend_dims_appear_when_active(self):
        space = W.build_param_space(
            _spec(use_bracket=True, tp_type="percent", trend_filter=True)
        )
        labels = {d["label"] for d in space}
        assert "tp_value" in labels
        assert "trend_ema_period" in labels


class TestMutateSpec:
    def test_returns_new_object_and_preserves_original(self):
        spec = _spec()
        space = W.build_param_space(spec)
        new = W.mutate_spec(spec, space, [99, 199, 20, 5.0])
        assert spec.blocks[0].params["fast"] == 10  # original untouched
        assert new.blocks[0].params["fast"] == 99
        assert new.blocks[0].params["slow"] == 199

    def test_validate_rejects_slow_le_fast(self):
        spec = _spec()
        space = W.build_param_space(spec)
        bad = W.mutate_spec(spec, space, [50, 10, 20, 5.0])  # slow < fast
        assert bad.validate() is not None


class TestObjectiveGuards:
    def test_under_traded_is_negative_inf(self):
        res = SimpleNamespace(error=None, metrics={"sharpe": 99.0, "n_trades": 1})
        assert W.objective_value(res, "sharpe", min_trades=5) == float("-inf")

    def test_errored_is_negative_inf(self):
        res = SimpleNamespace(error="boom", metrics={})
        assert W.objective_value(res) == float("-inf")

    def test_nan_sharpe_falls_back_to_sortino(self):
        # M29/M36: NAU güven sönümü n/(n+20) fallback zincirine de uygulanır
        # → 1.5 × 10/(10+20) = 0.5.
        res = SimpleNamespace(
            error=None, metrics={"sharpe": float("nan"), "sortino": 1.5, "n_trades": 10}
        )
        assert W.objective_value(res, "sharpe") == pytest.approx(1.5 * 10 / 30)


class TestOptimizeWindow:
    def _stub(self, cand, bars, **kw):
        # objective (sharpe) == chosen fast value → deterministic arg-max
        return SimpleNamespace(
            error=None,
            metrics={"sharpe": float(cand.blocks[0].params["fast"]), "n_trades": 10},
        )

    def test_picks_argmax(self):
        spec = _spec()
        space = W.build_param_space(spec)
        best = W.optimize_window(
            spec, None, space, None, None, None, n_samples=40, seed=1, run_fn=self._stub
        )
        # M29/M36: skor = sharpe × n/(n+20) güven sönümü (stub n_trades=10 →
        # ×1/3); tek fold'da penalized_score = değerin kendisi.
        assert best["objective"] == pytest.approx(
            float(best["params"]["ma_cross[0].fast"]) * 10 / 30
        )

    def test_windows_with_different_seeds_pick_different_params(self):
        spec = _spec()
        space = W.build_param_space(spec)
        b1 = W.optimize_window(
            spec, None, space, None, None, None, n_samples=15, seed=1, run_fn=self._stub
        )
        b2 = W.optimize_window(
            spec, None, space, None, None, None, n_samples=15, seed=2, run_fn=self._stub
        )
        # Different random exploration → almost surely different chosen fast.
        assert b1["params"] != b2["params"]


# ---------------------------------------------------------------------------
# Runner reconciliation
# ---------------------------------------------------------------------------


class TestRunnerParity:
    def test_comparable_metrics_projection(self):
        m = {
            "pnl": 100.0,
            "pnl_pct": 1.0,
            "win_rate": 0.5,
            "n_trades": 20,
            "sharpe_nautilus": 1.3,
            "sharpe": 2.7,  # primary — must be excluded
            "max_dd": -0.1,  # excluded
        }
        c = comparable_metrics(m)
        assert set(c) == {"pnl", "pnl_pct", "win_rate", "n_trades", "sharpe_nautilus"}
        assert "sharpe" not in c and "max_dd" not in c
        assert c["sharpe_nautilus"] == 1.3

    def test_engine_and_node_share_sharpe_nautilus(self):
        engine = {"sharpe": 2.7, "sharpe_nautilus": 1.3, "annualization": 365}
        node = {"sharpe": 1.3, "sharpe_nautilus": 1.3, "annualization": 252}
        assert (
            comparable_metrics(engine)["sharpe_nautilus"]
            == comparable_metrics(node)["sharpe_nautilus"]
        )
