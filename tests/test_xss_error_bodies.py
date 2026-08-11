"""Hata gövdelerinde yansıyan XSS — saldırı denemeleriyle kanıtlanır.

DeepR 2026-08-11 [ORTA]: birden çok hata yolunda ham istek verisi f-string ile
``HTMLResponse`` gövdesine escape edilmeden yazılıyordu. Bu teorik bir bulgu
DEĞİLDİ; üç şey aynı anda doğruydu:

* yanıtlar ``text/html`` ve htmx gövdeyi ``innerHTML`` ile basıyor,
* ``web/templates/studio.html`` ``/agent/run`` için 4xx gövdelerini bilerek
  swap ediyor (``d.shouldSwap = true; d.isError = false``),
* ``base.html`` ``htmx.config.allowScriptTags = true`` ayarlıyor,

ve uygulama cloudflared tüneliyle internete bakıyor.

Bu dosya "escape çağrıldı mı" diye BAKMAZ — payload'ı gerçekten gönderir ve
yanıt gövdesinde ham ``<script>`` / ``onerror=`` / öznitelikten kaçış
kalmadığını doğrular. Testin işi, birinin yarın aynı sink'i f-string'e geri
çevirmesini yakalamak.

Wiki References
---------------
Bkz: [[nau_deepr_toplu_sertlestirme_2026_08]]
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from markupsafe import Markup

import server
from web.shared import error_html, esc, safe_html

# Gerçek saldırı yükleri. Sırasıyla: düz script etiketi, öznitelikten çıkıp
# olay işleyicisi asma, tırnaksız öznitelik bağlamı, ve `<div class='...'>`
# tek-tırnaklı kabuğundan çıkma denemesi.
PAYLOADS = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "'><svg/onload=alert(1)>",
    "</div><script>fetch('//evil')</script>",
]


_DANGEROUS_TAGS = {"script", "svg", "img", "iframe", "object", "embed", "style"}


class _TagCollector(HTMLParser):
    """Gövdeyi TARAYICI GİBİ ayrıştırır — 'metinde geçiyor' ile 'etiket oldu'
    ayrımını yapabilmenin tek dürüst yolu bu. Kaçırılmış bir payload'da
    ``onerror=`` düz metin olarak GÖRÜNÜR; önemli olan bir etiketin özniteliği
    hâline gelmemesi."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attrs: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag.lower())
        self.attrs.extend((a.lower(), v) for a, v in attrs)

    handle_startendtag = handle_starttag


def assert_neutralised(body: str, payload: str) -> None:
    """Gövde payload'ı GÖSTERSİN ama ÇALIŞTIRMASIN."""
    assert payload not in body, f"ham payload gövdede duruyor: {body[:400]}"

    p = _TagCollector()
    p.feed(body)
    assert not (_DANGEROUS_TAGS & set(p.tags)), (
        f"payload etiket üretti: {sorted(set(p.tags))} — {body[:400]}"
    )
    for name, value in p.attrs:
        assert not name.startswith("on"), f"olay işleyicisi oluştu: {name}={value!r}"
        assert not (value or "").strip().lower().startswith("javascript:")

    # …ve kullanıcı hatayı yine OKUYABİLMELİ: kaçırılmış hâli gövdede olmalı.
    assert "&lt;" in body or "&#" in body or "&gt;" in body


@pytest.fixture()
def client(monkeypatch):
    """Auth kapalı (yerel varsayılan) + aynı-köken başlığı.

    ``Origin`` şart: aynı turda eklenen CSRF koruması, kökeni uyuşmayan
    yazma isteklerini 403'e çeviriyor — burada test edilen şey o değil.
    """
    monkeypatch.setattr(server, "_ACCESS_TOKEN", "")
    c = TestClient(server.app)
    c.headers.update({"Origin": "http://testserver"})
    return c


class TestSafeHtmlHelpers:
    """Ortak yardımcının sözleşmesi: şablon güvenilir, değerler değil."""

    @pytest.mark.parametrize("payload", PAYLOADS)
    def test_safe_html_escapes_values_but_keeps_template_markup(self, payload):
        out = safe_html("<b>{x}</b>", x=payload)

        assert out.startswith("<b>") and out.endswith("</b>")  # şablon HTML kaldı
        assert_neutralised(str(out), payload)

    @pytest.mark.parametrize("payload", PAYLOADS)
    def test_error_html_escapes_values(self, payload):
        resp = error_html("invalid date: {v}", 400, v=payload)

        assert resp.status_code == 400
        assert_neutralised(resp.body.decode(), payload)

    def test_markup_values_are_not_double_escaped(self):
        """Bilerek güvenli işaretlenmiş (``Markup``) bir parça yeniden kaçmaz.

        Aksi hâlde iç içe kullanım (``error_html`` gövdeyi kabuğa gömerken)
        ``&lt;b&gt;`` üretirdi."""
        out = safe_html("<p>{x}</p>", x=Markup("<b>ok</b>"))

        assert str(out) == "<p><b>ok</b></p>"

    def test_esc_renders_none_as_empty_not_the_word_none(self):
        assert str(esc(None)) == ""

    def test_toast_header_survives_turkish_and_strips_newlines(self):
        """``HX-Toast`` latin-1 kodlanır; Türkçe metin 500 üretmemeli.

        Ve CR/LF asla başlığa geçmemeli (başlık enjeksiyonu)."""
        resp = error_html("hata: {v}", 400, toast=True, v="şı\r\nX-Injected: 1\nkaçtı")

        toast = resp.headers["HX-Toast"]
        assert "\r" not in toast and "\n" not in toast
        assert "X-Injected" not in resp.headers


class TestStrategyRoutes:
    def test_save_custom_block_invalid_name_is_escaped(self, client):
        """`name` doğrulama BAŞARISIZ olduğunda yansıyor → tamamen serbest."""
        payload = '"><img src=x onerror=alert(1)>'

        r = client.post(
            "/strategy/blocks/save-custom",
            data={"name": payload, "meta_json": "{}", "code": "x"},
        )

        assert r.status_code == 400
        assert_neutralised(r.text, payload)

    def test_block_chat_new_unknown_name_is_escaped(self, client):
        """URL path parametresi; dal "kayıtlı değil"de çalışıyor.

        Yükte `/` YOK — bir eğik çizgi path'i böler ve istek route'a hiç
        ulaşmadan 404 olurdu, yani sink'i sınamamış olurduk."""
        payload = '"><img src=x onerror=alert(1)>'

        r = client.post(f"/strategy/blocks/{quote(payload, safe='')}/chat/new")

        assert r.status_code == 404
        assert_neutralised(r.text, payload)

    def test_save_custom_block_bad_meta_json_is_escaped(self, client, monkeypatch):
        """meta JSON hatası: istisna metni de gövdeye giriyor."""
        import custom_block_store as cbs

        monkeypatch.setattr(cbs, "is_valid_name", lambda n: True)
        r = client.post(
            "/strategy/blocks/save-custom",
            data={
                "name": "ok_name",
                "meta_json": '{"a": <script>alert(1)</script>}',
                "code": "x",
            },
        )

        assert r.status_code == 400
        assert "<script" not in r.text.lower()


class TestAgentRunRoute:
    """`/agent/run` en keskin yüzey: 4xx gövdesi bilerek DOM'a basılıyor."""

    def test_invalid_date_is_escaped(self, client):
        payload = '"><img src=x onerror=alert(1)>'

        r = client.post("/agent/run", data={"range_start": payload})

        assert r.status_code == 400
        assert_neutralised(r.text, payload)

    def test_unavailable_model_label_is_escaped(self, client, monkeypatch):
        """Bilinmeyen model id'si `OR · <girdi>` olarak aynen geri veriliyordu."""
        import agent

        payload = "<svg/onload=alert(1)>"
        monkeypatch.setattr(agent, "model_unavailable_reason", lambda m: "yok")
        monkeypatch.setattr(agent, "model_label", lambda m=None: f"OR · {m}")

        r = client.post("/agent/run", data={"model": payload})

        assert r.status_code == 400
        assert_neutralised(r.text, payload)


class TestBacktestRoutes:
    def test_progress_error_pre_block_is_escaped(self, client):
        """İKİNCİ DERECE sink: hata worker'da yazılıp sonraki poll'de basılıyor.

        `_set_error` üreticileri ham Form alanı taşıyor ("No bars for
        {ticker}"), yani yük POST ile yazılıp GET ile geri geliyor.
        """
        import web.routes.backtest as bt

        payload = "</pre><script>alert(1)</script>"
        run_id = "xsstest1"
        with bt._RUN_PROGRESS_LOCK:
            bt._RUN_PROGRESS[run_id] = {
                "done": True,
                "error": f"No bars for {payload}.",
                "result": None,
                "steps": [],
                "narrative": "",
                "spec_name": "",
            }
        try:
            r = client.get(f"/backtest/progress/{run_id}")
        finally:
            with bt._RUN_PROGRESS_LOCK:
                bt._RUN_PROGRESS.pop(run_id, None)

        assert r.status_code == 200
        assert_neutralised(r.text, payload)

    def test_ticker_options_escape_the_attribute_context(self, client, monkeypatch):
        """`value="…"` özniteliğinden çıkmak tek bir `"` ile mümkündü."""
        import web.routes.backtest as bt

        payload = '"><script>alert(1)</script>'
        monkeypatch.setattr(bt, "discover_index_tickers", lambda: [payload])

        r = client.get("/backtest/tickers")

        assert r.status_code == 200
        assert_neutralised(r.text, payload)
        # Öznitelik hâlâ kapalı bir öznitelik olmalı.
        assert r.text.count("<option") == 1


class TestReportsDetailErrorStillEscapes:
    """Zaten doğru olan tek sink ortak yardımcıya bağlandı — davranış korunmalı."""

    def test_detail_error_escapes(self):
        from web.routes.reports import _detail_error

        payload = "<script>alert(1)</script>"
        resp = _detail_error(f"Re-run failed: {payload}")

        assert resp.status_code == 200
        assert_neutralised(resp.body.decode(), payload)
