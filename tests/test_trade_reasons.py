"""Trade entry/exit reason capture tests.

Core of the /reports trade-detail feature: ComposedStrategy decision log +
order tags ("dr:<seq>"/"xr:<seq>"/"sl"/"tp"/"flip"/"eob") + positions↔fills
join (_extract_trades).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest import _extract_trades, run_composed_backtest
from composer import ComposedStrategySpec, SignalBlock
from sandbox import _build_instrument_bar_type

_RECIPE = {"symbol": "BTCUSDT", "interval": "60", "category": "linear"}


def _positions_df(rows: list[dict]) -> pd.DataFrame:
    base = {
        "ts_opened": pd.Timestamp("2024-01-02", tz="UTC"),
        "ts_closed": pd.Timestamp("2024-01-03", tz="UTC"),
        "avg_px_open": "100.0",
        "avg_px_close": "110.0",
        "entry": "BUY",
        "realized_pnl": "10.0 USDT",
        "duration_ns": 3_600_000_000_000,
    }
    return pd.DataFrame([{**base, **r} for r in rows])


def _fills_df(rows: dict[str, dict]) -> pd.DataFrame:
    """client_order_id → {tags, type} (fills report shape: index=id)."""
    df = pd.DataFrame.from_dict(rows, orient="index")
    df.index.name = "client_order_id"
    return df


_DECISIONS = [
    {
        "seq": 1,
        "kind": "entry",
        "side": "BUY",
        "bar_ts": 100,
        "submit_ts": 160,
        "logic": "OR",
        "blocks": [
            {
                "idx": 0,
                "type": "ma_cross",
                "label": "MA Cross",
                "params": {"fast": 5, "slow": 20},
                "values": {"fast": 42.1, "slow": 41.8},
            }
        ],
        "trend_bias": None,
    },
    {
        "seq": 2,
        "kind": "exit",
        "side": None,
        "bar_ts": 200,
        "submit_ts": 200,
        "logic": "OR",
        "blocks": [
            {
                "idx": 1,
                "type": "rsi_threshold",
                "label": "RSI Threshold",
                "params": {"threshold": 70},
                "values": {"rsi": 76.3},
            }
        ],
        "trend_bias": None,
    },
]


class TestReasonJoin:
    """Without engine: fake positions/fills/decisions → correct attribution."""

    def test_entry_dr_tag_and_signal_exit(self):
        pos = _positions_df([{"opening_order_id": "O-1", "closing_order_id": "O-2"}])
        fills = _fills_df(
            {
                "O-1": {"tags": "['dr:1']", "type": "MARKET"},
                "O-2": {"tags": "['xr:2']", "type": "MARKET"},
            }
        )
        t = _extract_trades(pos, fills_df=fills, decisions=_DECISIONS)[0]
        assert "MA Cross" in t["entry_reason"]
        assert "fast=5" in t["entry_reason"] and "42.1" in t["entry_reason"]
        assert t["exit_kind"] == "signal"
        assert "RSI Threshold" in t["exit_reason"] and "76.3" in t["exit_reason"]

    def test_sl_tp_flip_eob_kinds(self):
        pos = _positions_df(
            [
                {"opening_order_id": "E1", "closing_order_id": "C1"},
                {"opening_order_id": "E2", "closing_order_id": "C2"},
                {"opening_order_id": "E3", "closing_order_id": "C3"},
                {"opening_order_id": "E4", "closing_order_id": "C4"},
            ]
        )
        fills = _fills_df(
            {
                "C1": {"tags": "['sl']", "type": "STOP_MARKET"},
                "C2": {"tags": "['tp']", "type": "LIMIT"},
                "C3": {"tags": "['flip']", "type": "MARKET"},
                "C4": {"tags": "['eob']", "type": "MARKET"},
            }
        )
        kinds = [
            t["exit_kind"] for t in _extract_trades(pos, fills_df=fills, decisions=[])
        ]
        assert kinds == ["sl", "tp", "flip", "eob"]

    def test_type_fallback_when_tags_missing(self):
        """Old run / untagged order → inference from order type."""
        pos = _positions_df(
            [
                {"opening_order_id": "E1", "closing_order_id": "C1"},
                {"opening_order_id": "E2", "closing_order_id": "C2"},
            ]
        )
        fills = _fills_df(
            {
                "C1": {"tags": "", "type": "STOP_MARKET"},
                "C2": {"tags": None, "type": "LIMIT"},
            }
        )
        trades = _extract_trades(pos, fills_df=fills, decisions=[])
        assert trades[0]["exit_kind"] == "sl"
        assert trades[1]["exit_kind"] == "tp"

    def test_no_fills_df_degrades_to_none(self):
        """If fills not provided, current behavior: reason fields None, 7 old fields intact."""
        pos = _positions_df([{"opening_order_id": "X", "closing_order_id": "Y"}])
        t = _extract_trades(pos)[0]
        assert t["entry_reason"] is None and t["exit_kind"] is None
        assert t["pnl"] == 10.0 and t["side"] == "BUY"

    def test_missing_fill_row_no_crash(self):
        pos = _positions_df(
            [{"opening_order_id": "NONE-1", "closing_order_id": "NONE-2"}]
        )
        fills = _fills_df({"OTHER": {"tags": "['dr:1']", "type": "MARKET"}})
        t = _extract_trades(pos, fills_df=fills, decisions=_DECISIONS)[0]
        assert t["entry_reason"] is None and t["exit_kind"] is None

    def test_unfilled_decision_is_harmless(self):
        """Unfilled limit entry's decision stays position-less — no crash."""
        pos = _positions_df([{"opening_order_id": "O-1", "closing_order_id": "O-2"}])
        fills = _fills_df(
            {
                "O-1": {"tags": "['dr:1']", "type": "MARKET"},
                "O-2": {"tags": "['eob']", "type": "MARKET"},
            }
        )
        extra = _DECISIONS + [
            {**_DECISIONS[0], "seq": 99}  # decision not bound to any fill
        ]
        trades = _extract_trades(pos, fills_df=fills, decisions=extra)
        assert len(trades) == 1 and trades[0]["exit_kind"] == "eob"


def _bars(n: int = 400) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    t = np.arange(n)
    close = 30_000 + 2_000 * np.sin(t / 30.0) + t * 2.0
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + 10,
            "low": np.minimum(open_, close) - 10,
            "close": close,
            "volume": np.full(n, 100.0),
        },
        index=idx,
    )


def _run(spec: ComposedStrategySpec):
    instrument, bar_type = _build_instrument_bar_type(_RECIPE)
    return run_composed_backtest(
        spec,
        _bars(),
        iteration_id=1,
        rationale="reason test",
        instrument=instrument,
        bar_type=bar_type,
        venue=instrument.id.venue,
    )


class TestLogSpecRoundTrip:
    """Spec in log record → from_dict → validate (old AND new shape)."""

    _OLD_SUBSET = {
        "id": "old1",
        "name": "Old Record",
        "blocks": [
            {"type": "ma_cross", "role": "entry", "params": {"fast": 5, "slow": 20}}
        ],
        "entry_logic": "OR",
        "exit_logic": "OR",
        "order_type": "market",
        "trade_size": 0.1,
        "trade_size_mode": "fixed",
        "use_bracket": False,
        "sl_type": "percent",
        "sl_value": 2.0,
        "tp_type": "off",
        "tp_value": 4.0,
        "allow_short": False,
        "emulate": False,
    }

    def test_old_subset_record_fills_defaults(self):
        spec = ComposedStrategySpec.from_dict(self._OLD_SUBSET)
        assert spec.validate() is None
        assert spec.delay_fill is True  # default
        assert spec.trend_filter is False

    def test_new_full_record_preserves_fields(self):
        rec = {
            **self._OLD_SUBSET,
            "id": "new1",
            "trend_filter": True,
            "trend_interval": "240",
            "delay_fill": False,
            "limit_offset_bps": 3.5,
        }
        spec = ComposedStrategySpec.from_dict(rec)
        assert spec.validate() is None
        assert spec.trend_filter is True and spec.trend_interval == "240"
        assert spec.delay_fill is False and spec.limit_offset_bps == 3.5

    def test_unknown_block_type_fails_validate(self):
        rec = {
            **self._OLD_SUBSET,
            "blocks": [{"type": "deleted_custom", "role": "entry", "params": {}}],
        }
        spec = ComposedStrategySpec.from_dict(rec)
        assert spec.validate() is not None


class TestDetailEndpoint:
    """GET /reports/detail — log row → re-run → fragment."""

    def _make_record(self) -> dict:
        return {
            "ts": "2026-07-14T10:00:00.000000+00:00",
            "spec": dict(TestLogSpecRoundTrip._OLD_SUBSET),
            "instrument": "Bybit",
            "bars": {
                "symbol": "BTCUSDT",
                "category": "linear",
                "interval": "60",
                "start": "2024-01-01 00:00:00+00:00",
                "end": "2024-01-17 15:00:00+00:00",
            },
            "rationale": "user-run",
            "error": None,
            "metrics": {"n_trades": 2, "pnl": 739.9},
        }

    def test_detail_renders_reasons_and_chart(self, tmp_path, monkeypatch):
        import json as _json

        from fastapi.testclient import TestClient

        import web.routes.reports as rp
        from server import app
        from state import IterationResult

        log = tmp_path / "backtest_log.jsonl"
        rec = self._make_record()
        log.write_text(_json.dumps(rec) + "\n")
        monkeypatch.setattr(rp, "BACKTEST_LOG", log)
        rp._DETAIL_CACHE.clear()

        from datetime import UTC, datetime

        canned = IterationResult(
            id=0,
            strategy="composed",
            params={},
            metrics={"n_trades": 2, "pnl": 739.9},
            equity_curve=[],
            rationale="reports-detail",
            error=None,
            timestamp=datetime.now(UTC),
            trades=[
                {
                    "entry_time": 1704100000,
                    "exit_time": 1704200000,
                    "entry_price": 30000.0,
                    "exit_price": 30500.0,
                    "side": "BUY",
                    "pnl": 500.0,
                    "dur_min": 60,
                    "entry_reason": "MA Cross (fast=5, slow=20) · fast 30010 / slow 29990",
                    "exit_reason": "Stop-Loss",
                    "exit_kind": "sl",
                    "entry_detail": None,
                    "exit_detail": None,
                }
            ],
            bars_info=rec["bars"],
        )

        def fake_load(**kwargs):
            import pandas as _pd

            idx = _pd.date_range("2024-01-01", periods=10, freq="1h", tz="UTC")
            return _pd.DataFrame({"close": range(10)}, index=idx)

        monkeypatch.setattr("data.load_bybit_bars", fake_load)
        monkeypatch.setattr("sandbox.run_backtest_guarded", lambda *a, **k: canned)

        client = TestClient(app)
        resp = client.get("/reports/detail", params={"ts": rec["ts"]})
        assert resp.status_code == 200
        html = resp.text
        assert "data-price-chart" in html
        assert "MA Cross" in html
        assert "reproduced exactly" in html  # fidelity badge (n_trades + pnl matched)
        assert "Stop-Loss" in html

    def test_detail_unknown_ts(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import web.routes.reports as rp
        from server import app

        log = tmp_path / "backtest_log.jsonl"
        log.write_text("")
        monkeypatch.setattr(rp, "BACKTEST_LOG", log)
        rp._DETAIL_CACHE.clear()
        client = TestClient(app)
        resp = client.get("/reports/detail", params={"ts": "none"})
        assert resp.status_code == 200 and "not found" in resp.text

    def test_detail_non_bybit_row(self, tmp_path, monkeypatch):
        import json as _json

        from fastapi.testclient import TestClient

        import web.routes.reports as rp
        from server import app

        rec = self._make_record()
        rec["bars"] = {"ticker": "A.NASDAQ", "granularity": "1-DAY"}  # no symbol
        log = tmp_path / "backtest_log.jsonl"
        log.write_text(_json.dumps(rec) + "\n")
        monkeypatch.setattr(rp, "BACKTEST_LOG", log)
        rp._DETAIL_CACHE.clear()
        client = TestClient(app)
        resp = client.get("/reports/detail", params={"ts": rec["ts"]})
        assert resp.status_code == 200 and "Bybit" in resp.text


class TestChartWindowGuard:
    """/chart/data window/TF feasibility guard — rejects before loading data."""

    def test_too_fine_tf_rejected_without_data_load(self, monkeypatch):
        from fastapi.testclient import TestClient

        from server import app

        def boom(**kwargs):
            raise AssertionError("guard should have rejected before loading data")

        monkeypatch.setattr("data.load_bybit_bars", boom)
        client = TestClient(app)
        six_years = 6 * 365 * 86400
        resp = client.get(
            "/chart/data",
            params={
                "symbol": "BTCUSDT",
                "interval": "1",
                "start_ts": 1_600_000_000,
                "end_ts": 1_600_000_000 + six_years,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "too fine" in body["error"] and body["candles"] == []

    def test_feasible_tf_passes_guard(self, monkeypatch):
        from fastapi.testclient import TestClient

        from server import app

        called = {}

        def fake_load(**kwargs):
            import pandas as _pd

            called["yes"] = True
            idx = _pd.date_range("2024-01-01", periods=5, freq="1D", tz="UTC")
            return _pd.DataFrame(
                {c: [1.0] * 5 for c in ("open", "high", "low", "close", "volume")},
                index=idx,
            )

        monkeypatch.setattr("data.load_bybit_bars", fake_load)
        client = TestClient(app)
        six_years = 6 * 365 * 86400
        resp = client.get(
            "/chart/data",
            params={
                "symbol": "BTCUSDT",
                "interval": "D",  # 6y / 1d ≈ 2.2k candles — reasonable
                "start_ts": 1_600_000_000,
                "end_ts": 1_600_000_000 + six_years,
            },
        )
        assert resp.status_code == 200
        assert called.get("yes") and len(resp.json()["candles"]) == 5


class TestReasonCaptureIntegration:
    """Real engine: signal → tag → fills → reasoned trade."""

    def test_ma_cross_reasons_end_to_end(self):
        spec = ComposedStrategySpec(
            id="reas1",
            name="Reason E2E",
            description="",
            blocks=[
                SignalBlock(
                    type="ma_cross",
                    role="entry",
                    params={"fast": 5, "slow": 20, "direction": "up"},
                ),
                SignalBlock(
                    type="ma_cross",
                    role="exit",
                    params={"fast": 5, "slow": 20, "direction": "down"},
                ),
            ],
            trade_size=0.1,
        )
        r = _run(spec)
        assert r.error is None
        trades = r.trades or []
        assert trades, "no trades at all"
        for t in trades:
            assert t["entry_reason"] and "MA Cross" in t["entry_reason"]
            assert "fast=5" in t["entry_reason"]
            assert "·" in t["entry_reason"], "indicator values missing"
            assert t["exit_kind"] in ("signal", "eob")
        assert any(t["exit_kind"] == "signal" for t in trades)

    def test_bracket_sl_attribution(self):
        """Decline→rise→crash: entry on the rise, crash DEFINITELY triggers SL."""
        n = 400
        idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        seg1 = 31_000 - np.arange(100) * 20.0  # decline → 29k
        seg2 = seg1[-1] + np.arange(200) * 20.0  # rise → 33k (entry here)
        seg3 = seg2[-1] - np.arange(100) * 100.0  # crash → 23k (SL triggers)
        close = np.concatenate([seg1, seg2, seg3])
        open_ = np.concatenate([[close[0]], close[:-1]])
        bars = pd.DataFrame(
            {
                "open": open_,
                "high": np.maximum(open_, close) + 10,
                "low": np.minimum(open_, close) - 10,
                "close": close,
                "volume": np.full(n, 100.0),
            },
            index=idx,
        )
        spec = ComposedStrategySpec(
            id="reas2",
            name="SL E2E",
            description="",
            blocks=[
                SignalBlock(
                    type="ma_cross",
                    role="entry",
                    params={"fast": 5, "slow": 20, "direction": "up"},
                ),
            ],
            trade_size=0.1,
            use_bracket=True,
            sl_type="percent",
            sl_value=0.5,
            tp_type="off",  # SL-fallback path (reduce-only STOP_MARKET)
        )
        instrument, bar_type = _build_instrument_bar_type(_RECIPE)
        r = run_composed_backtest(
            spec,
            bars,
            iteration_id=1,
            rationale="sl test",
            instrument=instrument,
            bar_type=bar_type,
            venue=instrument.id.venue,
        )
        assert r.error is None
        kinds = {t["exit_kind"] for t in r.trades or []}
        assert "sl" in kinds, f"no SL attribution, seen: {kinds}"

    def test_delay_fill_submit_next_bar(self):
        """delay_fill=True (default): decision on signal bar, submit on next bar."""

        spec = ComposedStrategySpec(
            id="reas3",
            name="Delay",
            description="",
            blocks=[
                SignalBlock(
                    type="ma_cross",
                    role="entry",
                    params={"fast": 5, "slow": 20, "direction": "up"},
                ),
            ],
            trade_size=0.1,
        )
        assert spec.delay_fill is True
        r = _run(spec)
        assert r.error is None and (r.trades or [])
        # The decision log cannot be read through the strategy on the in-process
        # path without sandbox (it does not enter IterationResult) — we verify the
        # behavior via the trade reason: reason present → dr-tag join also worked
        # on the delay path.
        assert all(t["entry_reason"] for t in r.trades)
