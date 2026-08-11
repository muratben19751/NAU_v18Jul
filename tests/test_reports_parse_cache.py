"""reports._PARSE_CACHE — RAM sınırı ve geçersizleşme (DeepR 2026-08-11 [ORTA]).

Bulgu: `_parsed_log_records()` 20 MB'lık backtest_log.jsonl'in TÜM parse
edilmiş hâlini modül global'inde süresiz tutuyordu — ölçüldü: 109 kayıt,
91,3 MB kalıcı heap. /reports'a bir kez girmek sunucunun ayak izini 91 MB
artırıyordu ve bir daha düşmüyordu.

Ölçüm RAM'in nereye gittiğini de gösterdi: `metrics` alanının 20,17 MB'ının
20,03 MB'ı gömülü equity eğrileri. Liste/filtre/CSV yollarının hiçbiri bu
eğrileri okumuyor; /reports/detail ise satırı diskten yeniden okuyor. Bu
yüzden eğriler parse'tan hemen sonra düşürülüyor — aynı satırlar, ~90 MB
daha az RAM. Sınıra çarpılırsa veri KIRPILMAZ, önbellekleme atlanır ve
durum loglanır.

Wiki References
---------------
Bkz: [[nau_performans_denetimi]] (2026-08-11 turu).
"""

from __future__ import annotations

import json
import logging


def _rec(ts: str, *, curve_points: int = 3, pnl: float = 1.0) -> dict:
    return {
        "ts": ts,
        "rationale": "manual",
        "bars": {"symbol": "BTCUSDT", "interval": "60"},
        "spec": {"name": "s"},
        "metrics": {
            "pnl": pnl,
            "starting_cash": 10_000,
            "sharpe": 1.2,
            "n_trades": 4,
            "equity_curve_mtm": [[i, 100.0 + i] for i in range(curve_points)],
            "equity_curve_realized": [[i, 100.0] for i in range(curve_points)],
        },
    }


def _write_log(tmp_path, records) -> object:
    p = tmp_path / "backtest_log.jsonl"
    p.write_text(
        "".join(json.dumps(r) + "\n" for r in records),
        encoding="utf-8",
    )
    return p


def _patch(monkeypatch, path):
    import web.routes.reports as rp

    monkeypatch.setattr(rp, "BACKTEST_LOG", path)
    rp._PARSE_CACHE.clear()
    return rp


class TestHeavyFieldsAreNotRetained:
    def test_embedded_equity_curves_are_dropped_from_the_cache(
        self, tmp_path, monkeypatch
    ):
        """Bulgunun kendisi: logun %99'u olan eğriler RAM'de tutulmamalı."""
        rp = _patch(
            monkeypatch,
            _write_log(tmp_path, [_rec("2026-08-01T00:00:00+00:00", curve_points=50)]),
        )

        (rec,) = rp._parsed_log_records()

        assert "equity_curve_mtm" not in rec["metrics"]
        assert "equity_curve_realized" not in rec["metrics"]

    def test_every_field_the_table_reads_survives_the_slimming(
        self, tmp_path, monkeypatch
    ):
        """Kırpma yalnız eğrileri almalı — satırın kalanı aynen durmalı."""
        rp = _patch(
            monkeypatch, _write_log(tmp_path, [_rec("2026-08-01T00:00:00+00:00")])
        )

        (rec,) = rp._parsed_log_records()

        assert rec["ts"] == "2026-08-01T00:00:00+00:00"
        assert rec["bars"]["symbol"] == "BTCUSDT"
        assert rec["spec"] == {"name": "s"}
        assert rec["metrics"]["pnl"] == 1.0
        assert rec["metrics"]["starting_cash"] == 10_000
        assert rec["metrics"]["sharpe"] == 1.2

    def test_a_record_without_curves_is_untouched(self, tmp_path, monkeypatch):
        plain = {"ts": "2026-08-01T00:00:00+00:00", "metrics": {"pnl": 2.0}}
        rp = _patch(monkeypatch, _write_log(tmp_path, [plain]))

        (rec,) = rp._parsed_log_records()

        assert rec["metrics"] == {"pnl": 2.0}

    def test_the_rendered_rows_are_identical_to_the_unslimmed_ones(
        self, tmp_path, monkeypatch
    ):
        """Kırpma GÖRÜNÜR çıktıyı değiştirmemeli (davranış korunumu)."""
        rp = _patch(
            monkeypatch,
            _write_log(
                tmp_path,
                [
                    _rec("2026-08-01T00:00:00+00:00", pnl=5.0),
                    _rec("2026-08-02T00:00:00+00:00", pnl=-3.0),
                ],
            ),
        )
        slim_rows = rp._load_rows()

        # Aynı log, kırpma kapalıyken.
        rp._PARSE_CACHE.clear()
        monkeypatch.setattr(rp, "_slim_record", lambda rec: rec)
        full_rows = rp._load_rows()

        assert slim_rows == full_rows


class TestCacheHitMissAndInvalidation:
    def test_hit_an_unchanged_log_is_not_reparsed(self, tmp_path, monkeypatch):
        rp = _patch(
            monkeypatch, _write_log(tmp_path, [_rec("2026-08-01T00:00:00+00:00")])
        )
        first = rp._parsed_log_records()

        parses = {"n": 0}
        real_loads = json.loads

        def _counting_loads(s, *a, **k):
            parses["n"] += 1
            return real_loads(s, *a, **k)

        monkeypatch.setattr(rp.json, "loads", _counting_loads)
        second = rp._parsed_log_records()

        assert second is first
        assert parses["n"] == 0

    def test_miss_a_new_run_appended_to_the_log_invalidates_it(
        self, tmp_path, monkeypatch
    ):
        """GEÇERSİZLEŞME: anahtar (mtime_ns, size); append ikisini de değiştirir."""
        p = _write_log(tmp_path, [_rec("2026-08-01T00:00:00+00:00")])
        rp = _patch(monkeypatch, p)
        assert len(rp._parsed_log_records()) == 1

        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(_rec("2026-08-02T00:00:00+00:00")) + "\n")

        assert len(rp._parsed_log_records()) == 2

    def test_missing_log_is_not_fatal(self, tmp_path, monkeypatch):
        rp = _patch(monkeypatch, tmp_path / "nope.jsonl")
        assert rp._parsed_log_records() == []


class TestRecordCap:
    def test_over_the_cap_data_is_complete_but_caching_is_skipped_and_logged(
        self, tmp_path, monkeypatch, caplog
    ):
        """SINIR: tavan aşılınca satır KIRPILMAZ; sadece önbellek atlanır + log.

        Sessiz kırpma raporu eksik gösterirdi — bir denetim ekranında en
        kötü hata türü. Bunun yerine maliyet ödenir ama görünür olur.
        """
        rp = _patch(
            monkeypatch,
            _write_log(
                tmp_path,
                [
                    _rec(f"2026-08-01T00:00:{i:02d}+00:00", curve_points=1)
                    for i in range(5)
                ],
            ),
        )
        monkeypatch.setattr(rp, "_PARSE_CACHE_MAX_RECORDS", 3)

        with caplog.at_level(logging.WARNING):
            recs = rp._parsed_log_records()

        assert len(recs) == 5, "sınır satırları sessizce kırpmış"
        assert "backtest_log" not in rp._PARSE_CACHE
        assert any("parse cache SKIPPED" in r.message for r in caplog.records)

    def test_at_or_under_the_cap_the_cache_is_used(self, tmp_path, monkeypatch):
        rp = _patch(
            monkeypatch,
            _write_log(
                tmp_path,
                [
                    _rec(f"2026-08-01T00:00:{i:02d}+00:00", curve_points=1)
                    for i in range(3)
                ],
            ),
        )
        monkeypatch.setattr(rp, "_PARSE_CACHE_MAX_RECORDS", 3)

        rp._parsed_log_records()

        assert "backtest_log" in rp._PARSE_CACHE
