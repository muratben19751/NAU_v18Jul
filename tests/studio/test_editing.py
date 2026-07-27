import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from scripts.seed_studio import build_fixture
    from server import app as _host
    from strategy_studio.store import StrategyStore
    from web.routes import strategy_studio as main

    store = StrategyStore(tmp_path / "t.db")
    store.save(build_fixture())
    monkeypatch.setattr(main, "store", store)
    c = TestClient(_host)
    c.store = store
    return c


SID = "wt-funding-v3"


def _draft(client):
    return client.store.load_draft(SID)


def test_add_rule_creates_draft(client):
    r = client.post(f"/studio/{SID}/blocks/entry/rules", data={"indicator": "rsi"})
    assert r.status_code == 200
    assert "RSI" in r.text and 'id="block-entry"' in r.text
    assert "side-panel" in r.text  # oob side panel refresh
    d = _draft(client)
    assert d is not None
    assert d.entry.rules[-1].indicator == "rsi"
    assert d.entry.rules[-1].params["len"].value == 14  # registry default


def test_add_rule_as_filter(client):
    client.post(
        f"/studio/{SID}/blocks/exit/rules",
        data={"indicator": "relative_volume", "as_filter": "true"},
    )
    d = _draft(client)
    assert d.exit.filters[-1].indicator == "relative_volume"


def test_add_unknown_indicator_422(client):
    r = client.post(
        f"/studio/{SID}/blocks/entry/rules", data={"indicator": "hokus_pokus"}
    )
    assert r.status_code == 422 and "unknown indicator" in r.text


def test_edit_param_valid(client):
    d, _ = client.store.working_copy(SID)
    wt = d.entry.rules[0]
    r = client.patch(
        f"/studio/{SID}/rules/{wt.id}", data={"param": "n1", "value": "12"}
    )
    assert r.status_code == 200
    d2 = _draft(client)
    assert d2.entry.rules[0].params["n1"].value == 12
    # optimize range preserved through the edit
    assert d2.entry.rules[0].params["n1"].optimize is not None


def test_edit_param_out_of_bounds_422(client):
    d, _ = client.store.working_copy(SID)
    wt = d.entry.rules[0]
    r = client.patch(
        f"/studio/{SID}/rules/{wt.id}", data={"param": "n1", "value": "9999"}
    )
    assert r.status_code == 422 and "above max" in r.text
    assert _draft(client) is None  # invalid edit never persisted


def test_edit_target(client):
    d, _ = client.store.working_copy(SID)
    fz = d.entry.rules[1]
    client.patch(
        f"/studio/{SID}/rules/{fz.id}", data={"param": "target", "value": "-1.75"}
    )
    assert _draft(client).entry.rules[1].target.value == -1.75


def test_delete_rule(client):
    d, _ = client.store.working_copy(SID)
    rid = d.exit.rules[0].id
    r = client.request("DELETE", f"/studio/{SID}/rules/{rid}")
    assert r.status_code == 200
    assert len(_draft(client).exit.rules) == 1


def test_delete_last_entry_rule_blocked(client):
    d, _ = client.store.working_copy(SID)
    for rule in list(d.entry.rules)[1:]:
        client.request("DELETE", f"/studio/{SID}/rules/{rule.id}")
    last = _draft(client).entry.rules[0].id
    r = client.request("DELETE", f"/studio/{SID}/rules/{last}")
    assert r.status_code == 422 and "at least one rule" in r.text


def test_block_match_and_regime_evaluate(client):
    client.patch(f"/studio/{SID}/blocks/entry", data={"match": "any"})
    client.patch(f"/studio/{SID}/blocks/regime", data={"evaluate": "4h"})
    d = _draft(client)
    assert d.entry.match == "any" and d.regime.evaluate == "4h"


def test_risk_edit_and_bounds(client):
    client.patch(f"/studio/{SID}/risk", data={"name": "take_profit_r", "value": "2.2"})
    assert _draft(client).risk.take_profit_r.value == 2.2
    r = client.patch(
        f"/studio/{SID}/risk", data={"name": "risk_per_trade_pct", "value": "9"}
    )
    assert r.status_code == 422


def test_instrument_toggle_and_last_active_guard(client):
    client.patch(f"/studio/{SID}/instruments/EURUSD")
    d = _draft(client)
    assert [i.active for i in d.instruments] == [True, True, False]
    client.patch(f"/studio/{SID}/instruments/EURUSD")
    r = client.patch(f"/studio/{SID}/instruments/XAUUSD")
    assert r.status_code == 422 and "at least one instrument" in r.text


def test_instrument_add_lands_active(client):
    r = client.post(
        f"/studio/{SID}/instruments", data={"symbol": "dogeusdt", "timeframe": "1h"}
    )
    assert r.status_code == 200 and "DOGEUSDT · 1h" in r.text
    inst = _draft(client).instruments[-1]
    assert (inst.symbol, inst.timeframe, inst.active) == ("DOGEUSDT", "1h", True)


def test_instrument_add_rejects_duplicate_and_bad_input(client):
    r = client.post(
        f"/studio/{SID}/instruments", data={"symbol": "XAUUSD", "timeframe": "1h"}
    )
    assert r.status_code == 422 and "already configured" in r.text
    r = client.post(
        f"/studio/{SID}/instruments", data={"symbol": "BTC/USDT", "timeframe": "1h"}
    )
    assert r.status_code == 422 and "invalid symbol" in r.text
    r = client.post(
        f"/studio/{SID}/instruments", data={"symbol": "BTCUSDT", "timeframe": "3h"}
    )
    assert r.status_code == 422 and "timeframe must be one of" in r.text
    assert client.store.load_draft(SID) is None  # nothing persisted


def test_instrument_delete_and_last_active_guard(client):
    r = client.delete(f"/studio/{SID}/instruments/EURUSD")
    assert r.status_code == 200 and "EURUSD" not in r.text
    assert [i.symbol for i in _draft(client).instruments] == ["XAUUSD", "NAS100"]
    # XAUUSD is the only active one left — removing it would leave none.
    r = client.delete(f"/studio/{SID}/instruments/XAUUSD")
    assert r.status_code == 422 and "at least one instrument" in r.text
    r = client.delete(f"/studio/{SID}/instruments/NOPE")
    assert r.status_code == 422 and "not configured" in r.text


def test_instrument_picker_offers_catalog_symbols_and_timeframes(client):
    page = client.get(f"/studio/{SID}")
    assert 'list="instr-symbols"' in page.text
    # BTCUSDT is in BYBIT_SYMBOLS, so it is always a "downloaded" suggestion
    assert '<option value="BTCUSDT" label="downloaded">' in page.text
    assert '<option value="4h"' in page.text  # timeframe choice


def test_instrument_picker_offers_the_whole_bybit_board(client, monkeypatch):
    """Suggestions are the tradable universe, not just what we already hold.

    The regression this pins: the picker listed only catalog symbols, so a box
    with four downloaded series offered four choices even though `load_bybit_bars`
    fetches any listed contract on first run.
    """
    from web.routes import strategy_studio as ss

    monkeypatch.setattr(
        "data.list_bybit_instruments",
        lambda category="linear", *, fetch=True: ("BTCUSDT", "XRPUSDT", "WIFUSDT"),
    )
    monkeypatch.setattr(ss, "_warm_symbol_universe", lambda: None)
    ss._symbols_cache = None
    try:
        page = client.get(f"/studio/{SID}").text
    finally:
        ss._symbols_cache = None
    # Not downloaded, still offered — and after the downloaded ones.
    assert '<option value="WIFUSDT">' in page
    assert page.index('label="downloaded"') < page.index('<option value="WIFUSDT">')


def test_instrument_picker_survives_an_empty_universe(client, monkeypatch):
    """Bybit unreachable on a cold cache degrades the list, never the page."""
    from web.routes import strategy_studio as ss

    monkeypatch.setattr(
        "data.list_bybit_instruments",
        lambda category="linear", *, fetch=True: (),
    )
    monkeypatch.setattr(ss, "_warm_symbol_universe", lambda: None)
    ss._symbols_cache = None
    try:
        page = client.get(f"/studio/{SID}")
    finally:
        ss._symbols_cache = None
    assert page.status_code == 200
    assert '<option value="BTCUSDT" label="downloaded">' in page.text


def test_draft_survives_reload_and_page_shows_it(client):
    d, _ = client.store.working_copy(SID)
    wt = d.entry.rules[0]
    client.patch(f"/studio/{SID}/rules/{wt.id}", data={"param": "n1", "value": "8"})
    page = client.get(f"/studio/{SID}")
    assert "n1: 8" in page.text and "draft — unsaved" in page.text


def test_save_promotes_draft(client):
    d, _ = client.store.working_copy(SID)
    wt = d.entry.rules[0]
    client.patch(f"/studio/{SID}/rules/{wt.id}", data={"param": "n1", "value": "8"})
    r = client.post(f"/studio/{SID}/save")
    assert r.status_code == 200 and "v2" in r.text and "saved" in r.text
    assert _draft(client) is None
    v2 = client.store.load(SID)
    assert v2.version == 2 and v2.entry.rules[0].params["n1"].value == 8
    assert v2.parent_version == 1


def test_save_without_draft_422(client):
    assert client.post(f"/studio/{SID}/save").status_code == 422


def test_discard(client):
    d, _ = client.store.working_copy(SID)
    wt = d.entry.rules[0]
    client.patch(f"/studio/{SID}/rules/{wt.id}", data={"param": "n1", "value": "8"})
    r = client.post(f"/studio/{SID}/discard")
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"
    assert _draft(client) is None


def test_compiler_still_accepts_edited_draft(client):
    from strategy_studio.compiler import compile_strategy

    client.post(f"/studio/{SID}/blocks/entry/rules", data={"indicator": "rsi"})
    d = _draft(client)
    c = compile_strategy(d)
    assert any(x.indicator == "rsi" for x in c.entry.conditions)
