import json

import pytest
from fastapi.testclient import TestClient

SID = "wt-funding-v3"


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


def _tiny_sweep(client):
    """Reduce the fixture to a 2-param, ~20-combo sweep."""
    d, _ = client.store.working_copy(SID)
    for _b, _rid, name, p in d.optimized_params():
        p.optimize = None
    wt = d.entry.rules[0]
    from strategy_studio.schema import OptimizeRange
    wt.params["n1"].optimize = OptimizeRange(min=8, step=2, max=12)
    d.risk.take_profit_r.optimize = OptimizeRange(min=1.4, step=0.2, max=2.0)
    client.store.save_draft(d)
    return d


def test_toggle_optimize_roundtrip(client):
    d, _ = client.store.working_copy(SID)
    wt = d.entry.rules[0]
    r = client.patch(f"/studio/{SID}/opt/toggle",
                     data={"owner": wt.id, "param": "n1"})
    assert r.status_code == 200
    assert 'hx-swap-oob="true"' in r.text          # block refreshed oob
    d2 = client.store.load_draft(SID)
    assert d2.entry.rules[0].params["n1"].optimize is None  # was on -> off
    client.patch(f"/studio/{SID}/opt/toggle",
                 data={"owner": wt.id, "param": "n1"})
    d3 = client.store.load_draft(SID)
    assert d3.entry.rules[0].params["n1"].optimize is not None  # default range


def test_toggle_risk_param(client):
    client.patch(f"/studio/{SID}/opt/toggle",
                 data={"owner": "risk", "param": "max_concurrent"})
    d = client.store.load_draft(SID)
    assert d.risk.max_concurrent.optimize is not None


def test_range_edit_and_validation(client):
    d, _ = client.store.working_copy(SID)
    wt = d.entry.rules[0]
    r = client.patch(f"/studio/{SID}/opt/range",
                     data={"owner": wt.id, "param": "n1",
                           "min": "4", "step": "1", "max": "10"})
    assert r.status_code == 200
    d2 = client.store.load_draft(SID)
    opt = d2.entry.rules[0].params["n1"].optimize
    assert (opt.min, opt.step, opt.max) == (4, 1, 10)
    bad = client.patch(f"/studio/{SID}/opt/range",
                       data={"owner": wt.id, "param": "n1",
                             "min": "10", "step": "1", "max": "4"})
    assert bad.status_code == 422


def test_optimize_end_to_end_and_sorted(client):
    _tiny_sweep(client)
    r = client.post(f"/studio/{SID}/optimize")
    assert r.status_code == 200
    opt = client.store.latest_opt(SID)
    assert opt["status"] == "done"
    results = json.loads(opt["results"])
    assert 0 < len(results) <= 10
    # Ranked by the walk-forward score (penalized fold objective), which is the
    # number the panel shows next to the rank. The reported `dsr` is a
    # different statistic — deflated, over the stitched OOS curve — so it is
    # deliberately NOT the sort key.
    scores = [x["score"] for x in results]
    assert scores == sorted(scores, reverse=True)
    panel = client.get(f"/studio/{SID}/optimize/panel")
    assert "APPLY" in panel.text and "Top" in panel.text


def test_panel_shows_what_the_ranking_and_the_dsr_actually_mean(client):
    """The months are a ratio, and the reported DSR is deflated — both are
    surprising enough that the panel has to say so, or the numbers read as
    calendar lengths and as the single-run DSR."""
    _tiny_sweep(client)
    client.post(f"/studio/{SID}/optimize")
    panel = client.get(f"/studio/{SID}/optimize/panel").text

    assert "of sample" in panel                    # in-sample months → share
    assert "DSR*" in panel and "trials" in panel    # deflated, and against what
    assert "out-of-sample" in panel.lower()


def test_optimize_no_params_422(client):
    d = _tiny_sweep(client)
    for _b, _rid, _n, p in d.optimized_params():
        p.optimize = None
    client.store.save_draft(d)
    r = client.post(f"/studio/{SID}/optimize")
    assert r.status_code == 422 and "no parameters" in r.text


def test_optimize_limit_422(client, monkeypatch):
    from web.routes import strategy_studio as main
    _tiny_sweep(client)
    monkeypatch.setattr(main, "OPTIMIZER_MAX_RUNS", 10)
    r = client.post(f"/studio/{SID}/optimize")
    assert r.status_code == 422 and "exceeds the optimizer limit" in r.text


def test_apply_result_updates_draft(client):
    _tiny_sweep(client)
    client.post(f"/studio/{SID}/optimize")
    best = json.loads(client.store.latest_opt(SID)["results"])[0]
    r = client.post(f"/studio/{SID}/optimize/apply", data={"rank": "1"})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"
    d = client.store.load_draft(SID)
    for key, val in best["params"].items():
        owner, name = key.split(".", 1)
        if owner == "risk":
            assert float(getattr(d.risk, name).value) == pytest.approx(val)
        else:
            rule = next(r_ for _b, r_ in d.iter_rules() if r_.id == owner)
            assert float(rule.params[name].value) == pytest.approx(val)


def test_apply_bad_rank_422(client):
    _tiny_sweep(client)
    client.post(f"/studio/{SID}/optimize")
    r = client.post(f"/studio/{SID}/optimize/apply", data={"rank": "99"})
    assert r.status_code == 422
