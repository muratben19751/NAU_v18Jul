"""Sessizce yutulan hataların yüzeye çıkması (DeepR 2026-08-11 [ORTA]).

Dört bulgu da aynı sınıftan: kod bir şeyin BOZULDUĞUNU biliyordu ama ekranda
"sorun yok" görünüyordu. Her biri farklı bir yanlış okumaya yol açıyordu:

* Strategy Lab'in backtest log yazımı düşünce panel yeşil bitiyor, ama koşu
  /reports'ta yok — "bu koşuyu yapmış mıydım" cevapsız.
* Fills raporu alınamayınca her trade'in çıkış sebebi boşalıyor — "strateji
  hiç SL/TP kullanmamış" diye okunuyor.
* Blok önizlemesinde `evaluate()` çökünce grafik düz çiziliyor — kullanıcı
  bloğun MANTIĞINI kurcalıyor, oysa kod patlamış.
* Grafik gösterge hesabı patlayınca katman hiç görünmüyor — "bu stratejide
  gösterge yok" sanılıyor.

Testler bu dört yolun her birinde sebebin GÖRÜNÜR olduğunu pinler.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

# ── 1) Strategy Lab: backtest log yazımı ─────────────────────────────────────


def _lab_state(run_id: str):
    from web.routes import lab

    with lab._LAB_LOCK:
        lab._LAB_PROGRESS[run_id] = {
            "phases": [],
            "backtest_steps": [],
            "done": False,
            "result": None,
            "error": None,
            "strategy_name": "",
            "audit_degraded": False,
            "audit_error": None,
        }
    return lab


def test_lab_log_write_failure_marks_audit_degraded(monkeypatch):
    """Log yazımı düşerse koşu devam eder ama panel bunu SÖYLER."""
    run_id = "labaudit"
    lab = _lab_state(run_id)
    monkeypatch.setattr(
        lab,
        "_log_backtest",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = SimpleNamespace(log_ts="")
    try:
        lab._record_backtest_log(run_id, object(), result, "Bybit", {})
        with lab._LAB_LOCK:
            state = lab._LAB_PROGRESS[run_id]
            assert state["audit_degraded"] is True
            assert "disk full" in state["audit_error"]
        # log_ts boş kalmalı: tear sheet linki ölü bir anahtara açılmasın.
        assert result.log_ts == ""
    finally:
        with lab._LAB_LOCK:
            lab._LAB_PROGRESS.pop(run_id, None)


def test_lab_log_write_success_leaves_no_warning(monkeypatch):
    """Mutlu yol kirlenmesin: başarılı yazımda bayrak açılmaz, ts damgalanır."""
    run_id = "labclean"
    lab = _lab_state(run_id)
    monkeypatch.setattr(lab, "_log_backtest", lambda *a, **k: "2026-08-11T00:00:00")
    result = SimpleNamespace(log_ts="")
    try:
        lab._record_backtest_log(run_id, object(), result, "Bybit", {})
        with lab._LAB_LOCK:
            assert lab._LAB_PROGRESS[run_id]["audit_degraded"] is False
        assert result.log_ts == "2026-08-11T00:00:00"
    finally:
        with lab._LAB_LOCK:
            lab._LAB_PROGRESS.pop(run_id, None)


@pytest.mark.parametrize("template", ["lab_progress.html", "lab_result.html"])
def test_lab_templates_render_the_audit_warning(template):
    """Uyarı hem canlı panelde hem KALICI sonuç ekranında görünmeli.

    Sonuç ekranı poll etmediği için orada gösterilmezse uyarı hiç görülmez.
    """
    from server import templates

    ctx = {
        "run_id": "x",
        "phases": [],
        "done": True,
        "error": None,
        "market": {},
        "audit_degraded": True,
        "audit_error": "Backtest log unavailable: disk full",
        "state": {
            "phases": [],
            "backtest_steps": [],
            "strategy_name": "S",
            "audit_degraded": True,
            "audit_error": "Backtest log unavailable: disk full",
        },
        "last": {
            "spec_name": "S",
            "trades": [],
            "metrics": {},
            "params": {},
            "steps": [],
        },
    }
    html = templates.get_template(f"fragments/{template}").render(**ctx)
    assert "disk full" in html
    assert "AUDIT DEGRADED" in html


# ── 2) Fills raporu → çıkış sebepleri ────────────────────────────────────────


def test_fills_report_failure_returns_reason_not_silent_none():
    """Rapor alınamazsa sebep METNİ geri gelmeli (sessiz None değil)."""
    from backtest import _reason_fills_report

    def boom():
        raise RuntimeError("trader not initialised")

    frame, error = _reason_fills_report(
        SimpleNamespace(generate_order_fills_report=boom)
    )
    assert frame is None
    assert error == "RuntimeError: trader not initialised"


def test_fills_report_success_passes_frame_through():
    from backtest import _reason_fills_report

    df = pd.DataFrame({"tags": ["sl"]}, index=["O-1"])
    frame, error = _reason_fills_report(
        SimpleNamespace(generate_order_fills_report=lambda: df)
    )
    assert error is None
    assert frame is df


def test_exit_reasons_error_reaches_the_view_model():
    """metrics'teki sebep iteration_row üzerinden şablona taşınmalı."""
    from datetime import UTC, datetime

    from state import IterationResult
    from web.viewmodels import iteration_row

    r = IterationResult(
        id=1,
        strategy="composed:x",
        params={},
        metrics={"pnl": 1.0, "exit_reasons_error": "RuntimeError: boom"},
        equity_curve=[],
        rationale="",
        error=None,
        timestamp=datetime.now(UTC),
    )
    assert iteration_row(r)["exit_reasons_error"] == "RuntimeError: boom"
    # Sağlıklı koşuda anahtar None: şablon uyarıyı boşuna göstermesin.
    r.metrics.pop("exit_reasons_error")
    assert iteration_row(r)["exit_reasons_error"] is None


def test_trade_table_says_reasons_are_unavailable_not_absent():
    """Exit sütunu boş '—' değil, 'alınamadı' demeli."""
    from server import templates

    last = {
        "spec_name": "S",
        "params": {},
        "metrics": {},
        "steps": [],
        "exit_reasons_error": "RuntimeError: boom",
        "trades": [
            {
                "entry_time": 1_700_000_000,
                "exit_time": 1_700_003_600,
                "entry_price": 100.0,
                "exit_price": 110.0,
                "side": "BUY",
                "pnl": 10.0,
                "dur_min": 60,
                "exit_kind": None,
                "exit_reason": None,
                "entry_reason": None,
            }
        ],
    }
    html = templates.get_template("fragments/backtest_result.html").render(
        last=last, market={}
    )
    assert "Çıkış sebepleri bu koşu için alınamadı" in html
    assert "RuntimeError: boom" in html


def test_report_detail_marks_missing_reasons():
    from server import templates

    html = templates.get_template("fragments/report_detail.html").render(
        ts="2026-08-11T00:00:00",
        spec_name="S",
        fidelity_ok=True,
        old_n=1,
        new_n=1,
        old_pnl=1.0,
        new_pnl=1.0,
        metrics={"exit_reasons_error": "RuntimeError: boom"},
        chart_url="/chart/data",
        chart_symbol="BTCUSDT",
        chart_category="linear",
        chart_interval="60",
        chart_dom_id="c1",
        last={"spec_name": "S", "params": {}, "trades": []},
        trades=[
            {
                "entry_time": 1_700_000_000,
                "exit_time": 1_700_003_600,
                "entry_price": 100.0,
                "exit_price": 110.0,
                "side": "BUY",
                "pnl": 10.0,
                "exit_kind": None,
                "exit_reason": None,
                "entry_reason": None,
            }
        ],
    )
    assert "Çıkış sebepleri bu koşu için alınamadı" in html
    # Satır düzeyinde de: Exit hücresi '—' değil '?', Exit Reason 'alınamadı'.
    assert ">alınamadı</td>" in html
    assert 'title="RuntimeError: boom">?</span>' in html


# ── 3) Blok önizlemesi: evaluate() çökmesi ───────────────────────────────────

_CRASHING_BLOCK = """\
def max_lookback(params):
    return 10

def evaluate(state, block, closes, indicators, portfolio):
    return closes[-1] + None
"""

_SILENT_BLOCK = """\
def max_lookback(params):
    return 10

def evaluate(state, block, closes, indicators, portfolio):
    return None
"""


@pytest.fixture
def btc_cache(tmp_path, monkeypatch):
    """`_preview_signals`'ın okuduğu BTC 1m parquet'ini tmp'ye yönlendirir."""
    import data

    n = 200
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0 + i * 0.01 for i in range(n)],
            "volume": [10.0] * n,
        },
        index=idx,
    )
    monkeypatch.setattr(data, "BYBIT_CACHE_DIR", tmp_path)
    df.to_parquet(tmp_path / "linear_BTCUSDT_1m.parquet")
    return tmp_path


def test_preview_crash_is_reported_not_shown_as_no_signal(btc_cache):
    """Çökme ile 'hiç sinyal yok' AYRI: istisna metni geri gelmeli."""
    from web.routes.strategy import _preview_signals

    out = _preview_signals(_CRASHING_BLOCK, {"params": {}}, "entry")
    assert out["closes"], "fiyat serisi yine de çizilebilmeli"
    assert all(s["sig"] is None for s in out["signals"])
    # Ayırt edici bilgi: kaç barda ve NE hatası.
    assert out["eval_error_bars"] == len(out["signals"])
    assert "TypeError" in out["eval_error"]


def test_preview_without_signals_reports_no_error(btc_cache):
    """Gerçekten sinyal üretmeyen blok hata bayrağı KALDIRMAMALI."""
    from web.routes.strategy import _preview_signals

    out = _preview_signals(_SILENT_BLOCK, {"params": {}}, "entry")
    assert all(s["sig"] is None for s in out["signals"])
    assert out["eval_error"] is None and out["eval_error_bars"] == 0


def test_preview_total_failure_returns_reason(btc_cache):
    """Önizleme hiç koşamazsa boş {} değil, sebebi taşıyan bir sözlük döner."""
    from web.routes.strategy import _preview_signals

    out = _preview_signals("def evaluate(:\n", {"params": {}}, "entry")
    assert "error" in out and out["error"]
    assert "closes" not in out


@pytest.mark.parametrize(
    "template,anchor",
    [
        ("custom_block_generated.html", "evaluate() crashed on"),
        ("block_chat_thread.html", "barda hata verdi"),
    ],
)
def test_preview_templates_show_the_exception(template, anchor):
    from server import templates

    chart_data = {
        "closes": [100.0, 101.0],
        "dates": ["2024-01-01 00:00", "2024-01-01 00:01"],
        "signals": [{"i": 0, "sig": None}, {"i": 1, "sig": None}],
        "eval_error": "TypeError: unsupported operand type(s)",
        "eval_error_bars": 2,
    }
    proposal = {"name": "b", "code": "...", "meta": {"label": "b", "params": {}}}
    html = templates.get_template(f"fragments/{template}").render(
        proposal=proposal,
        chart_data=chart_data,
        label="b",
        description="d",
        role_hint="entry",
        conv_id="c",
        messages=[],
        meta=proposal["meta"],
    )
    assert anchor in html
    assert "TypeError" in html


# ── 4) Grafik gösterge hesabı ────────────────────────────────────────────────


def test_chart_payload_carries_the_indicator_error(monkeypatch):
    """Gösterge hesabı patlarsa mumlar gelir ama sebep de payload'da olur."""
    from fastapi.testclient import TestClient

    import chart_indicators
    import composer
    from server import app

    def fake_load(**kwargs):
        idx = pd.date_range("2024-01-01", periods=5, freq="1D", tz="UTC")
        return pd.DataFrame(
            {c: [1.0] * 5 for c in ("open", "high", "low", "close", "volume")},
            index=idx,
        )

    def boom(*a, **k):
        raise ValueError("unknown block type")

    monkeypatch.setattr("data.load_bybit_bars", fake_load)
    monkeypatch.setattr(composer, "load_catalog", lambda: [SimpleNamespace(id="s1")])
    monkeypatch.setattr(chart_indicators, "indicators_for_spec", boom)

    resp = TestClient(app).get(
        "/chart/data",
        params={"symbol": "BTCUSDT", "interval": "D", "bars": 50, "spec_id": "s1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["candles"]) == 5, "gösterge hatası grafiği düşürmemeli"
    assert body["indicators"] == {"overlays": [], "panes": []}
    # Boş katman "gösterge yok" DEĞİL: sebep açıkça taşınıyor.
    assert "unknown block type" in body["indicators_error"]


def test_chart_payload_has_no_error_when_indicators_succeed(monkeypatch):
    from fastapi.testclient import TestClient

    import chart_indicators
    import composer
    from server import app

    def fake_load(**kwargs):
        idx = pd.date_range("2024-01-01", periods=5, freq="1D", tz="UTC")
        return pd.DataFrame(
            {c: [1.0] * 5 for c in ("open", "high", "low", "close", "volume")},
            index=idx,
        )

    monkeypatch.setattr("data.load_bybit_bars", fake_load)
    monkeypatch.setattr(composer, "load_catalog", lambda: [SimpleNamespace(id="s1")])
    monkeypatch.setattr(
        chart_indicators,
        "indicators_for_spec",
        lambda *a, **k: {"overlays": [{"name": "RSI", "data": []}], "panes": []},
    )

    resp = TestClient(app).get(
        "/chart/data",
        params={"symbol": "BTCUSDT", "interval": "D", "bars": 50, "spec_id": "s1"},
    )
    body = resp.json()
    assert body["indicators_error"] is None
    assert body["indicators"]["overlays"][0]["name"] == "RSI"
