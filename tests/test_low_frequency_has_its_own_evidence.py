"""Düşük frekanslı strateji "ölçülemez" değil — YANLIŞ ŞEY ölçülüyordu.

Doğrulama makinesi işlem sayısına dayanıyor: WFO pencere başına ≥5, sıralama
≥20, Monte Carlo işlem sırasını karıştırıyor. 19 yılda 13-20 işlem yapan bir
rejim filtresi bu üçünde de ölçülemez ve kapı haklı olarak "yetersiz kanıt"
der — ama söyleyecek başka sözü de yoktur.

Oysa o stratejinin iddiası zaten işlemlerde değil MARUZİYETTEDİR: "uzun süreli
düşüşlerden kaçarım". Bu, 13 gözlem üzerinden değil 4.899 BAR üzerinden
ölçülebilir.

ÖLÇÜLDÜ (QQQC 19 yıl, 2026-08-21):
  MA 5/250   %25 düşüş · aynı maruziyetli rastgele maske %51 · p=0,005
  MA 50/100  %28        · %49                                · p=0,019
  MA 50/200  %28        · %49                                · p=0,033
  MA 20/100  %28        · %47                                · p=0,139

YOL BOYUNCA İKİ HATA (ikisi de burada kayıtlı, çünkü ikisi de tekrar edilebilir):

1. Önce "Calmar üstünlüğü sadece AZ YATIRIM yapmaktan geliyor" sandım. Kontrol
   çürüttü: sabit maruziyet %100'den %30'a inerken Calmar 0,20-0,22'de SABİT
   kalıyor — CAGR ve düşüş orantılı küçülüyor, oran değişmiyor.
2. Sonra ORTALAMA BAR GETİRİSİ ile ölçtüm; yanlış istatistikti. Strateji
   ortalamada daha kötü barlarda olsa bile (içeri 5,15 bp / dışarı 5,95 bp)
   KÜMELENMİŞ düşüşlerden kaçarak maxDD'yi ezebilir — ve tam olarak bunu
   yapıyor. İddia neyse istatistik o olmalı.

Wiki References
---------------
Bkz: [[kapi_ucdan_uca_dogrulandi_2026_08_21]], [[auto_kapi_ve_geri_bildirim]]
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from auto.robustness import exposure_drawdown_evidence


def _bars(n: int = 600, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0004, 0.01, n)
    # Ortada belirgin bir düşüş dönemi: kaçınmanın ÖLÇÜLEBİLECEĞİ bir olay.
    r[250:330] = rng.normal(-0.004, 0.012, 80)
    idx = pd.date_range("2010-01-01", periods=n, freq="1D", tz="UTC")
    return pd.DataFrame({"close": 100 * np.cumprod(1 + r)}, index=idx)


def _trades(bars, spans):
    out = []
    for a, b in spans:
        out.append(
            {
                "entry_time": int(bars.index[a].timestamp()),
                "exit_time": int(bars.index[b].timestamp()),
            }
        )
    return out


def test_avoiding_the_drawdown_window_scores_better_than_random_timing():
    """Düşüş dönemini atlayan maske, aynı süreli rastgele maskeleri geçmeli."""
    bars = _bars()
    # 250-330 arası düşüşü ATLA, geri kalanda piyasada ol.
    good = _trades(bars, [(0, 245), (335, 599)])
    ev = exposure_drawdown_evidence(bars, good, n_shift=300)
    assert ev is not None
    assert ev["max_dd"] < ev["null_median_dd"], "kaçınma düşüşe yansımıyor"
    assert ev["p_value"] < 0.20, ev


def test_the_metric_is_deterministic_for_the_same_candidate():
    """Aynı aday aynı p almalı — kapı yanındaki bir sayı koşudan koşuya oynamamalı."""
    bars = _bars()
    tr = _trades(bars, [(0, 245), (335, 599)])
    a = exposure_drawdown_evidence(bars, tr, n_shift=200)
    b = exposure_drawdown_evidence(bars, tr, n_shift=200)
    assert a == b


def test_extreme_exposure_is_not_measurable():
    """Neredeyse hep içeride/dışarıda olan bir maskede karşılaştırılacak null yok."""
    bars = _bars()
    assert exposure_drawdown_evidence(bars, _trades(bars, [(0, 599)])) is None
    assert exposure_drawdown_evidence(bars, _trades(bars, [(10, 20)])) is None
    assert exposure_drawdown_evidence(bars, []) is None
    assert exposure_drawdown_evidence(None, [{"entry_time": 1, "exit_time": 2}]) is None


def test_it_never_decides_only_reports():
    """KARAR VERMEZ — aynı ölçüm bilerek seçilmiş KÖTÜ ayarlarda da anlamlı çıkıyor.

    Ölçüldü: MA 80/90 (neredeyse hep piyasada, ayırt edici değil) p=0,021 ile
    "anlamlı". Yani test aile içinde ayırt ETMİYOR; kapıyı açsaydı zayıf
    parametrelendirmeleri de geçirirdi. Bu yüzden yalnız teşhis.
    """
    import inspect

    import auto.robustness as ar

    src = inspect.getsource(ar.exposure_drawdown_evidence)
    assert "KARAR VERMEZ" in src
    # Verdict/karar yapılarına dokunmamalı.
    assert "WfoVerdict" not in src and "ok=" not in src
    # Ve hiçbir kapı bu fonksiyonu karar için çağırmamalı.
    gate = inspect.getsource(ar.wfo_verdict)
    assert "exposure_drawdown_evidence" not in gate


# ---------------------------------------------------------------------------
# Neden AİLE İÇİ ayırt edici bir ölçüt YOK — ve yazılmamalı
# ---------------------------------------------------------------------------


def test_the_diagnostic_deliberately_reports_only_the_stable_property():
    """Kapsam bir tercih değil, ÖLÇÜM sonucu.

    "Aile içinde ayırt eden bir ölçüt tasarla" isteği ölçümle kapandı. 29 MA
    parametrelendirmesi, eğitim serisinin iki yarısında ayrı ayrı (QQQC,
    2003-2013 / 2013-2023):

      parametre SIRASI (Calmar)  ρ = −0,06   → KALICI DEĞİL
      parametre SIRASI (maxDD)   ρ = +0,08   → KALICI DEĞİL
      aile MEDYAN Calmar         0,17 → 0,29 → KALICI DEĞİL (rejime bağlı)
      aile MEDYAN maxDD          %29 → %27   → KALICI

    Somut hâli: ilk yarının en iyisi (MA 50/100, Calmar 0,29) ikinci yarıda
    29 parametre arasında **19.** sırada. Diğer ilk-beş: 7., 22., 26., 12.

    Üç sonuç, üçü de bu teşhisin kapsamını belirliyor:

    1. **Parametre seçimine kredi verilemez.** Sıra korelasyonu sıfırken
       herhangi bir seçici gürültü seçer. (Aynı sebeple WFO'nun pencere başına
       GA optimizasyonu da her pencerede gürültü seçiyor olabilir — ayrı bir
       soru, ama aynı ölçümden doğuyor.)
    2. **Risk-ayarlı üstünlük aile düzeyinde bile sertifikalanamaz**: ilk on
       yıl 2008'i içeriyor, ikincisi güçlü boğa; Calmar rejime bağlı ve rejim
       ekstrapole edilemez.
    3. **Düşüşten kaçınma sertifikalanabilir** — hem zamanda kalıcı hem
       rastgele maskelere karşı anlamlı.

    Bu test, teşhisin kapsamını genişletmeye çalışan bir sonraki kişiyi
    yukarıdaki ölçüme yönlendirmek için var.
    """
    import ast
    import inspect
    import textwrap

    import auto.robustness as ar

    # Docstring ve yorumlar Calmar'dan SÖZ EDER (neden dışlandığını anlatmak
    # için); iddia edilen şey ÇALIŞAN kodun ne hesapladığı. Bu yüzden gövde
    # docstring'siz olarak yeniden üretilip öyle kontrol edilir.
    tree = ast.parse(textwrap.dedent(inspect.getsource(ar.exposure_drawdown_evidence)))
    fn = tree.body[0]
    if (
        fn.body
        and isinstance(fn.body[0], ast.Expr)
        and isinstance(fn.body[0].value, ast.Constant)
        and isinstance(fn.body[0].value.value, str)
    ):
        fn.body = fn.body[1:]
    code = ast.unparse(fn)
    assert "max_dd" in code and "null_median_dd" in code
    assert "calmar" not in code.lower(), (
        "Calmar bacağı eklenmiş — aile düzeyinde bile kalıcı değil (0,17 → 0,29)"
    )
    keys = {"time_in_market", "max_dd", "null_median_dd", "p_value", "n_shift"}
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(5)
    r = rng.normal(0.0004, 0.01, 600)
    r[250:330] = rng.normal(-0.004, 0.012, 80)
    idx = pd.date_range("2010-01-01", periods=600, freq="1D", tz="UTC")
    bars = pd.DataFrame({"close": 100 * np.cumprod(1 + r)}, index=idx)
    tr = [
        {
            "entry_time": int(bars.index[0].timestamp()),
            "exit_time": int(bars.index[245].timestamp()),
        },
        {
            "entry_time": int(bars.index[335].timestamp()),
            "exit_time": int(bars.index[599].timestamp()),
        },
    ]
    ev = ar.exposure_drawdown_evidence(bars, tr, n_shift=100)
    assert set(ev) == keys, f"rapor yüzeyi genişlemiş: {set(ev) ^ keys}"
