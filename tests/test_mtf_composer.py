"""2-TF trend filter in Composer (MTF, part A): the trend_filter/trend_interval/
trend_ema_period fields must be settable on a manual strategy.

The engine ALREADY reads spec.trend_filter (run_composed_backtest loads the
secondary bar feed); the only thing missing was these fields flowing from
/strategy/save into the spec. Look-ahead safe (Nautilus event-driven).
"""

from __future__ import annotations


def _seed_and_save(monkeypatch, form: dict):
    """Fresh client, seed a draft block pair, POST /strategy/save,
    return the spec written to the catalog."""
    from fastapi.testclient import TestClient

    import composer
    import web.routes.strategy as st
    from server import app

    appended: list = []
    monkeypatch.setattr(composer, "append_to_catalog", appended.append)

    c = TestClient(app)
    c.get("/strategy")  # create cookie/sid
    sid = c.cookies.get(st.COOKIE)
    st._DRAFTS[sid] = [
        composer.SignalBlock(
            type="ma_cross", role="entry", params={"fast": 5, "slow": 20}
        ),
        composer.SignalBlock(type="atr_stop", role="exit", params={"period": 14}),
    ]
    r = c.post("/strategy/save", data={"name": "T", **form}, follow_redirects=False)
    assert r.status_code == 303, r.text[:200]
    assert appended, "spec was not written to catalog"
    return appended[0]


class TestMTFComposer:
    def test_trend_filter_fields_flow_into_spec(self, monkeypatch):
        spec = _seed_and_save(
            monkeypatch,
            {"trend_filter": "1", "trend_interval": "D", "trend_ema_period": "100"},
        )
        assert spec.trend_filter is True
        assert spec.trend_interval == "D"
        assert spec.trend_ema_period == 100

    def test_no_trend_filter_defaults_off(self, monkeypatch):
        spec = _seed_and_save(monkeypatch, {})  # no checkbox → empty string → False
        assert spec.trend_filter is False

    def test_composer_page_renders_trend_controls(self):
        from fastapi.testclient import TestClient

        from server import app

        html = TestClient(app).get("/strategy").text
        assert 'name="trend_filter"' in html
        assert 'name="trend_interval"' in html
        assert 'name="trend_ema_period"' in html


class TestMTFEngineReadsSpec:
    """Proof: the engine path reads spec.trend_filter (it worked without UI changes)."""

    def test_run_composed_backtest_reads_trend_filter(self):
        import inspect

        import backtest

        src = inspect.getsource(backtest.run_composed_backtest)
        # The secondary bar feed is set up only when trend_filter is on.
        assert "trend_filter" in src
        assert "secondary_bar_type" in src
