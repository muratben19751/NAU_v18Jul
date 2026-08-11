"""Dependency-free shared constants — every module can read it without circular imports.

- ``STARTING_CASH``: the shared starting cash for backtest.py and composer.py
  (kept here so it comes from a single source; a duplicated constant could silently diverge).
- ``NO_WINDOW_FLAGS``: the ``creationflags`` value that prevents subprocesses on
  Windows from opening a console window.
- ``stamp_buy_hold_benchmark``/``benchmark_and_excess``: the buy-and-hold
  benchmark/excess-return formula, shared so backtest_robustness.py,
  parallel_exec.py, and web/routes/agent_backtest.py can't independently
  re-derive it into three (or four) silently-drifting copies.
- ``env_float``/``env_int``: env-override parsing (DeepR 2026-08-09 [ORTA]),
  character-for-character identical in backtest_robustness.py,
  parallel_exec.py, and wfo_optimizer.py before this — a bugfix to one would
  silently not reach the other two.
"""

from __future__ import annotations

import math
import os
import subprocess

STARTING_CASH = 10_000.0


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# On Windows, when a CONSOLE application (claude CLI, bash/gunzip/awk) is launched,
# a terminal window opens and closes on every call — even if the server runs
# consoleless via pythonw, because a consoleless parent creates a NEW window for a
# console-bearing child. CREATE_NO_WINDOW never creates the console; `startupinfo`/`windowsHide`
# is ignored by Windows Terminal on this machine, so this is the only reliable way.
# On POSIX it MUST be 0 — subprocess rejects non-zero creationflags there.
NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def benchmark_and_excess(
    first: float, last: float, pnl_pct: float
) -> tuple[float, float] | None:
    """Core buy-and-hold benchmark/excess-return formula. None if not computable."""
    if not math.isfinite(first) or not math.isfinite(last) or first <= 0:
        return None
    benchmark = last / first - 1.0
    return benchmark, float(pnl_pct) - benchmark


# Buy&hold'un iki bacaklı işlem maliyeti (giriş + çıkış), hesap yüzdesi olarak.
# Sıfır değil çünkü "aynı maliyet tabanı" iddiası ancak iki taraf da maliyet
# ödediğinde doğrudur; küçük ama dürüst. IBKR Pro sabit tarifesinde $10k'lık tek
# bir alım-satım çifti ~2 × $1 minimum = %0,02.
BENCHMARK_ROUND_TRIP_COST_FRACTION = env_float("NAU_BENCHMARK_RT_COST", 0.0002)

# Fiyat serisi temettü ayarlı DEĞİLSE buy&hold getirisi eksik hesaplanır: QQQ'da
# ~%0,55/yıl, 23 yılda bileşik olarak ~%13. Varsayılan 0 — bilinmeyen bir veriyi
# uydurmak, eksik saymaktan daha kötü. Enstrüman biliniyorsa operatör ayarlar.
BENCHMARK_DIVIDEND_YIELD_ANNUAL = env_float("NAU_BENCHMARK_DIV_YIELD", 0.0)

_SECONDS_PER_YEAR = 365.25 * 24 * 3600


def annualized_return(total_return: float, years: float) -> float | None:
    """Kümülatif getiriyi CAGR'a çevir. Hesaplanamıyorsa None.

    Kapı eşiğinin veri penceresinden BAĞIMSIZ olması bu dönüşüme bağlı:
    kümülatif fark 23 yıllık bir seride %2093, 1 yıllıkta %20 çıkar ve aynı
    eşik iki koşuda bambaşka bir sertlik demeye başlar (ölçüm 2026-08-10:
    12 adayın 12'si kümülatif kapıda elendi, 9'u kârlı olmasına rağmen).
    """
    if not math.isfinite(total_return) or not math.isfinite(years) or years <= 0:
        return None
    growth = 1.0 + total_return
    if growth <= 0:
        # Toplam kayıp: CAGR tanımsız (negatif tabanın kesirli kuvveti).
        return -1.0
    return growth ** (1.0 / years) - 1.0


def window_years(bars) -> float | None:
    """Bar penceresinin takvim uzunluğu (yıl). İndeks zaman damgalı değilse None."""
    try:
        idx = bars.index
        span = (idx[-1] - idx[0]).total_seconds()
    except (AttributeError, TypeError, IndexError):
        return None
    if not math.isfinite(span) or span <= 0:
        return None
    return span / _SECONDS_PER_YEAR


def max_drawdown_fraction(closes) -> float | None:
    """Buy&hold'un maksimum düşüşü (negatif kesir; -0.53 = %53 düşüş).

    Risk-ayarlı karşılaştırmanın benchmark bacağı: stratejinin Calmar'ını
    "buy&hold ne kadar acı çektirdi" ile aynı ölçekte karşılaştırabilmek için.
    """
    peak = None
    worst = 0.0
    for value in closes:
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v) or v <= 0:
            continue
        peak = v if peak is None or v > peak else peak
        if peak:
            worst = min(worst, v / peak - 1.0)
    return worst if peak is not None else None


def stamp_buy_hold_benchmark(
    metrics: dict | None, bars, *, label: str | None = None
) -> None:
    """Attach buy-and-hold benchmark/excess-return fields to ``metrics``, in place.

    ``bars`` is a DataFrame with a "close" column spanning the window being
    scored. A missing/degenerate window (fewer than 2 rows, non-finite or
    non-positive first close) leaves ``metrics`` untouched rather than
    stamping a misleading value.
    """
    if not metrics or bars is None or len(bars) < 2 or "close" not in bars:
        return
    try:
        first = float(bars["close"].iloc[0])
        last = float(bars["close"].iloc[-1])
        pnl_pct = metrics.get("pnl_pct")
        if pnl_pct is None:
            pnl_pct = float(metrics.get("pnl") or 0.0) / STARTING_CASH
        result = benchmark_and_excess(first, last, float(pnl_pct))
    except (TypeError, ValueError, KeyError, IndexError):
        return
    if result is None:
        return
    benchmark, excess = result
    metrics["benchmark_return_fraction"] = benchmark
    metrics["excess_return_fraction"] = excess
    # Legacy aliases keep archived viewers and old callers readable.
    metrics["benchmark_return_pct"] = benchmark
    metrics["excess_pnl_pct"] = excess
    # The strategy return is net of its simulated costs, while this simple
    # close-to-close reference has no trading costs — keep that contract
    # explicit so downstream ranking/reporting cannot label it net alpha.
    metrics["benchmark_cost_basis"] = "gross_buy_and_hold_no_costs"
    metrics["strategy_return_cost_basis"] = "net_simulated_costs"
    if label is not None:
        metrics["benchmark"] = label
    _stamp_annualized_comparison(metrics, bars, benchmark, float(pnl_pct))


def _stamp_annualized_comparison(
    metrics: dict, bars, benchmark_gross: float, pnl_pct: float
) -> None:
    """Pencere-bağımsız, aynı maliyet tabanlı karşılaştırma alanlarını ekle.

    Kümülatif fark (`excess_return_fraction`) geriye uyumluluk için duruyor ama
    KARAR ölçütü olamaz: büyüklüğü tamamen veri penceresinin uzunluğuna bağlı ve
    iki bacağı farklı maliyet tabanında (brüt benchmark, net strateji). Buradaki
    alanlar o iki kusuru da kapatır — yıllıklandırılmış ve iki taraf da net.

    Temettü: fiyat serisi temettü ayarlı değilse buy&hold gerçekte daha yüksek
    getirir; varsayılan 0 ile bu FARK GÖRMEZDEN GELİNMEZ, sadece sayılmaz —
    `benchmark_dividend_yield_annual` alanı kaydedildiği için okuyan kişi
    karşılaştırmanın hangi varsayımla yapıldığını bilir.
    """
    years = window_years(bars)
    if years is None:
        return
    div = BENCHMARK_DIVIDEND_YIELD_ANNUAL
    bench_cagr = annualized_return(
        benchmark_gross - BENCHMARK_ROUND_TRIP_COST_FRACTION, years
    )
    strat_cagr = annualized_return(pnl_pct, years)
    if bench_cagr is None or strat_cagr is None:
        return
    bench_cagr += div
    metrics["window_years"] = years
    metrics["benchmark_cagr"] = bench_cagr
    metrics["strategy_cagr"] = strat_cagr
    metrics["annualized_alpha"] = strat_cagr - bench_cagr
    metrics["benchmark_dividend_yield_annual"] = div
    metrics["benchmark_net_cost_basis"] = "round_trip_cost_and_optional_dividends"
    try:
        bench_dd = max_drawdown_fraction(bars["close"])
    except (KeyError, TypeError):
        bench_dd = None
    if bench_dd is None:
        return
    metrics["benchmark_max_dd"] = bench_dd
    metrics["benchmark_calmar"] = bench_cagr / max(abs(bench_dd), 0.01)
    strat_dd = metrics.get("max_dd")
    try:
        strat_dd = float(strat_dd)
    except (TypeError, ValueError):
        return
    if math.isfinite(strat_dd):
        metrics["strategy_calmar"] = strat_cagr / max(abs(strat_dd), 0.01)
