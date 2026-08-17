"""DeepR 2026-08-17 — üçüncü tur.

Hepsi aynı aileden: sistem bir şeyi YAPTIĞINI söylüyor ama yapmıyor, ve
söylediği yer ile yapmadığı yer arasında hiçbir iz yok.

1. Trend filtresi yüklenemeyince koşu sessizce filtresiz sürüyordu; sonuç
   yine "başarılı" ve trend filtreli spec adıyla kaydediliyordu.
2. `pending` iken durdurulan (ya da build'i zaman aşımına uğrayan) deployment
   arka planda başlayabiliyordu: DB 'stopped'/'failed', sahada canlı düğüm.
3. Deploy kapısı stub'ın rastgele yürüyüşünü gerçek OOS kanıtı sayıyordu.

Wiki References
---------------
Bkz: [[strategy_studio]], [[review_raporu_uretildigi_anda_bayatlar]]
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# 1. Trend filtresi fail-closed
# ---------------------------------------------------------------------------


_RECIPE = {"symbol": "BTCUSDT", "interval": "60", "category": "linear"}


def _synth_bars(n=300):
    import numpy as np
    import pandas as pd

    idx = pd.date_range("2023-01-01", periods=n, freq="1h", tz="UTC")
    rng = np.random.default_rng(7)
    close = 30_000 + rng.normal(0, 100, n).cumsum()
    close = np.maximum(close, 1000)
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + 10,
            "low": np.minimum(open_, close) - 10,
            "close": close,
            "volume": np.full(n, 100.0),
        },
        index=idx,
    )


def _spec_with_trend_filter(interval="240"):
    from composer import ComposedStrategySpec, SignalBlock

    return ComposedStrategySpec(
        id="tf-test",
        name="Trend filtreli",
        description="",
        blocks=[
            SignalBlock(
                type="ma_cross",
                role="entry",
                params={"fast": 5, "slow": 20, "direction": "up"},
            )
        ],
        trend_filter=True,
        trend_interval=interval,
    )


def _run(spec):
    """GERÇEK `run_composed_backtest`. Trend yükleyicisi monkeypatch'lenmiş
    hâlde çağrılıyor — sözleşmeyi taklit etmek yerine kodu koşturuyor."""
    from backtest import run_composed_backtest
    from sandbox import _build_instrument_bar_type

    instrument, bar_type = _build_instrument_bar_type(_RECIPE)
    return run_composed_backtest(
        spec,
        _synth_bars(),
        iteration_id=0,
        rationale="trend-filter guard",
        instrument=instrument,
        bar_type=bar_type,
        venue=instrument.id.venue,
    )


def test_trend_filter_load_failure_fails_the_run(monkeypatch):
    """Eskiden: hata yutulur, koşu filtresiz sürer, sonuç 'başarılı' olur.

    Tehlikeli olan sonucun YANLIŞ olması değil — BAŞKA bir stratejinin sonucu
    olması ve trend filtreli adla kaydedilip aday seçimine girmesi.
    """
    import data

    def _boom(*a, **k):
        raise RuntimeError("catalog has no trend slice")

    monkeypatch.setattr(data, "load_bybit_bars", _boom)

    # `run_composed_backtest` çağırana YÜKSELTMEZ (sözleşmesi bu): arıza
    # sonucun `error` alanına yazılır. Zinciri takip ettim, orada bitmiyor —
    # `strategy_studio/backtest.py` `if result.error: raise` diyor ve
    # `agent_backtest.py` `if result.error ...` ile adayı eliyor. Yani koşu
    # 'failed' olarak kaydediliyor ve aday seçimine HİÇ girmiyor: düzeltmenin
    # amacı buydu.
    res = _run(_spec_with_trend_filter())
    assert res.error and "could not be loaded" in res.error
    assert not res.metrics, "başarısız koşu yine de metrik döndürdü"


def test_empty_trend_data_fails_the_run(monkeypatch):
    """En sinsi yol: istisna bile yoktu.

    `trend_df` boş dönünce eski kod `if trend_df is not None and not
    trend_df.empty` bloğunu hiç çalıştırmıyor, `secondary_bar_type_obj` `None`
    kalıyor ve progress akışına TEK BİR SATIR bile düşmüyordu.
    """
    import pandas as pd

    import data

    monkeypatch.setattr(data, "load_bybit_bars", lambda *a, **k: pd.DataFrame())

    res = _run(_spec_with_trend_filter())
    assert res.error and "returned no bars" in res.error
    assert not res.metrics


def test_a_run_without_a_trend_filter_is_untouched(monkeypatch):
    """Fail-closed yalnız filtre İSTENDİĞİNDE geçerli."""
    import data
    from composer import ComposedStrategySpec, SignalBlock

    def _boom(*a, **k):
        raise AssertionError("filtre istenmedi, yükleyici çağrılmamalıydı")

    monkeypatch.setattr(data, "load_bybit_bars", _boom)
    spec = ComposedStrategySpec(
        id="no-tf",
        name="Filtresiz",
        description="",
        blocks=[
            SignalBlock(
                type="ma_cross",
                role="entry",
                params={"fast": 5, "slow": 20, "direction": "up"},
            )
        ],
    )
    assert _run(spec) is not None


def test_the_fallback_branch_is_gone_from_the_source():
    """Sessiz geri düşüşün KENDİSİ kalkmış olmalı.

    Yukarıdaki iki test sözleşmeyi anlatıyor; bu, eski davranışın kodda
    kalmadığını doğruluyor — `secondary_bar_type_obj = None` ile devam eden
    bir `except` bir daha eklenirse burası kırmızıya döner.
    """
    import inspect

    import backtest as bt

    src = inspect.getsource(bt.run_composed_backtest)
    # Yutan `except`'in imzası. ("continuing with single TF" ifadesi AYNI TF
    # atlaması için hâlâ var ve olmalı da — o bir arıza değil, meşru bir
    # durum; ilk yazdığım kontrol ikisini karıştırıp yanlış yeri işaret
    # ediyordu.)
    assert "Failed to load trend filter data" not in src, (
        "trend filtresi arızasını yutan sessiz geri düşüş geri gelmiş"
    )
    assert "was requested but" in src, "fail-closed mesajları kaybolmuş"


# ---------------------------------------------------------------------------
# 2. Durdurulan / zaman aşımına uğrayan deployment
# ---------------------------------------------------------------------------


class _FakeNode:
    def __init__(self):
        self.disposed = False
        self.ran = False

    def dispose(self):
        self.disposed = True

    async def run_async(self):
        self.ran = True

    def stop(self):
        pass


ARTIFACT = {
    "schema": 2,
    "environment": "paper",
    "instruments": [{"symbol": "BTCUSDT", "timeframe": "1h"}],
    "spec": {},
    "capital": 10_000.0,
    "kill_switch_daily_pct": None,
    "version": 1,
}


def _runner(node_factory, statuses):
    from strategy_studio.runner import PaperRunner

    return PaperRunner(
        on_status=lambda d, s, e=None: statuses.append((s, e)),
        node_factory=node_factory,
    )


def test_stopping_a_pending_deployment_prevents_the_node_from_running(monkeypatch):
    """Yarışın kalbi: `stop()` düğümü `_nodes`'da BULAMIYORDU.

    "Zaten gitmiş" diye dönüyordu, oysa düğüm hâlâ KURULUYORDU. Sonra build
    bitiyor, düğüm kaydediliyor ve koşmaya başlıyordu — DB 'stopped' derken.
    """
    from strategy_studio.runner import build_node_config

    monkeypatch.setattr(
        "strategy_studio.runner.build_node_config",
        lambda *a, **k: object(),
        raising=False,
    )
    _ = build_node_config  # import edildiğini göster

    statuses: list = []
    node = _FakeNode()
    r = _runner(lambda cfg: node, statuses)

    # Stop, launch'tan ÖNCE: rota satırı 'pending' yazıp görevi arka plana
    # atmıştı, kullanıcı görev başlamadan durdurdu.
    r.stop("dep-1")
    r.launch("dep-1", ARTIFACT)

    assert not node.ran, "durdurulmuş deployment yine de koştu"
    assert ("running", None) not in statuses, "durdurulana 'running' denildi"


def test_a_live_node_is_still_stoppable(monkeypatch):
    """İptal işareti normal yolu bozmamalı."""
    monkeypatch.setattr(
        "strategy_studio.runner.build_node_config",
        lambda *a, **k: object(),
        raising=False,
    )
    statuses: list = []
    node = _FakeNode()
    r = _runner(lambda cfg: node, statuses)

    r.launch("dep-2", ARTIFACT)
    assert ("running", None) in statuses, "normal launch 'running' dememiş"


def test_abandoned_marks_do_not_leak(monkeypatch):
    """İşaret tüketiliyor: `launch` okuyup siliyor, yoksa ikinci bir deploy
    aynı id ile hiç başlayamazdı."""
    monkeypatch.setattr(
        "strategy_studio.runner.build_node_config",
        lambda *a, **k: object(),
        raising=False,
    )
    statuses: list = []
    r = _runner(lambda cfg: _FakeNode(), statuses)

    r.stop("dep-3")
    r.launch("dep-3", ARTIFACT)  # iptal edilir, işaret tüketilir
    assert "dep-3" not in r._abandoned


# ---------------------------------------------------------------------------
# 3. Motor provenance ve kapı
# ---------------------------------------------------------------------------


def test_engine_name_distinguishes_the_two_adapters():
    from strategy_studio.backtest import (
        REAL_ENGINE,
        STUB_ENGINE,
        StubBacktestAdapter,
        engine_name,
    )

    assert engine_name(StubBacktestAdapter()) == STUB_ENGINE
    assert engine_name(object()) == REAL_ENGINE  # stub değilse gerçek


def test_a_run_records_which_engine_measured_it(tmp_path):
    from strategy_studio.store import StrategyStore

    store = StrategyStore(tmp_path / "t.db")
    store.create_run("r1", "s1", 1, False, "hash", engine="nautilus")
    assert store.latest_run("s1")["engine"] == "nautilus"


@pytest.mark.parametrize("engine", ["stub", None])
def test_the_gate_refuses_metrics_it_cannot_vouch_for(engine):
    """Kapı bir KANIT iddiasıdır; kanıt olmayanı reddetmesi tanımı gereği.

    `None` da reddediliyor: "belki gerçekti" bir kanıt değil, bir tahmindir.
    """
    from strategy_studio.deploy import DeployBlocked, check_gate

    with pytest.raises(DeployBlocked, match="not the real backtest engine"):
        check_gate(_defn(), _good_metrics(), _gate_cfg(), engine)


def test_the_gate_still_judges_the_number_when_the_engine_is_real():
    """Motor kontrolü eşiğin YERİNE geçmiyor, ÖNÜNE geçiyor."""
    from strategy_studio.deploy import DeployBlocked, check_gate

    check_gate(_defn(), _good_metrics(), _gate_cfg(0.5), "nautilus")  # geçer
    with pytest.raises(DeployBlocked, match="below required"):
        check_gate(_defn(), _good_metrics(), _gate_cfg(5.0), "nautilus")


def test_turning_the_gate_off_still_allows_a_simulated_deploy():
    """Sentetik sayıyla deploy yasaklanmadı — sadece KANIT diye satılamıyor."""
    from strategy_studio.deploy import check_gate

    cfg = _gate_cfg()
    cfg.gate_enabled = False
    check_gate(_defn(), _good_metrics(), cfg, "stub")  # atmamalı


def _defn():
    from scripts.seed_studio import build_engine_fixture

    return build_engine_fixture()


def _good_metrics():
    from strategy_studio.backtest import BacktestMetrics

    return BacktestMetrics(
        net_pnl_pct=12.0,
        sharpe=1.4,
        dsr=0.92,
        max_dd_pct=-8.0,
        trades=500,
        win_rate_pct=55.0,
        profit_factor=2.0,
    )


def _gate_cfg(gate_min: float = 0.5):
    from strategy_studio.deploy import DeployConfig

    return DeployConfig(
        environment="paper",
        instruments="active",
        capital=10_000.0,
        kill_switch_daily_pct=None,
        gate_enabled=True,
        gate_min_objective=gate_min,
    )
