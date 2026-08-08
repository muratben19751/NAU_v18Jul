"""`_require_auth` middleware + `_is_authenticated`/`_auth_cookie_value`.

DeepR 2026-08-08 [ORTA]: NAU_ACCESS_TOKEN's whole gate — the /login form, the
cookie check, and the HTMX-vs-browser redirect split — had zero test
coverage. `_ACCESS_TOKEN` is read once at import time, so tests monkeypatch
the already-imported `server` module attribute rather than the environment
(see the `auth_client`/`secure_auth_client` fixtures). Cookie-round-trip
assertions use `base_url="https://testserver"` since the cookie is `secure`
and httpx's jar refuses to resend a Secure cookie over a plain-http base URL
— see [[nau_deepr_toplu_sertlestirme_2026_08]] and test_auth_cookie_logout.py
for the same gotcha.

Wiki References
---------------
See: [[nau_deepr_toplu_sertlestirme_2026_08]]
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server

TOKEN = "s3cr3t-test-token"


@pytest.fixture()
def auth_client(monkeypatch):
    """Plain http base_url — used where the response itself (redirect target,
    status code) is what's asserted, not whether a cookie round-trips."""
    monkeypatch.setattr(server, "_ACCESS_TOKEN", TOKEN)
    server._login_failures.clear()
    client = TestClient(server.app)
    yield client
    server._login_failures.clear()


@pytest.fixture()
def secure_auth_client(monkeypatch):
    """https base_url — required for a logged-in client's cookie to actually
    be resent on later requests (see module docstring)."""
    monkeypatch.setattr(server, "_ACCESS_TOKEN", TOKEN)
    server._login_failures.clear()
    client = TestClient(server.app, base_url="https://testserver")
    yield client
    server._login_failures.clear()


class TestAuthDisabledIsANoOp:
    def test_no_access_token_means_every_route_is_open(self, monkeypatch):
        monkeypatch.setattr(server, "_ACCESS_TOKEN", "")
        client = TestClient(server.app)

        r = client.get("/", follow_redirects=False)

        assert r.status_code == 200


class TestUnauthenticatedBrowserRequest:
    def test_protected_route_redirects_to_login(self, auth_client):
        r = auth_client.get("/", follow_redirects=False)

        assert r.status_code == 303
        assert r.headers["location"] == "/login"

    def test_login_page_itself_is_reachable_without_a_cookie(self, auth_client):
        r = auth_client.get("/login", follow_redirects=False)

        assert r.status_code == 200

    def test_static_assets_are_reachable_without_a_cookie(self, auth_client):
        r = auth_client.get("/static/app.css", follow_redirects=False)

        assert r.status_code == 200

    def test_bogus_cookie_value_is_rejected(self, auth_client):
        auth_client.cookies.set("nau_auth", "not-the-right-hash")

        r = auth_client.get("/", follow_redirects=False)

        assert r.status_code == 303
        assert r.headers["location"] == "/login"


class TestUnauthenticatedHtmxRequest:
    def test_htmx_request_gets_401_with_hx_redirect_not_a_303(self, auth_client):
        r = auth_client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)

        assert r.status_code == 401
        assert r.headers["hx-redirect"] == "/login"


class TestAuthenticatedRequest:
    def test_valid_cookie_reaches_the_protected_route(self, secure_auth_client):
        secure_auth_client.post("/login", data={"token": TOKEN}, follow_redirects=False)

        r = secure_auth_client.get("/", follow_redirects=False)

        assert r.status_code == 200

    def test_authenticated_htmx_request_is_not_redirected(self, secure_auth_client):
        secure_auth_client.post("/login", data={"token": TOKEN}, follow_redirects=False)

        r = secure_auth_client.get(
            "/", headers={"HX-Request": "true"}, follow_redirects=False
        )

        assert r.status_code == 200


class TestAuthCookieValueHelpers:
    def test_is_authenticated_true_only_for_the_correct_cookie(self, monkeypatch):
        from starlette.requests import Request

        monkeypatch.setattr(server, "_ACCESS_TOKEN", TOKEN)
        correct = server._auth_cookie_value(TOKEN)

        def _req(cookie_value):
            scope = {
                "type": "http",
                "headers": [(b"cookie", f"nau_auth={cookie_value}".encode())],
            }
            return Request(scope)

        assert server._is_authenticated(_req(correct)) is True
        assert server._is_authenticated(_req("wrong")) is False
        assert server._is_authenticated(_req("")) is False

    def test_auth_cookie_value_is_a_deterministic_hash_of_the_token(self):
        a = server._auth_cookie_value("token-a")
        b = server._auth_cookie_value("token-a")
        c = server._auth_cookie_value("token-b")

        assert a == b
        assert a != c
        assert a != "token-a"  # not stored/compared in plaintext
