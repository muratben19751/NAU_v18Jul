"""POST /data/refresh/index date validation (DeepR 2026-08-08 [DÜŞÜK]).

Before this, `start`/`end` went straight to `refresh_row("index", ...)` with
no format check; `datetime.fromisoformat` raised deep inside, and the
route's blanket `except Exception: raise HTTPException(500, str(e))` turned
a bad user-typed date into a 500 with the raw Python exception text leaked
to the client. web/routes/backtest.py already had this exact check
(`_invalid_date_range`, 400 + `HX-Toast`) for its own date fields; this
route now has the same contract (400, no HX-Toast — this endpoint returns a
row fragment, not a toast-driven form).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from server import app

    return TestClient(app)


def _post(client, **overrides):
    data = {"ticker": "I:SPX", "granularity": "1d"}
    data.update(overrides)
    return client.post("/data/refresh/index", data=data)


class TestInvalidDateRange:
    def test_unparseable_start_is_400_not_500(self, client):
        r = _post(client, start="not-a-date")
        assert r.status_code == 400
        assert "YYYY-MM-DD" in r.text
        assert "Traceback" not in r.text

    def test_malformed_calendar_date_is_400(self, client):
        r = _post(client, start="2024-13-45")
        assert r.status_code == 400
        assert "YYYY-MM-DD" in r.text

    def test_end_before_start_is_400(self, client):
        r = _post(client, start="2024-06-01", end="2024-01-01")
        assert r.status_code == 400
        assert "before the start date" in r.text

    def test_no_raw_exception_text_leaks_to_the_client(self, client):
        r = _post(client, start="garbage")
        assert "ValueError" not in r.text
        assert "isoformat" not in r.text


class TestValidDateRangeIsUnaffected:
    def test_blank_dates_pass_validation(self, client, monkeypatch):
        import web.routes.data as data_mod

        monkeypatch.setattr(
            data_mod,
            "refresh_row",
            lambda *a, **k: {"source": "index", "key": "I:SPX", "bar_types": []},
        )

        r = _post(client, start="", end="")

        assert r.status_code == 200

    def test_well_formed_range_passes_validation(self, client, monkeypatch):
        import web.routes.data as data_mod

        monkeypatch.setattr(
            data_mod,
            "refresh_row",
            lambda *a, **k: {"source": "index", "key": "I:SPX", "bar_types": []},
        )

        r = _post(client, start="2024-01-01", end="2024-06-01")

        assert r.status_code == 200
