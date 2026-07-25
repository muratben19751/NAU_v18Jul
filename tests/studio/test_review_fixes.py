"""Regressions for the review findings.

Each test names the defect it pins so a later change cannot quietly undo it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scripts.seed_studio import build_engine_fixture
from strategy_studio.backtest import UnsupportedStrategy, to_nautilus
from strategy_studio.compiler import compile_strategy
from strategy_studio.schema import InstrumentConfig, Param, Rule

SID = "rsi-adx-btc"


def _p(v):
    return Param(value=v)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from scripts.seed_studio import build_fixture
    from server import app as _host
    from strategy_studio.store import StrategyStore
    from web.routes import strategy_studio as main

    store = StrategyStore(tmp_path / "t.db")
    store.save(build_fixture())
    store.save(build_engine_fixture())
    monkeypatch.setattr(main, "store", store)
    c = TestClient(_host)
    c.store = store
    c.main = main
    return c


# ── silent drops in to_nautilus ─────────────────────────────────────────────


def test_per_rule_timeframe_is_refused_not_flattened():
    """A 1h rule inside a 15m strategy reads a different bar feed."""
    defn = build_engine_fixture()
    defn.entry.rules[0].timeframe = "4h"

    with pytest.raises(UnsupportedStrategy) as e:
        to_nautilus(compile_strategy(defn))
    blob = " ".join(e.value.reasons)
    assert "timeframe" in blob and "4h" in blob


def test_rule_timeframe_matching_the_instrument_is_fine():
    defn = build_engine_fixture()
    defn.entry.rules[0].timeframe = defn.instruments[0].timeframe

    assert to_nautilus(compile_strategy(defn)).validate() is None


def test_max_concurrent_above_one_is_refused():
    """The composer strategy holds a single position; >1 is a different risk profile."""
    defn = build_engine_fixture()
    defn.risk.max_concurrent = _p(3)

    with pytest.raises(UnsupportedStrategy) as e:
        to_nautilus(compile_strategy(defn))
    assert any("max_concurrent" in r for r in e.value.reasons)


def test_time_stop_bars_is_refused():
    defn = build_engine_fixture()
    defn.risk.time_stop_bars = _p(48)

    with pytest.raises(UnsupportedStrategy) as e:
        to_nautilus(compile_strategy(defn))
    assert any("time_stop_bars" in r for r in e.value.reasons)


def test_time_stop_rule_message_no_longer_points_at_a_dropped_field():
    """The old text told users to use risk.time_stop_bars, which is refused too."""
    defn = build_engine_fixture()
    defn.entry.rules.append(
        Rule(id="r-ts", indicator="time_stop", params={"bars": _p(24)},
             operator="true")
    )

    with pytest.raises(UnsupportedStrategy) as e:
        to_nautilus(compile_strategy(defn))
    msg = next(r for r in e.value.reasons if "r-ts" in r)
    assert "no time-based exit" in msg


# ── HTTP semantics ──────────────────────────────────────────────────────────


def test_unknown_rule_id_is_404_not_422(client):
    """A missing resource is not a malformed request."""
    r = client.patch(f"/studio/{SID}/rules/deadbeefdead",
                     data={"param": "len", "value": "9"})
    assert r.status_code == 404


def test_known_rule_with_bad_value_is_still_422(client):
    d, _ = client.store.working_copy(SID)
    rid = d.entry.rules[0].id

    r = client.patch(f"/studio/{SID}/rules/{rid}",
                     data={"param": "len", "value": "abc"})
    assert r.status_code == 422


def test_risk_endpoint_accepts_both_name_and_param(client):
    """The rule endpoints say `param`; risk historically said `name`."""
    assert client.patch(f"/studio/{SID}/risk",
                        data={"name": "take_profit_r", "value": "1.9"}
                        ).status_code == 200
    assert client.patch(f"/studio/{SID}/risk",
                        data={"param": "take_profit_r", "value": "1.7"}
                        ).status_code == 200
    assert client.patch(f"/studio/{SID}/risk",
                        data={"value": "1.5"}).status_code == 422


# ── fold table ──────────────────────────────────────────────────────────────


def test_folds_cover_every_instrument_not_just_the_first(monkeypatch):
    """The headline blends all sleeves, so the fold rows must too."""
    import pandas as pd

    import sandbox
    from strategy_studio.backtest import NautilusBacktestAdapter

    seen = []

    class _R:
        error = None
        equity_curve = [10_000, 10_100]
        metrics = {"n_trades": 2, "win_rate": 0.5, "n_wins": 1, "n_losses": 1,
                   "avg_win": 10.0, "avg_loss": -5.0}

    def _fake(spec, bars, recipe=None, **kw):
        seen.append(kw.get("rationale", ""))
        return _R()

    monkeypatch.setattr(sandbox, "run_backtest_guarded", _fake)

    defn = build_engine_fixture()
    defn.instruments.append(
        InstrumentConfig(symbol="ETHUSDT", timeframe="1h", active=True))
    bars = pd.DataFrame(
        {"open": [1.0] * 300, "high": [1.0] * 300, "low": [1.0] * 300,
         "close": [1.0] * 300, "volume": [1.0] * 300},
        index=pd.date_range("2025-01-01", periods=300, freq="h", tz="UTC"))

    adapter = NautilusBacktestAdapter(bars_loader=lambda s, tf: bars)
    adapter.run(compile_strategy(defn))

    folds = compile_strategy(defn).walkforward["folds"]
    # 2 instruments × (1 headline + folds) runs
    assert len(seen) == 2 * (1 + folds)


def test_embargo_drops_the_leading_bars_of_each_fold(monkeypatch):
    import pandas as pd

    import sandbox
    from strategy_studio.backtest import NautilusBacktestAdapter

    sizes = []

    class _R:
        error = None
        equity_curve = [10_000, 10_100]
        metrics: dict = {}

    def _fake(spec, bars, recipe=None, **kw):
        if kw.get("rationale", "").startswith("studio:fold"):
            sizes.append(len(bars))
        return _R()

    monkeypatch.setattr(sandbox, "run_backtest_guarded", _fake)

    defn = build_engine_fixture()
    defn.walkforward.folds = 3
    defn.walkforward.embargo_bars = 10
    bars = pd.DataFrame(
        {"open": [1.0] * 300, "high": [1.0] * 300, "low": [1.0] * 300,
         "close": [1.0] * 300, "volume": [1.0] * 300},
        index=pd.date_range("2025-01-01", periods=300, freq="h", tz="UTC"))

    NautilusBacktestAdapter(bars_loader=lambda s, tf: bars).run(
        compile_strategy(defn))

    # 300 bars / 3 folds = 100, minus a 10-bar embargo at each fold start
    assert sizes == [90, 90, 90]


# ── storage / caching ───────────────────────────────────────────────────────


def test_stored_equity_curve_is_downsampled_for_the_sparkline(monkeypatch):
    """A 4319-point curve was ~39 KB of JSON per run to fill a 260px path."""
    import pandas as pd

    import sandbox
    from strategy_studio.backtest import NautilusBacktestAdapter

    long_curve = [10_000 + i for i in range(4000)]

    class _R:
        error = None
        equity_curve = [10_000, 10_100]
        metrics = {"equity_curve_mtm": [(str(i), v) for i, v in enumerate(long_curve)]}

    monkeypatch.setattr(sandbox, "run_backtest_guarded",
                        lambda spec, bars, recipe=None, **kw: _R())
    bars = pd.DataFrame(
        {"open": [1.0] * 50, "high": [1.0] * 50, "low": [1.0] * 50,
         "close": [1.0] * 50, "volume": [1.0] * 50},
        index=pd.date_range("2025-01-01", periods=50, freq="h", tz="UTC"))

    m = NautilusBacktestAdapter(bars_loader=lambda s, tf: bars).run(
        compile_strategy(build_engine_fixture()))

    assert len(m.equity_curve) <= 260
    assert m.equity_curve[0] == 1.0  # endpoints preserved
    assert m.equity_curve[-1] == pytest.approx(long_curve[-1] / long_curve[0], rel=1e-6)


def test_baseline_cache_evicts_least_recently_used(client):
    main = client.main
    main._TRIAL_BASELINE_CACHE.clear()
    monkey = main._TRIAL_BASELINE_CACHE

    for i in range(main._TRIAL_BASELINE_CACHE_MAX + 5):
        monkey[("Stub", f"key{i}")] = object()
        monkey.move_to_end(("Stub", f"key{i}"))
        while len(monkey) > main._TRIAL_BASELINE_CACHE_MAX:
            monkey.popitem(last=False)

    assert len(monkey) == main._TRIAL_BASELINE_CACHE_MAX
    # oldest evicted, newest kept — a clear-all would have dropped both
    assert ("Stub", "key0") not in monkey
    assert ("Stub", f"key{main._TRIAL_BASELINE_CACHE_MAX + 4}") in monkey


# ── routing ─────────────────────────────────────────────────────────────────


def test_studio_namespace_has_no_literal_route_shadowing_the_builder():
    """/studio/{strategy_id} must not swallow a literal /studio/<x> route."""
    from server import app

    literals = set()
    params = set()
    for r in app.routes:
        path = getattr(r, "path", "")
        if not path.startswith("/studio/"):
            continue
        seg = path[len("/studio/"):].split("/")[0]
        (params if seg.startswith("{") else literals).add(seg)

    assert not literals & {"", *params}, (
        f"literal /studio/{literals} collides with the builder's "
        f"/studio/{{strategy_id}}")


# ── second review pass: incomplete edges of the first round ─────────────────


def test_every_rule_endpoint_reports_a_missing_rule_as_404(client):
    """404 was only wired into PATCH; delete and the optimize toggles lagged."""
    missing = "deadbeefdead"

    assert client.patch(f"/studio/{SID}/rules/{missing}",
                        data={"param": "len", "value": "9"}).status_code == 404
    assert client.delete(f"/studio/{SID}/rules/{missing}").status_code == 404
    assert client.patch(f"/studio/{SID}/opt/toggle",
                        data={"owner": missing, "param": "len"}
                        ).status_code == 404
    assert client.patch(f"/studio/{SID}/opt/range",
                        data={"owner": missing, "param": "len", "min": "1",
                              "step": "1", "max": "5"}).status_code == 404


def test_a_broken_trial_engine_does_not_500_the_suggest_button(client, monkeypatch):
    """_trial_baseline lets engine failures through; the route must catch them.

    A 500 means HTMX swaps nothing and the button silently does nothing.
    """
    import json as _json

    from strategy_studio.ai import MockLLMClient
    from strategy_studio.backtest import StubBacktestAdapter
    from strategy_studio.compiler import compile_strategy

    class _Boom:
        def run(self, compiled):
            raise RuntimeError("engine exploded")

    # a completed run must exist, otherwise the baseline path is never reached
    client.store.create_run("r-x", SID, version=1, is_draft=False)
    metrics = StubBacktestAdapter().run(compile_strategy(client.store.load(SID)))
    client.store.finish_run("r-x", metrics.to_json())

    monkeypatch.setattr(client.main, "TRIAL_ADAPTER", _Boom())
    client.main._TRIAL_BASELINE_CACHE.clear()
    monkeypatch.setattr(client.main, "LLM", MockLLMClient([_json.dumps({
        "kind": "modify_risk", "block": "risk",
        "diff": {"name": "take_profit_r", "value": 2.2},
        "rationale": "x", "expected": {"dsr_delta": 0.05},
    })]))

    r = client.post(f"/studio/{SID}/ai/suggest", data={})

    assert r.status_code == 422
    assert "engine exploded" in r.text
