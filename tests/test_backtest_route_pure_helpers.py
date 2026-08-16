"""Pure(ish) helpers in web/routes/backtest.py with zero prior unit-test
coverage (DeepR 2026-08-08 #59): `_is_equity_target`/`_local_fallback_breakdown`
(#59-B1), `_predict_plan_warnings` (#59-B2), and `_preview_signals` (#59-B3)
are already plain, single-call-site module functions (called only from
`plan_preview()`) — the gap is test coverage, not architecture. No
production code changes in this pass; see #59's decomposition plan
(deferred: `run()`'s 3 instrument-kind branches, `describe()`'s worker body,
unifying `_preview_signals` with composer.py's `_eval_*` evaluators — a real
duplication, but the two operate on different object models and can't
trivially share code).

`test_plan_preview.py` already covers #59-B2's rules (1) allow_short and
(2) AND-3+ well via HTTP; this file adds direct unit coverage for all 5
rules in isolation, including the two (Bollinger-cross, no-recognized-
indicator) that HTTP testing left thin.

#59-B3's fixtures for `_preview_signals` were derived EMPIRICALLY: the
composite price series below was run against the real function first (not
hand-derived from reading the source) to discover which bar index each
signal actually fires at — this file's assertions describe verified
behavior, not a guess at what the algorithm should do. Notably this
surfaced that `price_breakout` fires "20-bar düşük kırılımı" (a SHORT) at
bar 16 before any long fires — which became the allow_short-suppression
test case, since suppressing it reveals the first LONG breakout instead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import web.routes.backtest as bt


class TestMatchBuiltin:
    """`_INDICATOR_TO_BUILTIN`'in anahtarları `agent._HINT_INDICATORS`'ın kanonik
    adları OLMAK ZORUNDA — aksi halde `_match_builtin` sessizce None döner ve
    kullanıcıya var olan builtin yerine 'yeni custom blok yazılacak' denir.
    (Regresyon: hacim ailesinin anahtarı `"Hacim"` yazılmıştı, kanonik ad `"Volume"`.)
    """

    def test_volume_condition_matches_volume_spike_builtin(self):
        m = bt._match_builtin("entry", "Hacim patlaması", "volume spike üzerine giriş")
        assert m is not None
        assert m["type"] == "volume_spike"

    def test_every_key_is_a_canonical_hint_indicator_name(self):
        from agent import _HINT_INDICATORS

        unknown = set(bt._INDICATOR_TO_BUILTIN) - set(_HINT_INDICATORS)
        assert not unknown, f"kanonik olmayan anahtar(lar): {sorted(unknown)}"

    def test_every_value_is_a_real_builtin_block(self):
        unknown = set(bt._INDICATOR_TO_BUILTIN.values()) - set(bt.BLOCK_CATALOG)
        assert not unknown, f"katalogda olmayan blok tip(ler)i: {sorted(unknown)}"

    def test_atr_stop_is_exit_only(self):
        assert bt._match_builtin("entry", "ATR stop", "ATR tabanlı çıkış") is None
        m = bt._match_builtin("exit", "ATR stop", "ATR tabanlı çıkış")
        assert m is not None and m["type"] == "atr_stop"

    def test_no_recognized_indicator_returns_none(self):
        assert bt._match_builtin("entry", "Bir şey", "kârlı bir giriş") is None


class TestIsEquityTarget:
    def test_all_empty_bybit_is_not_equity(self):
        assert bt._is_equity_target("bybit", "linear", "", "") is False

    def test_empty_instrument_kind_no_ticker_is_not_equity(self):
        assert bt._is_equity_target("", "", "", "") is False

    def test_ticker_set_is_equity_regardless_of_kind(self):
        assert bt._is_equity_target("bybit", "linear", "AAPL", "") is True

    def test_ext_instrument_set_is_equity_regardless_of_kind(self):
        assert bt._is_equity_target("bybit", "linear", "", "AAPL.NASDAQ") is True

    def test_index_kind_is_equity(self):
        assert bt._is_equity_target("index", "", "", "") is True

    def test_external_kind_is_equity(self):
        assert bt._is_equity_target("external", "", "", "") is True

    def test_us_index_kind_is_equity(self):
        assert bt._is_equity_target("us-index", "", "", "") is True

    def test_kind_match_is_case_insensitive(self):
        assert bt._is_equity_target("INDEX", "", "", "") is True
        assert bt._is_equity_target("External", "", "", "") is True
        assert bt._is_equity_target("US-INDEX", "", "", "") is True

    def test_none_kind_does_not_raise(self):
        assert bt._is_equity_target(None, "", "", "") is False


class TestLocalFallbackBreakdown:
    def test_no_indicators_uses_truncated_description_as_label(self):
        long_desc = "x" * 100
        bd = bt._local_fallback_breakdown(long_desc, [])
        assert bd["label"] == long_desc[:40]

    def test_no_indicators_and_blank_description_uses_placeholder(self):
        bd = bt._local_fallback_breakdown("   ", [])
        assert bd["label"] == "Described strategy"

    def test_first_indicator_used_as_label_when_present(self):
        bd = bt._local_fallback_breakdown("some description", ["rsi_threshold"])
        assert bd["label"] == "rsi_threshold-based strategy"

    def test_fixed_shape(self):
        bd = bt._local_fallback_breakdown("desc", ["ma_cross"])
        assert bd["entry_logic"] == "OR"
        assert bd["exit_logic"] == "OR"
        assert bd["usage"] == {}
        assert len(bd["conditions"]) == 2
        roles = [c["role"] for c in bd["conditions"]]
        assert roles == ["entry", "exit"]
        assert bd["conditions"][0]["desc"] == "desc"


def _rows(n_entry=1, reuse=True):
    return [{"role": "entry", "reuse": reuse} for _ in range(n_entry)]


def _bd(entry_logic="OR"):
    return {"entry_logic": entry_logic}


class TestPredictPlanWarnings:
    def test_no_rule_fires_returns_empty_list(self):
        w = bt._predict_plan_warnings(
            "simple rsi strategy",
            _bd(),
            _rows(n_entry=1, reuse=True),
            "bybit",
            "BTCUSDT",
            "linear",
            "",
            "",
            allow_short=True,
        )
        assert w == []

    def test_rule1_allow_short_off_plus_short_language_warns(self):
        w = bt._predict_plan_warnings(
            "go short when RSI is overbought",
            _bd(),
            _rows(reuse=True),
            "bybit",
            "BTCUSDT",
            "linear",
            "",
            "",
            allow_short=False,
        )
        assert any(x["level"] == "info" and "short" in x["text"].lower() for x in w)

    def test_rule1_does_not_fire_when_allow_short_is_on(self):
        w = bt._predict_plan_warnings(
            "go short when RSI is overbought",
            _bd(),
            _rows(reuse=True),
            "bybit",
            "BTCUSDT",
            "linear",
            "",
            "",
            allow_short=True,
        )
        assert not any("short" in x["text"].lower() for x in w)

    def test_rule2_and_with_3plus_entries_warns(self):
        w = bt._predict_plan_warnings(
            "rsi and volume and macd",
            _bd(entry_logic="AND"),
            _rows(n_entry=3, reuse=True),
            "bybit",
            "BTCUSDT",
            "linear",
            "",
            "",
        )
        assert any(x["level"] == "warn" and "AND" in x["text"] for x in w)

    def test_rule2_does_not_fire_below_3_entries(self):
        w = bt._predict_plan_warnings(
            "rsi and volume",
            _bd(entry_logic="AND"),
            _rows(n_entry=2, reuse=True),
            "bybit",
            "BTCUSDT",
            "linear",
            "",
            "",
        )
        assert not any("AND" in x["text"] for x in w)

    def test_rule3_equity_target_warns_about_size_precision(self):
        w = bt._predict_plan_warnings(
            "simple strategy",
            _bd(),
            _rows(reuse=True),
            "index",
            "",
            "",
            "",
            "",
        )
        assert any("size_precision" in x["text"] for x in w)

    def test_rule4_bollinger_cross_warns(self):
        w = bt._predict_plan_warnings(
            "buy on bollinger band cross",
            _bd(),
            _rows(reuse=True),
            "bybit",
            "BTCUSDT",
            "linear",
            "",
            "",
        )
        assert any("Bollinger" in x["text"] for x in w)

    def test_rule4_bollinger_without_cross_language_does_not_warn(self):
        w = bt._predict_plan_warnings(
            "buy near lower bollinger band",
            _bd(),
            _rows(reuse=True),
            "bybit",
            "BTCUSDT",
            "linear",
            "",
            "",
        )
        assert not any("Bollinger" in x["text"] for x in w)

    def test_rule5_no_recognized_indicator_warns(self):
        w = bt._predict_plan_warnings(
            "some vague custom idea",
            _bd(),
            _rows(n_entry=1, reuse=False),
            "bybit",
            "BTCUSDT",
            "linear",
            "",
            "",
        )
        assert any(
            x["level"] == "info" and "custom from scratch" in x["text"] for x in w
        )

    def test_rule5_does_not_fire_when_any_row_is_reused(self):
        w = bt._predict_plan_warnings(
            "rsi strategy",
            _bd(),
            [{"role": "entry", "reuse": True}, {"role": "entry", "reuse": False}],
            "bybit",
            "BTCUSDT",
            "linear",
            "",
            "",
        )
        assert not any("custom from scratch" in x["text"] for x in w)

    def test_multiple_rules_fire_together(self):
        w = bt._predict_plan_warnings(
            "short on bollinger band cross and rsi and volume",
            _bd(entry_logic="AND"),
            _rows(n_entry=3, reuse=False),
            "index",
            "",
            "",
            "",
            "",
            allow_short=False,
        )
        levels_texts = [(x["level"], x["text"]) for x in w]
        assert len(w) == 5  # all five rules fire at once
        assert any("short" in t.lower() for _, t in levels_texts)
        assert any("AND" in t for _, t in levels_texts)
        assert any("size_precision" in t for _, t in levels_texts)
        assert any("Bollinger" in t for _, t in levels_texts)
        assert any("custom from scratch" in t for _, t in levels_texts)


def _composite_bars(n=500):
    """Four-phase synthetic BTC 1m series, empirically verified (see module
    docstring) to fire all 7 built-in entry types + both exit types within
    `_preview_signals(..., n_bars=490)`:
      A (0-99):   tight oscillation (warmup)
      B (100-199): steady decline (RSI oversold, MA/EMA decline)
      C (200-399): steady rally (MA/EMA cross up, new highs, momentum, one
                   volume spike at bar 250)
      D (400-499): tight range, then one violent single-bar crash at the
                   very end (Bollinger lower-band break)
    """
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    close = np.zeros(n)
    close[0] = 100.0
    for i in range(1, 100):
        close[i] = close[i - 1] + np.sin(i / 5.0) * 0.05
    for i in range(100, 200):
        close[i] = close[i - 1] - 0.35
    for i in range(200, 400):
        close[i] = close[i - 1] + 0.5
    for i in range(400, 499):
        close[i] = close[i - 1] + np.sin(i) * 0.02
    close[499] = close[498] - 15.0
    high = close + 0.5
    low = close - 0.5
    vol = np.full(n, 10.0)
    vol[250] = 50.0
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


@pytest.fixture
def bybit_cache(tmp_path, monkeypatch):
    """Points data.BYBIT_CACHE_DIR (the name _preview_signals imports
    locally) at a tmp_path holding the composite series as a real parquet
    file, and returns a `rows_for(entry_type=..., exit_type=...)` builder."""
    import data

    monkeypatch.setattr(data, "BYBIT_CACHE_DIR", tmp_path)
    _composite_bars().to_parquet(tmp_path / "linear_BTCUSDT_1m.parquet")

    def rows_for(entry_type=None, exit_type=None):
        rows = []
        if entry_type:
            rows.append({"role": "entry", "reuse": {"type": entry_type}})
        if exit_type:
            rows.append({"role": "exit", "reuse": {"type": exit_type}})
        return rows

    return rows_for


class TestPreviewSignalsEdgeCases:
    def test_missing_cache_file_returns_none(self, tmp_path, monkeypatch):
        import data

        monkeypatch.setattr(data, "BYBIT_CACHE_DIR", tmp_path)  # no file written
        assert bt._preview_signals([{"role": "entry"}], allow_short=True) is None

    def test_fewer_than_40_bars_returns_none(self, tmp_path, monkeypatch):
        import data

        monkeypatch.setattr(data, "BYBIT_CACHE_DIR", tmp_path)
        idx = pd.date_range("2024-01-01", periods=10, freq="1min", tz="UTC")
        small = pd.DataFrame(
            {
                "open": [100.0] * 10,
                "high": [101.0] * 10,
                "low": [99.0] * 10,
                "close": [100.0] * 10,
                "volume": [10.0] * 10,
            },
            index=idx,
        )
        small.to_parquet(tmp_path / "linear_BTCUSDT_1m.parquet")
        rows = [{"role": "entry", "reuse": {"type": "momentum"}}]
        assert bt._preview_signals(rows, allow_short=True) is None

    def test_no_matched_builtin_returns_none(self, bybit_cache):
        assert bt._preview_signals(bybit_cache(), allow_short=True) is None


class TestPreviewSignalsEntryTypes:
    """Each assertion's expected (bar, signal, reason) was read off a real
    run of _preview_signals against `_composite_bars()`, not hand-derived —
    see the module docstring."""

    def _first_fire(self, bybit_cache, entry_type, allow_short=True):
        res = bt._preview_signals(
            bybit_cache(entry_type=entry_type), allow_short=allow_short, n_bars=490
        )
        fired = [s for s in res["signals"] if s["sig"] in ("long", "short")]
        return fired[0] if fired else None

    def test_rsi_threshold(self, bybit_cache):
        s = self._first_fire(bybit_cache, "rsi_threshold")
        assert s["sig"] == "long"
        assert "RSI<30" in s["reason"]

    def test_ma_cross(self, bybit_cache):
        s = self._first_fire(bybit_cache, "ma_cross")
        assert s["sig"] == "long"
        assert "MA yukarı kesişim" in s["reason"]

    def test_ema_cross(self, bybit_cache):
        s = self._first_fire(bybit_cache, "ema_cross")
        assert s["sig"] == "long"
        assert "EMA yukarı kesişim" in s["reason"]

    def test_bollinger_break(self, bybit_cache):
        s = self._first_fire(bybit_cache, "bollinger_break")
        assert s["sig"] == "long"
        assert "Bollinger" in s["reason"]

    def test_volume_spike(self, bybit_cache):
        s = self._first_fire(bybit_cache, "volume_spike")
        assert s["sig"] == "long"
        assert "hacim" in s["reason"]

    def test_momentum(self, bybit_cache):
        s = self._first_fire(bybit_cache, "momentum")
        assert s["sig"] == "long"
        assert "momentum" in s["reason"]

    def test_price_breakout_short_fires_first_when_shorts_allowed(self, bybit_cache):
        s = self._first_fire(bybit_cache, "price_breakout", allow_short=True)
        assert s["sig"] == "short"
        assert "düşük kırılımı" in s["reason"]

    def test_price_breakout_allow_short_false_skips_to_the_long_break(
        self, bybit_cache
    ):
        """allow_short suppression, proven on real (not synthetic-toggle)
        data: the natural first fire is a SHORT (previous test) — turning
        allow_short off doesn't just delete that signal, it reveals the
        next LONG opportunity the short had been masking."""
        s = self._first_fire(bybit_cache, "price_breakout", allow_short=False)
        assert s["sig"] == "long"
        assert "yüksek kırılımı" in s["reason"]


class TestPreviewSignalsExits:
    def test_atr_stop_exit_fires_and_does_not_raise(self, bybit_cache):
        res = bt._preview_signals(
            bybit_cache(entry_type="momentum", exit_type="atr_stop"),
            allow_short=True,
            n_bars=490,
        )
        exits = [s for s in res["signals"] if s["sig"] == "exit"]
        assert exits
        assert exits[0]["reason"] == "3x ATR stop"

    def test_rsi_overbought_exit_fires(self, bybit_cache):
        res = bt._preview_signals(
            bybit_cache(entry_type="momentum", exit_type="rsi_threshold"),
            allow_short=True,
            n_bars=490,
        )
        exits = [s for s in res["signals"] if s["sig"] == "exit"]
        assert exits
        assert "RSI>70" in exits[0]["reason"]


class TestPreviewSignalsOverlaysAndPanes:
    def test_rsi_gets_a_separate_pane_not_a_price_overlay(self, bybit_cache):
        res = bt._preview_signals(
            bybit_cache(entry_type="rsi_threshold"), allow_short=True, n_bars=490
        )
        assert res["overlays"] == []
        assert [p["label"] for p in res["panes"]] == ["RSI 14"]

    def test_ma_cross_gets_price_overlays_not_a_pane(self, bybit_cache):
        res = bt._preview_signals(
            bybit_cache(entry_type="ma_cross"), allow_short=True, n_bars=490
        )
        assert [o["label"] for o in res["overlays"]] == ["MA 10", "MA 30"]
        assert res["panes"] == []

    def test_unmatched_types_produce_no_overlay_or_pane(self, bybit_cache):
        res = bt._preview_signals(
            bybit_cache(entry_type="momentum"), allow_short=True, n_bars=490
        )
        assert res["overlays"] == []
        assert res["panes"] == []
