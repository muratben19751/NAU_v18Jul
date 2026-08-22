"""Harici (noktalı) enstrümanda CATEGORY uygulanmaz — ekran da kayıt da bunu söyler.

Ölçüldü 2026-08-22: QQQ.NASDAQ'ta "spot" (`e3271b87`) ve "linear" (`04e2dbff`)
seçilmiş iki AUTO koşusu aynı `NASDAQ · CASH` motorunda, aynı yoldan koştu.
Harici tarif kategori taşımaz (`_recipe`), veri `load_external_bars` ile gelir,
enstrüman harici katalogdan kurulur, tarih-aralığı düğmesi noktalı sembolde
kategoriyi hiç göndermez. Seçim yalnız ETİKET olarak sızıyordu: `session_start`,
kokpit BRIEF ve kazananın `robustness_log` kaydı — aynı enstrümanın iki koşusu
farklı seriymiş gibi görünüyordu.

Kapatma: rota `_effective_category` ile harici koşuda "" yazar (reports'un
"kategori yok" varsayılanı), brief "—" gösterir, formda select pasifleşir.
"""

from __future__ import annotations

import threading

import pandas as pd
import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# 1) Saf kural
# ---------------------------------------------------------------------------


def test_external_drops_the_category_and_bybit_keeps_it():
    from web.routes.agent_backtest import _effective_category

    assert _effective_category(True, "spot") == ""
    assert _effective_category(True, "linear") == ""
    assert _effective_category(False, "spot") == "spot"
    assert _effective_category(False, "linear") == "linear"


# ---------------------------------------------------------------------------
# 2) Rota: POST /agent/run — worker'a ve kokpit brief'ine ne gidiyor
# ---------------------------------------------------------------------------


def _bars(n: int = 400) -> pd.DataFrame:
    idx = pd.date_range("2020-01-02 14:30", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx
    )


@pytest.fixture
def run_client(monkeypatch):
    import data
    import web.routes.agent_backtest as ab
    from server import app

    monkeypatch.setattr(data, "_external_bar_dir", lambda *a, **k: (object(), object()))
    monkeypatch.setattr(data, "_external_adjusted_flag", lambda *a, **k: True)
    monkeypatch.setattr(data, "external_data_gap_report", lambda frame, **kw: None)
    monkeypatch.setattr(data, "load_external_bars", lambda *a, **k: _bars())
    monkeypatch.setattr(ab, "_AUTO_RUN_SLOTS", threading.BoundedSemaphore(50))

    got: dict = {}
    done = threading.Event()

    def fake_worker(**kw):
        got.update(kw)
        done.set()

    monkeypatch.setattr(ab, "_agent_worker", fake_worker)

    def _post(form: dict) -> dict:
        r = TestClient(app).post("/agent/run", data=form)
        assert r.status_code == 200, r.text[:300]
        assert done.wait(5)
        with ab._AGENT_LOCK:
            brief = dict(ab._AGENT_PROGRESS[got["run_id"]]["brief"])
        return {"worker": dict(got), "brief": brief}

    return _post


@pytest.mark.parametrize("chosen", ["spot", "linear"])
def test_a_dotted_symbol_records_no_category_whatever_the_form_said(run_client, chosen):
    """Seçim ne olursa olsun harici koşu kategorisiz: ekran ile kayıt aynı şeyi söyler."""
    out = run_client(
        {"symbol": "QQQ.NASDAQ", "category": chosen, "tfs": ["D"], "n_iterations": 2}
    )
    assert out["worker"]["category"] == ""
    assert out["brief"]["category"] == ""
    assert out["brief"]["symbol"] == "QQQ.NASDAQ"


def test_a_disabled_select_posts_nothing_and_still_ends_up_empty(run_client):
    """Form pasif select'i POST etmez → rota varsayılanı ("linear") görür; o da
    harici koşuda "" olmalı — aksi hâlde ekran "uygulanmaz" derken kayıt
    "linear" yazardı.
    """
    out = run_client({"symbol": "QQQ.NASDAQ", "tfs": ["D"], "n_iterations": 2})
    assert out["worker"]["category"] == ""
    assert out["brief"]["category"] == ""


# ---------------------------------------------------------------------------
# 3) Kokpit BRIEF: boş kategori "—" olarak görünür
# ---------------------------------------------------------------------------


def test_the_cockpit_brief_shows_a_dash_for_no_category():
    from web.templating import templates

    tpl = templates.env.get_template("fragments/auto_mission.html")
    src = tpl.render  # gerçek render tam bir mission_view ister; satırı doğrudan çivile
    assert src is not None
    raw = (
        templates.env.loader.get_source(templates.env, "fragments/auto_mission.html")
    )[0]
    assert 'mv.brief.category or "—"' in raw


# ---------------------------------------------------------------------------
# 4) Brief formu: harici sembolde kategori pasifleşir (kablo var mı)
# ---------------------------------------------------------------------------


def test_the_brief_form_wires_the_category_sync():
    from web.templating import templates

    raw = (templates.env.loader.get_source(templates.env, "studio.html"))[0]
    assert "window.mcCategorySync = function" in raw
    assert 'id="mc-category-note"' in raw
    # Sembol değişince VE açılışta çağrılıyor — ikisi de olmazsa sayfa yanlış
    # durumda açılabilir (QQQ varsayılan gelirken select aktif kalırdı).
    assert raw.count("window.mcCategorySync()") >= 2
