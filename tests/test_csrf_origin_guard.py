"""CSRF: durum değiştiren isteklerde köken doğrulaması + clickjacking başlıkları.

DeepR 2026-08-11 [ORTA]: uygulamada CSRF token / origin-referer kontrolü
hiçbir yerde yoktu. Tek dolaylı savunma `nau_auth` çerezinin `samesite="lax"`
olmasıydı — ve NAU_ACCESS_TOKEN boşken (uzun süre öyleydi) çerez hiç
gerekmediği için o savunma da yoktu. Operatörün tarayıcısında açılan kötücül
bir sayfa `POST /agent/run` (para harcatan LLM turu), `/loop/start`,
`/strategy/blocks/save-custom`, `/studio/{id}/deploy` çağırabiliyordu;
127.0.0.1'e bağlı olmak bunu engellemez, çünkü isteği atan kurbanın kendi
tarayıcısıdır.

Testler saldırıyı gerçekten kurar: çapraz köken başlığıyla POST atar ve
403 bekler. Ayrıca korumanın MEŞRU yolları kırmadığını da doğrular — GET'ler,
`/login`, ve aynı kökenli HTMX POST'ları.

Wiki References
---------------
Bkz: [[nau_deepr_toplu_sertlestirme_2026_08]]
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server

# Durum değiştiren, gerçekten pahalı/yıkıcı olan uçlar. Hepsi POST ve hepsi
# bulguda tek tek adı geçenler.
STATE_CHANGING = [
    ("POST", "/loop/start"),
    ("POST", "/loop/stop"),
    ("POST", "/agent/run"),
    ("POST", "/strategy/blocks/save-custom"),
    ("POST", "/reports/layout"),
]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(server, "_ACCESS_TOKEN", "")
    return TestClient(server.app)


class TestCrossOriginWritesAreRefused:
    @pytest.mark.parametrize(("method", "path"), STATE_CHANGING)
    def test_attacker_origin_is_403(self, client, method, path):
        """Kötücül sayfanın otomatik form/fetch POST'u — tarayıcı `Origin`
        koyar ve o başlık saldırganın kökenidir."""
        r = client.request(method, path, headers={"Origin": "https://evil.example"})

        assert r.status_code == 403
        assert "CSRF" in r.text

    @pytest.mark.parametrize(("method", "path"), STATE_CHANGING)
    def test_attacker_referer_is_403_when_origin_absent(self, client, method, path):
        """Eski/`Origin`'i bastıran tarayıcılar için `Referer` yedeği."""
        r = client.request(
            method, path, headers={"Referer": "https://evil.example/trap.html"}
        )

        assert r.status_code == 403

    def test_null_origin_is_refused(self, client):
        """referrer-policy `Origin`'i `null`'a indirebilir — o da yabancıdır."""
        r = client.post("/loop/stop", headers={"Origin": "null"})

        assert r.status_code == 403

    def test_lookalike_host_suffix_is_refused(self, client):
        """`testserver.evil.example` bizim host'umuz DEĞİL (ön-ek eşleşmesi yok)."""
        r = client.post("/loop/stop", headers={"Origin": "http://testserver.evil.com"})

        assert r.status_code == 403

    def test_refusal_body_is_not_html(self, client):
        """403 gövdesi htmx tarafından DOM'a basılsa bile zararsız kalmalı."""
        r = client.post("/loop/stop", headers={"Origin": "https://evil.example"})

        assert r.headers["content-type"].startswith("text/plain")
        assert "<" not in r.text


class TestLegitimateTrafficStillWorks:
    def test_same_origin_write_passes(self, client):
        r = client.post("/loop/stop", headers={"Origin": "http://testserver"})

        assert r.status_code != 403

    def test_https_origin_matches_http_host(self, client):
        """Tünel arkasında tarayıcı https görür, uygulama düz http dinler.

        Şema karşılaştırılsaydı her POST 403 olurdu — eşleşme HOST üzerinden."""
        r = client.post("/loop/stop", headers={"Origin": "https://testserver"})

        assert r.status_code != 403

    def test_same_origin_referer_passes(self, client):
        r = client.post("/loop/stop", headers={"Referer": "http://testserver/studio"})

        assert r.status_code != 403

    def test_get_requests_are_never_blocked(self, client):
        r = client.get("/", headers={"Origin": "https://evil.example"})

        assert r.status_code == 200

    def test_login_stays_reachable(self, client, monkeypatch):
        """`/login` muaf: kapıdan geçmemiş tarayıcının tek yazma yolu."""
        monkeypatch.setattr(server, "_ACCESS_TOKEN", "tok")
        server._login_failures.clear()
        try:
            r = client.post(
                "/login",
                data={"token": "tok"},
                headers={"Origin": "https://evil.example"},
                follow_redirects=False,
            )
        finally:
            server._login_failures.clear()

        assert r.status_code == 303  # 403 DEĞİL

    def test_headerless_client_is_allowed(self, client):
        """curl / pytest / operatörün script'i: `Origin` da `Referer` da yok.

        CSRF tanımı gereği bir TARAYICI-şaşırtma saldırısıdır; tarayıcı
        olmayan çağıran zaten kendi adına istek atıyor. Modern tarayıcılar
        GET/HEAD dışındaki her istekte `Origin` gönderdiği için bu boşluk bir
        tarayıcı tarafından ZORLANAMAZ."""
        r = client.post("/loop/stop")

        assert r.status_code != 403

    def test_require_origin_flag_closes_the_headerless_path(self, client, monkeypatch):
        """Sadece tarayıcıdan erişilen kurulumlar boşluğu kapatabilsin."""
        monkeypatch.setenv("NAU_CSRF_REQUIRE_ORIGIN", "1")

        r = client.post("/loop/stop")

        assert r.status_code == 403


class TestAllowedOriginsOverride:
    def test_env_allowlist_admits_a_rewritten_host(self, client, monkeypatch):
        """Tünel `Host`'u yeniden yazarsa operatörün elinde bir kol olmalı."""
        monkeypatch.setenv("NAU_ALLOWED_ORIGINS", "https://nautilus.muratben.com")

        r = client.post(
            "/loop/stop", headers={"Origin": "https://nautilus.muratben.com"}
        )

        assert r.status_code != 403

    def test_x_forwarded_host_is_accepted(self, client):
        r = client.post(
            "/loop/stop",
            headers={
                "Origin": "https://nautilus.muratben.com",
                "X-Forwarded-Host": "nautilus.muratben.com",
            },
        )

        assert r.status_code != 403


class TestSecurityHeaders:
    def test_clickjacking_is_blocked_two_ways(self, client):
        """Aynı bulgunun clickjacking ayağı: başlıksızken kötücül bir sayfa
        uygulamayı görünmez bir iframe'e alıp işlemleri TIKLAMA ile
        tetikleyebiliyordu. Origin kontrolü burada yardım etmez — tıklayan
        gerçekten kullanıcı ve istek gerçekten aynı kökenli."""
        r = client.get("/")

        assert r.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]

    def test_csp_locks_the_origin_down(self, client):
        csp = client.get("/").headers["Content-Security-Policy"]

        assert "default-src 'self'" in csp
        assert "connect-src 'self'" in csp
        assert "object-src 'none'" in csp
        # Yerelleştirmeden sonra hiçbir uzak script'e ihtiyaç yok.
        assert "unpkg" not in csp and "jsdelivr" not in csp
        # htmx `Function()` yoluna girmiyor (şablonlarda hx-on/hx-vals js: yok).
        assert "unsafe-eval" not in csp

    def test_nosniff_and_referrer_policy(self, client):
        r = client.get("/")

        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["Referrer-Policy"] == "same-origin"


class TestAuthCookieSameSite:
    def test_auth_cookie_is_samesite_strict(self, monkeypatch):
        """`lax` çapraz-site ÜST-SEVİYE gezinmede çerezi hâlâ gönderiyordu."""
        monkeypatch.setattr(server, "_ACCESS_TOKEN", "tok")
        server._login_failures.clear()
        c = TestClient(server.app, base_url="https://testserver")
        try:
            r = c.post(
                "/login",
                data={"token": "tok"},
                headers={"Origin": "https://testserver"},
                follow_redirects=False,
            )
        finally:
            server._login_failures.clear()

        set_cookie = r.headers["set-cookie"]
        assert "samesite=strict" in set_cookie.lower()
        assert "httponly" in set_cookie.lower()
        assert "secure" in set_cookie.lower()
