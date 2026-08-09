"""data._read_ticker_registry (DeepR 2026-08-09 [DÜŞÜK]).

discover_index_tickers() and _index_rows_uncached() used to each parse
TICKER_REGISTRY independently -- one shared helper now backs both. Locks in
two things: (1) both callers still behave the same on the happy path, and
(2) a corrupt registry file is now non-fatal for _index_rows_uncached() too
(previously it let json.JSONDecodeError propagate; discover_index_tickers()
already treated a corrupt file as "fall through and rescan").

Wiki References
---------------
See: [[nau_deepr_ucuncu_tur_2026_08_09]]
"""

from __future__ import annotations

import json

import data


class TestReadTickerRegistry:
    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "TICKER_REGISTRY", tmp_path / "missing.json")

        assert data._read_ticker_registry() is None

    def test_valid_file_returns_its_tickers(self, tmp_path, monkeypatch):
        reg = tmp_path / "_tickers.json"
        reg.write_text(json.dumps({"tickers": ["AAPL", "QQQ"]}))
        monkeypatch.setattr(data, "TICKER_REGISTRY", reg)

        assert data._read_ticker_registry() == ["AAPL", "QQQ"]

    def test_empty_tickers_list_returns_none(self, tmp_path, monkeypatch):
        reg = tmp_path / "_tickers.json"
        reg.write_text(json.dumps({"tickers": []}))
        monkeypatch.setattr(data, "TICKER_REGISTRY", reg)

        assert data._read_ticker_registry() is None

    def test_corrupt_json_returns_none_instead_of_raising(self, tmp_path, monkeypatch):
        reg = tmp_path / "_tickers.json"
        reg.write_text("{not valid json")
        monkeypatch.setattr(data, "TICKER_REGISTRY", reg)

        assert data._read_ticker_registry() is None


class TestDiscoverIndexTickersUsesTheSharedReader:
    def test_a_cached_registry_is_returned_without_rescanning(
        self, tmp_path, monkeypatch
    ):
        reg = tmp_path / "_tickers.json"
        reg.write_text(json.dumps({"tickers": ["AAPL", "QQQ"]}))
        monkeypatch.setattr(data, "TICKER_REGISTRY", reg)

        def _boom():
            raise AssertionError("must not rescan when a valid cache exists")

        monkeypatch.setattr(data, "_latest_available_file", _boom)

        assert data.discover_index_tickers() == ["AAPL", "QQQ"]

    def test_force_true_bypasses_the_cache_even_if_valid(self, tmp_path, monkeypatch):
        reg = tmp_path / "_tickers.json"
        reg.write_text(json.dumps({"tickers": ["STALE"]}))
        monkeypatch.setattr(data, "TICKER_REGISTRY", reg)
        monkeypatch.setattr(data, "_latest_available_file", lambda: None)

        try:
            data.discover_index_tickers(force=True)
        except FileNotFoundError:
            pass  # expected: force=True skips the cache and hits the "no source" path
        else:
            raise AssertionError(
                "expected FileNotFoundError from the forced rescan path"
            )

    def test_a_corrupt_registry_falls_through_to_rescan(self, tmp_path, monkeypatch):
        reg = tmp_path / "_tickers.json"
        reg.write_text("{not valid json")
        monkeypatch.setattr(data, "TICKER_REGISTRY", reg)
        monkeypatch.setattr(data, "_latest_available_file", lambda: None)

        try:
            data.discover_index_tickers()
        except FileNotFoundError:
            pass  # a corrupt cache must not short-circuit the rescan fallback
        else:
            raise AssertionError("expected FileNotFoundError from the rescan path")


class TestIndexRowsUncachedUsesTheSharedReader:
    def test_missing_registry_returns_empty_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "TICKER_REGISTRY", tmp_path / "missing.json")

        assert data._index_rows_uncached() == []

    def test_corrupt_registry_returns_empty_list_instead_of_raising(
        self, tmp_path, monkeypatch
    ):
        reg = tmp_path / "_tickers.json"
        reg.write_text("{not valid json")
        monkeypatch.setattr(data, "TICKER_REGISTRY", reg)

        assert data._index_rows_uncached() == []
