"""Ekranın gösterdiği durum, sunucunun bildiği durumla aynı kalmalı.

DeepR 2026-08-11 iki YÜKSEK bulgu çıkardı; ikisi de aynı sınıftan: bir durum
SAYFA RENDER'INDA bir kez hesaplanıyor, sonra değişiyor, ama ekran haber
almıyor.

* Dashboard'da Start'a basınca rozet "RUNNING" oluyordu ama Stop disabled
  kalıyordu — kullanıcı başlattığı döngüyü F5'lemeden durduramıyordu.
* /studio'nun Backtest sekmesi katalog boşken tüm gövdeyi bir empty-state ile
  değiştiriyordu; aynı sayfada Compose'da ilk strateji kaydedilince Backtest
  hâlâ "No saved strategies yet" diyordu.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from server import app


@pytest.fixture
def client():
    return TestClient(app)


# ── Dashboard: kontroller rozetle birlikte tazelenir ─────────────────────────


def _stop_button_disabled(html: str) -> bool | None:
    """Stop butonu disabled mı? Buton hiç yoksa None."""
    m = re.search(r'class="btn btn-danger"([^>]*)>', html)
    return None if m is None else ("disabled" in m.group(1))


def test_fragment_carries_the_controls_out_of_band(client):
    """Rozet yanıtı kontrolleri de taşımalı — ikisi aynı gerçeği gösteriyor."""
    r = client.get("/fragments/loop_status")
    assert r.status_code == 200
    assert 'hx-swap-oob="true"' in r.text
    assert 'id="loop-controls"' in r.text
    assert _stop_button_disabled(r.text) is not None, "Stop butonu fragment'te yok"


def test_page_render_does_not_duplicate_the_controls(client):
    """`oob` yalnız HTMX yanıtında doğru; sayfada ikizlenirse iki Start olur."""
    r = client.get("/")
    assert r.status_code == 200
    assert r.text.count('id="loop-controls"') == 1
    # Sayfadaki kapsayıcı OOB işareti TAŞIMAMALI.
    after = r.text.split('id="loop-controls"', 1)[1][:200]
    assert "hx-swap-oob" not in after


def test_controls_reflect_the_running_flag(client, monkeypatch):
    """Koşu canlıyken Stop etkin, Start disabled olmalı — ve tersi."""
    import web.routes.fragments as fragments

    class _FakeState:
        iterations: list = []

        def __init__(self, running: bool):
            self._running = running

        def snapshot(self):
            return None, None, self._running, "test"

    monkeypatch.setattr(fragments, "get_state", lambda: _FakeState(True))
    running_html = client.get("/fragments/loop_status").text
    assert _stop_button_disabled(running_html) is False, "koşarken Stop kapalı kalmış"
    assert "RUNNING" in running_html

    monkeypatch.setattr(fragments, "get_state", lambda: _FakeState(False))
    idle_html = client.get("/fragments/loop_status").text
    assert _stop_button_disabled(idle_html) is True, "boştayken Stop açık kalmış"


def test_has_catalog_is_computed_without_parsing_specs(monkeypatch):
    """2 saniyelik yoklama tüm kataloğu spec'lere çevirmemeli."""
    import composer

    calls = []
    monkeypatch.setattr(composer, "load_catalog", lambda: calls.append("parsed") or [])
    from web.viewmodels import loop_status_ctx

    class _S:
        iterations: list = []

    ctx = loop_status_ctx(_S(), False, "idle")
    assert "has_catalog" in ctx
    assert ctx["oob"] is True
    assert not calls, "has_catalog için load_catalog çağrılmamalı"


# ── Backtest gövdesi: boş durum kendini tazeleyebilmeli ──────────────────────


def test_backtest_body_container_always_exists(client):
    """Hedef eleman koşulun DIŞINDA olmalı, yoksa boş durum kendini yenileyemez."""
    for path in ("/studio", "/backtest"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.text.count('id="backtest-body-inner"') == 1, path


def test_empty_catalog_state_refreshes_itself(client, monkeypatch):
    """Katalog boşken parça 15 saniyede bir kendini tazelemeli."""
    import web.routes.studio as studio

    monkeypatch.setattr(studio, "load_catalog", lambda: [])
    html = client.get("/studio").text
    assert "No saved strategies yet" in html
    block = html.split('id="backtest-body-inner"', 1)[1][:600]
    assert 'hx-select="#backtest-body-inner"' in block
    assert 'hx-target="#backtest-body-inner"' in block
    assert "every 15s" in block


def test_empty_state_button_switches_tab_instead_of_reloading_the_same_page(
    client, monkeypatch
):
    """Eski buton `/studio`'ya gidiyordu — kullanıcı zaten oradayken hiçbir şey olmuyordu."""
    import web.routes.studio as studio

    monkeypatch.setattr(studio, "load_catalog", lambda: [])
    html = client.get("/studio").text
    # "No saved strategies yet" Compose sekmesinde de geçiyor — kapsayıcıdan
    # başlayarak ara ki doğru boş-durumu incelediğimizden emin olalım.
    block = html.split('id="backtest-body-inner"', 1)[1][:1200]
    assert "No saved strategies yet" in block
    assert "labTab('compose')" in block
    assert 'href="/studio"' not in block


def test_filled_catalog_has_no_refresh_trigger(client):
    """Katalog dolunca yoklama kendiliğinden durmalı — sürekli poll kalmasın."""
    html = client.get("/studio").text
    if "No saved strategies yet" in html:
        pytest.skip("bu kurulumda katalog boş; dolu-hâl yolu sınanamıyor")
    block = html.split('id="backtest-body-inner"', 1)[1][:600]
    assert "every 15s" not in block
