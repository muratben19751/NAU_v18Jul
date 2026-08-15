"""Defter append-only olsun, aynı tweet iki kez yazılmasın, yırtık satır öldürmesin.

İzleyici 5 dakikada bir AYNI arama sayfasını çeker: her turda aynı ~20 tweet
gelir ve yalnız aradaki fark yenidir. Dolayısıyla tekilleştirme bir optimizasyon
değil, ürünün kendisidir — bozulursa operatör her 5 dakikada bir aynı tweetlerin
e-postasını alır.

Defterin sözleşmesi: I/O'su çağrı yoluna hata sızdırmaz ve eşzamanlı append'in
yırttığı satır okumayı öldürmez, atlanır.
"""

from __future__ import annotations

import json

import pytest
import x_watch
from x_watch import Config, append_tweets, load_seen_ids, run_once

CARD = (
    '<article data-testid="tweet">'
    '<a href="/{author}/status/{tid}"><time datetime="2026-08-15T09:00:00.000Z"></time></a>'
    '<div data-testid="tweetText">{text}</div></article>'
)


def page(*ids: str) -> str:
    return (
        "<html><body>"
        + "".join(CARD.format(author="u", tid=i, text=f"ttkom {i}") for i in ids)
        + "</body></html>"
    )


@pytest.fixture
def cfg(tmp_path) -> Config:
    """Mail kapalı (`mail_to=""`), tüm yollar tmp_path altında — gerçek
    ``~/.cache`` köküne ve SMTP'ye hiç dokunulmaz."""
    return Config(
        query="ttkom",
        mail_to="",
        ledger_path=tmp_path / "x_watch.jsonl",
        state_path=tmp_path / "x_watch_state.json",
        storage_state_path=tmp_path / "x_storage_state.json",
    )


class TestTekillestirme:
    def test_ayni_tweet_ikinci_turda_yeniden_yazilmaz(self, cfg):
        first = run_once(cfg, html=page("1", "2"))
        assert (first.fetched, first.new) == (2, 2)

        # İkinci tur: bir eski + bir yeni. `seen` verilmiyor, yani defterden
        # yeniden kuruluyor — PM2 restart'ından sonraki hâli bu.
        second = run_once(cfg, html=page("2", "3"))
        assert (second.fetched, second.new) == (2, 1)

        ids = [
            json.loads(ln)["id"]
            for ln in cfg.ledger_path.read_text("utf-8").splitlines()
        ]
        assert ids == ["1", "2", "3"]

    def test_bellekteki_seen_kumesi_de_calisir(self, cfg):
        seen: set[str] = set()
        run_once(cfg, html=page("1"), seen=seen)
        again = run_once(cfg, html=page("1"), seen=seen)
        assert again.new == 0
        assert seen == {"1"}

    def test_dry_run_deftere_yazmaz(self, cfg):
        res = run_once(cfg, html=page("1", "2"), dry_run=True)
        assert res.new == 2
        assert not cfg.ledger_path.exists()


class TestDefterDayanikliligi:
    def test_yirtik_satir_okumayi_oldurmez(self, cfg):
        append_tweets([{"id": "1"}, {"id": "2"}], cfg.ledger_path)
        with open(cfg.ledger_path, "a", encoding="utf-8") as f:
            f.write('{"id": "3", "text": "yarım sat')  # eşzamanlı append'in izi
        assert load_seen_ids(cfg.ledger_path) == {"1", "2"}

    def test_olmayan_defter_bos_kume_dondurur(self, tmp_path):
        assert load_seen_ids(tmp_path / "yok.jsonl") == set()

    def test_kuyruk_okumasi_son_dilimle_sinirli(self, cfg):
        """Tam dosya okunmaz: her PM2 restart'ını yavaşlatırdı."""
        append_tweets([{"id": str(i)} for i in range(200)], cfg.ledger_path)
        seen = load_seen_ids(cfg.ledger_path, tail_bytes=200)
        assert seen, "kuyruktan hiç id çıkmadı — çıpa kırık"
        assert len(seen) < 200, "tail_bytes yok sayıldı, dosyanın tamamı okundu"
        assert "199" in seen, "en yeni kayıt kuyrukta olmalı"

    def test_yazma_hatasi_cagri_yoluna_sizmaz(self, cfg, monkeypatch):
        def boom(*a, **k):
            raise OSError("disk dolu")

        monkeypatch.setattr(x_watch, "open", boom, raising=False)
        append_tweets([{"id": "1"}], cfg.ledger_path)  # raise etmemeli

    def test_bos_liste_dosya_yaratmaz(self, cfg):
        append_tweets([], cfg.ledger_path)
        assert not cfg.ledger_path.exists()


class TestDonguHataYutar:
    def test_fetch_patlarsa_run_once_raise_etmez(self, cfg, monkeypatch):
        def boom(query, c=None):
            raise RuntimeError("ağ öldü")

        monkeypatch.setattr(x_watch, "fetch_search_html", boom)
        res = run_once(cfg)
        assert res.error.startswith("RuntimeError")
        assert (res.fetched, res.new) == (0, 0)

    def test_login_duvari_ayri_bayrakla_bildirilir(self, cfg, monkeypatch):
        def wall(query, c=None):
            raise x_watch.LoginRequired("oturum düştü")

        monkeypatch.setattr(x_watch, "fetch_search_html", wall)
        res = run_once(cfg)
        assert res.login_required and not res.rate_limited

    def test_hiz_siniri_ayri_bayrakla_bildirilir(self, cfg, monkeypatch):
        def limited(query, c=None):
            raise x_watch.RateLimited("429")

        monkeypatch.setattr(x_watch, "fetch_search_html", limited)
        res = run_once(cfg)
        assert res.rate_limited and not res.login_required


class TestParserSagligi:
    def test_sayfa_geldi_ama_kart_yoksa_parse_failed(self, cfg):
        res = run_once(cfg, html="<html><body>bomboş</body></html>")
        assert res.parse_failed, "sessiz sıfır ile bozuk parser ayırt edilemiyor"

    def test_gercekten_tweet_varken_parse_failed_olmaz(self, cfg):
        assert not run_once(cfg, html=page("1")).parse_failed

    def test_ardisik_basarisizlik_sayaci_birikir_ve_sifirlanir(self, cfg):
        for _ in range(3):
            run_once(cfg, html="<html>boş</html>")
        assert x_watch.load_state(cfg.state_path)["parse_fail_streak"] == 3
        run_once(cfg, html=page("1"))
        assert x_watch.load_state(cfg.state_path)["parse_fail_streak"] == 0
