"""👪 Aile medyanı KALICI kayda ulaşıyor — yalnız canlı akışa ve artefakta değil.

Ölçüldü 2026-08-21, koşu 5e132203 (söndürücü ölçütün ilk canlı ateşlenmesi):
iki finalistte de 👪 satırı düştü, oturum artefaktı `family` bloğunu
taşıdı (own 0,504 / medyan 0,471 / 16 kardeş), bellek-içi `rob_scan_log`
iki sayıyı taşıdı — ama `robustness_log.jsonl` kaydı ve oturum olayının
`summary`'si aileyi taşımıyordu. Koddaki yorum "sayılar kalıcı kayda
geçiyor" diyordu; kalıcı kaydı yazan `web.shared.log_robustness`
`family`'yi hiç okumuyordu. /reports'un birleştirdiği tek dosya o.

Bu dosya üç kopyayı çiviler: kalıcı kayıt, oturum özeti, ilerleme tablosu.
"""

from __future__ import annotations

import json

import pytest

import web.shared as sh

FAMILY = {
    "own_calmar": 0.5041467972132047,
    "median_calmar": 0.4712278107822479,
    "iqr_calmar": [0.443447382058146, 0.4930710391314705],
    "n_siblings": 20,
    "n_valid": 16,
}


def _rob(family=None) -> dict:
    rob = {"wfo_windows": [], "wfo_summary": {}, "mc": {}, "split": {}}
    if family is not None:
        rob["family"] = family
    return rob


# ---------------------------------------------------------------------------
# 1) Kalıcı kayıt — robustness_log.jsonl
# ---------------------------------------------------------------------------


def test_the_permanent_record_carries_the_whole_family_block(tmp_path, monkeypatch):
    """Beş skaler birden: medyan tek başına yorumlanamaz — IQR ve n_valid gerek."""
    log = tmp_path / "robustness_log.jsonl"
    monkeypatch.setattr(sh, "ROBUSTNESS_LOG", log)
    sh.log_robustness("spec1", "Spec", _rob(FAMILY), symbol="QQQ", interval="1-DAY")
    rec = json.loads(log.read_text(encoding="utf-8").strip())
    assert rec["family"] == pytest.approx(FAMILY)


def test_a_skipped_family_is_recorded_as_null_not_absent(tmp_path, monkeypatch):
    """`null` = "ölçüm atlandı" (sebebi canlı akışta); anahtarın yokluğu ise
    "bu kayıt aileden habersiz eski bir sürümden" demek. İkisi ayrı kalsın.
    """
    log = tmp_path / "robustness_log.jsonl"
    monkeypatch.setattr(sh, "ROBUSTNESS_LOG", log)
    sh.log_robustness("spec1", "Spec", _rob(), symbol="QQQ", interval="1-DAY")
    rec = json.loads(log.read_text(encoding="utf-8").strip())
    assert "family" in rec and rec["family"] is None


def test_nan_inside_the_family_block_does_not_poison_the_line(tmp_path, monkeypatch):
    """sanitize_floats kaydın tamamına uygulanıyor; aile bloğu istisna değil."""
    log = tmp_path / "robustness_log.jsonl"
    monkeypatch.setattr(sh, "ROBUSTNESS_LOG", log)
    fam = dict(FAMILY, own_calmar=float("nan"))
    sh.log_robustness("spec1", "Spec", _rob(fam), symbol="QQQ", interval="1-DAY")
    line = log.read_text(encoding="utf-8").strip()
    assert "NaN" not in line
    assert json.loads(line)["family"]["own_calmar"] is None


# ---------------------------------------------------------------------------
# 2) Oturum olayı — robustness_result.summary
# ---------------------------------------------------------------------------


def test_the_session_summary_carries_the_family():
    from web.routes.agent_backtest import _robustness_log_summary

    assert _robustness_log_summary(_rob(FAMILY))["family"] == FAMILY
    assert _robustness_log_summary(_rob())["family"] is None


# ---------------------------------------------------------------------------
# 3) İlerleme tablosu — rob_scan_log satırı, GERÇEK route üzerinden
# ---------------------------------------------------------------------------


def _entry(rows: list[dict]) -> dict:
    """Kazanansız biten bir tarama: route `done` dalında tabloyu render eder."""
    return {
        "phases": [],
        "steps": [],
        "done": True,
        "error": None,
        "audit_degraded": False,
        "audit_error": None,
        "strategy_name": "",
        "stop_requested": False,
        "continuous_mode": False,
        "winner_result": None,
        "winner_spec_name": "",
        "winner_spec_id": "",
        "winner_rob": None,
        "winner_holdout": None,
        "rob_scan_log": rows,
        "rob_scan_current": 0,
        "rob_scan_total": 0,
        "hint": "",
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_cache_read": 0,
        "tokens_cache_write": 0,
        "provider_cost_usd": 0.0,
    }


@pytest.fixture
def render_scan():
    """`_AGENT_PROGRESS` süreç-geneli; anlık görüntü al, temizle, geri koy."""
    from fastapi.testclient import TestClient

    import web.routes.agent_backtest as ab
    from server import app

    with ab._AGENT_LOCK:
        saved = dict(ab._AGENT_PROGRESS)
        ab._AGENT_PROGRESS.clear()
    client = TestClient(app)

    def _go(rows: list[dict]) -> str:
        run_id = "famtest1"
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS[run_id] = _entry(rows)
        r = client.get(f"/agent/progress/{run_id}")
        assert r.status_code == 200, r.text[:300]
        return r.text

    try:
        yield _go
    finally:
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS.clear()
            ab._AGENT_PROGRESS.update(saved)


def test_the_progress_table_shows_own_next_to_median(render_scan):
    row = {
        "rank": 1,
        "name": "Volatility Breakout",
        "score": -2.671,
        "passed": True,
        "overfitting_label": "✓ Robust",
        "mc_dd_p50": -21.0,
        "wf_pass": "—",
        "ms_label": "✓",
        "own_calmar": 0.5041,
        "family_median_calmar": 0.4712,
    }
    assert "0.50 / 0.47" in render_scan([row])


def test_rows_from_before_the_metric_render_a_dash_not_a_500(render_scan):
    """Eski oturum durumunda anahtar yok; Jinja'da `Undefined is not none`
    DOĞRU döner — korunmazsa `format(Undefined)` şablonu patlatır, ve bu
    şablon kullanıcının canlı ekranında poll'lanıyor.
    """
    row = {
        "rank": 1,
        "name": "OldRowBeforeFamily",
        "score": -5.0,
        "passed": False,
        "overfitting_label": "✗",
        "mc_dd_p50": None,
        "wf_pass": "—",
        "ms_label": "—",
    }
    html = render_scan([row])
    assert "OldRowBeforeFamily" in html
