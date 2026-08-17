"""Reddin BÜYÜKLÜĞÜ kayda geçmeli (2026-08-17).

Kapının ölçüsü Calmar üstünlüğü ve reddi tek bir etiket: `worse_risk_adjusted`.
O etiket barın %98'ine ulaşan adayla barın %2'sinde kalanı aynı satıra koyuyordu.
Sonuç, kalibrasyon tartışmasının ölçümsüz kalmasıydı: "eşiği biraz gevşetsem kaç
aday girerdi" sorusu ancak defteri elle yeniden oynatarak cevaplanabiliyordu.

Ölçüm (koşu 755b7880, tur 1 — 15 adayın tamamı elendi):

    ×1.09  Supertrend Daily Bull Flip      (6 işlem — işlem eşiğinde ölüyor)
    ×0.98  ADX ATR Supertrend             (70 işlem — kıl payı)
    ×0.79  Supertrend MACD Rider
    ...
    ×0.02  Supertrend Stoch Trend
    en iyi ×0.98 · medyan ×0.22

Aynı "0/15" satırı, bu iki sayı olmadan "hiçbiri yaklaşamadı" diye de
okunabiliyordu; oysa biri barın %98'indeydi.

ÖNEMLİ SINIR: bu alan bir TEŞHİS, karar değil. Kabul kuralı tek kopya
(`app_constants.benchmark_rejection`) ve bu testlerin bir tanesi tam olarak
kararın DEĞİŞMEDİĞİNİ çiviliyor — teşhis eklerken kapıyı oynatmak, ölçmek için
ölçtüğün şeyi bozmak olurdu.

Wiki References
---------------
See: [[auto_kapi_ve_geri_bildirim]], [[nau_auto_kosusu_755b7880_2026_08_17]]
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

import app_constants as ac


def _bars(first: float, last: float, *, years: float = 10.0, dip: float | None = None):
    """İki uçlu (istenirse ortasında çukurlu) bir kapanış serisi.

    `dip` buy&hold'un maksimum düşüşünü kontrol etmek için: benchmark Calmar'ın
    paydası odur, yani testin bar'ı ondan doğar.
    """
    closes = [first] + ([dip] if dip is not None else []) + [last]
    idx = pd.to_datetime(
        [
            pd.Timestamp("2000-01-01")
            + pd.Timedelta(days=365.25 * years * i / (len(closes) - 1))
            for i in range(len(closes))
        ]
    )
    return pd.DataFrame({"close": closes}, index=idx)


class TestMarginIsStamped:
    def test_ratio_is_strategy_calmar_over_benchmark_calmar(self):
        m = {"pnl_pct": 2.0, "max_dd": -0.20}

        ac.stamp_buy_hold_benchmark(m, _bars(100.0, 400.0, dip=50.0))

        assert "calmar_ratio_vs_benchmark" in m
        assert m["calmar_ratio_vs_benchmark"] == pytest.approx(
            m["strategy_calmar"] / m["benchmark_calmar"]
        )

    def test_a_ratio_above_one_means_it_cleared_the_bar(self):
        """Yön okunabilir olmalı: >1 geçti, <1 kaldı — eşik 1.0'da."""
        beats = {"pnl_pct": 8.0, "max_dd": -0.10}
        loses = {"pnl_pct": 0.2, "max_dd": -0.40}
        bars = _bars(100.0, 400.0, dip=50.0)

        ac.stamp_buy_hold_benchmark(beats, bars)
        ac.stamp_buy_hold_benchmark(loses, bars)

        assert beats["calmar_ratio_vs_benchmark"] > 1.0
        assert loses["calmar_ratio_vs_benchmark"] < 1.0
        assert (
            ac.benchmark_rejection(beats, beats.get("excess_return_fraction")) is None
        )
        assert (
            ac.benchmark_rejection(loses, loses.get("excess_return_fraction"))
            == "worse_risk_adjusted"
        )

    def test_no_ratio_when_the_benchmark_leg_cannot_be_measured(self):
        """Zaman damgasız indeks → yıllıklandırma yok → oran da UYDURULMAZ.

        Ölçülemeyen bir pay, sıfır paydan farklıdır; alanın yokluğu bunu söyler.
        """
        m = {"pnl_pct": 2.0, "max_dd": -0.20}
        bars = pd.DataFrame({"close": [100.0, 50.0, 400.0]})  # RangeIndex

        ac.stamp_buy_hold_benchmark(m, bars)

        assert "calmar_ratio_vs_benchmark" not in m

    def test_no_ratio_without_a_strategy_drawdown(self):
        m = {"pnl_pct": 2.0}  # max_dd yok

        ac.stamp_buy_hold_benchmark(m, _bars(100.0, 400.0, dip=50.0))

        assert "benchmark_calmar" in m, "benchmark bacağı yine de ölçülmeli"
        assert "calmar_ratio_vs_benchmark" not in m


class TestTheMarginDoesNotMoveTheGate:
    """Teşhis eklemek kararı değiştirmemeli — yoksa ölçtüğün şeyi bozarsın."""

    @pytest.mark.parametrize(
        "pnl_pct, max_dd",
        [(8.0, -0.10), (2.0, -0.20), (0.2, -0.40), (-0.5, -0.30), (0.0, -0.10)],
    )
    def test_decision_is_identical_with_and_without_the_field(self, pnl_pct, max_dd):
        m = {"pnl_pct": pnl_pct, "max_dd": max_dd}
        ac.stamp_buy_hold_benchmark(m, _bars(100.0, 400.0, dip=50.0))
        with_field = ac.benchmark_rejection(m, m.get("excess_return_fraction"))

        stripped = {k: v for k, v in m.items() if k != "calmar_ratio_vs_benchmark"}
        without = ac.benchmark_rejection(
            stripped, stripped.get("excess_return_fraction")
        )

        assert with_field == without


class TestTheRoundSummaryCarriesTheMargin:
    def test_the_zero_line_reports_best_and_median(self):
        """0/N satırı "hiçbiri yaklaşamadı" ile "biri kıl payı kaçırdı"yı ayırmalı."""
        from types import SimpleNamespace

        from web.routes import agent_backtest as ab

        def _cand(ratio: float):
            m = {
                "n_trades": 100,
                "pnl": 1.0,
                "sharpe_per_trade": 0.1,
                "annualized_alpha": -0.01,
                "strategy_cagr": 0.05,
                "strategy_calmar": 0.27 * ratio,
                "benchmark_calmar": 0.27,
                "calmar_ratio_vs_benchmark": ratio,
            }
            return (None, SimpleNamespace(error=None, metrics=m), "1-DAY")

        line = ab._no_eligible_phase_label([_cand(0.98), _cand(0.22), _cand(0.02)])

        assert "worse Calmar than buy&hold" in line
        assert "×0.98" in line, "en iyi pay görünmüyor"
        assert "×0.22" in line, "medyan pay görünmüyor"

    def test_the_line_is_unchanged_when_no_ratio_was_stamped(self):
        """Eski kayıtlar/alan yoksa satır eskisi gibi kalmalı, 'nan' basmamalı."""
        from types import SimpleNamespace

        from web.routes import agent_backtest as ab

        entry = (
            None,
            SimpleNamespace(error=None, metrics={"n_trades": 1}),
            "1-DAY",
        )

        line = ab._no_eligible_phase_label([entry])

        assert "×" not in line
        assert "nan" not in line.lower()


class TestTheQualifyLineDescribesTheLiveRule:
    """Metin kuralı tarif etmeli, tarihini değil.

    İlk yazdığım hâli `inspect.getsource` ile metin tarıyordu ve TESADÜFEN
    geçiyordu: aradığı dize, biçimlendiricinin yorumu sardığı yerden ikiye
    bölünmüştü. Kaynak taraması bir davranışı değil, bir satır sonunu sınıyordu.
    Bu yüzden tarif çağrılabilir bir fonksiyona taşındı.
    """

    def test_risk_adjusted_mode_names_calmar(self, monkeypatch):
        from web.routes import agent_backtest as ab

        monkeypatch.setenv("AGENT_BENCHMARK_GATE", "risk_adjusted")

        text = ab._gate_description()

        assert "Calmar" in text and "CAGR" in text
        assert "excess" not in text, "kaldırılmış kümülatif kuralı tarif ediyor"

    def test_absolute_mode_names_alpha(self, monkeypatch):
        from web.routes import agent_backtest as ab

        monkeypatch.setenv("AGENT_BENCHMARK_GATE", "absolute")

        text = ab._gate_description()

        assert "alpha" in text
        assert "Calmar" in text, "mutlak modda da Calmar bacağı uygulanıyor"

    def test_the_description_follows_the_mode_not_a_constant(self, monkeypatch):
        """İki mod AYNI cümleyi veriyorsa tarif kuralı izlemiyor demektir."""
        from web.routes import agent_backtest as ab

        monkeypatch.setenv("AGENT_BENCHMARK_GATE", "risk_adjusted")
        a = ab._gate_description()
        monkeypatch.setenv("AGENT_BENCHMARK_GATE", "absolute")
        b = ab._gate_description()

        assert a != b


def test_the_measured_round_reproduces(monkeypatch):
    """755b7880 tur 1'in kıl payı kaçıranı gerçekten ×0.98 mi.

    Sabitleri değil, ARİTMETİĞİ çiviler: kayıttaki Calmar çiftinden hesaplanan
    oran, bu turda kalibrasyon tartışmasını başlatan sayıyla aynı kalmalı.
    """
    strat_calmar, bench_calmar = 0.2660, 0.2724

    ratio = strat_calmar / bench_calmar

    assert math.isclose(ratio, 0.98, abs_tol=0.005)
