"""Dependency-free shared constants — every module can read it without circular imports.

- ``STARTING_CASH``: the shared starting cash for backtest.py and composer.py
  (kept here so it comes from a single source; a duplicated constant could silently diverge).
- ``NO_WINDOW_FLAGS``: the ``creationflags`` value that prevents subprocesses on
  Windows from opening a console window.
- ``stamp_buy_hold_benchmark``/``benchmark_and_excess``: the buy-and-hold
  benchmark/excess-return formula, shared so backtest_robustness.py,
  parallel_exec.py, and web/routes/agent_backtest.py can't independently
  re-derive it into three (or four) silently-drifting copies.
- ``benchmark_gate_mode``/``benchmark_rejection``: kabul kapısının TEK kuralı.
  Ana kapı (web/routes/agent_backtest.py) ile çok-sembol kapısı
  (backtest_robustness.py) aynı felsefeyi iki ayrı kopyada uyguluyordu ve
  kopyalar ıraksadı: ana kapı 2026-08-15'te risk-ayarlıya çekilirken çok-sembol
  kapısı terk edilen mutlak kuralı uygulamaya devam etti, sonra hizalandığında
  bu kez GERİ DÜŞME basamakları ayrı kaldı (biri yıllık alfaya, diğeri kümülatif
  farka düşüyordu). Bağımlılık yönü paylaşımı buraya zorluyor: route modülü
  robustness'ı içeri alıyor, ters yön döngü olurdu.
- ``env_float``/``env_int``: env-override parsing (DeepR 2026-08-09 [ORTA]),
  character-for-character identical in backtest_robustness.py,
  parallel_exec.py, and wfo_optimizer.py before this — a bugfix to one would
  silently not reach the other two.
- ``DATA_DIR``: kalıcı depolama kökü. Sekiz modül bunu birbirinden habersiz
  ``Path.home() / ".cache" / "nautilus_web_app"`` diye kendi içinde kuruyordu
  ve hiçbirinde ortam değişkeniyle yeniden yönlendirme yoktu (DeepR 2026-08-11
  [ORTA]); veri dizinini taşımak ya da ikinci bir instance koşturmak için sekiz
  dosyayı ayrı ayrı yamamak gerekiyordu.

Buraya bir şey eklerken kural: yalnız stdlib'e dayan, hiçbir uygulama modülünü
içeri alma. Modülün tek değeri döngü riski olmadan HER yerden okunabilmesi.

Wiki References
---------------
See: [[multi_symbol_generalization]] (kapının iki kez ıraksama tarihi),
[[auto_kapi_ve_geri_bildirim]] (kapının felsefesi), [[webapp_module_map]]
"""

from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path

STARTING_CASH = 10_000.0


def env_float(
    name: str, default: float, *, lo: float | None = None, hi: float | None = None
) -> float:
    """Ortam değişkenini float'a çevir; bozuk/boş değerde ``default``.

    ``lo``/``hi`` verilirse sonuç o aralığa kırpılır — çağıranların ayrı ayrı
    yazdığı ``max(0.01, ...)`` sarmalayıcılarının tek yerdeki karşılığı.
    """
    try:
        v = float(os.environ.get(name, "") or default)
    except ValueError:
        v = float(default)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def env_int(
    name: str, default: int, *, lo: int | None = None, hi: int | None = None
) -> int:
    """Ortam değişkenini int'e çevir; bozuk/boş değerde ``default`` (+ kırpma)."""
    try:
        v = int(os.environ.get(name, "") or default)
    except ValueError:
        v = int(default)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


# ── Kalıcı depolama kökü ─────────────────────────────────────────────────────
# Varsayılan BİLEREK değişmedi: mevcut kurulumların ~/.cache altındaki katalogu,
# token defteri ve oturum logları yerinde kalsın. Değişen tek şey, artık tek bir
# yerden geliyor ve NAU_DATA_DIR ile yeniden yönlendirilebiliyor olması.
#
# Import anında çözülür (eski davranışla aynı): modüller bunu kendi
# modül-global'lerine bağlıyor ve testler o bağları monkeypatch ediyor.
def default_data_dir() -> Path:
    return Path.home() / ".cache" / "nautilus_web_app"


def data_dir() -> Path:
    """Kalıcı veri kökü — ``NAU_DATA_DIR`` ile taşınabilir."""
    raw = (os.environ.get("NAU_DATA_DIR") or "").strip()
    return Path(raw).expanduser() if raw else default_data_dir()


DATA_DIR = data_dir()


# Strategy Studio'nun SQLite'ı tek istisnaydı: çalışma zamanı verisini KAYNAK
# AĞACINA (repo kökündeki studio.db) yazıyor. Varsayılan burada da korunuyor —
# taşımak, mevcut kurulumların kayıtlı stratejilerini görünmez kılardı — ama
# artık NAU_STUDIO_DB ile yönlendirilebilir ve konum tek bir yerde yazılı.
def studio_db_path() -> Path:
    raw = (os.environ.get("NAU_STUDIO_DB") or "").strip()
    return (
        Path(raw).expanduser() if raw else Path(__file__).resolve().parent / "studio.db"
    )


STUDIO_DB_PATH = studio_db_path()


# Bir tarih aralığının kabul edilebilir azami genişliği (gün).
#
# DeepR 2026-08-17 [YÜKSEK]: `0001-01-01`–`9999-12-31` biçim ve sıra
# kontrolünden geçiyordu (ikisi de tek tek geçerli, sıraları da doğru), sonra
# yükleyici gün gün ilerleyen bir liste kuruyordu: 3.652.059 `date` nesnesi,
# ardından `date.max + 1 gün` adımında `OverflowError`. Yani istek ne veri
# döndürüyordu ne de temiz bir hata — önce RAM/CPU yiyor, sonra 500 oluyordu.
#
# NEDEN GÜN CİNSİNDEN BİR TAVAN, NEDEN TABAN TARİH DEĞİL: kaynaklar farklı
# tarihlerde başlıyor (Bybit 2019, US index'ler 1960'lar) ve her birine ayrı
# taban koymak üç yerde ıraksayan üç kural demekti. Genişlik tavanı hepsine
# aynı cümleyi söylüyor.
#
# 100 YIL: hiçbir gerçek isteği reddetmeyecek kadar geniş (en uzun index
# geçmişi ~66 yıl), patolojik olanı kesecek kadar dar. 36.525 `date` nesnesi
# önemsiz; 3,6 milyon değil.
MAX_DATE_RANGE_DAYS = 36_525


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
        # RET PAYI (2026-08-17). Kapının ölçüsü Calmar üstünlüğü ve reddi tek bir
        # etiket: `worse_risk_adjusted`. O etiket %2 kaçıranla 60 kat kaçıranı
        # aynı satıra koyuyordu, yani "eşiği biraz gevşetsem ne olurdu" sorusu
        # ancak elle, defteri yeniden oynatarak cevaplanabiliyordu (ölçüm
        # 755b7880 tur 1: en yakın aday %2,3 kaçırdı, medyan aday barın YARISINDA
        # kaldı — ikisi de aynı gerekçeyle elendi).
        #
        # Oran seçildi, fark değil: Calmar'ın kendisi bir orandır ve barın
        # büyüklüğü pencereye göre değişir, dolayısıyla mutlak fark
        # karşılaştırılabilir bir sayı değil. 1.0 = tam eşikte, 0.5 = barın
        # yarısı, >1.0 = geçti. Kapı KARARINI bu alan üzerinden vermiyor —
        # tek karar kuralı `benchmark_rejection`'da kalıyor; bu yalnız teşhis.
        if (
            math.isfinite(bench_calmar := metrics["benchmark_calmar"])
            and bench_calmar > 0
        ):
            metrics["calmar_ratio_vs_benchmark"] = (
                metrics["strategy_calmar"] / bench_calmar
            )


# ---------------------------------------------------------------------------
# "Yeterince işlem" — bir sonucun karara girmeye hak kazanması için eşik
# ---------------------------------------------------------------------------
#
# Aynı soruya iki farklı cevap vardı: bir WFO penceresi 3 işlemle sayılıyordu,
# aynı anda 4 işlemli bir peer sayılmıyordu (`MIN_PEER_TRADES = 5`). Oysa soru
# tek: "bu kadar işlemden bir sonuç çıkar mı?" Farklı sayılar farklı gerekçe
# ister; gerekçesi olmayan fark yalnızca iki dosyanın birbirinden habersiz
# yazılmış olmasıdır.
#
# 5 seçildi çünkü daha gevşek olan taraf hizalandı, gevşek olan taraf değil.
# Bedeli ÖLÇÜLDÜ (2026-08-17, diskteki AUTO artefaktlarındaki 178 WFO
# penceresi): pencere başına işlem dağılımı {0: 70, 1: 88, 2: 20} — 3-4 işlem
# bandında TEK pencere yok, yani eşiği 3'ten 5'e çekmek hiçbir pencereyi
# elemiyor. (Aynı ölçüm ayrı bir şeyi de gösteriyor: bu koşularda hiçbir WFO
# penceresi 3 işleme bile ulaşmamış, yani WFO fiilen hiç konuşmuyor. Bu pencere
# boyutlandırmasının sorunu, eşiğin değil.)
MIN_DECISION_TRADES = 5


# ---------------------------------------------------------------------------
# Kabul kapısı — yukarıdaki alanları OKUYAN tek kural (bkz. modül docstring'i)
# ---------------------------------------------------------------------------


def benchmark_gate_mode() -> str:
    """Kapının ölçüsü: ``risk_adjusted`` (varsayılan) | ``absolute``.

    Kullanıcı kararı (2026-08-15).

    * ``absolute`` — yıllık alfa POZİTİF olmalı (buy&hold'u mutlak getiride geç)
      VE Calmar'ı da geç. Eski davranış.
    * ``risk_adjusted`` — ASIL ölçü Calmar üstünlüğü; alfanın pozitif olması ŞART
      DEĞİL, ama strateji para kazanıyor olmalı (CAGR > 0).

    Gerekçe ölçümden: koşu 1fa9870e'de en iyi aday (LRC Dip DMI ATR OBV, 1-HOUR)
    784 işlemde +%442 yaptı, düşüşü −%26'da tuttu ve Calmar'da buy&hold'u GEÇTİ
    (0,292 vs 0,269) — ama yıllık alfası −%6,8 olduğu için elendi. QQQ 22,7 yılda
    yılda %14,5 yapmış; long-only bir strateji piyasadan zaman zaman çıktığı için
    mutlak getiride kaybeder. Pencereyi kısaltmak da kurtarmıyor: QQQ'nun HER
    penceresinde CAGR %14-24 arası (3 yılda %24,2 ile daha da zor).

    Takas açık: bu mod "daha az getiri + çok daha az düşüş" stratejilerini
    kataloğa alır. Sermayeyi korumak öncelikse doğru; mutlak getiri
    kovalanıyorsa ``absolute`` moduna dönülmeli.

    Değer ÇAĞRI ANINDA okunur, import anında değil. İki kapı iki ayrı modülde
    yaşıyor; import-anı bir sabit, birini yeniden yükleyip diğerini yüklememek
    gibi sessiz bir ıraksama yüzeyi açardı (testlerin ``importlib.reload``
    numarasına ihtiyacı da bu yüzden kalktı).
    """
    return os.environ.get("AGENT_BENCHMARK_GATE", "risk_adjusted").strip().lower()


def benchmark_rejection(m: dict, legacy_excess: float | None) -> str | None:
    """Benchmark bacağı: yıllık alfa + risk-ayarlı üstünlük (yoksa eski kural).

    Ret gerekçesini döndürür; ``None`` = bu bacaktan geçti.

    Eski kural KÜMÜLATİF farktı ve bu bir araştırma kararı değil, veri
    penceresinin yan etkisiydi: QQQC'nin 23 yıllık serisinde buy&hold %2093
    yaptığı için eşik fiilen aşılamaz oldu. Ölçüm (koşu 44cb54e2, 2026-08-10):
    12 adayın 9'u kârlı, en iyisi Sharpe 0,79 / +196.880 USD — ve HİÇBİRİ
    geçemedi. Aynı arama 1 yıllık bir katalogda kolayca kazanan bulurdu; yani
    kapı stratejiyi değil, veri derinliğini ölçüyordu.

    Yerine iki koşul:

    * ``annualized_alpha > 0`` — yıllıklandırılmış, iki taraf da net (buy&hold
      gidiş-dönüş maliyeti düşülür, biliniyorsa temettü eklenir). Pencere
      uzunluğundan bağımsız.
    * risk-ayarlı üstünlük — stratejinin Calmar'ı buy&hold'unkini geçmeli.
      Getiriyi tek başına aşmak yetmez: aynı getiriyi yarı düşüşle üretmek de
      bir üstünlüktür, iki katı düşüşle üretmek değildir.

    Yıllıklandırma alanları yoksa (zaman damgasız pencere, eski kayıt) eski
    kümülatif kurala düşülür — sessizce kapıyı AÇMAK, kapatmaktan kötüdür.

    ``m`` bir aday koşusunun metrikleri de olabilir, çok-sembol testindeki tek
    bir peer'ın satırı da: ikisi de aynı damgalayıcıdan (bkz.
    ``_stamp_annualized_comparison``) geçtiği için aynı alanları taşır. Kural
    burada TEK kopya — iki kapının ıraksama tarihi için modül docstring'ine bak.
    """
    alpha = m.get("annualized_alpha")
    if alpha is None:
        try:
            excess = float(legacy_excess)
        except (TypeError, ValueError):
            # Ne yıllık alfa ne kümülatif fark var: benchmark bacağı hiç
            # ölçülmemiş. Kapı fail-closed — ölçülmemiş bir üstünlük yoktur.
            return "no_benchmark"
        if not math.isfinite(excess) or excess <= 0:
            return "below_benchmark"
        return None
    try:
        alpha = float(alpha)
    except (TypeError, ValueError):
        return "no_metrics"
    strat_calmar, bench_calmar = m.get("strategy_calmar"), m.get("benchmark_calmar")
    if benchmark_gate_mode() == "risk_adjusted":
        # Alfanın POZİTİF olması şart değil — ölçü Calmar üstünlüğü (aşağıdaki
        # ORTAK blokta, mod fark etmeksizin uygulanır). Burada yalnız TABAN
        # var: para kaybeden bir strateji, düşüşü küçük diye geçemez.
        try:
            cagr = float(m.get("strategy_cagr"))
        except (TypeError, ValueError):
            cagr = None
        if strat_calmar is None or bench_calmar is None:
            # Bu modun ÖLÇÜSÜ Calmar üstünlüğü ve o ölçülemedi → eski mutlak
            # kurala düş. Ölçülemeyen bir üstünlük, üstünlük sayılmaz.
            if not math.isfinite(alpha) or alpha <= 0:
                return "negative_alpha"
        elif cagr is not None:
            if not math.isfinite(cagr) or cagr <= 0:
                return "not_profitable"
        elif not math.isfinite(alpha) or alpha <= 0:
            # Kârlılık ölçülemedi, taban yok → alfaya geri dön (fail-closed).
            return "negative_alpha"
        # DİKKAT: burada erken dönme YOK. Calmar karşılaştırması aşağıdaki
        # ORTAK blokta yapılır. Bir önceki sürümde buradan `return None`
        # ediliyordu ve Calmar bacağı atlanıyordu — kapıyı zayıflatan sessiz
        # bir delik (test_alpha_gate_calibration yakaladı).
    elif not math.isfinite(alpha) or alpha <= 0:
        return "negative_alpha"
    if strat_calmar is None or bench_calmar is None:
        # Alfa pozitif ama risk bacağı ölçülemedi: kapıyı alfa ile geçir,
        # uydurulmuş bir risk karşılaştırmasıyla değil.
        return None
    try:
        strat_calmar, bench_calmar = float(strat_calmar), float(bench_calmar)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(strat_calmar) or strat_calmar <= bench_calmar:
        return "worse_risk_adjusted"
    return None
