"""AUTO oturum loguna yazılan uzun serilerin indirgenmesi — web'siz alan katmanı.

DeepR 2026-08-11 [ORTA]: bu saf fonksiyonlar `web/routes/agent_backtest.py`
içindeydi, ama HTTP ile ilgileri yok — bir JSON yapısındaki sayı dizilerini
küçültüyorlar. Bunun bedeli gerçek bir katman ihlaliydi: `compact_sessions.py`
(bir CLI aracı; bir web sunucusu değil) onları çağırabilmek için
``from web.routes.agent_backtest import SESSION_LOG_DIR, _thin_curves``
yazıyordu — yani tüm FastAPI router ağacını, erişim kapısını ve şablon ortamını
import ediyordu. Üstelik ALT ÇİZGİLİ bir adı, başka bir modülün "private"
yüzeyinden çekerek.

Yaprak modül; `auto/` kuralı gereği hiçbir `web.*` import edemez
(`tests/test_auto_layer_is_web_free.py` AST ile denetler).

Wiki References: [[nau_performans_denetimi]], [[webapp_module_map]]
"""

from __future__ import annotations

__all__ = [
    "downsample_indices",
    "is_point_series",
    "spark_points",
    "thin_curves",
    "thin_pair",
]


def downsample_indices(count: int, cap: int) -> list[int]:
    """Evenly-spaced indices (keeps first/last) shrinking a length-``count``
    sequence to ``cap`` points — shared by every curve-thinning path below so
    the rounding formula lives in exactly one place."""
    step = (count - 1) / (cap - 1)
    return [int(round(i * step)) for i in range(cap)]


def spark_points(curve, n: int = 40) -> list[float]:
    """Downsample an equity curve to at most ``n`` points for the cockpit
    sparkline. Keeps first/last so the visual slope stays honest."""
    if not curve:
        return []
    pts = [float(v) for v in curve if v is not None]
    if len(pts) <= n:
        return [round(v, 4) for v in pts]
    return [round(pts[i], 4) for i in downsample_indices(len(pts), n)]


def thin_pair(values, dates, cap: int = 400) -> tuple[list, list]:
    """Downsample an equity curve and its date axis on the SAME indices.

    ``thin_curves`` fixed the robustness payload; the ``backtest_result`` event
    then became the biggest line in the session log at ~301 KB apiece (measured
    on run 3cad3325), because ``equity_curve``/``equity_dates`` were written raw
    — 4,800 points for a single 15-minute iteration. No template reads these
    back (the session detail page counts the events and reads metrics; the tear
    sheet draws from the backtest log), so their only job here is forensic
    shape, and 400 points carry that.

    The two arrays MUST be reduced together: thinning them independently would
    silently de-align value i from date i, which is worse than dropping either.
    """
    vals = list(values or [])
    dts = list(dates or [])
    if len(vals) <= cap:
        return vals, dts
    idx = downsample_indices(len(vals), cap)
    out_v = [vals[i] for i in idx]
    # Dates may be absent or shorter (older records) — only index what exists.
    out_d = [dts[i] for i in idx if i < len(dts)] if dts else []
    return out_v, out_d


def is_point_series(seq) -> bool:
    """``[[ts, value], …]`` biçiminde bir seri mi? (dict listeleri HARİÇ)"""
    return all(
        isinstance(v, (list, tuple))
        and 2 <= len(v) <= 4
        and all(isinstance(x, (int, float, str)) for x in v)
        for v in seq
    )


def thin_curves(obj, cap: int = 40):
    """Oturum loguna yazılacak yapıdaki uzun sayı dizilerini indirger (KOPYA döner).

    `backtest_result` yolunda equity eğrileri ~40 noktaya indirgeniyordu ama
    `robustness_result` yolunda ham yazılıyordu; aynı ders ikinci yüzeye
    uygulanmamıştı. Bedeli ölçüldü: olay başına **3,5 MB** (88 WFO penceresi ×
    train/test/naive metrikleri, 50 Monte Carlo eğrisi, 8.605 noktalık ham OOS
    eğrisi) ve 76 oturumda **11,8 GB** disk — tek dosya 4,7 GB. Bu, `/sessions`
    listesinin soğuk açılışını 114 saniyeye çıkaran maliyetin de kaynağı.

    İndirgenen şey **seri**dir ve seriler bu yükte İKİ biçimde gelir:

    - düz sayı dizisi — ``mc.curves_sample[i]``, ``mc.percentile_curves.p50``
    - **zaman damgalı çift** dizisi — ``equity_curve_mtm`` şu şekildedir:
      ``[["2021-06-03T19:00:00+00:00", 10000.0], …]``

    İlk sürüm yalnız birincisini tanıyordu (`all(isinstance(v, (int, float)))`)
    ve üretimde **en büyük kalemi ıskaladı**: `wfo_windows` 2,39 MB ve
    `split.*.equity_curve_mtm` 0,35 MB indirgenmeden yazılmaya devam etti,
    olay 2,97 MB kaldı. Ders: indirgeme kuralı verinin GERÇEK şekline göre
    yazılmalı — "eğri = sayı listesi" varsayımı doğrulanmamıştı.

    Sözlükler, dizgeler ve dict listeleri (ör. işlem kayıtları) olduğu gibi
    korunur — indirgeme bir görselleştirme kaybıdır, adli kayıt kaybı değil.
    Girdi ASLA değiştirilmez: aynı `rob` sözlüğü karar ve ekran yollarında da
    kullanılıyor.
    """
    if isinstance(obj, dict):
        return {k: thin_curves(v, cap) for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) > cap and obj:
            if all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in obj
            ):
                return spark_points(obj, cap)
            if is_point_series(obj):
                # Çiftleri BOZMADAN seyrelt: aynı adım mantığı, elemanlar aynen
                # korunur (zaman damgası + değer birlikte anlamlı).
                step = (len(obj) - 1) / (cap - 1)
                return [obj[int(round(i * step))] for i in range(cap)]
        return [thin_curves(v, cap) for v in obj]
    return obj
