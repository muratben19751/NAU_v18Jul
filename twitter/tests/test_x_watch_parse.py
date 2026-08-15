"""`x_watch.parse_tweets` sabit bir HTML'den doğru kartları çıkarsın.

Kazıyıcının en kırılgan parçası parser'dır: X'in HTML'i habersiz değişir ve
bozulma SESSİZDİR (boş liste, "bugün tweet yok"tan ayırt edilemez). Bu dosya o
sessizliği kırar — sözleşme kaydedilmiş bir sayfa üzerinde yazılıdır, ağa
çıkılmaz, ve gerçek sayfanın parser'ı zorlayan özellikleri (alıntı kartı, emoji
`<img>`, metinsiz tweet, tweet DIŞI /status/ linkleri) fixture'da temsil edilir.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from x_watch import parse_tweets

FIXTURE = Path(__file__).parent / "fixtures" / "x_search_ttkom.html"


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    return parse_tweets(FIXTURE.read_text(encoding="utf-8"))


class TestKartlariCikarir:
    def test_dort_tweet_bulur(self, rows):
        assert [r["id"] for r in rows] == [
            "1822334455667788990",
            "1822334455667799001",
            "1822334455667700123",
            "1822334455667711234",
        ]

    def test_yazar_ve_url_dogru(self, rows):
        first = rows[0]
        assert first["author"] == "borsaci_ali"
        assert first["url"] == "https://x.com/borsaci_ali/status/1822334455667788990"

    def test_zaman_damgasi_okunur(self, rows):
        assert rows[0]["created_at"] == "2026-08-15T09:12:03.000Z"

    def test_metin_ic_ice_span_ve_emojiyi_birlestirir(self, rows):
        text = rows[0]["text"]
        assert "TTKOM bilanço sonrası direnç kırdı" in text
        assert "🚀" in text, "emoji `<img alt>` olarak gelir, düşmemeli"
        assert "hedef 52.30" in text


class TestZorDurumlar:
    def test_alintili_tweet_dis_kartin_kimligini_alir(self, rows):
        """İç içe `<article>` kendi kaydını doğurmamalı, dıştakini de çalmamalı."""
        ids = [r["id"] for r in rows]
        assert "1799999999999999999" not in ids, "alıntılanan tweet ayrı kart sayıldı"
        quoted_card = rows[1]
        assert quoted_card["author"] == "piyasa_notu"
        assert "önemli buldum" in quoted_card["text"]

    def test_tweettext_oncesi_status_linki_kimligi_bozmaz(self, rows):
        card = rows[2]
        assert card["id"] == "1822334455667700123"
        assert card["text"].startswith("TTKOM: Yönetim Kurulu")

    def test_metinsiz_tweet_dusmez_ama_metni_bos_kalir(self, rows):
        card = rows[3]
        assert card["id"] == "1822334455667711234"
        assert not card.get("text")

    def test_article_disindaki_status_linki_tweet_sayilmaz(self, rows):
        assert "1700000000000000000" not in [r["id"] for r in rows]


class TestBozukGirdideOlmez:
    @pytest.mark.parametrize(
        "html",
        [
            "",
            "<html><body><p>hiç tweet yok</p></body></html>",
            '<article data-testid="tweet"><div data-testid="tweetText">kapanmamış',
            "<<<>>> düz metin değil bile",
        ],
    )
    def test_bos_ya_da_bozuk_html_patlamaz(self, html):
        out = parse_tweets(html)
        assert isinstance(out, list)
        assert all(r.get("id") for r in out)

    def test_ayni_tweet_iki_kez_gorunurse_tekillestirilir(self):
        card = (
            '<article data-testid="tweet">'
            '<a href="/a/status/123"><time datetime="2026-01-01T00:00:00.000Z"></time></a>'
            '<div data-testid="tweetText">x</div></article>'
        )
        assert len(parse_tweets(card * 2)) == 1
