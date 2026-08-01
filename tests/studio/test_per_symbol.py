"""Per-symbol breakdown routes: /runs/latest/symbols and .../symbols/{idx}/trades.

Runs go through the real POST /backtest flow on the stub adapter (TestClient
executes background tasks synchronously), so what is under test is the whole
chain: run → store blob → from_json → table/trade fragments.
"""

import pytest
from fastapi.testclient import TestClient

from strategy_studio.backtest import BacktestMetrics, InstrumentMetrics


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from scripts.seed_studio import build_fixture
    from server import app as _host
    from strategy_studio.store import StrategyStore
    from web.routes import strategy_studio as main

    store = StrategyStore(tmp_path / "t.db")
    fx = build_fixture()
    for ic in fx.instruments:  # fixture ships only XAUUSD active
        ic.active = True
    store.save(fx)
    monkeypatch.setattr(main, "store", store)
    return TestClient(_host)


def _run_backtest(client) -> None:
    assert client.post("/studio/wt-funding-v3/backtest").status_code == 200


def test_symbols_table_lists_every_instrument(client):
    _run_backtest(client)
    r = client.get("/studio/wt-funding-v3/runs/latest/symbols")

    assert r.status_code == 200
    # one row per active instrument of the fixture, ps-table markup
    assert "ps-table" in r.text
    for sym in ["XAUUSD", "EURUSD", "NAS100"]:
        assert sym in r.text


def test_footer_offers_symbols_button_only_with_detail(client):
    _run_backtest(client)
    assert "Symbols" in client.get("/studio/wt-funding-v3/runs/latest").text


def test_results_pane_includes_per_symbol_section(client):
    _run_backtest(client)
    r = client.get("/studio/wt-funding-v3/runs/latest/folds")

    assert r.status_code == 200
    assert "Per symbol" in r.text


def test_legacy_run_degrades_gracefully(client):
    from web.routes import strategy_studio as main

    main.store.create_run("legacy1", "wt-funding-v3", 1, False, "h")
    legacy = BacktestMetrics(
        net_pnl_pct=1.0,
        sharpe=0.5,
        dsr=0.4,
        max_dd_pct=-3.0,
        trades=7,
        win_rate_pct=50.0,
        profit_factor=1.2,
    )
    main.store.finish_run("legacy1", legacy.to_json())

    r = client.get("/studio/wt-funding-v3/runs/latest/symbols")
    assert r.status_code == 200
    assert "No per-symbol detail" in r.text
    # and the footer offers no Symbols button for it
    assert "Symbols" not in client.get("/studio/wt-funding-v3/runs/latest").text


def test_trades_fragment_and_bounds(client):
    from web.routes import strategy_studio as main

    main.store.create_run("run2", "wt-funding-v3", 1, False, "h")
    m = BacktestMetrics(
        net_pnl_pct=2.0,
        sharpe=0.7,
        dsr=0.5,
        max_dd_pct=-2.0,
        trades=1,
        win_rate_pct=100.0,
        profit_factor=2.0,
        per_instrument=[
            InstrumentMetrics(
                symbol="XAUUSD",
                timeframe="15m",
                net_pnl_pct=2.0,
                sharpe=0.7,
                max_dd_pct=-2.0,
                trades=1,
                win_rate_pct=100.0,
                profit_factor=2.0,
                equity_curve=[1.0, 1.02],
                trades_detail=[
                    {
                        "entry_time": 1_700_000_000,
                        "exit_time": 1_700_003_600,
                        "entry_price": 1900.5,
                        "exit_price": 1910.0,
                        "side": "BUY",
                        "pnl": 9.5,
                        "dur_min": 60,
                        "exit_reason": "take profit",
                    }
                ],
            )
        ],
    )
    main.store.finish_run("run2", m.to_json())

    r = client.get("/studio/wt-funding-v3/runs/latest/symbols/0/trades")
    assert r.status_code == 200
    assert "BUY" in r.text and "+9.50" in r.text and "take profit" in r.text

    assert (
        client.get("/studio/wt-funding-v3/runs/latest/symbols/9/trades").status_code
        == 404
    )


def test_stub_run_renders_range_in_footer_and_table(client):
    """The stub reports its notional window (now − lookback); both surfaces
    show it — the strip as a Range chip, the table as a per-row column."""
    import re

    _run_backtest(client)

    footer = client.get("/studio/wt-funding-v3/runs/latest")
    m = re.search(r"(\d{4}-\d{2}-\d{2}) → (\d{4}-\d{2}-\d{2})", footer.text)
    assert m, "footer strip should show the backtest date range"
    assert m.group(1) < m.group(2)

    table = client.get("/studio/wt-funding-v3/runs/latest/symbols")
    assert ">Range<" in table.text
    assert m.group(1) in table.text and m.group(2) in table.text
