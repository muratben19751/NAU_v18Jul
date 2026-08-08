"""Global request body size cap (DeepR 2026-08-08 [ORTA]).

No route bounded request body size — web/routes/reports.py's post_layout()
parsed `await request.json()` on whatever a client sent, and every other
route was the same. `server._limit_request_body` is app-wide ASGI middleware
so no individual route has to remember a check, and it covers both the
common case (an honest Content-Length header) and a client that lies about
it / uses chunked transfer.

Wiki References
---------------
See: [[nau_deepr_toplu_sertlestirme_2026_08]]
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import server


def _client():
    return TestClient(server.app)


class TestDeclaredContentLength:
    def test_oversized_declared_length_is_rejected_before_the_route_runs(self):
        c = _client()
        big = "x" * (server._MAX_REQUEST_BODY_BYTES + 1024)

        r = c.post("/reports/layout", content=big.encode())

        assert r.status_code == 413

    def test_small_body_is_unaffected(self):
        c = _client()

        r = c.post("/reports/layout", json={"pageSize": 50})

        assert r.status_code == 200


class TestStreamedBodyWithoutAnAccurateContentLength:
    def test_oversized_chunked_body_is_rejected_even_if_the_route_swallows_it(self):
        """post_layout wraps `await request.json()` in a broad `except
        Exception` — without the post-call_next override this would surface
        as the route's own generic 400, hiding that the real problem was an
        oversized body."""
        c = _client()
        chunk = b"x" * (1024 * 1024)
        over_limit_chunks = server._MAX_REQUEST_BODY_BYTES // len(chunk) + 2

        def gen():
            for _ in range(over_limit_chunks):
                yield chunk

        r = c.post("/reports/layout", content=gen())

        assert r.status_code == 413

    def test_small_streamed_body_is_unaffected(self):
        c = _client()

        def gen():
            yield b'{"pageSize": 50}'

        r = c.post("/reports/layout", content=gen())

        assert r.status_code == 200


class TestOrdinaryRequestsStillWork:
    def test_get_with_no_body_is_unaffected(self):
        c = _client()

        r = c.get("/tokens/badge")

        assert r.status_code == 200

    def test_ordinary_form_post_is_unaffected(self):
        c = _client()

        r = c.post("/login", data={"token": "irrelevant"})

        assert r.status_code == 401  # wrong token, but the request itself was fine
