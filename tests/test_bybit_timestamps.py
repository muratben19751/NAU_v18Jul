"""Faz 1a — Bybit bars are timestamped at bar CLOSE, not open.

Bybit klines (and pandas-resampled index bars) carry the bar's OPEN time.
Nautilus convention is that a completed bar's ts_event is its CLOSE time
(open + interval). ``_bars_from_df`` applies that shift exactly once, and the
raw pandas ingestion must stay open-time so the warm-cache cursor doesn't skip
a bar.
"""

import pandas as pd
import pytest

from backtest import (
    _bar_interval_ns,
    _bars_from_df,
    _make_bybit_bar_type,
    _make_bybit_instrument,
    _make_index_bar_type,
    _make_index_instrument,
)

_OPEN = pd.Timestamp("2024-01-01T00:00:00Z")
_OPEN_NS = int(_OPEN.tz_convert(None).value)


def _one_bar_df():
    return pd.DataFrame(
        {
            "open": [100.0],
            "high": [102.0],
            "low": [99.0],
            "close": [101.0],
            "volume": [5.0],
        },
        index=pd.DatetimeIndex([_OPEN], name="timestamp"),
    )


class TestBarIntervalNs:
    @pytest.mark.parametrize(
        "interval,secs",
        [
            ("1", 60),
            ("5", 300),
            ("15", 900),
            ("60", 3600),
            ("240", 14400),
            ("D", 86400),
        ],
    )
    def test_bybit_interval_ns(self, interval, secs):
        inst = _make_bybit_instrument()
        bt = _make_bybit_bar_type(inst.id, interval)
        assert _bar_interval_ns(bt) == secs * 1_000_000_000

    @pytest.mark.parametrize("gran,secs", [("1d", 86400), ("1m", 60)])
    def test_index_interval_ns(self, gran, secs):
        inst = _make_index_instrument("SPY")
        bt = _make_index_bar_type(inst.id, gran)
        assert _bar_interval_ns(bt) == secs * 1_000_000_000


class TestCloseTimeShift:
    @pytest.mark.parametrize(
        "interval,secs",
        [("1", 60), ("5", 300), ("60", 3600), ("240", 14400), ("D", 86400)],
    )
    def test_ts_event_is_open_plus_interval(self, interval, secs):
        inst = _make_bybit_instrument()
        bt = _make_bybit_bar_type(inst.id, interval)
        bars = _bars_from_df(bt, inst, _one_bar_df())
        assert len(bars) == 1
        assert bars[0].ts_event == _OPEN_NS + secs * 1_000_000_000

    def test_ts_init_equals_ts_event(self):
        inst = _make_bybit_instrument()
        bt = _make_bybit_bar_type(inst.id, "1")
        bars = _bars_from_df(bt, inst, _one_bar_df())
        assert bars[0].ts_init == bars[0].ts_event

    def test_ts_init_never_before_ts_event(self):
        """Invariant across a multi-bar frame: ts_init >= ts_event for every bar."""
        inst = _make_bybit_instrument()
        bt = _make_bybit_bar_type(inst.id, "1")
        idx = pd.date_range("2024-01-01", periods=10, freq="1min", tz="UTC")
        df = pd.DataFrame(
            {
                "open": range(10),
                "high": [x + 2 for x in range(10)],
                "low": [x - 1 for x in range(10)],
                "close": [x + 1 for x in range(10)],
                "volume": [1.0] * 10,
            },
            index=idx,
        )
        bars = _bars_from_df(bt, inst, df)
        assert all(b.ts_init >= b.ts_event for b in bars)


class TestIngestionStaysOpenTime:
    """The shift lives in _bars_from_df, NOT in ingestion — the raw Bybit page
    index must remain the exchange OPEN time so the warm-cache cursor
    (cached_end_ms + step_ms) doesn't skip a bar."""

    def test_fetch_page_index_is_raw_open_ms(self, monkeypatch):
        import data

        open_ms = int(_OPEN.value // 1_000_000)  # ns → ms
        rows = [[str(open_ms), "100", "102", "99", "101", "5", "0"]]

        class _Resp:
            status_code = 200

            def json(self):
                return {"retCode": 0, "result": {"list": rows}}

            def raise_for_status(self):
                pass

        # Bybit fetch now goes through a thread-local requests.Session
        # (_get_bybit_session().get), so patch the session's get, not the
        # module-level requests.get.
        monkeypatch.setattr(
            data._get_bybit_session(), "get", lambda *a, **k: _Resp()
        )
        df = data._fetch_bybit_page("linear", "BTCUSDT", "1", 0, 1)
        assert not df.empty
        # Index is the raw open time, un-shifted.
        assert int(df.index[0].value // 1_000_000) == open_ms
