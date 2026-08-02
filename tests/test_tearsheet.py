"""Tear sheet: render model + the resolvers behind GET /tearsheet.

The overlay's promise is that it never re-runs anything — every listing's store
already holds the metrics and the curve. These tests pin that: each resolver is
fed a stored record and must produce a populated sheet, and a record it cannot
show must degrade to a stated reason rather than an empty grid.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from web.tearsheet import tearsheet_error, tearsheet_view

# A realistic log record: metrics carry BOTH equity curves, as log_backtest writes.
_METRICS = {
    "starting_cash": 10_000.0,
    "pnl": -1276.23,
    "pnl_pct": -0.127623,
    "sharpe": -1.2037,
    "sortino": 0.1804,
    "profit_factor": 1.0974,
    "win_rate": 0.30708,
    "max_dd": -0.13022,
    "n_trades": 508,
    "n_wins": 156,
    "n_losses": 352,
    "commission_total": 1016.0,
    "runner": "BacktestEngine",
    "equity_curve_realized": [10_000.0, 9_800.0, 8_723.77],
    "equity_curve_mtm": [
        ["2003-09-10T13:30:00+00:00", 10_000.0],
        ["2003-10-10T13:30:00+00:00", 9_800.0],
        ["2003-11-10T13:30:00+00:00", 8_723.77],
    ],
}


class TestRenderModel:
    def test_curves_leave_the_kpi_grid_and_drive_the_charts(self):
        v = tearsheet_view(title="X", metrics=_METRICS)
        keys = {k["key"] for k in v["kpis"]}
        # A 5000-point array rendered as a KPI tile would be nonsense.
        assert "equity_curve_mtm" not in keys
        assert "equity_curve_realized" not in keys
        assert len(v["equity_mtm"]) == 3
        assert v["has_equity"] and v["has_monthly"]

    def test_absent_metrics_are_dropped_not_dashed(self):
        """A thin source shows a short honest grid, not sixteen em dashes."""
        v = tearsheet_view(title="X", metrics={"pnl": 5.0, "n_trades": 2})
        assert {k["key"] for k in v["kpis"]} == {"pnl", "n_trades"}

    def test_tone_marks_loss_and_sub_par_profit_factor(self):
        v = tearsheet_view(title="X", metrics=_METRICS)
        tone = {k["key"]: k["tone"] for k in v["kpis"]}
        assert tone["pnl"] == "down"
        assert tone["max_dd"] == "down"  # a drawdown is never "good"
        assert tone["profit_factor"] == "up"  # 1.097 > 1 → profitable
        assert (
            tearsheet_view(title="X", metrics={"profit_factor": 0.8})["kpis"][0]["tone"]
            == "down"
        )

    def test_undated_curve_says_why_monthly_is_missing(self):
        v = tearsheet_view(title="X", metrics={"pnl": 1.0}, equity=[1.0, 1.1])
        assert not v["has_monthly"]
        assert any("timestamp" in n for n in v["notes"])

    def test_wins_losses_summary(self):
        assert tearsheet_view(title="X", metrics=_METRICS)["wins_losses"] == (
            "156 win / 352 loss"
        )

    def test_error_view_keeps_the_shell_renderable(self):
        v = tearsheet_error("gone")
        assert v["error"] == "gone" and v["kpis"] == [] and not v["has_equity"]


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    """Point the log resolver at a two-record file."""
    from web.routes import tearsheet as ts_route

    path = tmp_path / "backtest_log.jsonl"
    recs = [
        {
            "ts": "2026-08-02T17:13:48.513747+00:00",
            "elapsed_sec": 25.886,
            "spec": {"name": "ADX-ATR"},
            "bars": {
                "ticker": "QQQ.NASDAQ",
                "granularity": "5-MINUTE",
                "start": "2003-09-10",
                "end": "2026-04-30",
                "n_bars": 323_199,
            },
            "rationale": "agent-run · iter 1/5",
            "error": None,
            "metrics": _METRICS,
        },
        {
            "ts": "2026-08-02T18:00:00+00:00",
            "spec": {"name": "Broken"},
            "bars": {},
            "error": "boom",
            "metrics": {},
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    monkeypatch.setattr(ts_route, "BACKTEST_LOG", path)
    return path


class TestLogResolver:
    def test_reads_a_stored_run_without_rerunning_it(self, log_file):
        from web.routes.tearsheet import _log_view

        v = _log_view("2026-08-02T17:13:48.513747+00:00")
        assert v["error"] == ""
        assert v["title"] == "ADX-ATR"
        assert v["subtitle"] == "QQQ.NASDAQ · 5-MINUTE"
        assert ("Range", "2003-09-10 → 2026-04-30") in v["ident"]
        assert v["has_equity"]

    def test_missing_ts_is_a_stated_reason(self, log_file):
        from web.routes.tearsheet import _log_view

        assert "no longer in the backtest log" in _log_view("nope")["error"]

    def test_failed_run_reports_its_error(self, log_file):
        from web.routes.tearsheet import _log_view

        assert "boom" in _log_view("2026-08-02T18:00:00+00:00")["error"]


class TestSessionResolver:
    """AUTO iterations / Session Logs address a run by position in the file."""

    @pytest.fixture
    def session(self, tmp_path, monkeypatch):
        from web.routes import agent_backtest as ab

        monkeypatch.setattr(ab, "SESSION_LOG_DIR", tmp_path)
        lines = [
            {"event": "step", "msg": "noise"},
            {
                "event": "backtest_result",
                "ts": "2026-08-02T17:00:00+00:00",
                "iteration": 0,
                "round": 1,
                "interval": "60",
                "spec_name": "First",
                "metrics": {"pnl": 12.0, "n_trades": 3},
                "equity_curve": [10_000.0, 10_012.0],
                "equity_dates": ["2026-01-01", "2026-02-01"],
                "bars_info": {"symbol": "BTCUSDT"},
                "score": 0.42,
            },
            {
                "event": "backtest_result",
                "ts": "2026-08-02T17:05:00+00:00",
                "iteration": 1,
                "spec_name": "Second",
                "metrics": {"pnl": -3.0},
                "bars_info": {},
            },
        ]
        (tmp_path / "abcdef01.jsonl").write_text(
            "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8"
        )
        return "abcdef01"

    def test_index_addresses_the_nth_backtest_not_the_nth_line(self, session):
        from web.routes.tearsheet import _session_view

        # Index 0 must skip the "step" line that precedes it in the file.
        assert _session_view(session, 0)["title"] == "First"
        assert _session_view(session, 1)["title"] == "Second"

    def test_dated_sibling_curve_enables_monthly(self, session):
        from web.routes.tearsheet import _session_view

        v = _session_view(session, 0)
        assert v["has_monthly"] and v["equity_dates"] == ["2026-01-01", "2026-02-01"]

    def test_out_of_range_and_bad_id_are_refused(self, session):
        from web.routes.tearsheet import _session_view

        assert "not in the session log" in _session_view(session, 9)["error"]
        assert "Invalid session id" in _session_view("../etc", 0)["error"]


class TestStudioResolver:
    def test_percentages_are_converted_and_the_gap_is_stated(self):
        from web.routes.tearsheet import _studio_metrics

        raw = json.dumps(
            {
                "net_pnl_pct": 12.5,
                "sharpe": 1.4,
                "dsr": 0.9,
                "max_dd_pct": 8.0,
                "trades": 42,
                "win_rate_pct": 55.0,
                "profit_factor": 1.8,
                "equity_curve": [1.0, 1.125],
            }
        )
        metrics, curve = _studio_metrics(raw)
        # This store keeps 12.5 meaning 12.5%; the shared formatter wants 0.125.
        assert metrics["pnl_pct"] == pytest.approx(0.125)
        assert metrics["max_dd"] == pytest.approx(0.08)
        assert metrics["n_trades"] == 42
        assert curve == [1.0, 1.125]


class TestRoute:
    @pytest.fixture
    def client(self):
        import server

        return TestClient(server.app)

    def test_unknown_source_is_refused_in_the_normal_shell(self, client):
        r = client.get("/tearsheet?src=wat")
        assert r.status_code == 200
        assert "Unknown tear sheet source" in r.text

    def test_missing_ts_is_refused(self, client):
        assert "Missing run timestamp" in client.get("/tearsheet?src=log").text

    def test_fragment_uses_its_own_dom_ids(self, client, log_file):
        """The overlay opens ON TOP of the live result screen, which owns
        #equity-data / #equity-single — sharing either would kill its chart."""
        r = client.get("/tearsheet?src=log&ts=2026-08-02T17:13:48.513747%2B00:00")
        assert r.status_code == 200
        assert 'id="tsh-equity"' in r.text
        assert 'id="equity-single"' not in r.text
        assert 'id="equity-data"' not in r.text
