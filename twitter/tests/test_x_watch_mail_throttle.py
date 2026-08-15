"""E-posta kısması: yoğun günde 288 mail değil, `mail_min_s` başına bir özet.

İzleyici 5 dakikada bir koşar. Her yeni tweette anında mail atmak, TTKOM gibi
konuşulan bir sembolde gün içinde yüzlerce bildirim demektir — ve okunmayan
bildirim, bildirim değildir. Kısma bu yüzden ürünün parçası.

İki şey pazarlık konusu değil ve burada bağlanıyor:
  1. Bekleyen tweetler mail atılana kadar DİSKTE durur — PM2 `autorestart`
     süreci habersiz yeniden başlatır, bellekte tutulsa sessizce kaybolurlardı.
  2. Gönderim başarısız olursa bekleyenler TEMİZLENMEZ; aksi hâlde SMTP'nin
     kötü bir günü, tweetleri hiç görülmeden yutardı.
"""

from __future__ import annotations

import pytest
import x_watch
from x_watch import Config, format_digest, run_once

CARD = (
    '<article data-testid="tweet">'
    '<a href="/u/status/{tid}"><time datetime="2026-08-15T09:00:00.000Z"></time></a>'
    '<div data-testid="tweetText">ttkom {tid}</div></article>'
)


def page(*ids: str) -> str:
    return "".join(CARD.format(tid=i) for i in ids)


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        query="ttkom",
        mail_to="alici@example.com",
        mail_min_s=900,
        smtp_user="gonderen@example.com",
        smtp_password="uygulama-sifresi",
        ledger_path=tmp_path / "x_watch.jsonl",
        state_path=tmp_path / "x_watch_state.json",
        storage_state_path=tmp_path / "x_storage_state.json",
    )


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, str]]:
    """Gönderilen (konu, gövde) çiftlerini toplar; SMTP'ye hiç gidilmez."""
    box: list[tuple[str, str]] = []

    def fake(subject, body, c):
        box.append((subject, body))
        return True

    monkeypatch.setattr(x_watch, "send_mail", fake)
    return box


class TestKisma:
    def test_ilk_yeni_tweet_hemen_mail_atar(self, cfg, sent):
        res = run_once(cfg, html=page("1"))
        assert res.mailed
        assert len(sent) == 1
        assert "1 yeni 'ttkom'" in sent[0][0]

    def test_pencere_icinde_ikinci_mail_atilmaz(self, cfg, sent):
        run_once(cfg, html=page("1"))
        res = run_once(cfg, html=page("1", "2"))
        assert res.new == 1
        assert not res.mailed
        assert len(sent) == 1, "kısma penceresi delindi"

    def test_pencere_dolunca_birikenler_tek_mailde_cikar(self, cfg, sent, monkeypatch):
        run_once(cfg, html=page("1"))
        run_once(cfg, html=page("2"))
        run_once(cfg, html=page("3"))
        assert len(sent) == 1

        # Zamanı ileri sar: son gönderimin üstünden pencere kadar geçmiş olsun.
        state = x_watch.load_state(cfg.state_path)
        assert len(state["pending"]) == 2, "bekleyenler diskte durmalı"
        state["last_mail_ts"] -= cfg.mail_min_s + 1
        x_watch.save_state(state, cfg.state_path)

        res = run_once(cfg, html=page("4"))
        assert res.mailed
        assert len(sent) == 2
        body = sent[1][1]
        assert "ttkom 2" in body and "ttkom 3" in body and "ttkom 4" in body
        assert "3 yeni" in sent[1][0]
        assert x_watch.load_state(cfg.state_path)["pending"] == []

    def test_yeni_tweet_yoksa_mail_atilmaz(self, cfg, sent):
        run_once(cfg, html=page("1"))
        sent.clear()
        run_once(cfg, html=page("1"))
        assert sent == []

    def test_gonderim_basarisizsa_bekleyenler_korunur(self, cfg, monkeypatch):
        monkeypatch.setattr(x_watch, "send_mail", lambda s, b, c: False)
        res = run_once(cfg, html=page("1"))
        assert not res.mailed
        assert [r["id"] for r in x_watch.load_state(cfg.state_path)["pending"]] == ["1"]


class TestMailKapaliyken:
    def test_mail_to_bosken_smtp_ye_hic_dokunulmaz(self, cfg, monkeypatch):
        off = Config(**{**cfg.__dict__, "mail_to": ""})

        def boom(*a, **k):  # pragma: no cover - çağrılmamalı
            raise AssertionError("mail kapalıyken SMTP'ye gidildi")

        monkeypatch.setattr(x_watch.smtplib, "SMTP_SSL", boom)
        monkeypatch.setattr(x_watch.smtplib, "SMTP", boom)
        res = run_once(off, html=page("1"))
        assert res.new == 1 and not res.mailed

    def test_smtp_kimligi_eksikse_sessizce_gecilir(self, cfg, monkeypatch, caplog):
        half = Config(**{**cfg.__dict__, "smtp_password": ""})

        def boom(*a, **k):  # pragma: no cover - çağrılmamalı
            raise AssertionError("kimlik eksikken bağlanmaya çalışıldı")

        monkeypatch.setattr(x_watch.smtplib, "SMTP_SSL", boom)
        assert x_watch.send_mail("konu", "gövde", half) is False


class TestSmtpYolu:
    def test_465_ssl_kullanir_ve_mesaji_gonderir(self, cfg, monkeypatch):
        captured = {}

        class FakeSMTP:
            def __init__(self, host, port, context=None, timeout=None):
                captured["host"], captured["port"] = host, port

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, user, pw):
                captured["user"] = user

            def send_message(self, msg):
                captured["msg"] = msg

        monkeypatch.setattr(x_watch.smtplib, "SMTP_SSL", FakeSMTP)
        assert x_watch.send_mail("konu", "gövde", cfg) is True
        assert captured["port"] == 465
        assert captured["msg"]["To"] == "alici@example.com"
        assert captured["msg"]["Subject"] == "konu"

    def test_smtp_patlarsa_false_doner_raise_etmez(self, cfg, monkeypatch):
        def boom(*a, **k):
            raise OSError("bağlantı reddedildi")

        monkeypatch.setattr(x_watch.smtplib, "SMTP_SSL", boom)
        assert x_watch.send_mail("konu", "gövde", cfg) is False


class TestParserUyarisi:
    def test_ardisik_bosluktan_sonra_tek_uyari_gider(self, cfg, sent):
        for _ in range(x_watch.PARSE_FAIL_ALERT_AFTER + 3):
            run_once(cfg, html="<html>boş</html>")
        alerts = [s for s in sent if "parser" in s[0]]
        assert len(alerts) == 1, "uyarı ya hiç gitmedi ya da her turda tekrarladı"

    def test_esik_altinda_uyari_gitmez(self, cfg, sent):
        for _ in range(x_watch.PARSE_FAIL_ALERT_AFTER - 1):
            run_once(cfg, html="<html>boş</html>")
        assert not [s for s in sent if "parser" in s[0]]


class TestOzetBicimi:
    def test_ozet_yazar_metin_ve_baglantiyi_tasir(self):
        body = format_digest(
            [
                {
                    "author": "ali",
                    "text": "TTKOM güçlü",
                    "url": "https://x.com/ali/status/1",
                }
            ],
            "ttkom",
        )
        assert "@ali" in body and "TTKOM güçlü" in body
        assert "https://x.com/ali/status/1" in body

    def test_metinsiz_tweet_ozeti_bozmaz(self):
        body = format_digest([{"author": "a", "url": "u"}], "ttkom")
        assert "(metin çıkarılamadı)" in body
