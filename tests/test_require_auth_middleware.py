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


class TestDeployedWithoutAuthWarns:
    """DeepR 2026-08-09 [ORTA]: an unset token used to be a silent no-op even
    under pm2 (PM2_HOME set) -- a fully unauthenticated internet-facing app
    with no signal anywhere an operator forgot to export the token."""

    def test_warns_when_deployed_with_no_token(self, caplog):
        with caplog.at_level("WARNING"):
            warned = server._warn_if_unauthenticated_and_deployed(
                "", {"PM2_HOME": "/home/user/.pm2"}
            )

        assert warned is True
        assert "NAU_ACCESS_TOKEN is unset" in caplog.text

    def test_silent_in_local_dev_without_pm2_home(self, caplog):
        with caplog.at_level("WARNING"):
            warned = server._warn_if_unauthenticated_and_deployed("", {})

        assert warned is False
        assert caplog.text == ""

    def test_silent_when_deployed_with_a_token_set(self, caplog):
        with caplog.at_level("WARNING"):
            warned = server._warn_if_unauthenticated_and_deployed(
                "s3cr3t", {"PM2_HOME": "/home/user/.pm2"}
            )

        assert warned is False
        assert caplog.text == ""


class TestDeployedWithoutATokenIsRefused:
    """Uyarı 2026-08-09'da eklenmişti ve DAVRANIŞ hiç değişmemişti: log'a bir
    satır düşüyor, uygulama tünelden kapısız servis etmeye devam ediyordu.
    Bir güvenlik kontrolünün "uyar ama yine de yap" hâli, koruma sayılmaz."""

    def test_pm2_without_a_token_refuses_every_request(self, monkeypatch):
        monkeypatch.setattr(server, "_ACCESS_TOKEN", "")
        monkeypatch.setenv("PM2_HOME", "/home/user/.pm2")
        monkeypatch.delenv("NAU_ALLOW_NO_AUTH", raising=False)

        r = TestClient(server.app).get("/", follow_redirects=False)

        assert r.status_code == 503
        assert "NAU_ACCESS_TOKEN" in r.text
        assert "NAU_ALLOW_NO_AUTH" in r.text, "ne yapılacağını söylemeyen 503"

    def test_login_and_static_are_refused_too(self, monkeypatch):
        """Kapısız bir dağıtımda "giriş sayfası açık kalsın" istisnası,
        girilecek bir sır olmadığı için kapıyı açık tutmanın kibar adıdır."""
        monkeypatch.setattr(server, "_ACCESS_TOKEN", "")
        monkeypatch.setenv("PM2_HOME", "/home/user/.pm2")
        monkeypatch.delenv("NAU_ALLOW_NO_AUTH", raising=False)
        client = TestClient(server.app)

        assert client.get("/login", follow_redirects=False).status_code == 503
        assert client.get("/static/app.js", follow_redirects=False).status_code == 503

    def test_local_dev_without_pm2_is_untouched(self, monkeypatch):
        """Token'sız yerel geliştirme sessizce çalışmaya devam etmeli —
        aksi hâlde tek satırlık bir sertleştirme bütün süiti 503'e çevirir."""
        monkeypatch.setattr(server, "_ACCESS_TOKEN", "")
        monkeypatch.delenv("PM2_HOME", raising=False)

        assert server._deployed_without_a_token() is False
        assert TestClient(server.app).get("/login").status_code == 200

    def test_the_escape_hatch_has_to_be_stated(self, monkeypatch):
        """Kapıyı açmak artık bir cümle kurmayı gerektiriyor; unutmakla aynı
        şey değil."""
        monkeypatch.setattr(server, "_ACCESS_TOKEN", "")
        monkeypatch.setenv("PM2_HOME", "/home/user/.pm2")

        monkeypatch.setenv("NAU_ALLOW_NO_AUTH", "1")
        assert server._deployed_without_a_token() is False

        monkeypatch.setenv("NAU_ALLOW_NO_AUTH", "maybe")
        assert server._deployed_without_a_token() is True, "her değer 'evet' sayıldı"

    def test_a_token_under_pm2_is_the_normal_case(self, monkeypatch):
        monkeypatch.setattr(server, "_ACCESS_TOKEN", TOKEN)
        monkeypatch.setenv("PM2_HOME", "/home/user/.pm2")

        assert server._deployed_without_a_token() is False


class TestTheDeploymentMarkerIsOneRule:
    """Aynı "dağıtıldık mı" kuralı ÜÇ yerde ayrı ayrı yazılmıştı.

    KAYIT DÜZELTMESİ (2026-08-17): bu sınıf önce "PM2_HOME çocuk sürece hiç
    geçmiyor, kapı canlıda açık" iddiasıyla yazıldı. İddia YANLIŞTI ve iki
    kanıtı da ölçüm hatasıydı — `pm2 env <id>` süreçten değil yapılandırmadan
    okur, `urllib.urlopen` ise yönlendirmeyi takip edip `303 → /login → 200`
    zincirini tek bir `200` gibi gösterir. Doğrudan ölçüm: pm2 altındaki bir
    süreç `PM2_HOME`'u görüyor, ve kapı zaten kapalıydı.

    Geriye kalan gerçek kusur çoklu kopyaydı; testler onu tutuyor.
    """

    def test_serve_py_sets_the_marker_before_server_is_imported(self):
        """`_ACCESS_TOKEN` import anında okunur; işaret sonra konsa geç kalır."""
        from pathlib import Path

        src = Path("serve.py").read_text(encoding="utf-8")
        marker = src.index('os.environ.setdefault("NAU_DEPLOYED"')
        # uvicorn.run("server:app", ...) — server bu satırda import edilir.
        assert marker < src.index('"server:app"')

    def test_either_marker_counts_as_deployed(self):
        assert server._is_deployed({"NAU_DEPLOYED": "1"}) is True
        assert server._is_deployed({"PM2_HOME": "/home/u/.pm2"}) is True
        assert server._is_deployed({}) is False

    def test_the_token_file_is_read_under_either_marker(self, tmp_path, monkeypatch):
        """Dosya yedeği İKİ işaretle de okunmalı; yerel dev ikisinde de sessiz."""
        f = tmp_path / ".nau_access_token"
        f.write_text("s3cr3t\n", encoding="utf-8")
        monkeypatch.setattr(server, "_ACCESS_TOKEN_FILE", f)

        assert server._read_access_token({"NAU_DEPLOYED": "1"}) == "s3cr3t"
        assert server._read_access_token({"PM2_HOME": "/home/u/.pm2"}) == "s3cr3t"
        assert server._read_access_token({}) == "", "yerel dev sessizce açık kalmalı"

    def test_the_503_fires_under_the_serve_py_marker_too(self, monkeypatch):
        monkeypatch.setattr(server, "_ACCESS_TOKEN", "")
        monkeypatch.delenv("PM2_HOME", raising=False)
        monkeypatch.delenv("NAU_ALLOW_NO_AUTH", raising=False)
        monkeypatch.setenv("NAU_DEPLOYED", "1")

        assert (
            TestClient(server.app).get("/", follow_redirects=False).status_code == 503
        )

    def test_all_three_consumers_read_one_rule(self):
        """Üç tüketici ayrı ayrı `PM2_HOME`'a bakıyordu; işaret yanlış olunca
        üçü BİRDEN yanlış oldu. Aynı kuralın üç kopyası bu depoda tekrarlayan
        kusur (bkz. benchmark kapısı, işlem eşiği)."""
        import inspect
        from pathlib import Path

        src = Path("server.py").read_text(encoding="utf-8")
        body = src[src.index("def _is_deployed") :]
        # `_is_deployed`'in KENDİ gövdesi dışında hiçbir yerde ham PM2_HOME okuması olmasın.
        own = inspect.getsource(server._is_deployed)
        assert body.replace(own, "").count('get("PM2_HOME")') == 0
