"""_index_rows() tembel TTL önbelleği (DeepR 2026-08-09 → 2026-08-11 [ORTA]).

08-09 turu `_bybit_rows`'un 30 sn'lik TTL desenini buraya taşıdı ama YANLIŞ
ŞEYİ önbelleğe aldı: tüm ticker evreninin satırları kuruluyor, `limit`/`query`
yalnız BİTMİŞ listeye uygulanıyordu. /data 50 satır istiyor; ölçüldü: 10.000
tickerlık bir registry ile bu 1826 ms sürüyor ve 65,8 MB'ı sunucu ömrü boyunca
modül global'inde tutuyordu (10k × 5 granularity = 50.000 hücre, her biri bir
parquet-footer stat'ı + katalog dizin glob'u).

Şimdi limit/query önce TICKER LİSTESİNE uygulanıyor (kardeşi
`_external_catalog_rows` zaten böyle) ve önbellek tembel: yalnız gerçekten
gösterilen tickerlar kurulur ve saklanır. Çağıranın gördüğü sonuç aynı.

refresh_row() önbelleği geçersizleştirmeye devam etmeli — yoksa zorla
tazeleme, tazeleme ÖNCESİ satırı geri verir.

Wiki References
---------------
See: [[nau_deepr_ucuncu_tur_2026_08_09]], [[nau_performans_denetimi]]
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import data


@pytest.fixture(autouse=True)
def _reset_cache():
    data._invalidate_index_rows_cache()
    yield
    data._invalidate_index_rows_cache()


@pytest.fixture()
def registry(monkeypatch):
    """Sahte ticker registry + her ticker için ucuz bir satır kurucusu."""
    tickers = ["QQQ", "SPY", "IWM"]
    monkeypatch.setattr(data, "_read_ticker_registry", lambda: list(tickers))

    built: list[list[str]] = []

    def _fake_uncached(wanted=None):
        wanted = list(tickers) if wanted is None else list(wanted)
        built.append(wanted)
        return [{"source": "index", "key": t, "generation": len(built)} for t in wanted]

    monkeypatch.setattr(data, "_index_rows_uncached", _fake_uncached)
    return {"tickers": tickers, "built": built}


class TestOnlyTheVisibleTickersPayForIO:
    """Bulgunun kendisi: 50 satır göstermek tüm evreni kurmamalı."""

    def test_limit_narrows_the_ticker_list_before_the_io(self, registry):
        rows = data._index_rows(limit=2)

        assert [r["key"] for r in rows] == ["QQQ", "SPY"]
        assert registry["built"] == [["QQQ", "SPY"]], (
            "limit uygulanmadan ÖNCE tüm evren kuruldu"
        )

    def test_query_narrows_the_ticker_list_before_the_io(self, registry):
        rows = data._index_rows(query="qq")

        assert [r["key"] for r in rows] == ["QQQ"]
        assert registry["built"] == [["QQQ"]]

    def test_no_filters_still_returns_every_ticker(self, registry):
        rows = data._index_rows()

        assert [r["key"] for r in rows] == ["QQQ", "SPY", "IWM"]

    def test_only_the_visible_rows_are_retained(self, registry):
        data._index_rows(limit=1)

        _stamp, by_ticker = data._index_rows_cache
        assert list(by_ticker) == ["QQQ"], "gösterilmeyen tickerlar da RAM'de tutuldu"


class TestTtlCaching:
    def test_second_call_within_the_ttl_reuses_the_cached_rows(self, registry):
        first = data._index_rows(limit=2)
        second = data._index_rows(limit=2)

        assert len(registry["built"]) == 1
        assert [r["key"] for r in first] == [r["key"] for r in second]
        assert first[0] is second[0]

    def test_a_widened_page_only_builds_the_newly_visible_tickers(self, registry):
        data._index_rows(limit=1)
        rows = data._index_rows(limit=3)

        assert [r["key"] for r in rows] == ["QQQ", "SPY", "IWM"]
        assert registry["built"] == [["QQQ"], ["SPY", "IWM"]], (
            "zaten kurulmuş ticker yeniden kuruldu"
        )

    def test_call_after_the_ttl_expires_recomputes(self, registry, monkeypatch):
        monkeypatch.setattr(data, "_INDEX_ROWS_TTL_S", 0.0)

        data._index_rows(limit=1)
        data._index_rows(limit=1)

        assert len(registry["built"]) == 2

    def test_invalidate_forces_an_immediate_recompute(self, registry):
        data._index_rows(limit=1)
        data._invalidate_index_rows_cache()
        data._index_rows(limit=1)

        assert len(registry["built"]) == 2

    def test_an_expired_window_drops_every_stale_row(self, registry, monkeypatch):
        """TTL dolunca sözlük tamamen atılmalı — bayat satır taşınmamalı."""
        data._index_rows()  # üç ticker de kurulsun
        monkeypatch.setattr(data, "_INDEX_ROWS_TTL_S", 0.0)

        rows = data._index_rows(limit=1)

        assert rows[0]["generation"] == 2
        _stamp, by_ticker = data._index_rows_cache
        assert list(by_ticker) == ["QQQ"]


class TestEmptyAndMissingRegistry:
    def test_a_missing_registry_yields_no_rows_and_no_io(self, monkeypatch):
        monkeypatch.setattr(data, "_read_ticker_registry", lambda: None)
        called = {"n": 0}
        monkeypatch.setattr(
            data,
            "_index_rows_uncached",
            lambda wanted=None: called.__setitem__("n", called["n"] + 1) or [],
        )

        assert data._index_rows(limit=50) == []
        assert called["n"] == 0

    def test_uncached_without_arguments_still_reads_the_registry(self, monkeypatch):
        """Doğrudan çağıranlar (ör. tests/test_ticker_registry_cache.py) için."""
        monkeypatch.setattr(data, "_read_ticker_registry", lambda: None)
        assert data._index_rows_uncached() == []


class TestRefreshRowBypassesTheCache:
    def test_index_refresh_never_returns_pre_refresh_cached_data(
        self, registry, monkeypatch
    ):
        """The regression this whole cache addition could have introduced:
        refresh_row() calls load_index_bars(force=True) then reads
        _index_rows() to hand the caller the updated row — if that read hit
        the TTL cache, a force-refresh would silently show stale data."""
        monkeypatch.setattr(data, "load_index_bars", MagicMock())

        # Prime the cache the way a normal /data page load would.
        primed = data._index_rows(limit=1)
        assert primed[0]["generation"] == 1

        row = data.refresh_row("index", ticker="QQQ", granularity="1d")

        assert row["generation"] == 2, "refresh_row returned the stale cached row"
        data.load_index_bars.assert_called_once()
        assert data.load_index_bars.call_args.kwargs["force"] is True
