"""Regression tests for /login's brute-force guard.

DeepR 2026-08-08 [YÜKSEK]: /login (single-operator NAU_ACCESS_TOKEN auth) had
no attempt limit and failures were never logged — a script could grind the
shared secret unnoticed on an internet-facing (Cloudflare-tunnelled) install.
`_ACCESS_TOKEN` is read once at import time, so tests must monkeypatch the
already-imported module attribute rather than the environment.

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
    client = TestClient(server.app)
    yield client
    server._login_failures.clear()


class TestLoginBasics:
    def test_correct_token_logs_in_and_sets_cookie(self, auth_client):
        r = auth_client.post("/login", data={"token": TOKEN}, follow_redirects=False)
        assert r.status_code == 303
        assert "nau_auth" in r.cookies

    def test_wrong_token_is_401_and_does_not_set_cookie(self, auth_client):
        r = auth_client.post("/login", data={"token": "wrong"}, follow_redirects=False)
        assert r.status_code == 401
        assert "nau_auth" not in r.cookies


class TestLoginRateLimit:
    def test_sixth_failed_attempt_in_the_window_is_rate_limited(self, auth_client):
        for _ in range(server._LOGIN_MAX_ATTEMPTS):
            r = auth_client.post(
                "/login", data={"token": "wrong"}, follow_redirects=False
            )
            assert r.status_code == 401

        r = auth_client.post("/login", data={"token": "wrong"}, follow_redirects=False)
        assert r.status_code == 429

    def test_rate_limit_blocks_even_the_correct_token(self, auth_client):
        """The limiter must gate on the failure count, not re-check the token
        first — otherwise it is trivially bypassed by the one request that
        matters to an attacker."""
        for _ in range(server._LOGIN_MAX_ATTEMPTS):
            auth_client.post("/login", data={"token": "wrong"}, follow_redirects=False)

        r = auth_client.post("/login", data={"token": TOKEN}, follow_redirects=False)

        assert r.status_code == 429
        assert "nau_auth" not in r.cookies

    def test_a_successful_login_clears_the_failure_count(self, auth_client):
        for _ in range(server._LOGIN_MAX_ATTEMPTS - 1):
            auth_client.post("/login", data={"token": "wrong"}, follow_redirects=False)

        r = auth_client.post("/login", data={"token": TOKEN}, follow_redirects=False)
        assert r.status_code == 303

        # the slate is clean again — a fresh run of failures does not
        # immediately trip the limiter because of the earlier attempts
        for _ in range(server._LOGIN_MAX_ATTEMPTS - 1):
            r = auth_client.post(
                "/login", data={"token": "wrong"}, follow_redirects=False
            )
            assert r.status_code == 401

    def test_rate_limit_is_scoped_per_client_ip(self):
        server._login_failures.clear()
        for _ in range(server._LOGIN_MAX_ATTEMPTS):
            server._record_login_failure("1.2.3.4")

        assert server._login_rate_limited("1.2.3.4") is True
        assert server._login_rate_limited("9.9.9.9") is False
        server._login_failures.clear()

    def test_failures_outside_the_window_do_not_count(self, monkeypatch):
        server._login_failures.clear()
        times = iter([0.0, 0.0, 0.0, 0.0, 0.0, server._LOGIN_WINDOW_S + 1])

        monkeypatch.setattr(server._time, "monotonic", lambda: next(times))
        for _ in range(server._LOGIN_MAX_ATTEMPTS):
            server._record_login_failure("2.2.2.2")

        # the 6th call to monotonic() lands after the window — stale entries
        # must have aged out, so this IP is no longer rate-limited
        assert server._login_rate_limited("2.2.2.2") is False
        server._login_failures.clear()


class TestClientIpPrefersCloudflareHeader:
    """DeepR 2026-08-09 [DÜŞÜK]: this app is only reachable through a
    Cloudflare Tunnel, so request.client.host is always the tunnel's own
    local peer -- every real caller collapsed into one rate-limit bucket.
    CF-Connecting-IP is the edge-computed real caller IP."""

    def test_two_different_cf_connecting_ip_values_get_independent_buckets(
        self, auth_client
    ):
        for _ in range(server._LOGIN_MAX_ATTEMPTS):
            r = auth_client.post(
                "/login",
                data={"token": "wrong"},
                headers={"CF-Connecting-IP": "5.6.7.8"},
                follow_redirects=False,
            )
            assert r.status_code == 401

        blocked = auth_client.post(
            "/login",
            data={"token": "wrong"},
            headers={"CF-Connecting-IP": "5.6.7.8"},
            follow_redirects=False,
        )
        assert blocked.status_code == 429

        # A different CF-Connecting-IP must not inherit the first one's count,
        # even though both requests share the same underlying TestClient
        # connection (i.e. the same request.client.host).
        still_open = auth_client.post(
            "/login",
            data={"token": "wrong"},
            headers={"CF-Connecting-IP": "9.9.9.9"},
            follow_redirects=False,
        )
        assert still_open.status_code == 401

    def test_missing_header_falls_back_to_request_client_host(self, auth_client):
        for _ in range(server._LOGIN_MAX_ATTEMPTS):
            auth_client.post("/login", data={"token": "wrong"}, follow_redirects=False)

        r = auth_client.post("/login", data={"token": "wrong"}, follow_redirects=False)
        assert r.status_code == 429
