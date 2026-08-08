"""_bybit_rows() TTL cache (DeepR 2026-08-08 [DÜŞÜK]).

Every /data GET rebuilt all 3 symbols × 3 categories × 8 intervals = 72
cells from scratch (a parquet-footer stat + a catalog dir-glob per cell).
Already off the event loop, so never a blocking bug — just repeated disk
I/O on every page visit. A short TTL cache fixes that; `refresh_row()` must
invalidate it so a forced refresh doesn't hand back stale pre-refresh data.

Wiki References
---------------
See: [[nau_deepr_toplu_sertlestirme_2026_08]]
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import data


@pytest.fixture(autouse=True)
def _reset_cache():
    data._invalidate_bybit_rows_cache()
    yield
    data._invalidate_bybit_rows_cache()


class TestTtlCaching:
    def test_second_call_within_the_ttl_reuses_the_cached_result(self, monkeypatch):
        calls = {"n": 0}
        sentinel = [{"source": "bybit", "key": "BTCUSDT"}]

        def _fake_uncached():
            calls["n"] += 1
            return sentinel

        monkeypatch.setattr(data, "_bybit_rows_uncached", _fake_uncached)

        first = data._bybit_rows()
        second = data._bybit_rows()

        assert calls["n"] == 1
        assert first is sentinel
        assert second is sentinel

    def test_call_after_the_ttl_expires_recomputes(self, monkeypatch):
        calls = {"n": 0}

        def _fake_uncached():
            calls["n"] += 1
            return [{"source": "bybit", "key": f"call-{calls['n']}"}]

        monkeypatch.setattr(data, "_bybit_rows_uncached", _fake_uncached)
        monkeypatch.setattr(data, "_BYBIT_ROWS_TTL_S", 0.0)

        data._bybit_rows()
        data._bybit_rows()

        assert calls["n"] == 2

    def test_invalidate_forces_an_immediate_recompute(self, monkeypatch):
        calls = {"n": 0}

        def _fake_uncached():
            calls["n"] += 1
            return [{"source": "bybit", "key": f"call-{calls['n']}"}]

        monkeypatch.setattr(data, "_bybit_rows_uncached", _fake_uncached)

        data._bybit_rows()
        data._invalidate_bybit_rows_cache()
        data._bybit_rows()

        assert calls["n"] == 2


class TestRefreshRowBypassesTheCache:
    def test_bybit_refresh_never_returns_pre_refresh_cached_data(self, monkeypatch):
        """The regression this whole cache addition could have introduced:
        refresh_row() calls load_bybit_bars(force_refresh=True) then reads
        _bybit_rows() to hand the caller the updated row — if that read hit
        the TTL cache, a force-refresh would silently show stale data."""
        calls = {"n": 0}

        def _fake_uncached():
            calls["n"] += 1
            return [
                {
                    "source": "bybit",
                    "key": "BTCUSDT",
                    "bar_types": [],
                    "generation": calls["n"],
                }
            ]

        monkeypatch.setattr(data, "_bybit_rows_uncached", _fake_uncached)
        monkeypatch.setattr(data, "load_bybit_bars", MagicMock())

        # Prime the cache the way a normal /data page load would.
        primed = data._bybit_rows()
        assert primed[0]["generation"] == 1

        row = data.refresh_row(
            "bybit", symbol="BTCUSDT", category="linear", interval="1"
        )

        assert row["generation"] == 2, "refresh_row returned the stale cached row"
        data.load_bybit_bars.assert_called_once()
        assert data.load_bybit_bars.call_args.kwargs["force_refresh"] is True
