"""Studio'da external-katalog enstrümanları (noktalı id'ler: QQQ.NASDAQ).

Sözleşme üç yüzeye yayılır ve üçü de burada kilitlenir:
- `add_instrument` noktalı id'yi kabul eder ama timeframe'i external kümesiyle
  (1m/5m/15m/1h/4h/1d) sınırlar — 30m/12h Bybit'e özgüdür.
- Adaptör noktayı ayırt eder: recipe external şekle döner, loader
  `load_external_bars`'a gider (Bybit yolu birebir korunur).
- Deploy kapısı noktalı id'yi reddeder: canlı Bybit akışı yoktur, node
  sessizce bar'sız otururdu.
"""

from __future__ import annotations

import pytest

from scripts.seed_studio import build_fixture
from strategy_studio.backtest import NautilusBacktestAdapter, UnsupportedStrategy
from strategy_studio.mutations import MutationError, add_instrument
from strategy_studio.runner import RunnerError, build_node_config


def _defn():
    return build_fixture()


# ── add_instrument ───────────────────────────────────────────────────


def test_add_accepts_dotted_external_id():
    d = _defn()
    add_instrument(d, "qqq.nasdaq", "1h")
    assert any(i.symbol == "QQQ.NASDAQ" for i in d.instruments)
    # BRK.B.NASDAQ: birden çok nokta da meşru bir external id'dir.
    add_instrument(d, "BRK.B.NASDAQ", "1d")
    assert any(i.symbol == "BRK.B.NASDAQ" for i in d.instruments)


@pytest.mark.parametrize("bad", [".NASDAQ", "QQQ.", "QQQ..NASDAQ", "QQ Q.NASDAQ"])
def test_add_rejects_malformed_dotted_ids(bad):
    with pytest.raises(MutationError, match="TICKER.VENUE"):
        add_instrument(_defn(), bad, "1h")


@pytest.mark.parametrize("tf", ["30m", "12h"])
def test_add_rejects_bybit_only_timeframes_for_external(tf):
    with pytest.raises(MutationError, match="external instrument"):
        add_instrument(_defn(), "QQQ.NASDAQ", tf)


def test_bybit_symbols_keep_the_full_timeframe_set():
    d = _defn()
    add_instrument(d, "XRPUSDT", "30m")  # external'da yok, Bybit'te var
    assert any(i.symbol == "XRPUSDT" and i.timeframe == "30m" for i in d.instruments)


# ── adapter ──────────────────────────────────────────────────────────


def test_recipe_shape_is_discriminated_by_the_dot():
    a = NautilusBacktestAdapter(bars_loader=lambda s, t: None)
    ext = a._recipe("QQQ.NASDAQ", "1h")
    assert ext == {
        "source": "external",
        "instrument_id": "QQQ.NASDAQ",
        "granularity": "1-HOUR",
        "initial_capital": a.initial_capital,
    }
    byb = a._recipe("BTCUSDT", "1h")
    assert byb["category"] == "linear" and byb["interval"] == "60"
    assert "source" not in byb


def test_default_loader_routes_dotted_ids_to_the_external_catalog(monkeypatch):
    calls = {}

    def fake_external(symbol, gran, start=None, end=None):
        calls["args"] = (symbol, gran)
        return "EXT_FRAME"

    def fail_bybit(*a, **k):  # Bybit yoluna sapma olmadığını da kanıtla
        raise AssertionError("bybit loader must not be called for dotted ids")

    monkeypatch.setattr("data.load_external_bars", fake_external)
    monkeypatch.setattr("data.load_bybit_bars", fail_bybit)
    a = NautilusBacktestAdapter()
    assert a._default_loader("AA.NASDAQ", "1d") == "EXT_FRAME"
    assert calls["args"] == ("AA.NASDAQ", "1-DAY")


def test_external_granularity_rejects_bybit_only_timeframes():
    with pytest.raises(UnsupportedStrategy):
        NautilusBacktestAdapter._external_granularity("30m")


# ── deploy gate ──────────────────────────────────────────────────────


def test_deploy_rejects_external_instruments():
    from strategy_studio.runner import SUPPORTED_ARTIFACT_SCHEMAS

    artifact = {
        "artifact_schema": next(iter(SUPPORTED_ARTIFACT_SCHEMAS)),
        "environment": "paper",
        "instruments": [
            {"symbol": "BTCUSDT", "timeframe": "1h"},
            {"symbol": "QQQ.NASDAQ", "timeframe": "1d"},
        ],
    }
    with pytest.raises(RunnerError, match="backtest-only.*QQQ.NASDAQ"):
        build_node_config(artifact, trader_id="T-1")
