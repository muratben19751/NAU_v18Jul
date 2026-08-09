"""_index_rows() TTL cache (DeepR 2026-08-09 [ORTA]).

Same shape as _bybit_rows' TTL cache (tests/test_bybit_rows_cache.py,
DeepR 2026-08-08) -- every /data GET rebuilt every ticker's parquet-footer
stats + nautilus_catalog_bar_state from scratch, and limit/query only
narrowed the FINAL list, not the I/O. The cache holds the full, unfiltered
build; limit/query are applied to the cached rows on every call.
refresh_row() must invalidate it so a forced refresh doesn't hand back
stale pre-refresh data.

Wiki References
---------------
See: [[nau_deepr_ucuncu_tur_2026_08_09]]
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


class TestTtlCaching:
    def test_second_call_within_the_ttl_reuses_the_cached_result(self, monkeypatch):
        calls = {"n": 0}
        sentinel = [{"source": "index", "key": "QQQ"}]

        def _fake_uncached():
            calls["n"] += 1
            return sentinel

        monkeypatch.setattr(data, "_index_rows_uncached", _fake_uncached)

        first = data._index_rows()
        second = data._index_rows()

        assert calls["n"] == 1
        assert first is sentinel
        assert second is sentinel

    def test_call_after_the_ttl_expires_recomputes(self, monkeypatch):
        calls = {"n": 0}

        def _fake_uncached():
            calls["n"] += 1
            return [{"source": "index", "key": f"call-{calls['n']}"}]

        monkeypatch.setattr(data, "_index_rows_uncached", _fake_uncached)
        monkeypatch.setattr(data, "_INDEX_ROWS_TTL_S", 0.0)

        data._index_rows()
        data._index_rows()

        assert calls["n"] == 2

    def test_invalidate_forces_an_immediate_recompute(self, monkeypatch):
        calls = {"n": 0}

        def _fake_uncached():
            calls["n"] += 1
            return [{"source": "index", "key": f"call-{calls['n']}"}]

        monkeypatch.setattr(data, "_index_rows_uncached", _fake_uncached)

        data._index_rows()
        data._invalidate_index_rows_cache()
        data._index_rows()

        assert calls["n"] == 2


class TestLimitAndQueryFilterTheCachedRows:
    """limit/query used to narrow the ticker list BEFORE the expensive
    per-ticker I/O; now they filter the already-built (and cached) rows —
    behavior must stay identical from the caller's side."""

    @pytest.fixture(autouse=True)
    def _stub_rows(self, monkeypatch):
        calls = {"n": 0}

        def _fake_uncached():
            calls["n"] += 1
            return [
                {"source": "index", "key": "QQQ"},
                {"source": "index", "key": "SPY"},
                {"source": "index", "key": "IWM"},
            ]

        monkeypatch.setattr(data, "_index_rows_uncached", _fake_uncached)
        self.calls = calls

    def test_query_filters_case_insensitively_without_recomputing(self):
        rows = data._index_rows(query="qq")

        assert [r["key"] for r in rows] == ["QQQ"]
        assert self.calls["n"] == 1  # the stub ran once, not once per filter

    def test_limit_truncates_without_recomputing(self):
        rows = data._index_rows(limit=2)

        assert [r["key"] for r in rows] == ["QQQ", "SPY"]
        assert self.calls["n"] == 1

    def test_no_filters_returns_every_cached_row(self):
        rows = data._index_rows()

        assert [r["key"] for r in rows] == ["QQQ", "SPY", "IWM"]


class TestRefreshRowBypassesTheCache:
    def test_index_refresh_never_returns_pre_refresh_cached_data(self, monkeypatch):
        """The regression this whole cache addition could have introduced:
        refresh_row() calls load_index_bars(force=True) then reads
        _index_rows() to hand the caller the updated row — if that read hit
        the TTL cache, a force-refresh would silently show stale data."""
        calls = {"n": 0}

        def _fake_uncached():
            calls["n"] += 1
            return [{"source": "index", "key": "QQQ", "generation": calls["n"]}]

        monkeypatch.setattr(data, "_index_rows_uncached", _fake_uncached)
        monkeypatch.setattr(data, "load_index_bars", MagicMock())

        # Prime the cache the way a normal /data page load would.
        primed = data._index_rows()
        assert primed[0]["generation"] == 1

        row = data.refresh_row("index", ticker="QQQ", granularity="1d")

        assert row["generation"] == 2, "refresh_row returned the stale cached row"
        data.load_index_bars.assert_called_once()
        assert data.load_index_bars.call_args.kwargs["force"] is True
