"""Characterization tests for GET /studio (web/routes/studio.py::page).

DeepR 2026-08-09 [YÜKSEK]: studio.py reaches directly into 4 other route
modules' private (`_`-prefixed) state (backtest.py, strategy.py,
agent_backtest.py's raw `_AGENT_LOCK`/`_AGENT_PROGRESS`) with zero coverage
of its own — `tests/test_studio_ui_fixes.py` drives `/backtest/*`, and
`tests/studio/*.py` drive the *different* `/studio/{id}` canvas router
(web/routes/strategy_studio.py), never bare `GET /studio`. Before touching
any of the reached-into names, this file locks in current behavior —
especially the active-run-id scan, the one piece involving `_AGENT_LOCK`
(a lock with documented deadlock history, see tests/test_lock_nesting.py).
"""

from __future__ import annotations

import pytest


def _client():
    from fastapi.testclient import TestClient

    from server import app

    return TestClient(app)


@pytest.fixture
def stub_catalog(monkeypatch):
    import web.routes.studio as studio

    monkeypatch.setattr(studio, "load_catalog", lambda: [])
    return studio


@pytest.fixture
def clean_agent_progress():
    """_AGENT_PROGRESS is process-global, shared with every other test in the
    suite. A test asserting "no active run" must not assume it starts empty —
    another test can legitimately leave a not-yet-cleaned-up entry behind
    (observed: this file's own no-active-run tests passed in isolation but
    failed inside the full suite). Snapshot, clear, restore — never mutate
    other tests' state permanently."""
    import web.routes.agent_backtest as ab

    with ab._AGENT_LOCK:
        saved = dict(ab._AGENT_PROGRESS)
        ab._AGENT_PROGRESS.clear()
    try:
        yield
    finally:
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS.clear()
            ab._AGENT_PROGRESS.update(saved)


def _progress_entry(*, done: bool) -> dict:
    return {
        "phases": [],
        "steps": [],
        "done": done,
        "error": None,
        "strategy_name": "",
        "stop_requested": False,
        "continuous_mode": False,
    }


def test_studio_page_renders_200_with_no_active_run(stub_catalog, clean_agent_progress):
    resp = _client().get("/studio")

    assert resp.status_code == 200
    assert "running = false" in resp.text


def test_auto_brief_opens_with_the_operators_working_defaults(stub_catalog):
    """Brief boş bir formla değil, fiilen koşturulan ayarlarla açılır.

    Alanların yarısı şablonda sabit, yarısı rotada çözülüyor; ikisi bir arada
    doğru olmadan "açılış = son koşu" sözü tutulmaz.
    """
    import re

    html = _client().get("/studio").text

    tfs = dict(re.findall(r'name="tfs" value="([^"]+)" hidden([^>]*)', html))
    assert [tf for tf, attrs in tfs.items() if "checked" in attrs] == ["60", "240", "D"]
    assert '<option value="relaxed" selected>Relaxed</option>' in html
    assert 'name="n_iterations" min="2" max="15" value="15"' in html
    # Boş = "formdan tavan koyma"; koşuyu sunucunun sert tavanları bağlar.
    assert 'name="max_hours" min="0.5" step="0.5" value=""' in html
    assert 'name="max_total_tokens" min="10000" step="10000" value=""' in html
    # Sembol ve model canlı listelerden geliyor: istenen değer o an yoksa
    # görünmeyen bir seçenek seçili kalmasın diye geri düşülür.
    import web.routes.studio as studio

    want = studio.AUTO_DEFAULT_SYMBOL
    expected = want if want in studio._mc_external_symbols() else "BTCUSDT"
    assert f'<option value="{expected}" selected>' in html


def test_auto_brief_model_default_matches_the_id_the_deployed_picker_shows():
    """AUTO_DEFAULT_MODEL, pm2'nin pinlediği uç id'siyle BİREBİR aynı olmalı.

    Picker'ın içeriği ortama bağlı: ecosystem.config.js
    ``NAUTILUS_OPENROUTER_MODELS`` pin'ini verdiği için canlı uygulamada liste
    ağdan değil o pin'den doğuyor ve satır ``or:<pin>`` oluyor — satıcı öneki
    YOK. Sabit bununla uyuşmazsa `_mc_default_model` sessizce ""e düşer: kutu
    boş açılır, kimse hata görmez, START kullanıcının seçmediği bir uçla koşar.

    Bu tam olarak 2026-08-16'da yaşandı: sabit ağdan gelen listeye (satıcı
    önekli `or:qwen/...`) bakılarak "düzeltilmek" üzereydi, oysa canlı listeyi
    pin belirliyordu. Ölçümü pm2'nin env'i olmadan almak yanıltıyor — bu yüzden
    kaynak env değil, deploy dosyasının kendisi.
    """
    import re
    from pathlib import Path

    import web.routes.studio as studio

    ecosystem = Path(__file__).resolve().parents[1] / "ecosystem.config.js"
    if not ecosystem.exists():
        pytest.skip("pm2 giriş dosyası bu makinede yok")
    pin = re.search(
        r'NAUTILUS_OPENROUTER_MODELS:\s*"([^"]*)"', ecosystem.read_text("utf-8")
    )
    if pin is None or not pin.group(1).strip():
        pytest.skip("deploy bir model pin'i vermiyor; liste ağdan geliyor")

    pinned = [s.strip() for s in pin.group(1).split(",") if s.strip()]
    assert studio.AUTO_DEFAULT_MODEL in [f"or:{mid}" for mid in pinned]


def test_auto_brief_model_falls_back_to_the_app_default_when_unlisted(stub_catalog):
    """Sabit picker'da yoksa kutu BOŞA düşer — işaretsiz bir seçenek kalmaz.

    Geri düşme kasıtlı (bkz. `_mc_default_model`), ama sessiz: kullanıcı brief'i
    açtığında farkı ancak MODEL kutusuna bakarsa görür. İki yönü de sabitliyoruz
    ki geri düşme bir gün "seçili hiçbir şey yok"a dönüşmesin.
    """
    import re

    import web.routes.studio as studio

    def selected(html: str) -> list[str]:
        block = re.search(r'name="model"(.*?)</select>', html, re.S)
        assert block is not None, "MODEL seçicisi hiç render edilmedi"
        return re.findall(r'value="([^"]*)"[^>]*selected', block.group(1))

    listed = [("", "varsayılan"), (studio.AUTO_DEFAULT_MODEL, "OR · pinli uç")]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(studio, "_llm_models", lambda: listed)
        assert selected(_client().get("/studio").text) == [studio.AUTO_DEFAULT_MODEL]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            studio,
            "_llm_models",
            lambda: [("", "varsayılan"), ("claude-opus-5", "Opus")],
        )
        assert selected(_client().get("/studio").text) == [""]


def test_symbol_default_and_picker_share_one_catalog_scan(stub_catalog):
    """Dış katalog taraması sayfa başına BİR kez koşmalı, iki kez değil.

    `list_external_instruments` önbelleksiz bir dizin taraması; varsayılanı ayrı
    çözmek onu sessizce ikinci kez çağırıyordu. Sayı kadar önemlisi: iki çağrı
    iki AYRI görüntü demek — picker'ın listelemediği bir id "seçili" gelebilir.
    """
    import re

    import web.routes.studio as studio

    calls = []
    catalog = ["BBB.NASDAQ", studio.AUTO_DEFAULT_SYMBOL]

    def counting_scan():
        calls.append(1)
        return list(catalog)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(studio, "_mc_external_symbols", counting_scan)
        html = _client().get("/studio").text

    assert len(calls) == 1, f"katalog taraması {len(calls)} kez koştu"
    block = re.search(r'name="symbol"(.*?)</select>', html, re.S)
    assert block is not None, "SYMBOL seçicisi hiç render edilmedi"
    assert re.findall(r'value="([^"]*)"[^>]*selected', block.group(1)) == [
        studio.AUTO_DEFAULT_SYMBOL
    ]


def test_symbol_default_falls_back_when_the_catalog_lacks_it(stub_catalog):
    """İstenen id o an taranan katalogda yoksa formun ilk satırı seçili kalır."""
    import re

    import web.routes.studio as studio

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(studio, "_mc_external_symbols", lambda: ["BBB.NASDAQ"])
        html = _client().get("/studio").text

    block = re.search(r'name="symbol"(.*?)</select>', html, re.S)
    assert re.findall(r'value="([^"]*)"[^>]*selected', block.group(1)) == ["BTCUSDT"]


def test_studio_page_sets_the_draft_session_cookie_when_missing(stub_catalog):
    resp = _client().get("/studio")

    assert "nautlab_sid" in resp.cookies


def test_studio_page_reflects_the_newest_active_run(stub_catalog, monkeypatch):
    import web.routes.agent_backtest as ab

    older_done = "aaaaaaaa"
    newer_active = "bbbbbbbb"
    with ab._AGENT_LOCK:
        ab._AGENT_PROGRESS[older_done] = _progress_entry(done=True)
        ab._AGENT_PROGRESS[newer_active] = _progress_entry(done=False)
    try:
        resp = _client().get("/studio")
    finally:
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS.pop(older_done, None)
            ab._AGENT_PROGRESS.pop(newer_active, None)

    assert resp.status_code == 200
    assert "running = true" in resp.text
    assert f"/agent/progress/{newer_active}?view=mission" in resp.text
    assert f"/agent/stop/{newer_active}" in resp.text
    assert older_done not in resp.text


def test_studio_page_ignores_a_run_already_marked_done(
    stub_catalog, clean_agent_progress
):
    import web.routes.agent_backtest as ab

    finished = "cccccccc"
    with ab._AGENT_LOCK:
        ab._AGENT_PROGRESS[finished] = _progress_entry(done=True)
    try:
        resp = _client().get("/studio")
    finally:
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS.pop(finished, None)

    assert resp.status_code == 200
    assert "running = false" in resp.text
    assert finished not in resp.text
