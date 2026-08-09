"""chart_indicators.py -- zero test coverage before this file existed
(DeepR 2026-08-09 [DÜŞÜK]). Covers the pure indicator math (_sma/_ema/_rsi/
_bollinger/_series) and every spec-block branch indicators_for_spec()
dispatches on, including the cross-block-type dedup (ma_cross/ema_cross/
macd_cross share one fingerprint namespace) and the rsi_threshold
entry+exit same-period pane-reuse path.

Wiki References
---------------
See: [[nau_deepr_ucuncu_tur_2026_08_09]]
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

import chart_indicators as ci


def _block(type_, **params):
    return SimpleNamespace(type=type_, params=params)


def _spec(*blocks):
    return SimpleNamespace(blocks=list(blocks))


class TestSma:
    def test_pads_none_before_the_window_is_full(self):
        out = ci._sma([1.0, 2.0, 3.0, 4.0], 3)
        assert out[:2] == [None, None]

    def test_matches_a_hand_computed_average(self):
        out = ci._sma([1.0, 2.0, 3.0, 4.0, 5.0], 3)
        assert out[2] == pytest.approx(2.0)  # (1+2+3)/3
        assert out[3] == pytest.approx(3.0)  # (2+3+4)/3
        assert out[4] == pytest.approx(4.0)  # (3+4+5)/3

    def test_period_zero_or_negative_returns_all_none(self):
        assert ci._sma([1.0, 2.0, 3.0], 0) == [None, None, None]
        assert ci._sma([1.0, 2.0, 3.0], -1) == [None, None, None]

    def test_empty_closes_returns_empty(self):
        assert ci._sma([], 5) == []


class TestEma:
    def test_seeds_with_the_sma_of_the_first_period(self):
        out = ci._ema([1.0, 2.0, 3.0, 4.0], 3)
        assert out[:2] == [None, None]
        assert out[2] == pytest.approx(2.0)  # SMA(1,2,3)

    def test_subsequent_values_blend_with_the_smoothing_constant(self):
        closes = [1.0, 2.0, 3.0, 10.0]
        out = ci._ema(closes, 3)
        k = 2.0 / (3 + 1)
        expected = 10.0 * k + 2.0 * (1 - k)  # seed (index 2) was SMA=2.0
        assert out[3] == pytest.approx(expected)

    def test_insufficient_data_returns_all_none(self):
        assert ci._ema([1.0, 2.0], 3) == [None, None]

    def test_period_zero_or_negative_returns_all_none(self):
        assert ci._ema([1.0, 2.0, 3.0], 0) == [None, None, None]


class TestRsi:
    def test_all_gains_saturates_at_100(self):
        out = ci._rsi([1.0, 2.0, 3.0, 4.0, 5.0], 4)
        assert out[4] == pytest.approx(100.0)

    def test_all_losses_bottoms_at_0(self):
        out = ci._rsi([5.0, 4.0, 3.0, 2.0, 1.0], 4)
        assert out[4] == pytest.approx(0.0)

    def test_matches_wilders_formula_on_a_mixed_window(self):
        # period=2 over [10, 11, 10.5]: d1=+1 (gain), d2=-0.5 (loss) -- one of
        # each, so avg_gain/avg_loss are independently known from Wilder's own
        # definition (average of the first `period` gains/losses), not just
        # re-derived from the implementation under test.
        out = ci._rsi([10.0, 11.0, 10.5], 2)
        avg_gain, avg_loss = 1.0 / 2, 0.5 / 2
        expected = 100 - 100 / (1 + avg_gain / avg_loss)
        assert out[2] == pytest.approx(expected)

    def test_insufficient_data_returns_all_none(self):
        assert ci._rsi([1.0, 2.0], 5) == [None, None]

    def test_period_zero_or_negative_returns_all_none(self):
        assert ci._rsi([1.0, 2.0, 3.0], 0) == [None, None, None]


class TestBollinger:
    def test_mid_band_is_the_sma(self):
        closes = [1.0, 1.0, 1.0, 5.0]
        mid, _upper, _lower = ci._bollinger(closes, 4, 2.0)
        assert mid == ci._sma(closes, 4)

    def test_upper_and_lower_bands_match_a_hand_computed_stddev(self):
        closes = [1.0, 1.0, 1.0, 5.0]  # mean=2, variance=(3*1+9)/4=3
        mid, upper, lower = ci._bollinger(closes, 4, 2.0)
        sd = math.sqrt(3.0)
        assert mid[3] == pytest.approx(2.0)
        assert upper[3] == pytest.approx(2.0 + 2.0 * sd)
        assert lower[3] == pytest.approx(2.0 - 2.0 * sd)

    def test_bands_are_none_before_the_window_is_full(self):
        _mid, upper, lower = ci._bollinger([1.0, 2.0, 3.0], 5, 2.0)
        assert upper == [None, None, None]
        assert lower == [None, None, None]


class TestSeries:
    def test_drops_none_values(self):
        out = ci._series([1, 2, 3], [1.0, None, 3.0])
        assert out == [{"time": 1, "value": 1.0}, {"time": 3, "value": 3.0}]

    def test_rounds_to_eight_decimals_for_low_priced_instruments(self):
        # round(v, 4) used to flatten sub-1e-4 instruments (SHIB/PEPE) to 0.0.
        out = ci._series([1], [0.000012345678])
        assert out == [{"time": 1, "value": 0.00001235}]

    def test_empty_inputs_return_empty(self):
        assert ci._series([], []) == []


class TestIndicatorsForSpecMaCross:
    def test_default_periods_are_10_and_30(self):
        closes = [float(i) for i in range(40)]
        times = list(range(40))
        result = ci.indicators_for_spec(_spec(_block("ma_cross")), times, closes)

        names = {o["name"] for o in result["overlays"]}
        assert names == {"SMA10", "SMA30"}
        assert result["panes"] == []

    def test_ema_cross_and_macd_cross_default_to_12_and_26(self):
        closes = [float(i) for i in range(40)]
        times = list(range(40))
        result = ci.indicators_for_spec(_spec(_block("ema_cross")), times, closes)

        names = {o["name"] for o in result["overlays"]}
        assert names == {"EMA12", "EMA26"}

    def test_ema_cross_and_macd_cross_share_one_fingerprint_namespace(self):
        """Both render with _ema and neither's fingerprint includes the block
        type (`fp = f"EMA{period}"`), so a macd_cross at the same period as an
        existing ema_cross must NOT draw a second, redundant line."""
        closes = [float(i) for i in range(40)]
        times = list(range(40))
        result = ci.indicators_for_spec(
            _spec(
                _block("ema_cross", fast=12, slow=26),
                _block("macd_cross", fast=12, slow=26),
            ),
            times,
            closes,
        )

        names = [o["name"] for o in result["overlays"]]
        assert names == ["EMA12", "EMA26"]  # not duplicated

    def test_custom_periods_are_respected(self):
        closes = [float(i) for i in range(40)]
        times = list(range(40))
        result = ci.indicators_for_spec(
            _spec(_block("ma_cross", fast=5, slow=20)), times, closes
        )

        names = {o["name"] for o in result["overlays"]}
        assert names == {"SMA5", "SMA20"}


class TestIndicatorsForSpecRsiThreshold:
    def test_creates_one_pane_with_the_threshold_and_midline_refs(self):
        closes = [float(i) for i in range(20)]
        times = list(range(20))
        result = ci.indicators_for_spec(
            _spec(_block("rsi_threshold", period=14, threshold=30.0)), times, closes
        )

        assert len(result["panes"]) == 1
        pane = result["panes"][0]
        assert pane["label"] == "RSI(14)"
        assert [s["name"] for s in pane["series"]] == ["RSI14"]
        assert pane["refs"][0] == {"value": 30.0, "color": "rgba(239,68,68,0.5)"}
        assert pane["refs"][1]["value"] == 50

    def test_entry_and_exit_blocks_at_the_same_period_share_one_pane(self):
        """Common pattern: entry<30 + exit>70 on the same RSI(period) -- the
        second block must add a ref to the existing pane, not a new one."""
        closes = [float(i) for i in range(20)]
        times = list(range(20))
        result = ci.indicators_for_spec(
            _spec(
                _block("rsi_threshold", period=14, threshold=30.0),
                _block("rsi_threshold", period=14, threshold=70.0),
            ),
            times,
            closes,
        )

        assert len(result["panes"]) == 1
        refs = result["panes"][0]["refs"]
        assert {"value": 30.0, "color": "rgba(239,68,68,0.5)"} in refs
        assert {"value": 70.0, "color": "rgba(239,68,68,0.5)"} in refs

    def test_different_periods_get_separate_panes(self):
        closes = [float(i) for i in range(20)]
        times = list(range(20))
        result = ci.indicators_for_spec(
            _spec(
                _block("rsi_threshold", period=14, threshold=30.0),
                _block("rsi_threshold", period=7, threshold=20.0),
            ),
            times,
            closes,
        )

        assert len(result["panes"]) == 2


class TestIndicatorsForSpecBollingerBreak:
    def test_creates_mid_upper_lower_overlays(self):
        closes = [float(i % 5) for i in range(30)]
        times = list(range(30))
        result = ci.indicators_for_spec(
            _spec(_block("bollinger_break", period=20, k=2.0)), times, closes
        )

        names = [o["name"] for o in result["overlays"]]
        assert names == ["BB20 mid", "BB upper", "BB lower"]

    def test_same_period_and_k_is_not_duplicated(self):
        closes = [float(i % 5) for i in range(30)]
        times = list(range(30))
        result = ci.indicators_for_spec(
            _spec(
                _block("bollinger_break", period=20, k=2.0),
                _block("bollinger_break", period=20, k=2.0),
            ),
            times,
            closes,
        )

        assert len(result["overlays"]) == 3  # not 6


class TestIndicatorsForSpecPriceBreakout:
    def test_creates_high_low_channel_overlays(self):
        closes = [float(i) for i in range(30)]
        times = list(range(30))
        result = ci.indicators_for_spec(
            _spec(_block("price_breakout", lookback=10)), times, closes
        )

        names = [o["name"] for o in result["overlays"]]
        assert names == ["10-bar high", "10-bar low"]

    def test_nonpositive_lookback_is_ignored(self):
        closes = [float(i) for i in range(30)]
        times = list(range(30))
        result = ci.indicators_for_spec(
            _spec(_block("price_breakout", lookback=0)), times, closes
        )

        assert result["overlays"] == []


class TestIndicatorsForSpecUnhandledBlocks:
    def test_atr_stop_and_momentum_draw_nothing(self):
        """Documented scope gap: no overlay is meaningful for a trailing stop
        or a lookback-only momentum block."""
        closes = [float(i) for i in range(30)]
        times = list(range(30))
        result = ci.indicators_for_spec(
            _spec(_block("atr_stop"), _block("momentum")), times, closes
        )

        assert result == {"overlays": [], "panes": []}

    def test_empty_spec_draws_nothing(self):
        result = ci.indicators_for_spec(_spec(), [], [])
        assert result == {"overlays": [], "panes": []}


class TestColorCycling:
    def test_distinct_overlays_get_distinct_colors(self):
        closes = [float(i) for i in range(60)]
        times = list(range(60))
        result = ci.indicators_for_spec(
            _spec(_block("ma_cross", fast=5, slow=30)), times, closes
        )

        colors = [o["color"] for o in result["overlays"]]
        assert len(colors) == len(set(colors)) == 2  # SMA5 and SMA30 differ
