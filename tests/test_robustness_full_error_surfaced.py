"""Manuel robustness suite'inin `full_error` alanı ARTIK okunuyor.

DeepR 2026-08-11 [YÜKSEK]: `sandbox._manual_suite_child` sonuç sözlüğüne
`"full_error": full_result.error` koyuyordu ve docstring'i bunu sözleşmenin
parçası ilan ediyordu — ama projede bu anahtarı okuyan TEK BİR SATIR yoktu.
Sonuç: Monte Carlo için koşulan tam backtest çöktüğünde

  * `mc` sözlüğü `{"error": "No trade data."}` olarak kalıyordu,
  * route'un `if suite.get("error") and "wfo_windows" not in suite` dalı
    tetiklenmiyordu (çünkü `wfo_windows` doluydu),
  * kullanıcı WFO ve IS/OOS tablolarını dolu görüp koşuyu BAŞARILI sanıyordu,
  * hata mesajı hem ekrandan hem sunucu log'undan tamamen kayboluyordu.

Yani kullanıcı "stratejim işlem üretmemiş" diye düşünürken gerçek şuydu:
altyapı patlamıştı. Bu dosya üç katmanı da bağlar:

  1. sandbox — "0 işlem" ve "backtest çöktü" AYRI iki sonuç üretmeli,
  2. route   — `full_error` sonuca taşınmalı, loglanmalı, adım listesine
     yazılmalı,
  3. şablon  — ikisi GÖRSEL olarak ayrılmalı (nötr boş-durum vs kırmızı hata).

Wiki References: [[nau_deepr_dorduncu_tur_2026_08_11]]
"""

from __future__ import annotations

import logging
import queue
import re
import time
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from composer import ComposedStrategySpec, SignalBlock


def _spec() -> ComposedStrategySpec:
    return ComposedStrategySpec(
        id="rob-spec",
        name="Robustness Spec",
        description="",
        blocks=[
            SignalBlock(
                type="ma_cross",
                role="entry",
                params={"fast": 5, "slow": 20, "direction": "up"},
            ),
            SignalBlock(
                type="atr_stop", role="exit", params={"period": 14, "mult": 3.0}
            ),
        ],
    )


def _bars(n: int = 120) -> pd.DataFrame:
    idx = pd.date_range(
        end=datetime.now(UTC).replace(minute=0, second=0, microsecond=0),
        periods=n,
        freq="60min",
        tz="UTC",
    )
    close = 30_000.0 + 500.0 * np.sin(np.arange(n) / 7.0)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 20,
            "low": close - 20,
            "close": close,
            "volume": np.full(n, 3.0),
        },
        index=idx,
    )


# ===========================================================================
# 1) sandbox — "0 işlem" ile "backtest çöktü" aynı şey DEĞİL
# ===========================================================================
class _FakeFullResult:
    def __init__(self, error=None, trades=None):
        self.error = error
        self.trades = trades or []


def _run_child(monkeypatch, full_result) -> dict:
    """`_manual_suite_child`'ı AYNI süreçte, ağır parçaları saplayarak koştur.

    `_start_parent_watchdog` şart: ana süreçte `mp.parent_process()` None
    döner ve watchdog `os._exit(1)` ile pytest'i öldürürdü.
    """
    import backtest
    import backtest_robustness
    import sandbox

    monkeypatch.setattr(sandbox, "_start_parent_watchdog", lambda: None)
    monkeypatch.setattr(sandbox, "_child_stdio_guard", lambda: None)
    monkeypatch.setattr(
        sandbox,
        "_build_instrument_bar_type",
        lambda recipe, bars_df=None: (_DummyInstrument(), "BT"),
    )
    monkeypatch.setattr(backtest_robustness, "run_walk_forward", lambda *a, **k: [{}])
    monkeypatch.setattr(
        backtest_robustness, "wfo_aggregate", lambda w: {"n_windows": 1}
    )
    monkeypatch.setattr(
        backtest_robustness, "run_insample_oos_split", lambda *a, **k: {"ok": True}
    )
    monkeypatch.setattr(
        backtest_robustness,
        "run_monte_carlo",
        lambda *a, **k: {"p5_final": 1.0},
    )
    monkeypatch.setattr(backtest, "run_composed_backtest", lambda *a, **k: full_result)

    q: queue.Queue = queue.Queue()
    params = {
        "train_months": 3,
        "test_months": 1,
        "n_optimize": 1,
        "objective": "sharpe",
        "split_pct": 0.7,
        "n_sims": 10,
    }
    sandbox._manual_suite_child(q, (_spec(), _bars(), {"symbol": "BTCUSDT"}, params))

    result = None
    while not q.empty():
        kind, payload = q.get()
        if kind == "error":  # pragma: no cover - test would be broken
            pytest.fail(f"child raised: {payload}")
        if kind == "result":
            result = payload
    assert result is not None, "child produced no result"
    return result


class _DummyInstrument:
    class _Id:
        venue = "BYBIT_LINEAR"

    id = _Id()


class TestSandboxDistinguishesCrashFromNoTrades:
    def test_crashed_backtest_marks_mc_as_failed(self, monkeypatch):
        """Tam backtest patlarsa `mc` "trade yok" DEĞİL, "hata" demeli.

        `failed=True` bayrağı şablonun dallandığı yer; hata metni de
        kaybolmadan `mc.error` içinde taşınmalı ki kullanıcı ne olduğunu
        okuyabilsin.
        """
        res = _run_child(
            monkeypatch, _FakeFullResult(error="RuntimeError: engine exploded")
        )
        assert res["full_error"] == "RuntimeError: engine exploded"
        assert res["mc"]["failed"] is True
        assert "engine exploded" in res["mc"]["error"]
        # WFO/IS-OOS gerçekten koşmuş — kısmi bozulma, tam çöküş değil.
        assert res["wfo_windows"] and res["split"]

    def test_zero_trades_is_not_reported_as_a_failure(self, monkeypatch):
        """0 işlem stratejiye dair bir OLGU; hata değil. `failed` False kalmalı."""
        res = _run_child(monkeypatch, _FakeFullResult(error=None, trades=[]))
        assert not res["full_error"]
        assert res["mc"]["failed"] is False
        assert "no trades" in res["mc"]["error"].lower()

    def test_healthy_run_still_produces_monte_carlo(self, monkeypatch):
        res = _run_child(
            monkeypatch, _FakeFullResult(error=None, trades=[{"pnl": 1.0}])
        )
        assert not res["full_error"]
        assert res["mc"] == {"p5_final": 1.0}


# ===========================================================================
# 2) route — `full_error` sonuca taşınır, loglanır, adım listesine yazılır
# ===========================================================================
@pytest.fixture
def rob_client(monkeypatch, tmp_path):
    """Robustness route'unu diske ve ağa dokunmadan sürülebilir hâle getir."""
    import composer
    import data
    import sandbox
    import server
    import web.routes.robustness as rob

    monkeypatch.setattr(composer, "load_catalog", lambda: [_spec()])
    monkeypatch.setattr(data, "load_bybit_bars", lambda **kw: _bars())
    monkeypatch.setattr(
        data, "_bybit_cache_path", lambda *a, **k: tmp_path / "nope.parquet"
    )
    # Gerçek ~/.cache/nautilus_web_app/robustness_log.jsonl'a yazma.
    monkeypatch.setattr(rob, "_log_robustness", lambda *a, **k: None)
    monkeypatch.setattr(sandbox, "run_manual_suite_guarded", None)  # test doldurur
    return TestClient(server.app)


def _drive_robustness(client, timeout_s: float = 20.0) -> str:
    resp = client.post(
        "/robustness/run",
        data={
            "spec_id": "rob-spec",
            "symbol": "BTCUSDT",
            "category": "linear",
            "interval": "60",
        },
    )
    assert resp.status_code == 200, resp.text
    m = re.search(r"/robustness/progress/([A-Za-z0-9]+)", resp.text)
    assert m, resp.text[:400]
    run_id = m.group(1)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        poll = client.get(f"/robustness/progress/{run_id}")
        assert poll.status_code == 200
        if "hx-trigger" not in poll.text or "Unknown run ID" in poll.text:
            return poll.text
        time.sleep(0.15)
    return ""


_DEGRADED_SUITE = {
    "wfo_windows": [],
    "wfo_summary": {},
    # split'in kendi hata dalı: şablon dolu bir split sözlüğü bekler, boş dict
    # verirsek testin konusu olmayan bir KeyError'a düşer.
    "split": {"error": "not run"},
    "mc": {"error": "Full backtest failed: ValueError: boom", "failed": True},
    "full_error": "ValueError: boom",
}


class TestRouteSurfacesFullError:
    def test_full_error_reaches_the_rendered_result(
        self, rob_client, monkeypatch, caplog
    ):
        """Uçtan uca: sandbox `full_error` döndürür → ekranda kırmızı bant.

        Eski davranışta bu POST tamamen yeşil bir robustness paneliyle
        dönüyordu; tek fark Monte Carlo sekmesindeki gri "No Trade data."
        satırıydı. Test hem sunucu log'unu hem HTML'i kontrol eder: bilgi iki
        kanaldan da kaybolmamalı.
        """
        import sandbox

        monkeypatch.setattr(
            sandbox, "run_manual_suite_guarded", lambda *a, **k: dict(_DEGRADED_SUITE)
        )
        with caplog.at_level(logging.WARNING, logger="web.routes.robustness"):
            html = _drive_robustness(rob_client)

        assert html, "robustness run did not finish"
        assert "ValueError: boom" in html
        assert "Partially degraded run" in html
        # Sunucu log satırı da geri geldi.
        assert any("full backtest for Monte Carlo" in r.message for r in caplog.records)

    def test_persisted_robustness_record_keeps_the_degradation(self, tmp_path):
        """/reports geçmişe bakarken ekrandan farklı bir hikâye anlatmasın.

        `log_robustness` sonucun yalnız seçili alanlarını yazıyor; `full_error`
        ve `mc.failed` o listeye eklenmezse kalıcı kayıt "Monte Carlo yok"
        diyip nedenini yutardı — düzeltilen sessizliğin log tarafındaki ikizi.
        """
        import json

        import web.shared as shared

        log_path = tmp_path / "robustness_log.jsonl"
        original = shared.ROBUSTNESS_LOG
        try:
            shared.ROBUSTNESS_LOG = log_path
            shared.log_robustness("rob-spec", "S", dict(_DEGRADED_SUITE))
        finally:
            shared.ROBUSTNESS_LOG = original

        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["full_error"] == "ValueError: boom"
        assert record["monte_carlo"]["failed"] is True

    def test_healthy_run_shows_no_degraded_banner(self, rob_client, monkeypatch):
        """Regresyon koruması: sağlıklı koşuda kırmızı bant GÖRÜNMEMELİ."""
        import sandbox

        healthy = {
            "wfo_windows": [],
            "wfo_summary": {},
            "split": {"error": "not run"},
            "mc": {"error": "The backtest produced no trades", "failed": False},
            "full_error": "",
        }
        monkeypatch.setattr(
            sandbox, "run_manual_suite_guarded", lambda *a, **k: dict(healthy)
        )
        html = _drive_robustness(rob_client)
        assert html
        assert "Partially degraded run" not in html
        assert "Monte Carlo could not run" not in html


# ===========================================================================
# 3) şablon — iki durum GÖRSEL olarak ayrı
# ===========================================================================
def _render(result: dict) -> str:
    from server import templates

    return templates.get_template("fragments/robustness_result.html").render(
        r=result, request=None
    )


_BASE_RESULT = {
    "spec_name": "S",
    "symbol": "BTCUSDT",
    "category": "linear",
    "interval": "60",
    "start_date": "2026-01-01",
    "end_date": "2026-02-01",
    "n_bars": 100,
    "wfo_windows": [],
    "wfo_summary": {},
    "split": {"error": "not run"},
    "train_months": 3,
    "test_months": 1,
    "n_sims": 10,
    "n_optimize": 5,
    "objective": "sharpe",
}


class TestTemplateSeparatesErrorFromNoTrades:
    def test_crash_renders_an_error_box_not_an_empty_state(self):
        html = _render(
            {
                **_BASE_RESULT,
                "mc": {
                    "error": "Full backtest failed: KeyError: 'close'",
                    "failed": True,
                },
                "full_error": "KeyError: 'close'",
            }
        )
        assert "Monte Carlo could not run" in html
        assert "KeyError" in html
        # Sekme başlığı da işaretli: kullanıcı tıklamadan bozulmayı görmeli.
        assert "Monte Carlo ✗" in html
        # Nötr boş-durum kutusu bu dalda KULLANILMAMALI.
        assert '<div class="empty-state">Full backtest failed' not in html

    def test_zero_trades_stays_a_neutral_empty_state(self):
        html = _render(
            {
                **_BASE_RESULT,
                "mc": {
                    "error": "The backtest produced no trades — Monte Carlo needs "
                    "at least one trade.",
                    "failed": False,
                },
                "full_error": "",
            }
        )
        assert "empty-state" in html
        assert "Monte Carlo could not run" not in html
        assert "Partially degraded run" not in html
