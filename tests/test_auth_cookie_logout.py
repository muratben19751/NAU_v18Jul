"""Auth cookie hardening + /logout (DeepR 2026-08-08 [ORTA]).

Before this: `nau_auth` was set without `secure`, valid for 30 days, and
there was no way to end a session short of rotating NAU_ACCESS_TOKEN itself
(no /logout route at all). `_ACCESS_TOKEN` is read once at import time, so
tests monkeypatch the already-imported `server` module attribute rather than
the environment.

The cookie is now `Secure`, which httpx's cookie jar takes literally: it
will not resend a Secure cookie on a request whose URL scheme isn't https.
`TestClient`'s default base_url is `http://testserver`, so the fixture below
overrides it to `https://testserver` — otherwise every "authenticated"
request in these tests would silently drop the cookie and land back on the
login page, which would make a broken auth flow look identical to a working
one instead of failing loudly.

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
    monkeypatch.setattr(server, "_ACCESS_TOKEN", TOKEN)
    server._login_failures.clear()
    client = TestClient(server.app, base_url="https://testserver")
    yield client
    server._login_failures.clear()


def _login(client):
    return client.post("/login", data={"token": TOKEN}, follow_redirects=False)


class TestAuthCookieFlags:
    def test_cookie_is_marked_secure(self, auth_client):
        r = _login(auth_client)
        set_cookie = r.headers.get("set-cookie", "")
        assert "Secure" in set_cookie
        assert "HttpOnly" in set_cookie

    def test_authenticated_request_actually_round_trips_the_cookie(self, auth_client):
        """Sanity check for the base_url=https fixture itself — if this ever
        fails, every other 'authenticated' assertion in this file is a
        false positive (the redirect-to-login would masquerade as success)."""
        _login(auth_client)

        r = auth_client.get("/", follow_redirects=False)

        assert r.status_code == 200


class TestLogout:
    def test_logout_clears_the_cookie_and_redirects_to_login(self, auth_client):
        _login(auth_client)
        assert auth_client.get("/", follow_redirects=False).status_code == 200

        r = auth_client.post("/logout", follow_redirects=False)

        assert r.status_code == 303
        assert r.headers["location"] == "/login"
        # the client's cookie jar no longer carries a valid nau_auth
        r2 = auth_client.get("/", follow_redirects=False)
        assert r2.status_code == 303
        assert r2.headers["location"] == "/login"

    def test_logout_without_a_session_still_redirects_cleanly(self, auth_client):
        r = auth_client.post("/logout", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"


class TestAuthEnabledTemplateGlobal:
    def test_true_when_access_token_set(self, auth_client):
        assert server.templates.env.globals["auth_enabled"]() is True

    def test_false_when_access_token_unset(self, monkeypatch):
        monkeypatch.setattr(server, "_ACCESS_TOKEN", "")
        assert server.templates.env.globals["auth_enabled"]() is False

    def test_logout_link_hidden_when_auth_disabled(self, monkeypatch):
        monkeypatch.setattr(server, "_ACCESS_TOKEN", "")
        client = TestClient(server.app)

        r = client.get("/")

        assert r.status_code == 200
        assert 'action="/logout"' not in r.text

    def test_logout_link_shown_when_auth_enabled(self, auth_client):
        _login(auth_client)

        r = auth_client.get("/", follow_redirects=False)

        assert r.status_code == 200
        assert 'action="/logout"' in r.text
