"""Regresyon çapaları (kalan HIGH boşluklar) — suite analizinin top_gaps #13-16'sı.
Pür-birim + hafif-stub çapalar; parquet/gerçek-motor fixture'ları için bkz.
test_regression_anchors_e2e.py.

Kapsanan:
- #14a  wfo_aggregate skorer (oos_sharpe_penalized = mean-0.5·std, efficiency,
        param_cv kararsızlık dalı, boş → {})  [current-output pin]
- #14b  optimize_window fold-kabul kapısı (M462: geçerli-fold oranı >= 0.6)
- #13   agent_backtest _robustness_passed + _ms_score_factor (winner gate)
- #16   parallel_exec BacktestPool.run_units timeout dalı (M300 done-future toplama)
- #15   strategies STRATEGY_REGISTRY (H7) + indicators.py sayısal değerler [pin]
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from test_parallel_exec import pool  # noqa: F401  (pytest fixture)

import parallel_exec as PE
import wfo_optimizer as W
from composer import ComposedStrategySpec, SignalBlock


# ===========================================================================
# #14a (high) — wfo_aggregate scorer aggregation math (current-output pin)
# ===========================================================================
class TestWfoAggregateScorer:
    def _windows(self):
        # İki pencere: test sharpe [1.0, 3.0] → mean=2.0, popülasyon std=1.0 →
        # penalized = 2.0 - 0.5·1.0 = 1.5. param 'fast'[10,12]→cv=1/11=0.091
        # (kararlı), 'slow'[10,40]→cv=15/25=0.6 (>0.5, kararsız). train_obj mean
        # 2.0 → efficiency = 2/2 = 1.0.
        return [
            {
                "test_metrics": {"sharpe": 1.0},
                "chosen_params": {"fast": 10, "slow": 10},
                "train_objective": 2.0,
            },
            {
                "test_metrics": {"sharpe": 3.0},
                "chosen_params": {"fast": 12, "slow": 40},
                "train_objective": 2.0,
            },
        ]

    def test_penalized_and_stability(self):
        from backtest_robustness import wfo_aggregate

        r = wfo_aggregate(self._windows())
        assert r["oos_sharpe_penalized"] == 1.5  # mean - 0.5·std (M28 dağılım cezası)
        assert r["wfo_efficiency"] == 1.0  # OOS/IS verimliliği
        assert r["param_cv"] == {"fast": 0.091, "slow": 0.6}  # std/|mean|
        assert r["unstable_params"] == ["slow"]  # cv>0.5 dalı
        assert r["stability_label"] == "kararsız (overfit riski)"

    def test_empty_returns_empty_dict(self):
        from backtest_robustness import wfo_aggregate

        assert wfo_aggregate([]) == {}


# ===========================================================================
# #14b (high, M462) — optimize_window fold-kabul kapısı (>= WF_MIN_VALID_FOLDS_FRAC)
# ===========================================================================
def _fg_spec():
    return ComposedStrategySpec(
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


def _fg_bars():
    idx = pd.date_range("2023-01-01", periods=60, freq="D")
    return pd.DataFrame({"close": np.arange(60.0)}, index=idx)


def _fg_stub(n_invalid):
    # Fold sırasına göre ilk n_invalid fold'u -inf (n_trades=0 → eşik altı),
    # kalanları geçerli (sharpe=1.0, n_trades=10) yapan stub run_fn.
    calls = {"i": 0}

    def stub(cand, bars, **kw):
        calls["i"] += 1
        if calls["i"] <= n_invalid:
            return SimpleNamespace(
                error=None, metrics={"sharpe_per_trade": 5.0, "n_trades": 0}
            )
        return SimpleNamespace(
            error=None, metrics={"sharpe_per_trade": 1.0, "n_trades": 10}
        )

    return stub


class TestSharpePerTradeAlignment:
    """NAU paritesi: composite/objektif 0.3 terimi PER-TRADE sharpe kullanır
    (annualized 252-gün 'sharpe' DEĞİL). Annualized'a dönüş bu testleri kırar."""

    def test_objective_value_uses_per_trade_sharpe(self):
        from wfo_optimizer import WFO_TRADE_CONF_K, objective_value

        # sharpe (annualized)=8.0 ≠ sharpe_per_trade=2.0 → per-trade seçilmeli.
        res = SimpleNamespace(
            error=None,
            metrics={"sharpe": 8.0, "sharpe_per_trade": 2.0, "n_trades": 100},
        )
        v = objective_value(res, "sharpe")
        conf = 100 / (100 + WFO_TRADE_CONF_K)
        assert v == pytest.approx(2.0 * conf)  # 8.0*conf olsaydı annualized'dı

    def test_score_uses_per_trade_sharpe(self):
        import math

        import web.routes.agent_backtest as ab

        # pnl_pct=0 → calmar=0 → base = 0.3*clamp(sharpe_term). sharpe_per_trade=2.0
        # kullanılırsa base=0.6; annualized sharpe=8.0 kullanılsa base=2.4 olurdu.
        res = SimpleNamespace(
            error=None,
            metrics={
                "sharpe": 8.0,
                "sharpe_per_trade": 2.0,
                "n_trades": 100,
                "max_dd": -0.2,
                "pnl_pct": 0.0,
            },
        )
        score = ab._score(res)
        conf = 100 / (100 + 20)
        assert score == pytest.approx(0.3 * 2.0 * conf)
        assert not math.isclose(score, 0.3 * 8.0 * conf)  # annualized DEĞİL


class TestFoldAcceptanceGate:
    def test_two_of_three_valid_survives(self):
        # 2/3 = 0.667 >= 0.6 → aday SKORLANIR (yaşar). space=[] → tek aday, 3 fold.
        bars = _fg_bars()
        assert len(W.build_fold_bounds(bars)) == 3  # 3-fold önkoşulu
        best = W.optimize_window(
            _fg_spec(), bars, [], None, None, None, run_fn=_fg_stub(1)
        )
        # İki geçerli fold: obj = 1.0 × 10/(10+20) = 1/3; penalized = mean-0.5·std = 1/3.
        assert best["objective"] != float("-inf")
        assert best["objective"] == pytest.approx(1.0 * 10 / 30)

    def test_one_of_three_valid_rejected(self):
        # 1/3 = 0.333 < 0.6 → aday -inf (reddedilir); best naive fallback'e düşer.
        bars = _fg_bars()
        assert len(W.build_fold_bounds(bars)) == 3
        best = W.optimize_window(
            _fg_spec(), bars, [], None, None, None, run_fn=_fg_stub(2)
        )
        assert best["objective"] == float("-inf")


# ===========================================================================
# #13 (high) — agent_backtest winner gate: _robustness_passed + _ms_score_factor
# ===========================================================================
class TestRobustnessPassed:
    """run_id=None ile çağrılır → _add_step (I/O) tetiklenmez, saf mantık test edilir."""

    def _clean(self):
        # 3 kriter GERÇEKTEN değerlendirilir (IS/OOS, WFO, MC), hiçbiri failed değil.
        return {
            "split": {"overfitting_label": "✓ Sağlam"},
            "wfo_windows": [{"test_n_trades": 5}],
            "oos_sharpe_penalized": 1.0,
            "mc": {"max_dd_p50": -10.0},
        }

    def test_clean_three_eval_strict_passes(self):
        import web.routes.agent_backtest as ab

        assert ab._robustness_passed(self._clean(), strict=True) is True

    def test_two_eval_strict_fails_relaxed_passes(self):
        import web.routes.agent_backtest as ab

        rob = {"split": {"overfitting_label": "✓ Sağlam"}, "mc": {"max_dd_p50": -10.0}}
        assert ab._robustness_passed(rob, strict=True) is False  # gereken ≥3
        assert ab._robustness_passed(rob, strict=False) is True  # gereken ≥2

    def test_failed_is_oos_label_fails(self):
        import web.routes.agent_backtest as ab

        rob = self._clean()
        rob["split"] = {"overfitting_label": "✗ aşırı-uyum"}
        assert ab._robustness_passed(rob, strict=True) is False

    def test_monte_carlo_median_dd_below_limit_fails(self):
        import web.routes.agent_backtest as ab

        assert ab._MC_DD_LIMIT == -25.0
        rob = self._clean()
        rob["mc"] = {"max_dd_p50": -30.0}  # < -25.0 → failed
        assert ab._robustness_passed(rob, strict=True) is False

    def test_monte_carlo_median_dd_above_limit_ok(self):
        import web.routes.agent_backtest as ab

        rob = self._clean()
        rob["mc"] = {"max_dd_p50": -20.0}  # > -25.0 → geçer
        assert ab._robustness_passed(rob, strict=True) is True

    def test_penalized_sharpe_non_positive_fails(self):
        import web.routes.agent_backtest as ab

        rob = self._clean()
        rob["oos_sharpe_penalized"] = -0.5  # pen <= 0 → WFO failed
        assert ab._robustness_passed(rob, strict=True) is False

    def test_empty_or_error_fails(self):
        import web.routes.agent_backtest as ab

        assert ab._robustness_passed({}, strict=True) is False
        assert ab._robustness_passed({"error": "boom"}, strict=True) is False


class TestMsScoreFactor:
    """multi-symbol pass_rate → çarpan ∈ [0.15, 1.0] (current-output pin)."""

    def test_pass_rate_one_gives_ceiling(self):
        import web.routes.agent_backtest as ab

        rob = {
            "multi_symbol": {
                "pass_rate": 1.0,
                "generalization_label": "✓",
                "n_valid": 5,
            }
        }
        assert ab._ms_score_factor(rob) == 1.0

    def test_real_zero_pass_rate_hits_floor(self):
        import web.routes.agent_backtest as ab

        rob = {
            "multi_symbol": {
                "pass_rate": 0.0,
                "generalization_label": "✗ sembol-spesifik",
                "n_valid": 5,
            }
        }
        assert ab._ms_score_factor(rob) == 0.15

    def test_insufficient_data_is_neutral(self):
        import web.routes.agent_backtest as ab

        # M653: pass_rate=0.0 ama 'yetersiz veri' → DEĞERLENDİRİLEMEDİ → nötr 0.575.
        rob = {
            "multi_symbol": {
                "pass_rate": 0.0,
                "generalization_label": "— (yetersiz veri)",
                "n_valid": 0,
            }
        }
        assert ab._ms_score_factor(rob) == 0.575

    def test_missing_and_none_are_neutral(self):
        import web.routes.agent_backtest as ab

        assert ab._ms_score_factor({}) == 0.575
        assert ab._ms_score_factor(None) == 0.575


# ===========================================================================
# #16 (high, M300/M23) — BacktestPool.run_units timeout dalı: done-but-unyielded
# future'lar toplanır (düşürülmez), süresi dolan unit in-band 'unit timeout' olur,
# havuz yeniden kurulup sonraki batch'i kabul eder.
# ===========================================================================
_PROBE_HELPER_SRC = """
def probe_run_unit(unit):
    import time
    s = float(unit.get("_probe_sleep", 0.0))
    if s:
        time.sleep(s)
    return {"key": unit["key"], "metrics": {"ret": 1.0}, "error": None, "n_trades": 3}
"""


class TestRunUnitsTimeout:
    def test_timeout_collects_done_and_rebuilds_pool(self, pool, monkeypatch):  # noqa: F811
        # spawn worker'ları repo kökünden import edebilsin diye yardımcı modülü
        # diske yaz (probe_run_unit tek başına importlanabilir, ağır import yok).
        repo = Path(__file__).resolve().parents[1]
        helper = repo / "_probe_unit_tmp.py"
        helper.write_text(_PROBE_HELPER_SRC, encoding="utf-8")
        try:
            import _probe_unit_tmp  # repo kökünden importlanabilir

            monkeypatch.setattr(PE, "_run_unit", _probe_unit_tmp.probe_run_unit)

            # PARENT tarafını timeout bütçesinin ÜSTÜNE kadar durdur: ilk hızlı
            # future yield edildikten sonra progress_cb burada uyurken kalan hızlı
            # future'lar "done ama yield edilmemiş" durumuna düşer → M300 dalı.
            def _stall_cb(done, total, key):
                time.sleep(3.5)

            units = [
                {"key": "slow", "_probe_sleep": 7.0},  # timeout'u aşar
                {"key": "f1"},
                {"key": "f2"},
                {"key": "f3"},
                {"key": "f4"},
            ]
            out = pool.run_units(units, progress_cb=_stall_cb, timeout_s=2.0)

            # (1) hiçbir tamamlanmış anahtar düşmedi
            assert set(out) == {"slow", "f1", "f2", "f3", "f4"}
            # (2) hızlı unit'ler GERÇEK payload taşıyor (M300 revert → timeout payload → patlar)
            for k in ("f1", "f2", "f3", "f4"):
                assert out[k]["metrics"] == {"ret": 1.0}, k
                assert not out[k].get("error"), k
            # (3) yavaş unit in-band timeout
            assert "unit timeout" in out["slow"]["error"]
            assert out["slow"]["metrics"] is None
            # (4) havuz yeniden kuruldu → sonraki batch kabul ediliyor
            out2 = pool.run_units([{"key": "g1"}, {"key": "g2"}], timeout_s=30.0)
            assert set(out2) == {"g1", "g2"}
            assert out2["g1"]["metrics"] == {"ret": 1.0}
            assert out2["g2"]["metrics"] == {"ret": 1.0}
        finally:
            helper.unlink(missing_ok=True)


# ===========================================================================
# #15 (high) — strategies STRATEGY_REGISTRY (H7 RSI 0-100) + indicators sayısal
# değerler. Harici referans yok → CURRENT-OUTPUT pin (şekil/ölçek sessizce
# değişirse kırılır). sharpe rel=1e-6 (platform mikro-farkına toleranslı, ama
# H7 ölçek bozulması mertebe değiştirir → yine yakalanır).
# ===========================================================================
def _si_bars(n: int = 400) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    t = np.arange(n)
    close = 100.0 + t * 0.05 + 8.0 * np.sin(t / 6.0) + 3.0 * np.sin(t / 2.0)
    open_ = np.empty(n)
    open_[0] = close[0]
    open_[1:] = close[:-1]
    high = np.maximum(open_, close) + 0.5
    low = np.minimum(open_, close) - 0.5
    vol = 1000.0 + (t % 10) * 10.0
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


class TestStrategyRegistryPins:
    def test_rsi_mean_reversion_pins(self):
        import backtest

        res = backtest.run_backtest(
            "rsi_mean_reversion", {}, _si_bars(400), iteration_id=0
        )
        assert res.error is None, res.error
        m = res.metrics
        assert m["n_trades"] == 11  # H7: RSI*100 rescale — ölçek bozulursa değişir
        assert m["pnl"] == pytest.approx(-0.13661076, rel=1e-6)
        assert m["sharpe"] == pytest.approx(-2.0041147573630287, rel=1e-6)

    def test_ma_crossover_pins(self):
        import backtest

        res = backtest.run_backtest("ma_crossover", {}, _si_bars(400), iteration_id=0)
        assert res.error is None, res.error
        m = res.metrics
        assert m["n_trades"] == 10
        assert m["pnl"] == pytest.approx(-1.4179974, rel=1e-6)
        assert m["sharpe"] == pytest.approx(-214.7339474225819, rel=1e-6)


class TestIndicatorNumericPins:
    @staticmethod
    def _series(n: int = 200):
        t = np.arange(n)
        closes = list(100.0 + t * 0.1 + 5.0 * np.sin(t / 5.0) + 2.0 * np.cos(t / 3.0))
        highs = [c + 1.0 for c in closes]
        lows = [c - 1.0 for c in closes]
        return closes, highs, lows

    def test_sma_ema_last(self):
        import indicators as ind

        closes, _, _ = self._series()
        assert ind.sma(closes, 20)[-1] == pytest.approx(119.29586603727049, rel=1e-9)
        assert ind.ema(closes, 20)[-1] == pytest.approx(120.51901191761092, rel=1e-9)

    def test_calc_adx(self):
        import indicators as ind

        closes, highs, lows = self._series()
        res = ind.calc_adx(highs, lows, closes, 14)
        assert res is not None
        assert res["adx"] == pytest.approx(36.87078688830347, rel=1e-9)
        assert res["plusDI"] == pytest.approx(19.380090683068175, rel=1e-9)
        assert res["minusDI"] == pytest.approx(9.83576978357438, rel=1e-9)

    def test_calc_stoch_rsi(self):
        import indicators as ind

        closes, _, _ = self._series()
        res = ind.calc_stoch_rsi(closes)
        assert res["k"] == pytest.approx(66.93895043205295, rel=1e-9)
        assert res["d"] == pytest.approx(79.69412409354591, rel=1e-9)

    def test_calc_wave_trend(self):
        import indicators as ind

        closes, highs, lows = self._series()
        res = ind.calc_wave_trend(highs, lows, closes)
        assert res is not None
        assert res["wt1"] == pytest.approx(33.64094572363672, rel=1e-9)
        assert res["wt2"] == pytest.approx(36.40738736594005, rel=1e-9)
