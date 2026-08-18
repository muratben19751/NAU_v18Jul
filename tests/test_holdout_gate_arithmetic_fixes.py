"""Mühürlü kapının dört düzeltmesi (44 ajanlı doğrulama turu, 2026-08-18).

`2db813b` mühürlü pencereyi sabit takvimden örneklemin oranına çevirdi ve
aritmetiği doğru yaptı — ama üç şey açık kaldı, biri de GERİ GİTTİ:

1. **Regresyon.** Peer penceresi (730 gün) serinin KENDİ sonuna göre
   kesiliyordu. Mühür 60 günken bu pencerenin ~%8'i mühürlünün içindeydi;
   mühür 1254 güne çıkınca **%100'ü** içine düştü — çok-sembol kapısı adayın
   görmesi yasak veriden karar verir hâle geldi.
2. **Birim çelişkisi.** Sıralama kapısı 20 girişi EĞİTİM penceresinde,
   mühürlü kapı aynı 20'yi onun 1/6'sı bir pencerede istiyordu: 5,7 kat daha
   sıkı. Sıralamayı geçen her aday mühürde aritmetik olarak ölçülemezdi.
3. **Yapısal olarak susan uyarı.** `holdout_feasibility` yalnız 20/n > 1/3
   iken konuşuyordu, yani n < 60 bar. Yeni pencere ~862 bar; uyarı bir daha
   asla ateşleyemezdi.
4. **Bayat ekran metni.** Form "son 60 gün mühürlenir" diyordu; karar anında
   gerçek pencere 1254 gündü (20,7 kat).

Wiki References
---------------
Bkz: [[nau_holdout_dogrulama_turu_2026_08_18]], [[auto_kapi_ve_geri_bildirim]],
[[multi_symbol_generalization]]
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _daily(n: int, start: str = "2003-09-10") -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="1D", tz="UTC")
    return pd.DataFrame(
        {"close": 1.0, "open": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0}, index=idx
    )


class _Spec:
    id = "x"
    name = "x"

    def to_dict(self):
        return {"id": "x", "name": "x", "blocks": []}


# ---------------------------------------------------------------------------
# 1) Peer penceresi mührün peşinden gider
# ---------------------------------------------------------------------------


def test_the_peer_window_ends_at_the_anchor_not_at_the_series_end():
    from backtest_robustness import _clip_peer_window

    df = _daily(3000)
    anchor = df.index[1500]
    out = _clip_peer_window(df, 730, anchor)
    assert out.index[-1] <= anchor, "kesim mühürlü kuyruğa taşıyor"
    assert out.index[0] >= anchor - pd.Timedelta(days=730)


def test_the_measured_regression_is_closed():
    """Ölçülen vaka: 22 yıllık seride mühür 730 günlük peer penceresini yutuyordu."""
    from backtest_robustness import _clip_peer_window
    from web.routes.agent_backtest import _split_holdout

    df = _daily(22 * 365)
    trimmed, hold = _split_holdout(df)
    seal_start = hold.index[0]

    # ESKİ davranış (çapa yok): pencere serinin kendi sonuna göre kesiliyor.
    old = _clip_peer_window(df, 730, None)
    leaked_old = (old.index >= seal_start).mean()
    assert leaked_old == pytest.approx(1.0), (
        f"regresyonun kendisi yeniden üretilemedi (sızıntı %{100 * leaked_old:.0f})"
    )

    # YENİ davranış: çapa eğitim çerçevesinin son barı.
    new = _clip_peer_window(df, 730, trimmed.index[-1])
    assert (new.index >= seal_start).sum() == 0, "peer penceresi hâlâ mühre giriyor"
    assert len(new) > 300, "pencere mühür yüzünden boşaldı"


def test_the_anchor_survives_a_naive_timestamp():
    from backtest_robustness import _as_utc_datetime

    assert _as_utc_datetime(None) is None
    aware = _as_utc_datetime(pd.Timestamp("2020-01-01", tz="UTC"))
    naive = _as_utc_datetime(pd.Timestamp("2020-01-01"))
    assert aware == naive, "naive damga UTC sayılmadı — pencere kayar"


def test_both_branches_share_one_clipper():
    """Kesim tek yerde: iki kopya iki farklı pencere demekti.

    Paralel worker kendi kopyasını taşıyordu ve `end_ms`'i hiç okumuyordu;
    sıralı yol düzeltilseydi bile paralel yol mührü delmeye devam ederdi.
    Bu yüzden ÇAĞRI YERİ sayılıyor, davranış değil: elle kopyalanan bir kesim
    ifadesi geri gelirse test kırılır.
    """
    pattern = re.compile(r"index\[-1\]\s*-\s*timedelta\(days=days\)")
    hits = {}
    for rel in ("backtest_robustness.py", "parallel_exec.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        hits[rel] = len(pattern.findall(src))
    assert hits == {"backtest_robustness.py": 1, "parallel_exec.py": 0}, (
        f"kesim ifadesi kopyalanmış: {hits}"
    )


def test_run_multi_symbol_pins_the_window_for_the_pool():
    """Paralel dalda pencere EBEVEYNDE sabitlenir ve external dalda da gönderilir."""
    from backtest_robustness import run_multi_symbol

    captured: dict = {}

    def _run_many(units, **kw):
        captured["units"] = units
        return {u["key"]: {"error": "no data"} for u in units}

    anchor = pd.Timestamp("2021-06-30", tz="UTC")
    run_multi_symbol(
        _Spec(),
        primary_symbol="QQQC",
        symbols=["SPY.ARCA", "IWM.ARCA"],
        interval="1-DAY",
        days=730,
        run_many=_run_many,
        source="external",
        end_anchor=anchor,
    )
    units = captured["units"]
    assert units, "birim üretilmedi"
    for u in units:
        assert u["end_ms"] == int(anchor.timestamp() * 1000), (
            "external birimler pencere sonunu taşımıyor — worker mührü göremez"
        )
        assert u["start_ms"] == int(
            (anchor - pd.Timedelta(days=730)).timestamp() * 1000
        )


def test_the_suite_derives_the_anchor_from_the_training_frame():
    """Çapa SABİT BİR TARİH değil, verilen çerçevenin son barı olmalı.

    Sabit yazılsaydı bir sonraki mühür değişikliğinde aynı sızıntı geri
    gelirdi; türetilen çapa kuralı takip eder.
    """
    import auto.robustness as ar

    src = inspect.getsource(ar.run_full_robustness)
    assert "end_anchor=" in src, "peer penceresi mühre bağlanmamış"
    m = re.search(r"end_anchor=\(?([^,\n]+)", src)
    assert m and "bars_df" in m.group(1), (
        f"çapa eğitim çerçevesinden türetilmiyor: {m.group(1) if m else None!r}"
    )


# ---------------------------------------------------------------------------
# 2) Eşik sıralamanın ORANI
# ---------------------------------------------------------------------------


def test_the_sealed_threshold_scales_with_the_window():
    from web.routes.agent_backtest import (
        HOLDOUT_MIN_TRADES,
        HOLDOUT_MIN_TRADES_FLOOR,
        holdout_min_trades,
    )

    req = holdout_min_trades(5159, 862)  # ölçülen koşu (QQQC, 1-DAY)
    assert HOLDOUT_MIN_TRADES_FLOOR <= req < HOLDOUT_MIN_TRADES
    assert req == HOLDOUT_MIN_TRADES_FLOOR


def test_the_measured_winner_is_no_longer_doomed_by_arithmetic():
    """Kazanan 52 işlem/5.159 bar hızıyla mühürde ~8,7 giriş üretir.

    Eski eşik 20'ydi: aday daha koşmadan elenmişti. Yeni eşik bu hızın altında
    olmalı, yoksa düzeltme aritmetiği değil yalnız cümleyi değiştirmiş olur.
    """
    from web.routes.agent_backtest import holdout_min_trades

    train_bars, train_trades, sealed_bars = 5159, 52, 862
    expected = train_trades / train_bars * sealed_bars
    req = holdout_min_trades(train_bars, sealed_bars)
    assert expected > req, f"beklenen {expected:.1f} giriş, eşik {req} — hâlâ ulaşılmaz"
    assert expected < 20, "vakanın kendisi değişmiş — 20 ulaşılabilir görünüyor"


def test_the_scaled_threshold_never_exceeds_the_old_constant():
    """Bu bir gevşetme; SIKILAŞTIRMA olmadığı da tutulmalı."""
    from web.routes.agent_backtest import HOLDOUT_MIN_TRADES, holdout_min_trades

    for train, sealed in ((5159, 862), (1000, 60), (300, 42), (200, 5000)):
        assert holdout_min_trades(train, sealed) <= HOLDOUT_MIN_TRADES


def test_the_threshold_never_falls_under_the_decision_floor():
    """Sharpe iki gözlem ister; oran sıfıra yaklaşsa da taban duruyor."""
    from app_constants import MIN_DECISION_TRADES
    from web.routes.agent_backtest import holdout_min_trades

    assert holdout_min_trades(100_000, 10) == MIN_DECISION_TRADES


def test_unknown_scale_falls_back_to_the_constant():
    from web.routes.agent_backtest import HOLDOUT_MIN_TRADES, holdout_min_trades

    for train, sealed in ((None, None), (0, 862), (5159, 0), (None, 862)):
        assert holdout_min_trades(train, sealed) == HOLDOUT_MIN_TRADES


def test_an_explicit_env_pin_disables_the_scaling():
    """Operatör sabitlediyse oran devreye girmemeli — 'sabit' sözü tutulmalı."""
    import importlib
    import os

    import web.routes.agent_backtest as ab

    os.environ["AGENT_HOLDOUT_MIN_TRADES"] = "7"
    try:
        mod = importlib.reload(ab)
        assert mod._HOLDOUT_MIN_TRADES_PINNED
        assert mod.holdout_min_trades(5159, 862) == 7
    finally:
        os.environ.pop("AGENT_HOLDOUT_MIN_TRADES", None)
        importlib.reload(ab)


def test_the_verdict_uses_the_scaled_threshold():
    from web.routes.agent_backtest import _holdout_promotion_verdict

    ok, why = _holdout_promotion_verdict(6, 0.1, 1.0, 0.1, min_trades=5)
    assert ok, f"ölçeklenmiş eşik kararın içine girmemiş: {why}"
    ok, why = _holdout_promotion_verdict(4, 0.1, 1.0, 0.1, min_trades=5)
    assert not ok and "need 5" in why


def test_the_gate_reads_one_threshold_everywhere():
    """`measured`, karar ve operatöre yazılan cümle AYNI sayıdan beslenmeli."""
    import web.routes.agent_backtest as ab

    src = inspect.getsource(ab._run_promotion_gate)
    assert '"measured": _n >= _req_trades' in src
    assert "min_trades=_req_trades" in src
    assert "if _n < HOLDOUT_MIN_TRADES" not in src, "sabit eşik kapıda kalmış"


# ---------------------------------------------------------------------------
# 3) Uyarı adayın ÖLÇÜLEN hızına bağlı
# ---------------------------------------------------------------------------


def test_the_old_warning_was_structurally_silent():
    """Vakayı belgele: 862 barlık pencerede genel eşik asla ateşleyemez."""
    from web.routes.agent_backtest import (
        HOLDOUT_PLAUSIBLE_ENTRY_RATE,
        holdout_feasibility,
    )

    rate, warn = holdout_feasibility(862)
    assert warn is None and rate < HOLDOUT_PLAUSIBLE_ENTRY_RATE


def test_the_candidate_rate_makes_the_same_window_speak():
    """Aynı 862 bar, kazananın kendi hızıyla: uyarı ateşlemeli."""
    from web.routes.agent_backtest import holdout_feasibility

    slow = 2.3 / 252  # yılda 2,3 işlem, günlük bar
    _, warn = holdout_feasibility(862, min_trades=20, candidate_rate=slow)
    assert warn and "ÖLÇÜLEMEDİ" in warn
    assert "~7.9 entries" in warn, warn


def test_a_fast_enough_candidate_is_silent():
    from web.routes.agent_backtest import holdout_feasibility

    _, warn = holdout_feasibility(862, min_trades=5, candidate_rate=52 / 5159)
    assert warn is None


def test_a_candidate_with_no_trades_does_not_divide_by_zero():
    from web.routes.agent_backtest import holdout_feasibility

    _, warn = holdout_feasibility(862, min_trades=5, candidate_rate=0.0)
    assert warn and "no entries at all" in warn


def test_the_forecast_is_printed_before_the_holdout_runs():
    """Bilgi zincirin SONUNDA değil, mühürlü koşudan önce verilmeli."""
    import web.routes.agent_backtest as ab

    src = inspect.getsource(ab._run_promotion_gate)
    fore = src.index("Sealed OOS forecast")
    run = src.index("run_backtest_guarded(")
    assert fore < run, "tahmin mühürlü koşudan sonra basılıyor"


# ---------------------------------------------------------------------------
# 4) Ekran ve kayıt gerçek kuralı söylüyor
# ---------------------------------------------------------------------------


def test_the_form_no_longer_promises_sixty_days():
    html = (ROOT / "web/templates/agent_backtest.html").read_text(encoding="utf-8")
    assert "the last 60 days are sealed off" not in html
    assert "15% of the sample" in html


def test_the_run_config_marks_the_constants_as_bounds():
    """Kayıt "60 gün / 20 işlem" derken bunların SINIR olduğunu da söylemeli."""
    from web.routes.agent_backtest import _effective_run_config

    cfg = _effective_run_config()
    assert cfg["holdout_days_is_floor"] is True
    assert cfg["holdout_min_trades_is_ceiling"] is True
    assert cfg["holdout_min_trades_floor"] < cfg["holdout_min_trades"]
    assert cfg["holdout_min_trades_ranking_reference"] == cfg["min_trades"]
    # Eski anahtarlar duruyor: denetim defterini okuyan testler ve kayıtlar var.
    assert cfg["holdout_days"] and cfg["holdout_min_trades"]


def test_the_record_carries_the_threshold_it_was_judged_by():
    """Sabit varsayan bir okuyucu, ölçeklenmiş kararı yanlış yorumlar."""
    import web.routes.agent_backtest as ab

    src = inspect.getsource(ab._run_promotion_gate)
    for key in ('"min_trades_required"', '"train_bars"', '"train_trades"'):
        assert key in src, f"{key} artefakta yazılmıyor"
