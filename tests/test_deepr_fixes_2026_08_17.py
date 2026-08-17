"""DeepR 2026-08-17 raporunun doğrulanan bulguları.

Hepsi "kod çalışıyor ama söz verdiğini yapmıyor" sınıfından — hiçbiri istisna
atmıyordu, hiçbiri kırmızı test üretmiyordu. Bu yüzden her birinin testi
DAVRANIŞI değil VAADİ sınıyor:

1. ``NAU_STUDIO_DB`` gerçekten depoyu taşıyor mu (test izolasyonu bu söze
   dayanıyor — ``tests/browser/conftest.py``).
2. max-dd hedefinde varsayılan kapı geçilebilir mi (eşik pozitifti, metrik
   negatif; hiçbir gerçek sonuç geçemiyordu).
3. Geçersiz blok rolü reddediliyor mu (kaydedilip runtime'da sessizce yok
   sayılıyordu).
4. Tamsayı optimizer aralığı tek noktaya çöküyor / işaret bozuyor mu.

Wiki References
---------------
Bkz: [[strategy_studio]], [[review_raporu_uretildigi_anda_bayatlar]]
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Compose ekranındaki gerçek bir katalog bloğu — rol testinin 400'ü rolden
# gelmeli, "Unknown block"tan değil.
_BLOCK = "rsi_threshold"

# ---------------------------------------------------------------------------
# 1. NAU_STUDIO_DB — store kendi yolunu uydurmasın
# ---------------------------------------------------------------------------


def test_studio_db_env_var_actually_moves_the_store(tmp_path):
    """Ayrı SÜREÇ: değişken import anında okunuyor, monkeypatch geç kalırdı.

    Bu, ``tests/browser/conftest.py``'nin dayandığı sözün ta kendisi. Store
    kendi ``parents[1] / "studio.db"`` yolunu kurduğu sürece o süit gerçek
    repo kökündeki DB'ye yazıyordu ve kimse fark etmiyordu.
    """
    target = tmp_path / "moved.db"
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from strategy_studio.store import StrategyStore;"
            "print(StrategyStore().db_path)",
        ],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=os.environ | {"NAU_STUDIO_DB": str(target)},
        check=True,
    )
    assert out.stdout.strip() == str(target)


def test_studio_db_default_is_unchanged_when_env_is_unset(monkeypatch):
    """Varsayılan TAŞINMADI — taşımak mevcut kurulumların stratejilerini
    görünmez kılardı (``app_constants`` bu kararı zaten yazıyor)."""
    monkeypatch.delenv("NAU_STUDIO_DB", raising=False)
    from app_constants import studio_db_path
    from strategy_studio.store import StrategyStore

    assert StrategyStore().db_path == str(studio_db_path())
    assert studio_db_path().name == "studio.db"


# ---------------------------------------------------------------------------
# 2. max_dd hedefinde varsayılan kapı geçilebilir olmalı
# ---------------------------------------------------------------------------


def test_default_gate_for_max_dd_is_on_the_metrics_own_scale():
    from strategy_studio.deploy import default_gate_min

    assert default_gate_min("max_dd") < 0, (
        "max_dd_pct negatif üretiliyor; pozitif eşik hiçbir gerçek sonucun "
        "geçemeyeceği bir kapıdır"
    )
    assert default_gate_min("sharpe") == 0.8
    assert default_gate_min("bilinmeyen-hedef") == 0.8  # DSR'ye düşer


def test_a_healthy_max_dd_run_clears_the_default_gate():
    """Asıl regresyon: -8%'lik gayet iyi bir düşüş, eşik +0.8 iken
    ``-8.00 below required 0.80`` diye reddediliyordu."""
    from scripts.seed_studio import build_engine_fixture
    from strategy_studio.deploy import check_gate, default_gate_min

    defn = build_engine_fixture()
    defn.walkforward.objective = "max_dd"
    metrics = _metrics(max_dd_pct=-8.0)
    cfg = _cfg(gate_min=default_gate_min("max_dd"))

    # Motor GERÇEK: kapı 2026-08-17'den beri sayıya bakmadan önce sayının
    # nereden geldiğine bakıyor. Buranın konusu eşik, motor kontrolü değil.
    check_gate(defn, metrics, cfg, "nautilus")  # atmamalı


def test_a_catastrophic_max_dd_still_blocks():
    """Kapı gevşetilmedi, ÖLÇEĞİ düzeltildi: -%60 hâlâ reddedilmeli."""
    from scripts.seed_studio import build_engine_fixture
    from strategy_studio.deploy import (
        DeployBlocked,
        check_gate,
        default_gate_min,
    )

    defn = build_engine_fixture()
    defn.walkforward.objective = "max_dd"
    cfg = _cfg(gate_min=default_gate_min("max_dd"))

    with pytest.raises(DeployBlocked, match="MAX_DD"):
        check_gate(defn, _metrics(max_dd_pct=-60.0), cfg, "nautilus")


def _metrics(**over):
    from strategy_studio.backtest import BacktestMetrics

    base = dict(
        net_pnl_pct=12.0,
        sharpe=1.2,
        dsr=0.9,
        max_dd_pct=-8.0,
        trades=42,
        win_rate_pct=55.0,
        profit_factor=1.4,
    )
    base.update(over)
    return BacktestMetrics(**base)


def _cfg(*, gate_min: float):
    from strategy_studio.deploy import DeployConfig

    return DeployConfig(
        environment="paper",
        instruments="active",
        capital=10_000.0,
        kill_switch_daily_pct=None,
        gate_enabled=True,
        gate_min_objective=gate_min,
    )


# ---------------------------------------------------------------------------
# 3. Blok rolü whitelist'i
# ---------------------------------------------------------------------------


@pytest.fixture()
def compose_client():
    from fastapi.testclient import TestClient

    from server import app

    return TestClient(app)


def test_unknown_block_role_is_rejected(compose_client):
    """``BlockRole = Literal["entry", "exit"]`` — motor spec'i TAM olarak bu
    iki değere göre bölüyor. ``bogus`` rollü blok eskiden kaydediliyor, sonra
    sinyal hesabına hiç katılmıyordu: ekranda duran ölü bir blok."""
    r = compose_client.post("/strategy/drafts", data={"type": _BLOCK, "role": "bogus"})
    assert r.status_code == 400
    # Reddin SEBEBİ rol olmalı — geçerli bir blok tipi kullanılıyor, yoksa
    # test kendi "Unknown block" 400'ünü doğrulayıp yeşil kalırdı.
    assert "role" in r.text.lower() and "bogus" in r.text


@pytest.mark.parametrize("role", ["entry", "exit"])
def test_valid_block_roles_still_pass(compose_client, role):
    r = compose_client.post("/strategy/drafts", data={"type": _BLOCK, "role": role})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 4. Tamsayı optimizer aralığı
# ---------------------------------------------------------------------------


def _range_for(value):
    from strategy_studio.mutations import _default_range
    from strategy_studio.schema import Param

    return _default_range(Param(name="p", value=value))


def test_int_range_never_collapses_to_a_single_point():
    """``value=1`` için ±%50 aralık ``int()`` kırpmasıyla ``[1, 1]`` oluyordu —
    optimizer yalnız mevcut değeri deniyor, "sweep" hiçbir şey aramıyordu."""
    for v in (1, 2, 3, 5, 14, 200):
        r = _range_for(v)
        assert r.max > r.min, f"value={v} aralığı çöktü: [{r.min}, {r.max}]"
        assert r.max - r.min >= r.step, f"value={v} tek adıma bile yetmiyor"


def test_int_range_preserves_sign():
    """``int(-0.5) or 2`` üst ucu +2 yapıp aralığı negatiften pozitife
    taşıyordu — optimizer parametrenin işaretini kendiliğinden değiştiriyordu."""
    for v in (-1, -3, -10):
        r = _range_for(v)
        assert r.max < 0, f"value={v} aralığının üst ucu işaret değiştirdi: {r.max}"
        assert r.min < r.max < 0


def test_int_range_brackets_the_current_value():
    """Aralık mevcut değeri içermeli, yoksa sweep başlangıç noktasını hiç
    denemez."""
    for v in (2, 3, 10, 14, -5):
        r = _range_for(v)
        assert r.min <= v <= r.max, f"value={v} kendi aralığının dışında"


def test_float_params_are_untouched():
    r = _range_for(2.0)
    assert (r.min, r.max) == (1.0, 3.0)
    assert r.step != int(r.step)  # tamsayıya yuvarlanmadı
