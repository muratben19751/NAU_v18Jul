"""DeepR 2026-08-17 — ikinci tur (5–8. bulgular).

İlk turun dosyası ``test_deepr_fixes_2026_08_17.py``. Bu turda kalanlar:

1. Aşırı geniş tarih aralığı (``0001-01-01``–``9999-12-31``) hem RAM/CPU yiyor
   hem ``date.max + 1`` taşmasıyla 500 üretiyordu.
2. ``render_md`` ham HTML'i geçiriyordu; AUTO review sayfası kullanıcının
   ``hint``'ini ve LLM strateji adlarını ``|safe`` ile basıyor.
3. Optimize rotası koşan sweep'i sormadan yenisini açıyordu.
4. AI önerisi URL'deki stratejiyle eşleştirilmiyordu.

Wiki References
---------------
Bkz: [[strategy_studio]], [[review_raporu_uretildigi_anda_bayatlar]]
"""

from __future__ import annotations

from datetime import date
from html.parser import HTMLParser

import pytest

# ---------------------------------------------------------------------------
# 1. Tarih aralığı genişliği
# ---------------------------------------------------------------------------


def test_absurd_date_range_is_refused_at_the_http_boundary():
    """Biçim ve sıra tek tek geçerli — kabul eden tam da buydu."""
    from web.shared import invalid_date_range

    err = invalid_date_range("0001-01-01", "9999-12-31")
    assert err and "too wide" in err


def test_realistic_ranges_still_pass():
    """Tavan hiçbir gerçek isteği kesmemeli: en uzun index geçmişi ~66 yıl."""
    from web.shared import invalid_date_range

    assert invalid_date_range("2024-01-01", "2024-12-31") is None
    assert invalid_date_range("1930-01-01", "2026-01-01") is None


def test_inverted_range_still_reports_inversion_not_width():
    """Ters aralığa önce 'ters' demek, 'çok geniş' demekten yardımcı."""
    from web.shared import invalid_date_range

    err = invalid_date_range("2025-01-01", "2024-01-01")
    assert err and "before the start" in err


def test_loader_refuses_the_range_itself():
    """Yükleyici HTTP sınırına GÜVENMEMELİ: script/test/yeni uç de çağırabilir.

    Eski kod burada `while d <= end: d += timedelta(days=1)` ile 3,6 milyon
    `date` nesnesi kurup `date.max`'ta `OverflowError` atıyordu.
    """
    from data import load_index_bars

    with pytest.raises(ValueError, match="day limit"):
        load_index_bars("SPX", date(1, 1, 1), date(9999, 12, 31))


# ---------------------------------------------------------------------------
# 2. render_md — güvenilmeyen içerikte çalıştırılabilir HTML üretmemeli
# ---------------------------------------------------------------------------


class _Audit(HTMLParser):
    """Üretilen HTML'de çalıştırılabilir ne varsa topla.

    Kaçırılmış metinde ``onerror`` GEÇEBİLİR — o zararsız, görünür yazıdır.
    Bu yüzden kontrol substring değil: gerçekten AYRIŞTIRILMIŞ bir etiket ya da
    öznitelik mi diye bakıyor. (İlk elde yazdığım substring kontrolü tam da bu
    yüzden altı vektörün üçünü yanlışlıkla "kötü" işaretlemişti.)
    """

    _EXEC_TAGS = {"script", "iframe", "object", "embed"}
    _EXEC_SCHEMES = {"javascript", "data", "vbscript"}

    def __init__(self) -> None:
        super().__init__()
        self.bad: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._EXEC_TAGS:
            self.bad.append(f"<{tag}>")
        for k, v in attrs:
            if k.lower().startswith("on"):
                self.bad.append(f"{k}=")
            if k.lower() in ("href", "src") and v:
                s = "".join(c for c in v.strip().lower() if c not in "\t\r\n\x00")
                head, sep, _ = s.partition(":")
                if sep and head in self._EXEC_SCHEMES:
                    self.bad.append(f"{k}={head}:")


def _executable_bits(html: str) -> list[str]:
    a = _Audit()
    a.feed(html)
    return a.bad


@pytest.mark.parametrize(
    "payload",
    [
        "<img src=x onerror=alert(1)>",
        "<script>alert(1)</script>",
        '<div onclick="alert(1)">x</div>',
        '<a href="javascript:x">y</a>',
        "<svg onload=alert(1)>",
        "[t](javascript:alert(1))",
        "[t](JaVa\tScRiPt:alert(1))",  # tarayıcı bunu da çalıştırır
        "![i](data:text/html;base64,PHNjcmlwdD4=)",
    ],
)
def test_untrusted_markdown_emits_nothing_executable(payload):
    from web.shared import render_md

    assert _executable_bits(render_md(payload)) == []


def test_untrusted_is_the_default():
    """Dördüncü çağıran doğru olanı HATIRLAMAK zorunda kalmasın."""
    from web.shared import render_md

    assert _executable_bits(render_md("<script>alert(1)</script>")) == []


@pytest.mark.parametrize(
    "src,expect",
    [
        ("[ok](https://x.com)", 'href="https://x.com"'),
        ("[rel](/sayfa)", 'href="/sayfa"'),
        ("[m](mailto:a@b.c)", 'href="mailto:a@b.c"'),
    ],
)
def test_legitimate_links_survive(src, expect):
    from web.shared import render_md

    assert expect in render_md(src)


def test_markdown_structure_survives_escaping():
    """Kaçırma HTML karakterlerine dokunuyor, markdown sözdizimine değil."""
    from web.shared import render_md

    out = render_md("# H\n\n| a |\n|---|\n| 1 |\n\n**b**\n\n```py\nx=1\n```")
    for tag in ("<h1>", "<table>", "<strong>", "<code"):
        assert tag in out, f"{tag} kayboldu"


def test_trusted_content_keeps_intentional_html():
    """Repo wiki sayfaları kasıtlı HTML içerebilir; kaçırmak onları bozardı."""
    from web.shared import render_md

    assert '<div class="note">x</div>' in render_md(
        '<div class="note">x</div>', trusted=True
    )


def test_repo_wiki_routes_opt_in_to_trusted():
    """İki güvenilir çağıran AÇIKÇA işaretli olmalı — sessizce değil."""
    import inspect

    from web.routes import strategy, wiki

    assert "trusted=True" in inspect.getsource(wiki._render)
    assert "trusted=True" in inspect.getsource(strategy.wiki_html_for)


# ---------------------------------------------------------------------------
# 3. Optimize eşzamanlılığı
# ---------------------------------------------------------------------------

SID = "rsi-adx-btc"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from scripts.seed_studio import build_engine_fixture
    from server import app as _host
    from strategy_studio.store import StrategyStore
    from web.routes import strategy_studio as main

    store = StrategyStore(tmp_path / "t.db")
    store.save(build_engine_fixture())
    monkeypatch.setattr(main, "store", store)
    c = TestClient(_host)
    c.store = store
    return c


def test_second_optimize_is_refused_while_one_runs(client, monkeypatch):
    """Her POST, koşanı hiç sormadan yeni bir arka plan işi açıyordu.

    Arayüz yalnız en yeni işi gösterdiği için ötekiler GÖRÜNMEDEN motor koşusu
    üretmeye devam ediyordu.
    """
    from web.routes import strategy_studio as main

    # Koşan bir sweep satırı: rotayı gerçekten çalıştırmadan aynı durumu kur.
    client.store.create_opt("run-in-flight", SID, 1, False)
    monkeypatch.setattr(main, "_reconcile_studio_jobs_once", lambda: None)

    r = client.post(f"/studio/{SID}/optimize")
    assert r.status_code == 409
    assert "already running" in r.text
    # Reddedilen istek ÇÖP SATIR bırakmamalı — eski davranışın ikinci zararı.
    assert client.store.latest_opt(SID)["run_id"] == "run-in-flight"


def test_an_orphaned_running_row_does_not_lock_the_button_forever(client):
    """Restart'tan kalan sahipsiz 'running' satırı uzlaştırma temizler.

    Koruma bu adım olmadan düğmeyi KALICI olarak kilitlerdi — bu yüzden
    `_job_in_flight` çağrılmadan önce `_reconcile_studio_jobs_once` var.
    """
    from web.routes import strategy_studio as main

    client.store.create_opt("orphan", SID, 1, False)
    main._JOBS_RECONCILED.discard(client.store.db_path)
    main._reconcile_studio_jobs_once()

    assert client.store.latest_opt(SID)["status"] == "interrupted"


# ---------------------------------------------------------------------------
# 4. Öneri sahipliği
# ---------------------------------------------------------------------------


def test_a_suggestion_cannot_be_accepted_from_another_strategys_url(client):
    """`sid` global, `strategy_id` yolun parçası — karşılaştırılmıyordu.

    `apply_suggestion` yalnız alanların uyup uymadığına bakar; hangi strateji
    için üretildiğine bakmaz. Yani A'ya ait uyumlu bir öneri B'nin taslağına
    uygulanabiliyordu.
    """
    from scripts.seed_studio import build_fixture

    other = build_fixture()
    client.store.save(other)
    client.store.add_suggestion("sug-1", SID, "entry", "{}", "review")

    r = client.post(f"/studio/{other.id}/ai/suggestions/sug-1/accept")
    assert r.status_code == 422
    assert client.store.get_suggestion("sug-1")["status"] == "review"


def test_a_suggestion_cannot_be_dismissed_from_another_strategys_url(client):
    """Ters yön: A'nın öneri DURUMU B'nin URL'sinden değiştirilebiliyordu."""
    from scripts.seed_studio import build_fixture

    other = build_fixture()
    client.store.save(other)
    client.store.add_suggestion("sug-2", SID, "entry", "{}", "review")

    r = client.post(f"/studio/{other.id}/ai/suggestions/sug-2/dismiss")
    assert r.status_code == 422
    assert client.store.get_suggestion("sug-2")["status"] == "review"
