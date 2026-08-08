"""Phase 1 — Measurement Layer tests.

Verification criteria from IMPROVEMENT_PLAN.md Phase 1:
1. Synthetic position series where MTM max_dd > realized max_dd.
2. Known return series → manual Sharpe(365) matches hand-calc within 1e-6.
3. Fee estimate > 0 → net PnL lower than fee=0 version.
4. _parse_money_column: bad value → 0.0, warning logged.
"""

from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_positions_df(realized_pnls: list[float]) -> pd.DataFrame:
    """Build a minimal positions DataFrame with ts_closed and realized_pnl."""
    n = len(realized_pnls)
    ts_closed = [int(1_700_000_000e9) + i * int(3600e9) for i in range(n)]  # 1h spacing
    return pd.DataFrame(
        {
            "realized_pnl": [f"{p:.4f} USDT" for p in realized_pnls],
            "ts_closed": ts_closed,
            "duration_ns": [int(1800e9)] * n,
            "commissions": [[]] * n,
        }
    )


# ---------------------------------------------------------------------------
# Test 1 — MTM max_dd > realized max_dd
# ---------------------------------------------------------------------------


class TestMtmDrawdown:
    """MTM drawdown must exceed realized drawdown when open position dips deep."""

    def test_mtm_dd_exceeds_realized(self):
        from backtest import STARTING_CASH, _max_dd_from_series

        # Realized: two trades, both close green (+5% each)
        realized_equity = [STARTING_CASH, STARTING_CASH * 1.05, STARTING_CASH * 1.10]
        realized_dd = _max_dd_from_series(realized_equity)
        assert realized_dd >= -0.01, f"Realized dd should be near 0, got {realized_dd}"

        # MTM: equity dropped 30% mid-trade before recovering
        # Simulate: 10k → 7k (open loss) → 10.5k (closed green)
        mtm_equity = [
            STARTING_CASH,
            STARTING_CASH * 0.95,
            STARTING_CASH * 0.80,
            STARTING_CASH * 0.70,  # -30% open loss
            STARTING_CASH * 0.85,
            STARTING_CASH * 1.05,  # closes green
        ]
        mtm_dd = _max_dd_from_series(mtm_equity)
        assert mtm_dd < -0.25, (
            f"MTM max_dd should be < -25% due to open drawdown, got {mtm_dd:.3f}"
        )
        assert mtm_dd < realized_dd, (
            f"MTM dd ({mtm_dd:.3f}) must be worse than realized ({realized_dd:.3f})"
        )

    def test_metrics_uses_mtm_when_provided(self):
        """_metrics returns MTM-based max_dd when mtm_equity is provided."""
        from unittest.mock import MagicMock

        from backtest import STARTING_CASH, _metrics

        # Two winning trades (realized dd ≈ 0)
        positions_df = _fake_positions_df([100.0, 150.0])

        engine = MagicMock()
        engine.portfolio.analyzer.get_performance_stats_returns.return_value = {}
        engine.portfolio.analyzer.get_performance_stats_general.return_value = {}
        engine.portfolio.analyzer.currencies = []
        engine.trader.generate_order_fills_report.return_value = pd.DataFrame()

        # MTM shows a 30% dip
        mtm = [STARTING_CASH * (1 - 0.30 * i / 10) for i in range(10)] + [
            STARTING_CASH * 1.025,
            STARTING_CASH * 1.05,
        ]
        metrics = _metrics(engine, positions_df, mtm_equity=mtm)

        assert metrics["max_dd"] < -0.20, (
            f"max_dd should reflect MTM dip, got {metrics['max_dd']:.3f}"
        )
        assert metrics["max_dd_mtm"] == metrics["max_dd"]
        assert "equity_curve_realized" in metrics


# ---------------------------------------------------------------------------
# Test 2 — Sharpe(365) manual calculation
# ---------------------------------------------------------------------------


class TestManualSharpe:
    """Manual Sharpe(365) must match hand-computed value within 1e-6."""

    def test_sharpe_365_known_series(self):
        from backtest import _sharpe_manual

        # Deterministic equity series: linear growth with noise
        np.random.seed(42)
        n = 1000
        daily_returns = 0.001 + 0.01 * np.random.randn(n)  # ~0.1% mean daily
        equity = np.cumprod(1 + daily_returns) * 10_000

        # Hand-calculate
        returns = np.diff(equity) / equity[:-1]
        expected = float(np.mean(returns) / np.std(returns, ddof=1) * math.sqrt(365))

        result = _sharpe_manual(list(equity), annualization=365)
        assert abs(result - expected) < 1e-6, (
            f"Sharpe mismatch: expected={expected:.8f}, got={result:.8f}"
        )

    def test_sharpe_nan_on_flat_equity(self):
        from backtest import _sharpe_manual

        flat = [10_000.0] * 100
        assert math.isnan(_sharpe_manual(flat)), "Flat equity should yield NaN Sharpe"

    def test_sharpe_nan_on_too_short(self):
        from backtest import _sharpe_manual

        assert math.isnan(_sharpe_manual([10_000.0])), "Single-point series → NaN"

    def test_sortino_uses_same_bar_annualization_as_sharpe(self):
        from backtest import _sortino_manual

        returns = np.array([0.01, -0.02, 0.03, -0.01, 0.005])
        equity = 10_000 * np.cumprod(np.r_[1.0, 1.0 + returns])
        expected = (
            float(np.mean(returns))
            / float(np.sqrt(np.mean(np.square(np.minimum(returns, 0.0)))))
            * math.sqrt(1764)
        )
        assert _sortino_manual(list(equity), annualization=1764) == pytest.approx(
            expected
        )

    def test_metrics_uses_365_annualization(self):
        """metrics['annualization'] must be 365 and sharpe_nautilus is separate."""
        from unittest.mock import MagicMock

        from backtest import _metrics

        positions_df = _fake_positions_df([50.0, 80.0, -20.0, 60.0])

        engine = MagicMock()
        engine.portfolio.analyzer.get_performance_stats_returns.return_value = {
            "Sharpe Ratio (252 days)": 1.5
        }
        engine.portfolio.analyzer.get_performance_stats_general.return_value = {}
        engine.portfolio.analyzer.currencies = []
        engine.trader.generate_order_fills_report.return_value = pd.DataFrame()

        metrics = _metrics(engine, positions_df)
        assert metrics["annualization"] == 365
        assert "sharpe_nautilus" in metrics
        assert metrics["sharpe_nautilus"] == pytest.approx(1.5, abs=1e-6)
        # H610: when there is NO MTM curve, primary sharpe = sharpe_nautilus
        # (frequency-correct 252-day) — NOT the bar-annualized manual sharpe
        # (which inflated ~725x on 1m). Without this pin, that regression would
        # leave sharpe_nautilus/annualization correct while silently breaking
        # the primary 'sharpe'.
        assert metrics["sharpe"] == pytest.approx(metrics["sharpe_nautilus"], abs=1e-6)

    def test_metrics_mtm_uses_matching_sharpe_and_sortino_annualization(self):
        from unittest.mock import MagicMock

        from backtest import _metrics, _sharpe_manual, _sortino_manual

        positions_df = _fake_positions_df([100.0, -20.0, 50.0])
        engine = MagicMock()
        engine.portfolio.analyzer.get_performance_stats_returns.return_value = {
            "Sharpe Ratio (252 days)": 0.1,
            "Sortino Ratio (252 days)": 0.2,
        }
        engine.portfolio.analyzer.get_performance_stats_general.return_value = {}
        engine.portfolio.analyzer.currencies = []
        engine.trader.generate_order_fills_report.return_value = pd.DataFrame()
        mtm = [10_000.0, 10_100.0, 9_900.0, 10_200.0, 10_150.0, 10_300.0]

        metrics = _metrics(engine, positions_df, mtm_equity=mtm, annualization=1764)

        assert metrics["sharpe"] == pytest.approx(_sharpe_manual(mtm, 1764))
        assert metrics["sortino"] == pytest.approx(_sortino_manual(mtm, 1764))
        assert metrics["sortino_nautilus"] == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Test 3 — Fee estimate lowers PnL
# ---------------------------------------------------------------------------


class TestFeeConstants:
    """Fee constants must exist and commission estimate must reduce PnL."""

    def test_fee_constants_exist(self):
        from backtest import BYBIT_MAKER_FEE_BPS, BYBIT_TAKER_FEE_BPS, SLIPPAGE_BPS

        assert BYBIT_TAKER_FEE_BPS > 0, "Taker fee must be positive"
        assert BYBIT_MAKER_FEE_BPS >= 0, "Maker fee must be non-negative"
        assert SLIPPAGE_BPS > 0, "Slippage must be positive"

    def test_commission_reduces_pnl(self):
        """When positions have commission data, net PnL should be lower."""
        from unittest.mock import MagicMock

        from backtest import _metrics

        # Positions without commission
        positions_no_fee = _fake_positions_df([100.0, 100.0])
        # Positions with commission (50 USDT total)
        positions_with_fee = positions_no_fee.copy()
        positions_with_fee["commissions"] = [["25.0 USDT"], ["25.0 USDT"]]

        engine = MagicMock()
        engine.portfolio.analyzer.get_performance_stats_returns.return_value = {}
        engine.portfolio.analyzer.get_performance_stats_general.return_value = {}
        engine.portfolio.analyzer.currencies = []
        engine.trader.generate_order_fills_report.return_value = pd.DataFrame()

        _metrics(engine, positions_no_fee)  # no-fee baseline (not asserted)
        m_with_fee = _metrics(engine, positions_with_fee)

        assert m_with_fee["commission_total"] > 0, "Commission should be > 0"
        assert m_with_fee["commission_total"] == pytest.approx(50.0, abs=0.01)

    def test_slippage_model_has_auditable_nonzero_estimate_when_report_is_zero(self):
        from unittest.mock import MagicMock

        from backtest import _metrics

        positions = _fake_positions_df([10.0, -2.0])
        engine = MagicMock()
        engine.portfolio.analyzer.get_performance_stats_returns.return_value = {}
        engine.portfolio.analyzer.get_performance_stats_general.return_value = {}
        engine.portfolio.analyzer.currencies = []
        engine.trader.generate_order_fills_report.return_value = pd.DataFrame(
            {"quantity": ["100", "50"], "slippage": [0.0, 0.0]}
        )
        metrics = _metrics(
            engine,
            positions,
            slippage_model_active=True,
            price_increment=0.01,
        )
        assert metrics["slippage_reported_total"] == 0
        assert metrics["slippage_estimated_total"] == pytest.approx(1.5)
        assert metrics["slippage_total"] == pytest.approx(1.5)
        assert metrics["slippage_fill_count"] == 2

    def test_slippage_computation_failure_is_logged_not_silent(self, caplog):
        """DeepR 2026-08-08 [ORTA]: this except used to be a bare `pass` — the
        exact failure class the surrounding comment warns about ($0 slippage
        reading as 'none' instead of 'the audit estimate broke')."""
        from unittest.mock import MagicMock

        from backtest import _metrics

        positions = _fake_positions_df([10.0, -2.0])
        engine = MagicMock()
        engine.portfolio.analyzer.get_performance_stats_returns.return_value = {}
        engine.portfolio.analyzer.get_performance_stats_general.return_value = {}
        engine.portfolio.analyzer.currencies = []
        engine.trader.generate_order_fills_report.side_effect = RuntimeError("boom")

        with caplog.at_level(logging.WARNING, logger="backtest"):
            metrics = _metrics(engine, positions, slippage_model_active=True)

        assert metrics["slippage_total"] == 0.0
        assert any(
            "slippage computation failed" in r.message.lower() for r in caplog.records
        )

    def test_commission_computation_failure_is_logged_not_silent(
        self, monkeypatch, caplog
    ):
        from unittest.mock import MagicMock

        from backtest import _metrics

        positions = _fake_positions_df([10.0, -2.0])
        engine = MagicMock()
        engine.portfolio.analyzer.get_performance_stats_returns.return_value = {}
        engine.portfolio.analyzer.get_performance_stats_general.return_value = {}
        engine.portfolio.analyzer.currencies = []
        engine.trader.generate_order_fills_report.return_value = pd.DataFrame()

        def _boom(self, *a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(pd.Series, "apply", _boom)

        with caplog.at_level(logging.WARNING, logger="backtest"):
            metrics = _metrics(engine, positions)

        assert metrics["commission_total"] == 0.0
        assert any(
            "commission_total computation failed" in r.message.lower()
            for r in caplog.records
        )

    def test_duration_computation_failure_is_logged_not_silent(
        self, monkeypatch, caplog
    ):
        from unittest.mock import MagicMock

        import backtest
        from backtest import _metrics

        positions = _fake_positions_df([10.0, -2.0])
        engine = MagicMock()
        engine.portfolio.analyzer.get_performance_stats_returns.return_value = {}
        engine.portfolio.analyzer.get_performance_stats_general.return_value = {}
        engine.portfolio.analyzer.currencies = []
        engine.trader.generate_order_fills_report.return_value = pd.DataFrame()

        def _boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(backtest.pd, "to_numeric", _boom)

        with caplog.at_level(logging.WARNING, logger="backtest"):
            metrics = _metrics(engine, positions)

        assert math.isnan(metrics["avg_duration_mins"])
        assert any(
            "avg_duration_mins computation failed" in r.message.lower()
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Test 4 — _parse_money_column: bad values warn, return 0.0
# ---------------------------------------------------------------------------


class TestParseMoneyColumn:
    """Bad values must return 0.0 and trigger a logging.warning."""

    def test_valid_values_parsed_correctly(self):
        from backtest import _parse_money_column

        s = pd.Series(["-3.38 USD", "12.50 USDT", "0.00 USD"])
        result = _parse_money_column(s)
        np.testing.assert_allclose(result, [-3.38, 12.50, 0.00], rtol=1e-6)

    def test_bad_value_returns_zero_and_warns(self, caplog):
        from backtest import _parse_money_column

        # Note: Python None in pd.Series becomes np.nan; str(np.nan)="nan" → float("nan")
        # is caught by ValueError and treated as 0.0 by _parse_money_column
        s = pd.Series(["100.0 USD", "NOT_A_NUMBER", "50.0 USDT"])
        with caplog.at_level(logging.WARNING, logger="backtest"):
            result = _parse_money_column(s)
        assert result[0] == pytest.approx(100.0)
        assert result[1] == pytest.approx(0.0), "Bad value should be 0.0"
        assert result[2] == pytest.approx(50.0)
        assert any("unparseable" in r.message.lower() for r in caplog.records), (
            "Expected a warning about unparseable values"
        )
