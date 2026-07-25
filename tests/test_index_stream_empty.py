"""_stream_ticker_rows must tolerate an empty/corrupt gzip source.

Regression: when the index gzip existed but the awk pipe produced no output at
all (empty or truncated file — not even a header line), pandas raised
``EmptyDataError: No columns to parse from file``. The missing-day contract
promises an empty typed frame instead, so a broken day should degrade quietly
rather than crash the whole load.
"""

from __future__ import annotations

import gzip
from datetime import date

import pandas as pd

EXPECTED_COLS = ["ticker", "value", "timestamp"]


def _seed_index_file(root, day: date, raw_bytes: bytes) -> None:
    """Write a .csv.gz at the path _index_file_for(day) expects."""
    p = root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.isoformat()}.csv.gz"
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wb") as fh:
        fh.write(raw_bytes)


class TestStreamTickerEmpty:
    def test_empty_gzip_returns_empty_frame(self, tmp_path, monkeypatch):
        import data

        monkeypatch.setattr(data, "INDEX_ROOT", tmp_path)
        day = date(2024, 3, 15)
        _seed_index_file(tmp_path, day, b"")  # decompresses to nothing

        df = data._stream_ticker_rows("AAPL", day)

        assert df.empty
        assert list(df.columns) == EXPECTED_COLS

    def test_missing_file_returns_empty_frame(self, tmp_path, monkeypatch):
        import data

        monkeypatch.setattr(data, "INDEX_ROOT", tmp_path)
        df = data._stream_ticker_rows("AAPL", date(2099, 1, 1))

        assert df.empty
        assert list(df.columns) == EXPECTED_COLS

    def test_populated_file_filters_ticker(self, tmp_path, monkeypatch):
        import data

        monkeypatch.setattr(data, "INDEX_ROOT", tmp_path)
        day = date(2024, 3, 16)
        content = (
            "ticker,value,timestamp\n"
            "AAPL,150.0,1710547200000000000\n"
            "MSFT,300.0,1710547200000000000\n"
            "AAPL,151.0,1710547260000000000\n"
        ).encode()
        _seed_index_file(tmp_path, day, content)

        df = data._stream_ticker_rows("AAPL", day)

        assert list(df.columns) == EXPECTED_COLS
        assert len(df) == 2
        assert set(df["ticker"]) == {"AAPL"}
