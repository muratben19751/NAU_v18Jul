"""load_bybit_bars must extend the cache in BOTH directions.

A later `end` appends to the tail (existing behavior); an earlier `start` now
backfills older history so the agent can request the widest available range and
have a narrow cache (e.g. the 7-day startup fetch) widened in place.
"""

from datetime import UTC, datetime, timedelta

import pandas as pd


def _mock_fetch(step_ms):
    """Synthetic _fetch_bybit_page: open-time bars across [start_ms, end_ms]."""

    def _fetch(category, symbol, interval, start_ms, end_ms):
        ts = list(range(start_ms, end_ms + 1, step_ms))
        if not ts:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        idx = pd.to_datetime(ts, unit="ms", utc=True)
        n = len(ts)
        return pd.DataFrame(
            {
                "open": [100.0] * n,
                "high": [101.0] * n,
                "low": [99.0] * n,
                "close": [100.5] * n,
                "volume": [1.0] * n,
            },
            index=idx,
        )

    return _fetch


class TestBackfill:
    def test_earlier_start_backfills_head(self, tmp_path, monkeypatch):
        import data

        monkeypatch.setattr(data, "BYBIT_CACHE_DIR", tmp_path / "bybit")
        monkeypatch.setattr(data, "NAUTILUS_CATALOG_DIR", tmp_path / "cat")
        data.BYBIT_CACHE_DIR.mkdir(parents=True)
        monkeypatch.setattr(data, "_fetch_bybit_page", _mock_fetch(data._BYBIT_MS["1"]))

        # Seed a NARROW cache: 10 one-minute bars around a base instant.
        base = datetime(2024, 1, 10, tzinfo=UTC)
        cache_idx = pd.to_datetime(
            [int((base + timedelta(minutes=i)).timestamp() * 1000) for i in range(10)],
            unit="ms",
            utc=True,
        )
        cache_df = pd.DataFrame(
            {c: [1.0] * 10 for c in ("open", "high", "low", "close", "volume")},
            index=cache_idx,
        )
        cache_path = data._bybit_cache_path("linear", "BTCUSDT", "1")
        cache_df.to_parquet(cache_path)

        # Request a window that starts 30 min BEFORE the cache and ends after it.
        start = base - timedelta(minutes=30)
        end = base + timedelta(minutes=20)
        out = data.load_bybit_bars(
            "BTCUSDT", interval="1", category="linear", start=start, end=end
        )

        combined = pd.read_parquet(cache_path)
        # Head backfilled: on-disk cache now reaches back to ~start (was base).
        assert combined.index[0] <= pd.Timestamp(start) + pd.Timedelta(minutes=1)
        # Original bars preserved (tail of the seed still present).
        assert combined.index[-1] >= pd.Timestamp(base + timedelta(minutes=9))
        # Returned slice honours the wide start.
        assert out.index[0] <= pd.Timestamp(start) + pd.Timedelta(minutes=1)
        # And it is genuinely wider than the 10-bar seed.
        assert len(out) > 10

    def test_no_refetch_when_cache_covers_request(self, tmp_path, monkeypatch):
        import data

        monkeypatch.setattr(data, "BYBIT_CACHE_DIR", tmp_path / "bybit")
        monkeypatch.setattr(data, "NAUTILUS_CATALOG_DIR", tmp_path / "cat")
        data.BYBIT_CACHE_DIR.mkdir(parents=True)

        calls = {"n": 0}
        base_fetch = _mock_fetch(data._BYBIT_MS["1"])

        def _counting_fetch(*a, **k):
            calls["n"] += 1
            return base_fetch(*a, **k)

        monkeypatch.setattr(data, "_fetch_bybit_page", _counting_fetch)

        base = datetime(2024, 2, 1, tzinfo=UTC)
        idx = pd.to_datetime(
            [int((base + timedelta(minutes=i)).timestamp() * 1000) for i in range(60)],
            unit="ms",
            utc=True,
        )
        df = pd.DataFrame(
            {c: [1.0] * 60 for c in ("open", "high", "low", "close", "volume")},
            index=idx,
        )
        df.to_parquet(data._bybit_cache_path("linear", "BTCUSDT", "1"))

        # Request a window fully inside the cache → no head, no tail fetch.
        data.load_bybit_bars(
            "BTCUSDT",
            interval="1",
            category="linear",
            start=base + timedelta(minutes=10),
            end=base + timedelta(minutes=40),
        )
        assert calls["n"] == 0
