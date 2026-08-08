"""GET "/" (dashboard) — the app's actual first screen.

DeepR 2026-08-08 [YÜKSEK]: no test file in the suite ever called
`client.get("/")` — a regression anywhere in its chain (state.py,
composer.load_catalog, get_market_info, or a template global like
loop_running()/first_studio_strategy_id()) would surface only as a blank/500
page for a real user, unreproduced in CI.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from server import app

    return TestClient(app)


class TestDashboardRoute:
    def test_root_renders_200(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_root_shows_dashboard_page_title(self, client):
        r = client.get("/")
        assert "Dashboard" in r.text

    def test_root_reflects_loop_not_running_by_default(self, client):
        from state import get_state

        state = get_state()
        with state.lock:
            state.running = False

        r = client.get("/")

        assert r.status_code == 200
        # loop_running() template global and the route's own `running` context
        # value both drive this — a broken chain here is exactly what an
        # untested "/" previously could not catch.
        assert "idle" in r.text.lower() or "stopped" in r.text.lower()
