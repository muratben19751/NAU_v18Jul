"""Komisyon ayrıştırması: ayrıştırılamayan KALEM sessizce 0 sayılmaz.

DeepR 2026-08-11 [ORTA]: `_metrics`'in dıştaki komisyon handler'ı bir önceki
turda loglanır hâle getirilmişti, ama içteki iki handler hâlâ sessizdi —
liste yolunda `except Exception: pass`, skaler yolda `except Exception:
return 0.0`. İç handler hatayı yuttuğu için dıştaki LOGLAYAN handler hiç
tetiklenmiyordu: 5 komisyon kaleminin 2'si ayrıştırılamazsa toplam sessizce
eksik çıkıyor, net P&L olduğundan İYİ görünüyordu. Kapı kalibrasyonu tam da
net/brüt tutarlılığı üzerine kurulu olduğu için bu doğrudan karar bozan bir
hata.

Sözleşme (bu dosyanın kilitlediği):
* Gerçekten "komisyon yok" demek olan değerler (None/NaN/boş dize/boş liste)
  hata DEĞİLDİR — sessizce 0 katkı verir.
* Ayrıştırılamayan bir kalem sayılır (`commission_unparsed`), toplam ALT SINIR
  olarak işaretlenir (`commission_total_is_partial`) ve bir uyarı loglanır.

Wiki References
---------------
Bkz: [[auto_kapi_ve_geri_bildirim]]
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pandas as pd
import pytest

from backtest import _metrics


def _positions(commissions: list) -> pd.DataFrame:
    n = len(commissions)
    return pd.DataFrame(
        {
            "realized_pnl": [f"{100.0:.4f} USDT"] * n,
            "ts_closed": [int(1_700_000_000e9) + i * int(3600e9) for i in range(n)],
            "duration_ns": [int(1800e9)] * n,
            "commissions": commissions,
        }
    )


def _engine() -> MagicMock:
    engine = MagicMock()
    engine.portfolio.analyzer.get_performance_stats_returns.return_value = {}
    engine.portfolio.analyzer.get_performance_stats_general.return_value = {}
    engine.portfolio.analyzer.currencies = []
    engine.trader.generate_order_fills_report.return_value = pd.DataFrame()
    return engine


class TestUnparsableItemIsNotSilentlyZero:
    def test_one_bad_item_in_a_list_is_counted_and_flagged(self, caplog):
        """5 kalemin 2'si bozuk — toplam eksik ÇIKAR ama artık bunu SÖYLER."""
        positions = _positions(
            [
                ["1.0 USDT", "n/a", "2.0 USDT"],
                ["3.0 USDT", "???"],
            ]
        )

        with caplog.at_level(logging.WARNING, logger="backtest"):
            m = _metrics(_engine(), positions)

        # Ayrıştırılabilen kalemler toplanır (alt sınır), bozuklar 0 sayılmaz:
        assert m["commission_total"] == pytest.approx(6.0)
        assert m["commission_unparsed"] == 2
        assert m["commission_total_is_partial"] is True
        assert any(
            "commission" in r.message.lower() and "lower bound" in r.message.lower()
            for r in caplog.records
        )

    def test_scalar_unparsable_value_is_flagged_not_zeroed_silently(self, caplog):
        positions = _positions(["1.25 USDT", "garbage"])

        with caplog.at_level(logging.WARNING, logger="backtest"):
            m = _metrics(_engine(), positions)

        assert m["commission_total"] == pytest.approx(1.25)
        assert m["commission_unparsed"] == 1
        assert m["commission_total_is_partial"] is True

    def test_clean_run_reports_no_parse_damage(self):
        m = _metrics(_engine(), _positions([["25.0 USDT"], ["25.0 USDT"]]))

        assert m["commission_total"] == pytest.approx(50.0)
        assert m["commission_unparsed"] == 0
        assert m["commission_total_is_partial"] is False

    def test_genuinely_empty_commission_cells_are_not_parse_errors(self):
        """None/NaN/boş dize/boş liste = 'komisyon yok'. Bunları hata saymak
        her temiz koşuyu kırmızıya boyardı — bayrak o zaman hiçbir şey anlatmaz."""
        m = _metrics(_engine(), _positions([[], None, float("nan"), "", ["0 USDT"]]))

        assert m["commission_total"] == pytest.approx(0.0)
        assert m["commission_unparsed"] == 0
        assert m["commission_total_is_partial"] is False

    def test_outer_failure_also_marks_the_total_partial(self, monkeypatch, caplog):
        """Dıştaki handler (şema kayması) da toplamı 'güvenilir' bırakmamalı."""

        def _boom(self, *a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(pd.Series, "apply", _boom)

        with caplog.at_level(logging.WARNING, logger="backtest"):
            m = _metrics(_engine(), _positions([["1.0 USDT"]]))

        assert m["commission_total"] == 0.0
        assert m["commission_total_is_partial"] is True

    def test_empty_run_reports_the_flag_fields_too(self):
        """Boş koşumun erken dönüşü de aynı anahtarları taşımalı — tüketici
        `m['commission_total_is_partial']` diye okuyor, KeyError almamalı."""
        m = _metrics(_engine(), pd.DataFrame())

        assert m["commission_unparsed"] == 0
        assert m["commission_total_is_partial"] is False
