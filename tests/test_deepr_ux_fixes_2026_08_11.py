"""DeepR 2026-08-11 [ORTA] kullanıcı-deneyimi turu — sekiz bulgunun kapağı.

Hepsinin ortak teması, kullanıcının GÖRDÜĞÜ ile uygulamanın YAPTIĞI arasındaki
sessiz fark:

1. Backtest yüzeyindeki 4xx'ler HX-Toast'suzdu ve htmx 1.x 2xx dışı yanıtları
   DOM'a basmadığı için düğmeye basmak hiçbir şey yapmıyormuş gibi görünüyordu.
2. `/chart/data` küçük bir pencere için bile önbelleğin başına kadar SINIRSIZ
   Bybit backfill'i başlatabiliyordu (istek saatlerce asılı).
3. Strategy Builder'ın "▶ Run backtest"i hızlı tıklamada sınırsız paralel koşu
   kuyruklıyordu.
4. SIMPLE sihirbazının "Improve with AI"ı backtest sonucunu AI'a hiç
   göndermiyordu — "sonucuna bakıp iyileştir" vaadi boştu.
5. Sihirbazın MAX'ı seçilen zaman dilimini yok sayıp hep 1 saatlik kapsamı
   dolduruyordu.
6. Sihirbaz Bybit kategorisini "linear" sabitliyor, coin listesi ise
   spot/linear karışıktı — listelenen şey ile koşulan şey aynı değildi.
7. Sol menüdeki "Strategy Builder" taze kurulumda ipucu bile vermeyen ölü bir
   bağlantıydı (pointer-events:none tooltip'i de öldürüyordu).
8. `nautlab_sid` çerezi 1 saatte sertçe düşüyor, uzun bir araştırma oturumunda
   sonuç paneli/taslaklar/tek-koşu koruması bir anda sıfırlanıyordu.

Şablon iddiaları RENDER EDİLEREK doğrulanır (dosyada string aramak, o parçanın
gerçekten sayfaya girdiğini kanıtlamaz). Gerçek kataloğa/studio.db'ye yazılmaz.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _client():
    from fastapi.testclient import TestClient

    from server import app

    return TestClient(app)


@pytest.fixture
def empty_catalog(monkeypatch):
    """Diskteki gerçek kataloğu testin dışında tut (okuma da deterministik olsun)."""
    import web.routes.backtest as bt
    import web.routes.studio as studio

    monkeypatch.setattr(studio, "load_catalog", lambda: [])
    monkeypatch.setattr(bt, "load_catalog", lambda: [])
    return studio


# ── Bulgu 1 · Sessiz 4xx: gövde + toast ─────────────────────────────────────


def test_run_with_a_blank_spec_id_says_what_to_do(empty_catalog):
    """#spec-picker'ın VARSAYILAN seçeneği boş spec_id gönderir.

    Bu bir "bulunamadı" hatası değil, hiç yapılmamış bir seçimdir: eski
    "Spec not found." metni kullanıcının yapmadığı bir şey hakkında
    konuşuyordu (ve toast'suz olduğu için ekrana hiç ulaşmıyordu).
    """
    resp = _client().post("/backtest/run", data={"spec_id": ""})

    assert resp.status_code == 400
    assert "HX-Toast" in resp.headers
    assert resp.headers["HX-Toast"].startswith("err|")
    assert "Pick a saved strategy" in resp.text


def test_run_with_a_deleted_spec_id_toasts_instead_of_dying_silently(empty_catalog):
    """Başka sekmede silinmiş bir spec: 404 kalır ama artık SÖYLENİR."""
    resp = _client().post("/backtest/run", data={"spec_id": "gone-1234"})

    assert resp.status_code == 404
    assert "reload the page" in resp.headers["HX-Toast"]


def test_describe_rejects_a_short_description_with_a_toast(empty_catalog):
    """PRO'daki hero '▶ Run Backtest' her zaman /backtest/describe'a gider;
    açıklama kısa olduğunda spinner sönüyor ve sebep hiç görünmüyordu."""
    resp = _client().post("/backtest/describe", data={"description": "kisa"})

    assert resp.status_code == 400
    assert "HX-Toast" in resp.headers
    assert "at least 10 characters" in resp.text


def test_robustness_run_without_a_spec_explains_itself(monkeypatch):
    """Robustness formunun spec_id'si #result panelinden JS ile doldurulur;
    panel yoksa boş gider ve eski hâlinde ekranda hiçbir şey olmuyordu."""
    import composer

    monkeypatch.setattr(composer, "load_catalog", lambda: [])

    resp = _client().post("/robustness/run", data={"spec_id": ""})

    assert resp.status_code == 404
    assert "HX-Toast" in resp.headers
    assert "run a backtest first" in resp.text


def test_toast_messages_survive_the_latin1_header_encoding(empty_catalog):
    """HX-Toast latin-1 kodlanır: mesajlarda em-dash/Türkçe karakter olursa
    kullanıcı '?' görür. Reddetme metinleri bilerek ASCII noktalamayla yazıldı."""
    resp = _client().post("/backtest/run", data={"spec_id": ""})

    assert "?" not in resp.headers["HX-Toast"]


def test_backtest_surface_swaps_4xx_bodies_but_never_the_409(empty_catalog):
    """AUTO'daki `htmx:beforeSwap` duruşunun backtest karşılığı.

    409 dışarıda bırakılmalı: "zaten bir koşu var" gövdesini #result'a basmak
    CANLI koşunun ilerleme panelini silerdi (onun geri bildirimi toast).
    """
    page = _client().get("/studio").text

    assert "htmx:beforeSwap" in page
    assert "BT_SHOW_4XX" in page
    for path in ("/backtest/run", "/backtest/describe", "/backtest/sweep"):
        assert f'"{path}": 1' in page
    assert "st === 409" in page  # 409 erkenden elenir → swap yok, sadece toast
    # Ham JSON (FastAPI doğrulama hatası) sonuç paneline basılmaz.
    assert 'indexOf("text/html")' in page
    # Kendini sonlandıran yoklama uçları listede OLMAMALI.
    assert '"/backtest/progress"' not in page


def test_catalog_handoff_creates_the_missing_picker_option(empty_catalog):
    """Compose'da az önce kaydedilen strateji için #spec-picker'da <option>
    yoktur (save_result.html onu OOB tazelemiyor) → seçici boş değere düşüp
    aynı sessiz 404'e çıkıyordu."""
    page = _client().get("/studio").text

    assert "window.studioBacktestSpec = function(specId, specName)" in page
    assert "sel.appendChild(opt)" in page


# ── Bulgu 2 · /chart/data indirme bütçesi ───────────────────────────────────


def _cov(start: str, end: str):
    return lambda *a, **k: {"start": start, "end": end}


def test_clamp_leaves_a_window_inside_the_cache_untouched(monkeypatch):
    import data
    from web.routes.chart import _clamp_backfill_window

    monkeypatch.setattr(data, "coverage_range", _cov("2024-01-01", "2026-01-01"))
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2025, 1, 3, tzinfo=UTC)

    assert _clamp_backfill_window("BTCUSDT", "linear", "1", start, end) == (
        start,
        end,
        "",
    )


def test_clamp_has_no_opinion_when_nothing_is_cached(monkeypatch):
    """Önbellek yoksa indirilecek şey pencerenin KENDİSİDİR ve o zaten
    _MAX_WINDOW_CANDLES ile sınırlı — burada ikinci bir kapı gerekmez."""
    import data
    from web.routes.chart import _clamp_backfill_window

    monkeypatch.setattr(data, "coverage_range", lambda *a, **k: None)
    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = datetime(2020, 1, 3, tzinfo=UTC)

    assert _clamp_backfill_window("BTCUSDT", "linear", "1", start, end) == (
        start,
        end,
        "",
    )


def test_clamp_trims_a_start_that_reaches_far_behind_the_cache(monkeypatch):
    """Asıl arıza buydu: DAR bir pencere bile (mum sayısı kapısından geçer)
    önbelleğin başına kadar olan TÜM boşluğu doldurmaya çalışıyordu."""
    import data
    from web.routes.chart import _MAX_BACKFILL_BARS, _clamp_backfill_window

    monkeypatch.setattr(data, "coverage_range", _cov("2024-01-01", "2026-01-01"))
    start = datetime(2023, 1, 1, tzinfo=UTC)  # önbelleğin ~1 yıl gerisi
    end = datetime(2025, 6, 1, tzinfo=UTC)

    new_start, new_end, notice = _clamp_backfill_window(
        "BTCUSDT", "linear", "1", start, end
    )

    assert new_start > start  # kırpıldı
    assert new_end == end  # kuyruk zaten önbelleğin içinde
    assert notice  # ve ne olduğu söyleniyor
    # Kırpma tam olarak bütçe kadar geriye izin verir (1m → bar = 60 sn).
    assert (
        datetime(2024, 1, 1, tzinfo=UTC) - new_start
    ).total_seconds() == _MAX_BACKFILL_BARS * 60


def test_clamp_reports_a_window_completely_out_of_reach(monkeypatch):
    import data
    from web.routes.chart import _clamp_backfill_window

    monkeypatch.setattr(data, "coverage_range", _cov("2024-01-01", "2026-01-01"))
    new_start, new_end, notice = _clamp_backfill_window(
        "BTCUSDT",
        "linear",
        "1",
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2020, 1, 3, tzinfo=UTC),
    )

    assert new_start >= new_end  # çağıran bunu hata olarak basar
    assert notice == "2024-01-01 … 2026-01-01"


def test_chart_data_refuses_an_out_of_reach_window_without_touching_the_network(
    monkeypatch,
):
    """/reports'ta eski bir koşunun işlemine tıklamak tam bu URL'i üretiyordu.
    Artık saatlerce asılı kalmak yerine sebebi yazan bir yanıt dönüyor."""
    import data

    monkeypatch.setattr(data, "coverage_range", _cov("2024-01-01", "2026-01-01"))

    def _must_not_run(*a, **k):
        raise AssertionError("backfill başlatıldı — bütçe kapısı geçildi")

    monkeypatch.setattr(data, "load_bybit_bars", _must_not_run)

    old = int(datetime(2020, 1, 1, tzinfo=UTC).timestamp())
    resp = _client().get(
        "/chart/data",
        params={
            "symbol": "BTCUSDT",
            "category": "linear",
            "interval": "1",
            "start_ts": old,
            "end_ts": old + 2 * 86400,
        },
    )

    body = resp.json()
    assert body["candles"] == []
    assert "outside the downloaded data" in body["error"]


def test_chart_reports_a_trimmed_window_to_the_user(monkeypatch):
    """Kırpma sessiz olmamalı: kullanıcı istediğinden dar bir grafiğe bakıyor."""
    import pandas as pd

    import data

    monkeypatch.setattr(data, "coverage_range", _cov("2024-01-01", "2026-01-01"))
    idx = pd.date_range("2025-01-01", periods=3, freq="h", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": [1.0] * 3,
            "high": [1.0] * 3,
            "low": [1.0] * 3,
            "close": [1.0] * 3,
            "volume": [1.0] * 3,
        },
        index=idx,
    )
    monkeypatch.setattr(data, "load_bybit_bars", lambda **k: frame)
    # Kapsam da SAHTE olmalı: `_clamp_backfill_window` `data.coverage_range`
    # okuyor ve yamalanmazsa test bu makinedeki GERÇEK Bybit önbelleğine
    # bağlanıyordu — kapsam 2026'da olduğu için istenen 2023 penceresi
    # "tamamen dışarıda" dalına düşüp `notice` yerine `error` dönüyordu.
    monkeypatch.setattr(
        data,
        "coverage_range",
        lambda *a, **k: {"start": "2023-01-01", "end": "2023-01-01"},
    )

    resp = _client().get(
        "/chart/data",
        params={
            "symbol": "BTCUSDT",
            "category": "linear",
            "interval": "1",
            "start_ts": int(datetime(2023, 1, 1, tzinfo=UTC).timestamp()),
            "end_ts": int(datetime(2023, 1, 20, tzinfo=UTC).timestamp()),
        },
    )

    assert "download budget" in resp.json()["notice"]


def test_chart_js_surfaces_the_notice():
    """Sunucunun söylediğini istemci de göstermeli, yoksa 'söylemek' olmuyor."""
    js = (ROOT / "web" / "static" / "chart.js").read_text(encoding="utf-8")

    assert "d.notice" in js and "showToast" in js


# ── Bulgu 3 · Builder: tek canlı koşu ───────────────────────────────────────


@pytest.fixture()
def builder_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from scripts.seed_studio import build_fixture
    from server import app
    from strategy_studio.store import StrategyStore
    from web.routes import strategy_studio as main

    store = StrategyStore(tmp_path / "t.db")
    fx = build_fixture()
    for ic in fx.instruments:
        ic.active = True
    store.save(fx)
    monkeypatch.setattr(main, "store", store)
    return TestClient(app), store, main


def test_builder_refuses_a_second_backtest_while_one_is_running(builder_client):
    """Her tık ayrı bir run_id + arka plan görevi kuyruklıyordu; nautilus
    motoruyla bunların her biri gerçek bir koşu ve hepsi ortak thread
    havuzunda — havuz dolunca bu sayfa dahil senkron rotalar susuyordu."""
    client, store, main = builder_client
    store.create_run("run-live", "wt-funding-v3", 1, False, "hash")
    main._claim_job("run-live")  # sahiplen: uzlaştırma 'interrupted' yapmasın
    try:
        resp = client.post("/studio/wt-funding-v3/backtest", data={})
    finally:
        main._release_job("run-live")

    assert resp.status_code == 409
    assert "already running" in resp.headers.get("HX-Toast", "")


def test_builder_guard_does_not_wedge_after_a_run_finishes(builder_client):
    """Guard'ın bedeli kalıcı bir kilit OLMAMALI: biten bir koşudan sonra
    düğme yine çalışır (restart'tan kalan sahipsiz satırlar için de
    _reconcile_studio_jobs_once önce koşar)."""
    client, _store, _main = builder_client

    first = client.post("/studio/wt-funding-v3/backtest", data={})
    second = client.post("/studio/wt-funding-v3/backtest", data={})

    assert first.status_code == 200
    assert second.status_code == 200


def test_builder_run_button_drops_duplicate_clicks(builder_client):
    """İstemci tarafı: hx-sync="this:drop" (AUTO START ile aynı desen)."""
    client, _store, _main = builder_client

    page = client.get("/studio/wt-funding-v3").text

    run_btn = page[page.find("/studio/wt-funding-v3/backtest") - 200 :][:400]
    assert 'hx-sync="this:drop"' in run_btn


# ── Bulgu 4 · "Improve with AI" backtest sonucunu gönderir ──────────────────


def test_simple_improve_sends_the_backtest_result_to_the_ai(empty_catalog):
    """Sihirbazın tüm vaadi buydu; alanlar boş gidince `_collect_chat_context`
    boş sözlük üretiyor ve AI sonuca hiç bakmadan jenerik cevap veriyordu."""
    page = _client().get("/studio").text

    assert "function swResultValues()" in page
    for field in ("bt_pnl_pct", "bt_sharpe", "bt_max_dd", "bt_n_trades", "bt_win_rate"):
        assert f"v.{field}" in page
    for field in ("rob_overfitting", "rob_verdict", "rob_oos_sharpe"):
        assert f"v.{field}" in page
    # ...ve gerçekten chat/new çağrısına ekleniyor.
    call = page[page.find('htmx.ajax("POST", "/backtest/chat/new"') :][:400]
    assert "swResultValues()" in call


def test_chat_new_actually_reads_those_fields():
    """Alan adları uçun imzasıyla eşleşmeli — yoksa sessizce boş gider."""
    import inspect

    from web.routes.backtest import chat_new

    params = inspect.signature(chat_new).parameters
    for field in ("bt_pnl_pct", "bt_sharpe", "bt_n_trades", "rob_verdict"):
        assert field in params


def test_collect_chat_context_builds_metrics_from_those_values():
    from web.routes.backtest import _collect_chat_context

    ctx = _collect_chat_context(
        "12.5", "1.4", "-8.0", "42", "55", "My Strategy", "60", "", "", "", "", ""
    )

    assert ctx["bt_metrics"]["pnl_pct"] == "12.5"
    assert ctx["robustness"] is None


# ── Bulgu 5 · MAX seçili zaman dilimini kullanır ────────────────────────────


def test_simple_max_uses_the_selected_timeframe(empty_catalog):
    """`.sw-quick.active` belge sırasındaki İLK aktif düğmeyi döndürüyordu ve
    bu Coin satırındakiydi (data-sw-interval taşımaz) → interval hep '60'."""
    page = _client().get("/studio").text

    assert "dataset.swInterval || '60'" not in page
    max_fn = page[page.find("window.swMaxRange") :][:400]
    assert "interval: swState.interval" in max_fn
    assert "symbol: swState.symbol" in max_fn


def test_simple_resyncs_the_date_range_when_the_selection_changes(empty_catalog):
    """AUTO brief'indeki mcSyncRange ile aynı ders: sembol/TF değişince eldeki
    tarihler artık başka bir serinin kapsamıdır."""
    page = _client().get("/studio").text

    for fn in (
        "function swPickSymbol",
        "function swPickInterval",
        "function swSymbolTyped",
    ):
        body = page[page.find(fn) :][:600]
        assert "swSyncRange()" in body, fn
    # Kutular boşken dokunmaz — boş = "tüm önbellek", bilinçli bir tercih.
    sync = page[page.find("function swSyncRange") :][:500]
    assert "if (!((s && s.value) || (e && e.value))) return;" in sync


# ── Bulgu 6 · Kategori: listelenen şey = koşulan şey ────────────────────────


def test_category_map_prefers_linear_when_a_symbol_lives_in_two_markets():
    from web.routes.studio import _bybit_symbol_category_map

    rows = [
        {"symbol": "BTCUSDT", "category": "spot"},
        {"symbol": "BTCUSDT", "category": "linear"},
    ]

    assert _bybit_symbol_category_map(rows) == {"BTCUSDT": "linear"}


def test_category_map_keeps_a_spot_only_symbol_as_spot():
    """Asıl arıza: bu sembol için de 'linear' gönderiliyordu → MAX 404 ('veri
    yok' yazıyor, veri aslında var) ve koşu yanlış seriyi arıyordu."""
    from web.routes.studio import _bybit_symbol_category_map

    rows = [{"symbol": "PEPEUSDT", "category": "spot"}]

    assert _bybit_symbol_category_map(rows) == {"PEPEUSDT": "spot"}


def test_category_map_tolerates_rows_without_a_category():
    from web.routes.studio import _bybit_symbol_category_map

    assert _bybit_symbol_category_map([{"symbol": "XUSDT"}]) == {"XUSDT": "linear"}
    assert _bybit_symbol_category_map(None) == {}


def test_simple_wizard_runs_a_spot_symbol_in_the_spot_category(
    empty_catalog, monkeypatch
):
    import web.routes.studio as studio

    monkeypatch.setattr(
        studio,
        "_bybit_symbols",
        lambda: [
            {"symbol": "BTCUSDT", "category": "linear"},
            {"symbol": "PEPEUSDT", "category": "spot"},
        ],
    )

    page = _client().get("/studio").text

    # Buton kategoriyi taşır, JS haritası da onu bilir.
    assert 'data-sw-symbol="PEPEUSDT" data-sw-category="spot"' in page
    assert '"PEPEUSDT": "spot"' in page
    # ...ve koşu artık sabit "linear" göndermiyor.
    run_fn = page[page.find("function swRun()") :][:1600]
    assert 'category: "linear"' not in run_fn
    assert "category: swState.category" in run_fn


# ── Bulgu 7 · Ölü menü öğesi kendini açıklar ────────────────────────────────


@pytest.fixture
def no_studio_strategy(monkeypatch):
    """Taze kurulum: Strategy Builder'ın gösterecek bir stratejisi yok."""
    from server import templates

    globals_ = templates.env.globals
    saved = globals_.get("first_studio_strategy_id")
    globals_["first_studio_strategy_id"] = lambda: None
    yield
    globals_["first_studio_strategy_id"] = saved


def test_disabled_builder_link_tells_the_user_why(empty_catalog, no_studio_strategy):
    """Eski hâli iki kez yalan söylüyordu: tooltip hiç görünmüyordu ve metin
    'Strategy Builder'da yeni oluştur' diyordu — öyle bir uç/buton yok."""
    page = _client().get("/studio").text

    # Çıpa `<span>` etiketi olmalı, düz metin DEĞİL: devre dışı hâlde
    # `onclick` mesajının kendisi de "Strategy Builder" içeriyor ve
    # `find("Strategy Builder")` o mesajın ortasını yakalayıp pencereyi
    # `return false`'tan önce kesiyordu (yanlış kırmızı — şablon doğruydu).
    anchor = page.find("<span>Strategy Builder</span>")
    link = page[anchor - 2400 : anchor]
    assert "nav-disabled" in link
    assert "showToast(" in link  # tıklayınca sebebi söyler
    assert "return false" in link  # ...ve hiçbir yere gitmez
    assert "seed_studio.py" in link  # gerçekten yapılabilir olan yol


def test_nav_disabled_style_no_longer_blocks_its_own_tooltip():
    """pointer-events:none tam da açıklamayı öldüren şeydi."""
    css = (ROOT / "web" / "static" / "app.css").read_text(encoding="utf-8")

    rule = css[css.find(".nav a.nav-disabled") :][:200]
    assert "pointer-events: none" not in rule
    assert "cursor: help" in rule


def test_enabled_builder_link_stays_a_plain_link(empty_catalog, monkeypatch):
    """Strateji VARSA hiçbir şey değişmemeli — bağlantı bağlantıdır."""
    from server import templates

    globals_ = templates.env.globals
    saved = globals_.get("first_studio_strategy_id")
    globals_["first_studio_strategy_id"] = lambda: "wt-funding-v3"
    try:
        page = _client().get("/studio").text
    finally:
        globals_["first_studio_strategy_id"] = saved

    # Çıpa `<span>` etiketi olmalı, düz metin DEĞİL: devre dışı hâlde
    # `onclick` mesajının kendisi de "Strategy Builder" içeriyor ve
    # `find("Strategy Builder")` o mesajın ortasını yakalayıp pencereyi
    # `return false`'tan önce kesiyordu (yanlış kırmızı — şablon doğruydu).
    anchor = page.find("<span>Strategy Builder</span>")
    link = page[anchor - 2400 : anchor]
    assert 'href="/studio/wt-funding-v3"' in link
    assert "nav-disabled" not in link


# ── Bulgu 8 · Oturum çerezi: uzun ömür + kayan süre ─────────────────────────


def test_session_cookie_outlives_a_research_session():
    """1 saatti; AUTO'nun varsayılan bütçesi tek başına 4 saat."""
    from web.shared import SESSION_COOKIE_MAX_AGE

    assert SESSION_COOKIE_MAX_AGE > 4 * 3600


def test_every_cookie_writer_uses_the_same_lifetime():
    """Dört modül aynı seçenekleri elle kopyalıyordu; biri güncellenince
    ötekiler sessizce eski ömürde kalıyordu."""
    from web.shared import SESSION_COOKIE_MAX_AGE

    for rel in ("web/routes/studio.py", "web/routes/lab.py", "web/routes/strategy.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "max_age=3600" not in src, rel
        assert "set_session_cookie" in src, rel
    assert SESSION_COOKIE_MAX_AGE == 30 * 24 * 3600


def test_studio_refreshes_the_cookie_on_a_revisit(empty_catalog):
    """Kayan süre: çerez eskiden yalnız YOKSA yazılıyordu, yani sayfayı
    yenilemek ömrü uzatmıyordu ve bir saat sonra sid (dolayısıyla son sonuç,
    taslaklar, tek-koşu koruması) düşüyordu."""
    client = _client()
    client.cookies.set("nautlab_sid", "sid-abc")

    resp = client.get("/studio")

    header = resp.headers.get("set-cookie", "")
    assert "nautlab_sid=sid-abc" in header
    assert "Max-Age=2592000" in header


def test_lab_refreshes_the_cookie_on_a_revisit(monkeypatch):
    client = _client()
    client.cookies.set("nautlab_sid", "sid-lab")

    resp = client.get("/lab")

    assert "nautlab_sid=sid-lab" in resp.headers.get("set-cookie", "")


def test_a_first_visit_still_mints_a_session(empty_catalog):
    resp = _client().get("/studio")

    assert resp.cookies.get("nautlab_sid")


def test_draft_routes_share_the_same_cookie_helper():
    """/strategy/* taslak uçları zaten kayan yenileme yapıyordu; ömür artık
    web.shared'daki TEK sabitten geliyor."""
    from web.routes.strategy import _sid_cookie
    from web.shared import SESSION_COOKIE_MAX_AGE

    resp = SimpleNamespace(
        cookies={}, set_cookie=lambda *a, **k: resp.cookies.update({a[0]: k})
    )
    _sid_cookie(resp, "sid-1")

    assert resp.cookies["nautlab_sid"]["max_age"] == SESSION_COOKIE_MAX_AGE
