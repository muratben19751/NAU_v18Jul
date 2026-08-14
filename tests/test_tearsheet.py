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
    # stamp_buy_hold_benchmark() writes these into every logged run (39 of the
    # last 43 records on disk carry them), so the fixture carries them too.
    "benchmark_return_fraction": 0.4502,
    "excess_return_fraction": -0.577823,
    "benchmark_cagr": 0.0387,
    "strategy_cagr": -0.0072,
    "annualized_alpha": -0.0459,
    "benchmark_max_dd": -0.5312,
    "benchmark_dividend_yield_annual": 0.0,
    "benchmark_cost_basis": "gross_buy_and_hold_no_costs",
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


class TestBuyAndHold:
    """The benchmark the run was already scored against, shown on the sheet.

    ``stamp_buy_hold_benchmark`` has been stamping these fields into every
    stored run for a while; until now the tear sheet dropped them, so a reader
    could see "Return -12.76%" without ever learning the market did +45% over
    the same window.
    """

    def _tiles(self, metrics: dict) -> dict:
        return {k["key"]: k for k in tearsheet_view(title="X", metrics=metrics)["kpis"]}

    def test_benchmark_sits_next_to_the_return_it_qualifies(self):
        keys = [k["key"] for k in tearsheet_view(title="X", metrics=_METRICS)["kpis"]]
        # Adjacency is the point: two returns a grid apart are not compared.
        assert keys[keys.index("pnl_pct") + 1] == "benchmark_return_fraction"
        assert keys[keys.index("pnl_pct") + 2] == "excess_return_fraction"

    def test_values_and_tones(self):
        t = self._tiles(_METRICS)
        assert t["benchmark_return_fraction"]["value"] == "45.02%"
        # The benchmark is the yardstick, not a result to be graded.
        assert t["benchmark_return_fraction"]["tone"] == ""
        assert t["excess_return_fraction"]["value"] == "-57.78%"
        assert t["excess_return_fraction"]["tone"] == "down"
        assert t["annualized_alpha"]["value"] == "-4.59%"

    def test_a_difference_is_rendered_with_its_sign(self):
        """ "+0.40%" cannot be misread as the losing side; "0.40%" can."""
        t = self._tiles({"excess_return_fraction": 0.004, "annualized_alpha": 0.0123})
        assert t["excess_return_fraction"]["value"] == "+0.40%"
        assert t["excess_return_fraction"]["tone"] == "up"
        assert t["annualized_alpha"]["value"] == "+1.23%"

    def test_benchmark_drawdown_rides_on_the_drawdown_tile(self):
        """Beating buy & hold at twice its drawdown is not beating it."""
        assert self._tiles(_METRICS)["max_dd"]["sub"] == "buy & hold -53.12%"

    def test_the_note_says_the_two_legs_are_not_one_cost_basis(self):
        notes = " ".join(tearsheet_view(title="X", metrics=_METRICS)["notes"])
        assert "net of simulated costs" in notes
        # Dividend yield defaults to 0, which UNDERCOUNTS buy & hold — the
        # sheet says so instead of quietly showing the smaller number.
        assert "dividend-adjusted" in notes

    def test_a_credited_dividend_yield_is_named_not_hidden(self):
        notes = " ".join(
            tearsheet_view(
                title="X",
                metrics={
                    "benchmark_return_fraction": 0.45,
                    "benchmark_dividend_yield_annual": 0.0055,
                },
            )["notes"]
        )
        assert "0.55% annual dividend" in notes

    def test_a_run_without_a_benchmark_gains_neither_tile_nor_note(self):
        """Strategy Builder stores no benchmark — it must not grow empty ones."""
        v = tearsheet_view(title="X", metrics={"pnl_pct": 0.1, "max_dd": -0.2})
        assert {k["key"] for k in v["kpis"]} == {"pnl_pct", "max_dd"}
        assert all(not k["sub"] for k in v["kpis"])
        assert not any("buy & hold" in n for n in v["notes"])


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
        import web.shared as shared

        # tearsheet._session_events reads web.shared.SESSION_LOG_DIR directly
        # (a local import) — not agent_backtest's re-bound copy, which the
        # autouse _isolate_session_logs fixture already points elsewhere.
        monkeypatch.setattr(shared, "SESSION_LOG_DIR", tmp_path)
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

    def test_the_rendered_sheet_carries_the_benchmark(self, client, log_file):
        """The render model can hold it and the template still drop it."""
        r = client.get("/tearsheet?src=log&ts=2026-08-02T17:13:48.513747%2B00:00")
        assert "Buy &amp; Hold" in r.text and "45.02%" in r.text
        assert "buy &amp; hold -53.12%" in r.text  # sub-line on the drawdown tile
