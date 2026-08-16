"""Oturumsuz sayfa `LoginRequired` sayılsın — `RateLimited` DEĞİL.

Bu ayrım kozmetik değil, kurtarma yolunu belirliyor:

  • `LoginRequired` → döngü durur, operatöre "yeniden giriş gerekiyor" maili
    gider, süreç `exit 2` ile çıkar.
  • `RateLimited`   → üstel geri çekilip devam eder (tavan 1 saat).

2026-08-15'te gerçek bir yakalama bu ikisinin karıştığını gösterdi. Oturumsuz
çekilen arama sayfası:

  • HTTP **200** dönüyor (hata kodu değil),
  • `/i/flow/login`'e **yönlendirmiyor**,
  • hiçbir giriş-formu işaretçisi **içermiyor**,
  • ama İngilizce "Something went wrong…" metnini **render ediyor**.

O metin `_LIMIT_MARKERS` içinde olduğu için çerez düştüğünde izleyici durumu
hız sınırı sanıyor, saatlerce geri çekilerek dönüyor ve uyarı mailini HİÇ
atmıyordu — tasarımın en önemli güvenlik davranışı sessizce ölüydü.

Bugünkü dayanak X'in kendi makine-okur alanı: `"isLoggedIn":false`.

Fixture gerçek yakalamadan kısaltılmıştır (`x_search_logged_out.html`); içine
yeni işaretçi dizgisi eklemeyin, testler tam da bazı dizgilerin YOKLUĞUNU
ölçüyor.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import x_watch
from x_watch import LoginRequired, RateLimited, _raise_if_blocked

FIXTURES = Path(__file__).parent / "fixtures"
LOGGED_OUT = (FIXTURES / "x_search_logged_out.html").read_text(encoding="utf-8")
WITH_TWEETS = (FIXTURES / "x_search_ttkom.html").read_text(encoding="utf-8")

SEARCH_URL = "https://x.com/search?q=ttkom&f=live"


class TestGercekOturumsuzSayfa:
    """Kaydedilmiş gerçek yakalama üzerinde sözleşme."""

    def test_login_required_atar(self):
        with pytest.raises(LoginRequired):
            _raise_if_blocked(LOGGED_OUT, SEARCH_URL)

    def test_rate_limited_ATMAZ(self):
        """Asıl regresyon: hız sınırı sanılırsa döngü sonsuza dek sessiz kalır."""
        with pytest.raises(LoginRequired):
            try:
                _raise_if_blocked(LOGGED_OUT, SEARCH_URL)
            except RateLimited as exc:  # pragma: no cover - regresyon
                pytest.fail(
                    "oturumsuz sayfa RateLimited sayıldı; döngü geri çekilip "
                    f"sonsuza dek döner, uyarı maili hiç gitmez: {exc}"
                )

    def test_mesaj_operatore_ne_yapacagini_soyler(self):
        with pytest.raises(LoginRequired, match="x_login.py"):
            _raise_if_blocked(LOGGED_OUT, SEARCH_URL)

    def test_fixture_gercek_yakalamanin_ozelliklerini_tasiyor(self):
        """Çıpa: fixture bayatlarsa ya da kirlenirse test yanlış şeyi korur."""
        assert '"isLoggedIn":false' in LOGGED_OUT, "kesin sinyal fixture'dan düşmüş"
        assert "Something went wrong" in LOGGED_OUT, "yanıltıcı metin fixture'da yok"
        # Bunların YOKLUĞU sözleşmenin parçası — eski tespit tam bu yüzden kördü.
        assert 'data-testid="tweet"' not in LOGGED_OUT
        assert "loginButton" not in LOGGED_OUT
        assert "/i/flow/login" not in LOGGED_OUT


class TestYaniltiziMetinArtikHizSiniriSaymiyor:
    def test_something_went_wrong_tek_basina_rate_limited_degil(self):
        html = "<html><body>Something went wrong, but don't fret.</body></html>"
        with pytest.raises(LoginRequired):
            # Oturum durumu bilinmiyorsa bile bu metin hız sınırı KANITI değil.
            _raise_if_blocked(html + '"isLoggedIn":false', SEARCH_URL)

    def test_something_went_wrong_listeden_cikarildi(self):
        assert not any("went wrong" in m for m in x_watch._LIMIT_MARKERS), (
            "bu metin oturumsuz sayfada render ediliyor; hız sınırı işaretçisi olamaz"
        )

    def test_gercek_hiz_siniri_hala_yakalanir(self):
        with pytest.raises(RateLimited):
            _raise_if_blocked("<html>Rate limit exceeded</html>", SEARCH_URL)

    def test_too_many_requests_da_yakalanir(self):
        with pytest.raises(RateLimited):
            _raise_if_blocked("<html>429 Too Many Requests</html>", SEARCH_URL)


class TestSaglikliSayfaGecer:
    def test_tweet_kartli_sayfa_engellenmis_sayilmaz(self):
        _raise_if_blocked(WITH_TWEETS, SEARCH_URL)  # raise etmemeli

    def test_oturum_aciksa_gecer(self):
        html = '<html>{"isLoggedIn":true}' + WITH_TWEETS + "</html>"
        _raise_if_blocked(html, SEARCH_URL)

    def test_hiz_siniri_metni_tweet_varken_gormezden_gelinir(self):
        """Kartlar geldiyse sayfa kullanılabilir; paketlenmiş metin yanıltmasın."""
        _raise_if_blocked(WITH_TWEETS + "Rate limit exceeded", SEARCH_URL)


class TestYonlendirme:
    @pytest.mark.parametrize(
        "url",
        [
            "https://x.com/i/flow/login?redirect_after_login=%2Fsearch",
            "https://x.com/login",
        ],
    )
    def test_giris_akisina_yonlendirme_login_required(self, url):
        with pytest.raises(LoginRequired, match="yönlendirildi"):
            _raise_if_blocked("<html>bomboş</html>", url)


class TestDonguDogruKurtarmayiSecer:
    """`run_once` bayrakları: `main()` bunlara bakarak durur ya da geri çekilir."""

    @pytest.fixture
    def cfg(self, tmp_path):
        return x_watch.Config(
            mail_to="",
            ledger_path=tmp_path / "l.jsonl",
            state_path=tmp_path / "s.json",
            storage_state_path=tmp_path / "ss.json",
        )

    def test_oturumsuz_sayfa_login_required_bayragini_kaldirir(self, cfg, monkeypatch):
        monkeypatch.setattr(
            x_watch,
            "fetch_search_html",
            lambda q, c=None: _raise_if_blocked(LOGGED_OUT, SEARCH_URL),
        )
        res = x_watch.run_once(cfg)
        assert res.login_required, "döngü durup mail atmalı"
        assert not res.rate_limited, "geri çekilip sonsuza dek dönmemeli"
