"""DeepR 2026-08-11 [DÜŞÜK/ORTA] turu — sessizce yanlış davranan beş uç.

Ortak tema yine "kullanıcının gördüğü ile uygulamanın yaptığı arasındaki fark",
ama bu sefer küçük yüzeylerde:

1. **Serbest metin sınırı.** Uygulamanın LLM'e metin geçiren uçlarının hepsi
   4000 karakterde kesiyordu; AUTO'nun `hint`'i ve Studio'nun `ask`'i
   kesmiyordu. AUTO'daki daha pahalı: `hint` HER iterasyonun prompt'una
   yeniden ekleniyor, yani yapıştırılan metnin maliyeti tur sayısıyla
   ÇARPILIYOR.
2. **`/lab/run` geçersiz tarih.** Ayrıştırılamayan tarih sessizce ``None``
   oluyor ve koşu VARSAYILAN pencerede başlıyordu — kullanıcı "2024'ü test
   ettim" sanıyor, eline başka bir dönemin sonucu geçiyordu. `/backtest` aynı
   kuralı zaten uyguluyordu.
3. **Bybit kategori varsayılanı.** Aynı BTCUSDT için üç yüzey üç farklı sabit
   gösteriyordu (AUTO `spot`, `/backtest` `linear`, SIMPLE `linear`) ve hiçbiri
   hangi serinin gerçekten indirilmiş olduğunu bilmiyordu. Yanlış tahmin eden
   yüzeyde MAX 404 veriyor, koşu da olmayan seriyi arıyordu.
4. **Blok parametre sınırları.** Katalogdaki `min`/`max` SADECE HTML input
   attribute'uydu; tarayıcı dışı her yol onları geçiyordu ve sonuç sessiz bir
   ölü bloktu — hata değil, "0 işlemli geçerli görünen bir backtest".
5. **Studio LLM muhasebesi [ORTA].** Studio'nun kendi istemcisi yanıttaki
   ``usage`` bloğunu atıyordu; `/tokens` rozeti ve AUTO'nun bütçe tavanı aynı
   defterden okuduğu için Studio'da harcanan token "hiç harcanmamış" görünüyordu.

Şablon iddiaları RENDER EDİLEREK doğrulanır. Gerçek kataloğa/studio.db'ye
yazılmaz.

Wiki References: [[webapp_module_map]]
"""

from __future__ import annotations

import pytest


def _client():
    from fastapi.testclient import TestClient

    from server import app

    return TestClient(app)


# ── 1 · Serbest metin sınırı ────────────────────────────────────────────────


class TestFreeTextIsBounded:
    def test_the_limit_has_one_definition(self):
        """Dört uç aynı sabiti okusun — kopya dörde çıkacaktı."""
        from web.routes import backtest as bt
        from web.routes import strategy as st
        from web.shared import MAX_LLM_TEXT_LEN

        assert bt._MAX_LLM_TEXT_LEN is MAX_LLM_TEXT_LEN
        assert st._MAX_LLM_TEXT_LEN is MAX_LLM_TEXT_LEN

    def test_auto_refuses_an_oversized_hint_before_spending_anything(self):
        from web.shared import MAX_LLM_TEXT_LEN

        r = _client().post(
            "/agent/run",
            data={"hint": "x" * (MAX_LLM_TEXT_LEN + 1), "symbol": "BTCUSDT"},
        )
        assert r.status_code == 400
        # Sebep gövdede olsun: "düğme çalışmadı" değil, "neden çalışmadı".
        assert "too long" in r.text and str(MAX_LLM_TEXT_LEN) in r.text

    def test_a_hint_at_the_limit_is_not_refused_for_length(self):
        """Sınırın kendisi geçerli — koşu başka bir sebeple düşebilir, ama
        'too long' demeyecek. Yoksa test tam sınırda yanlış yerden geçerdi."""
        from web.shared import MAX_LLM_TEXT_LEN

        r = _client().post(
            "/agent/run",
            data={"hint": "x" * MAX_LLM_TEXT_LEN, "symbol": "BTCUSDT"},
        )
        assert "too long" not in r.text

    def test_the_error_body_does_not_reflect_the_hint_back(self):
        """Yansıyan XSS sink'i olmasın: `hint` ham hâliyle gövdeye girmesin.

        `/agent/run`'ın 4xx gövdesi studio.html tarafından bilerek DOM'a
        basılıyor (`shouldSwap = true`) ve `allowScriptTags` açık.
        """
        from web.shared import MAX_LLM_TEXT_LEN

        payload = "<img src=x onerror=alert(1)>" + "y" * MAX_LLM_TEXT_LEN
        r = _client().post("/agent/run", data={"hint": payload, "symbol": "BTCUSDT"})
        assert r.status_code == 400
        assert "<img src=x onerror=" not in r.text

    @pytest.mark.parametrize(
        "path, extra",
        [
            ("/studio/{sid}/ai/suggest", {}),
            ("/studio/{sid}/ai/loop/start", {"max_iterations": "2"}),
        ],
    )
    def test_studio_refuses_an_oversized_ask(self, path, extra, tmp_path, monkeypatch):
        from scripts.seed_studio import build_fixture
        from strategy_studio.store import StrategyStore
        from web.routes import strategy_studio as mod
        from web.shared import MAX_LLM_TEXT_LEN

        # Canlı studio.db'ye ASLA dokunma.
        store = StrategyStore(tmp_path / "t.db")
        defn = build_fixture()
        store.save(defn)
        monkeypatch.setattr(mod, "store", store)

        r = _client().post(
            path.format(sid=defn.id),
            data={"ask": "x" * (MAX_LLM_TEXT_LEN + 1), **extra},
        )
        assert r.status_code == 422
        assert "too long" in r.text


# ── 2 · /lab/run geçersiz tarih ─────────────────────────────────────────────


class TestLabRejectsAnUnusableDateRange:
    @pytest.mark.parametrize(
        "start, end, expect",
        [
            ("not-a-date", "", "YYYY-MM-DD"),
            ("2024-13-45", "", "YYYY-MM-DD"),
            ("2024-06-01", "2024-01-01", "before the start"),
        ],
    )
    def test_it_says_why_instead_of_running_the_default_window(
        self, start, end, expect
    ):
        r = _client().post(
            "/lab/run",
            data={
                "hint": "rsi dip",
                "symbol": "BTCUSDT",
                "start_date": start,
                "end_date": end,
            },
        )
        assert r.status_code == 400, r.text
        assert expect in r.text
        # htmx 2xx dışını swap etmez; sebep toast ile de gitmeli.
        assert r.headers.get("HX-Toast", "").startswith("err|")

    def test_blank_dates_are_still_allowed(self):
        """Boş = tüm önbellek. Düzeltme bu yolu kapatmamalı."""
        from web.shared import invalid_date_range

        assert invalid_date_range("", "") is None

    def test_backtest_and_lab_now_share_one_rule(self):
        from web.routes import backtest as bt
        from web.shared import invalid_date_range

        assert bt._invalid_date_range is invalid_date_range


# ── 3 · Bybit kategori varsayılanı ──────────────────────────────────────────


class TestTheDefaultCategoryComesFromTheCatalog:
    def test_it_falls_back_to_linear_when_the_symbol_is_absent(self, monkeypatch):
        import web.templating as t

        monkeypatch.setattr("data.list_catalog_bybit_symbols", lambda: [])
        assert t.bybit_default_category("BTCUSDT") == "linear"

    def test_it_reports_spot_when_only_spot_is_cached(self, monkeypatch):
        import web.templating as t

        monkeypatch.setattr(
            "data.list_catalog_bybit_symbols",
            lambda: [{"symbol": "BTCUSDT", "category": "spot"}],
        )
        assert t.bybit_default_category("BTCUSDT") == "spot"

    def test_linear_wins_when_both_are_cached(self, monkeypatch):
        """Öncelik sırası SIMPLE'ın haritasıyla aynı olmalı, yoksa iki yüzey
        yine ayrışır."""
        import web.templating as t

        monkeypatch.setattr(
            "data.list_catalog_bybit_symbols",
            lambda: [
                {"symbol": "BTCUSDT", "category": "spot"},
                {"symbol": "BTCUSDT", "category": "linear"},
            ],
        )
        assert t.bybit_default_category("BTCUSDT") == "linear"

    def test_a_broken_catalog_does_not_break_the_page(self, monkeypatch):
        import web.templating as t

        def _boom():
            raise OSError("catalog unreadable")

        monkeypatch.setattr("data.list_catalog_bybit_symbols", _boom)
        assert t.bybit_default_category("BTCUSDT") == "linear"

    def test_the_auto_brief_renders_the_resolved_category_as_selected(
        self, monkeypatch
    ):
        """Şablonda gerçekten uygulanıyor mu — sabit kalmış olabilirdi."""
        import web.templating as t

        monkeypatch.setattr(t, "bybit_default_category", lambda *_: "spot")
        t.templates.env.globals["bybit_default_category"] = lambda *_: "spot"
        try:
            html = _client().get("/studio").text
            assert '<option value="spot" selected>spot</option>' in html
            assert '<option value="linear" >linear</option>' in html
        finally:
            t.templates.env.globals["bybit_default_category"] = t.bybit_default_category


# ── 4 · Blok parametrelerinde min/max sunucu tarafında ──────────────────────


class TestBlockParamsAreClampedServerSide:
    """DeepR 2026-08-11 [DÜŞÜK]: `POST /strategy/drafts` parametreyi yalnız
    `int()`/`float()` ile çeviriyordu; `min`/`max` SADECE HTML input
    attribute'uydu, yani tarayıcı dışı her yol (curl, otomasyon) onları
    geçiyordu. Sonuç sessiz bir ölü bloktu: `p_period=0` + donchian →
    `max([])`, `p_fast=0` + ma_cross → ZeroDivisionError; ikisi de
    `composer._eval_block`'un geniş `except`'i tarafından yutulup her barda
    `None` dönüyor, kullanıcı HATA değil "0 işlemli geçerli görünen bir
    backtest" görüyordu.
    """

    @staticmethod
    def _drafts_after(client, **params):
        data = {"type": "ma_cross", "role": "entry", **params}
        client.post("/strategy/drafts", data=data)
        from web.routes.strategy import session_drafts, session_id

        # Aynı istemcinin çerezi = aynı sid.
        sid = client.cookies.get("nautlab_sid")
        assert sid, "oturum çerezi yazılmadı"
        assert session_id is not None
        return session_drafts(sid)

    def test_a_zero_period_is_clamped_to_the_catalog_minimum(self):
        from composer import BLOCK_CATALOG

        client = _client()
        drafts = self._drafts_after(client, p_fast="0", p_slow="30")
        assert drafts, "taslak eklenmedi"
        lo = BLOCK_CATALOG["ma_cross"]["params"]["fast"]["min"]
        assert drafts[-1].params["fast"] == lo

    def test_an_absurd_period_is_clamped_to_the_catalog_maximum(self):
        from composer import BLOCK_CATALOG

        client = _client()
        drafts = self._drafts_after(client, p_fast="10", p_slow="999999")
        hi = BLOCK_CATALOG["ma_cross"]["params"]["slow"]["max"]
        assert drafts[-1].params["slow"] == hi

    def test_a_value_inside_the_range_is_untouched(self):
        client = _client()
        drafts = self._drafts_after(client, p_fast="9", p_slow="41")
        assert drafts[-1].params["fast"] == 9
        assert drafts[-1].params["slow"] == 41

    def test_the_form_path_and_the_llm_path_share_one_rule(self):
        """`agent._coerce_block` zaten `coerce_params`'tan geçiyordu; eksik
        halka form yoluydu. İkisi ayrışırsa aynı hata geri gelir."""
        import inspect

        from web.routes import strategy as st

        assert "coerce_params" in inspect.getsource(st.add_draft)


# ── 5 · Studio LLM çağrıları ortak token defterine düşsün ───────────────────


class TestStudioLlmCallsAreAccounted:
    def test_the_client_records_usage(self, monkeypatch):
        """Ayrıntılı senaryolar `test_llm_endpoint_default.py`'de; buradaki
        çıpa, kablonun hiç kopmadığını DeepR turunun kendi dosyasında da
        tutmak için."""
        import httpx

        import token_ledger
        from strategy_studio.ai import HttpAnthropicClient

        seen = []
        monkeypatch.setattr(
            token_ledger, "record", lambda m, u, p="": seen.append((m, u, p))
        )
        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, **kw: httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                },
                request=httpx.Request("POST", url),
            ),
        )
        HttpAnthropicClient(api_key="k").complete("p")
        assert seen and seen[0][2] == "studio:suggest"
