"""End-to-end tests for the /backtest/run → /backtest/progress/{run_id} →
result chain — the DeepR audit (2026-08-08) found this full user journey
(POST → poll → render) had zero test coverage: existing tests either mock
sandbox.run_backtest_guarded or call composer/state directly, skipping the
HTTP layer entirely.

DeepR 2026-08-11 [YÜKSEK] — "yeşil süit yanıltıcıydı": bu dosya pratikte
HİÇBİR YERDE koşmuyordu. İki katmanlı bir engel vardı:

1. `spec_id` fixture'ı `composer.load_catalog()` boşsa `pytest.skip` ediyordu.
   Katalog `~/.cache/nautilus_web_app/strategy_catalog.json`, yani repo DIŞINDA;
   temiz bir CI runner'ında hiç yok. Yani `-m e2e` ile açıkça çalıştırılsa bile
   test 0 iş yapıp yeşil dönüyordu, `ci.yml`'in 3-denemeli retry'ı da bunu
   "başarı" sayıyordu. ÇÖZÜM: katalog bağımlılığı ORTAMDAN çıkarıldı — testler
   `tmp_path`'e kendi mini kataloğunu tohumluyor (gerçek kataloğa asla
   dokunulmuyor), böylece skip edilecek bir sebep kalmıyor.

2. Zincirin geri kalanı canlı Bybit ağına bağlıydı ve `addopts = -m "not e2e"`
   yüzünden varsayılan koşumdan dışlanıyordu. ÇÖZÜM: aynı zincirin AĞSIZ bir
   ikizi eklendi (`TestBacktestChainOffline`) — `data.load_bybit_bars` sentetik
   bir OHLCV çerçevesiyle değiştiriliyor, geri kalan her şey (route, session
   guard, worker thread, sandbox alt süreci, gerçek Nautilus motoru, progress
   fragment'ı, sonuç render'ı) GERÇEK. Bu test varsayılan koşumda ve CI'da
   çalışır. Canlı Bybit testi silinmedi/zayıflatılmadı; `e2e` işaretiyle
   duruyor ve artık kataloğu olmadığı için değil, yalnızca ağ maliyeti
   yüzünden dışlanıyor.

Atlanan testler artık SESSİZ değil: `pyproject.toml`'daki `addopts` `-ra`
taşıyor (her skip'in gerekçesi özet satırında basılır) ve `ci.yml` skip
sayısını iş özetine yazar.

Wiki References: [[nau_deepr_dorduncu_tur_2026_08_11]]
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

os.environ.pop("NAU_ACCESS_TOKEN", None)


# ---------------------------------------------------------------------------
# Repo-içi tohum: gerçek ~/.cache kataloğuna ASLA dokunmadan bir strateji
# ---------------------------------------------------------------------------
def _seed_spec_dict() -> dict:
    """Yalnız built-in bloklardan oluşan bir spec — custom-block registry'ye,
    LLM'e, dış kataloğa bağımlı değil. ma_cross + atr_stop bu depodaki diğer
    testlerin de kullandığı en sade "gerçekten işlem üreten" bileşim."""
    return {
        "id": "e2e-seed-spec",
        "name": "E2E Seed MA Cross",
        "description": "seeded by tests — never written to the real catalog",
        "blocks": [
            {
                "type": "ma_cross",
                "role": "entry",
                "params": {"fast": 5, "slow": 20, "direction": "up"},
            },
            {
                "type": "atr_stop",
                "role": "exit",
                "params": {"period": 14, "mult": 3.0},
            },
        ],
        "trade_size": 0.01,
        "created_at": datetime.now(UTC).isoformat(),
    }


@pytest.fixture
def spec_id(tmp_path, monkeypatch):
    """Katalog dosyasını tmp_path'e taşı ve içine tek bir spec yaz.

    Eskiden burada `pytest.skip("no strategies in catalog.json")` vardı; test
    makineye bağlıydı ve CI'da sessizce atlanıyordu. Artık bağımlılık yok:
    fixture kendi kataloğunu kuruyor. `_CATALOG_RAW_CACHE` (mtime,size) ile
    memoize ettiği için elle sıfırlanmalı, yoksa önceki testin kataloğu
    sızabilir.
    """
    import composer

    catalog_file = tmp_path / "strategy_catalog.json"
    catalog_file.write_text(json.dumps([_seed_spec_dict()], indent=2), encoding="utf-8")
    monkeypatch.setattr(composer, "CATALOG_FILE", catalog_file)
    monkeypatch.setattr(composer, "_CATALOG_RAW_CACHE", None)
    monkeypatch.setattr(composer, "_CATALOG_ISSUES", None)

    catalog = composer.load_catalog()
    assert catalog, "seeded catalog must load — the seed spec is builtin-only"
    return catalog[0].id


@pytest.fixture
def client():
    import server

    return TestClient(server.app)


def _extract_run_id(html: str) -> str:
    m = re.search(r"/backtest/progress/([A-Za-z0-9]+)", html)
    assert m, f"no run_id found in response:\n{html[:500]}"
    return m.group(1)


def _poll_until_done(client, run_id: str, timeout_s: float) -> str:
    """HTMX'in yaptığını yap: bitene kadar `every 1s` sorgula.

    Bitiş işareti fragment'ın kendi kendini yeniden tetiklememesi
    (`hx-trigger` yok) — yani ya sonuç ya hata render'ı gelmiştir.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        poll = client.get(f"/backtest/progress/{run_id}")
        assert poll.status_code == 200
        if "hx-trigger" not in poll.text or "Unknown run ID" in poll.text:
            return poll.text
        time.sleep(0.3)
    return ""


def _synthetic_bybit_frame(n: int = 400, interval_min: int = 60) -> pd.DataFrame:
    """Deterministik, TREND İÇEREN sentetik OHLCV — ma_cross'un gerçekten
    kesişmesi (ve dolayısıyla işlem üretmesi) için düz gürültü yetmez."""
    idx = pd.date_range(
        end=datetime.now(UTC).replace(minute=0, second=0, microsecond=0),
        periods=n,
        freq=f"{interval_min}min",
        tz="UTC",
    )
    t = np.arange(n, dtype=float)
    # İki periyodu farklı sinüs → hızlı/yavaş ortalama defalarca kesişir.
    close = 30_000.0 + 2_000.0 * np.sin(t / 18.0) + 600.0 * np.sin(t / 5.0)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + 40.0
    low = np.minimum(open_, close) - 40.0
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, 12.5),
        },
        index=idx,
    )


def _assert_result_fragment(final_html: str) -> None:
    """Zincirin SONU gerçekten sonuç render'ı mı — yoksa hata/boş durum mu."""
    assert final_html, "backtest did not finish within the poll deadline"
    assert "Unknown run ID" not in final_html
    assert "✗ ERROR" not in final_html, f"the run errored out:\n{final_html[:1500]}"
    # A completed run renders backtest_result.html: PnL + a verdict badge
    # (the exact English labels this session translated from Turkish —
    # see web/templates/fragments/backtest_result.html).
    assert "verdict-badge" in final_html or "empty-state" in final_html
    if "verdict-badge" in final_html:
        for turkish_leftover in ("Güçlü", "Umut verici", "Elenmeli"):
            assert turkish_leftover not in final_html


# ---------------------------------------------------------------------------
# Ağsız zincir — VARSAYILAN koşumda ve CI'da çalışır
# ---------------------------------------------------------------------------
class TestBacktestChainOffline:
    """POST /backtest/run → progress poll → sonuç render'ı, ağ olmadan.

    Yalnızca tek bir şey sahte: bar kaynağı (`data.load_bybit_bars`). Route,
    oturum guard'ı, worker thread'i, sandbox alt süreci, Nautilus motoru,
    progress fragment'ı ve sonuç şablonu gerçek — yani zincirin kırıldığı yer
    (run_id ayrıştırma, poll fragment'ı, verdict-badge render'ı) burada da
    kırılır. Ortama bağlı değil: cache dosyası, canlı Bybit, gerçek katalog
    hiçbiri gerekmiyor.
    """

    def test_full_chain_offline(self, client, spec_id, monkeypatch):
        import data

        frame = _synthetic_bybit_frame()

        def _fake_load_bybit_bars(symbol, interval, category="linear", **kw):
            return frame

        monkeypatch.setattr(data, "load_bybit_bars", _fake_load_bybit_bars)

        resp = client.post(
            "/backtest/run",
            data={
                "spec_id": spec_id,
                "instrument_kind": "Bybit",
                "symbol": "BTCUSDT",
                "category": "linear",
                "interval": "60",
                "bybit_start": "",
                "bybit_end": "",
            },
        )
        assert resp.status_code == 200, resp.text
        run_id = _extract_run_id(resp.text)

        # Sandbox `force_subprocess=True` ile koşuyor: Windows'ta spawn + nautilus
        # importu tek başına birkaç saniye. 180s cömert ama CI'da askıda kalmaz.
        final_html = _poll_until_done(client, run_id, timeout_s=180)
        _assert_result_fragment(final_html)


# ---------------------------------------------------------------------------
# Canlı Bybit zinciri — gerçek ağ, `-m e2e` ile açıkça çalıştırılır
# ---------------------------------------------------------------------------
@pytest.mark.e2e
class TestBacktestRunProgressResultE2E:
    """Aynı zincir, ama gerçek Bybit verisiyle (ağ + dakikalar).

    Ağsız ikizinin yakalayamadığı tek şeyi kapsar: `load_bybit_bars`'ın
    gerçekten veri getirmesi ve o verinin şekli. Bu yüzden zayıflatılmadı,
    yalnızca katalog bağımlılığından kurtarıldı.
    """

    def test_full_chain_bybit_spec(self, client, spec_id):
        # First call establishes the run + returns the initial polling fragment.
        end = datetime.now(UTC).date()
        start = end - timedelta(days=30)
        resp = client.post(
            "/backtest/run",
            data={
                "spec_id": spec_id,
                "instrument_kind": "Bybit",
                "symbol": "BTCUSDT",
                "category": "linear",
                "interval": "60",
                "bybit_start": start.isoformat(),
                "bybit_end": end.isoformat(),
            },
        )
        assert resp.status_code == 200, resp.text
        run_id = _extract_run_id(resp.text)

        # A real run fetches live Bybit bars over the network — real headroom.
        final_html = _poll_until_done(client, run_id, timeout_s=150)
        _assert_result_fragment(final_html)
