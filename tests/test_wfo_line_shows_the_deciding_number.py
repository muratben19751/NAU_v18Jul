"""Ekrandaki WFO oranı, kararı veren oran olmalı.

AUTO koşusu 8aa18365 (2026-08-18), tur 3: bir aday dört ölçüt de yeşil
görünürken elendi.

    ❌ Failed — IS/OOS: ✓ Robust · WFO: 37/60 · MC median DD: -21.5% ·
                Multi-symbol: ✓ Generalizable

Artefakt açılıp sayıldığında sebep çıktı: satır KÂRLI pencereleri sayıyordu
(37/60 = %62, çıtayı geçer), kapı ise AL-TUT'U GEÇEN pencereleri
(28/60 = %47, çıtanın altında). Aynı listeden, aynı adla, iki farklı ölçüm —
ve 22 yıllık boğada sistematik ıraksıyorlar.

Aynı turdaki İKİNCİ aday da aynı sebeple düştü (alfa 0/2, ekranda 1/2) ve o da
görünmüyordu; yani 2/2.

Bu depoda ilke zaten yazılıydı — `_holdout_promotion_verdict`: "gerekçe metni
boolean ile TAM OLARAK aynı bayraklardan türetilmelidir, yoksa ikisi ıraksar".

Wiki References
---------------
Bkz: [[auto_kapi_ve_geri_bildirim]], [[nau_holdout_dogrulama_turu_2026_08_18]]
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from auto.robustness import WFO_MIN_TRADES, wfo_verdict

ROOT = Path(__file__).resolve().parents[1]


def _window(*, n_trades: int, pnl: float, excess: float | None, sharpe: float = 0.5):
    """`excess` artık ALFA anlamına gelir ve maliyet-eşli alana yazılır.

    Eski alan da bilerek doldurulur: kapı ona geri dönerse testler yeşil
    kalmasın diye DEĞİL — tersine, gerçek payload'da ikisi birlikte durduğu
    için kurgu da öyle olmalı; kaynağı `test_alpha_is_read_from_the_cost_matched_field`
    tutar.
    """
    metrics = {"n_trades": n_trades, "pnl": pnl, "sharpe": sharpe}
    if excess is not None:
        metrics["annualized_alpha"] = excess
        metrics["excess_return_fraction"] = excess
    return {"test_metrics_naive": metrics, "test_n_trades": n_trades}


def _basket(n_alpha: int, n_profit_only: int = 0, n_loss: int = 0):
    """Alfalı / sadece kârlı / zararlı pencerelerden bir sepet."""
    out = [
        _window(n_trades=WFO_MIN_TRADES + 1, pnl=100.0, excess=0.05)
        for _ in range(n_alpha)
    ]
    out += [
        _window(n_trades=WFO_MIN_TRADES + 1, pnl=100.0, excess=-0.05)
        for _ in range(n_profit_only)
    ]
    out += [
        _window(n_trades=WFO_MIN_TRADES + 1, pnl=-100.0, excess=-0.20)
        for _ in range(n_loss)
    ]
    return out


def test_the_measured_case_now_fails_visibly():
    """Ölçülen vaka: 37 kârlı / 28 alfalı / 60 geçerli → ret, gerekçesiyle."""
    v = wfo_verdict(_basket(n_alpha=28, n_profit_only=9, n_loss=23))
    assert v.valid == 60
    assert v.pnl_positive == 37, "vaka yeniden üretilemedi"
    assert v.alpha_positive == 28
    assert v.alpha_ratio == pytest.approx(28 / 60)
    assert not v.ok
    assert "28/60" in v.reason and "<50%" in v.reason


def test_the_displayed_ratio_is_the_deciding_ratio():
    """Asıl düzeltme bu: ekrandaki sayı kararın sayısı."""
    v = wfo_verdict(_basket(n_alpha=28, n_profit_only=9, n_loss=23))
    assert v.display == "28/60"
    assert v.display != f"{v.pnl_positive}/{v.valid}"


def test_a_profitable_but_alphaless_basket_does_not_pass():
    """Boğada kâr etmek yeterli değil — hepsi kârlı, hiçbiri al-tut'u geçmiyor."""
    v = wfo_verdict(_basket(n_alpha=0, n_profit_only=10))
    assert v.pnl_positive == 10 and v.alpha_positive == 0
    assert not v.ok and v.display == "0/10"


def test_enough_alpha_passes():
    v = wfo_verdict(_basket(n_alpha=6, n_profit_only=0, n_loss=4))
    assert v.ok and v.reason == "passed" and v.display == "6/10"


def test_missing_alpha_is_fail_closed():
    """Ölçülemeyen alfa olumlu sayılmaz — arşiv/slim satırlar kapıyı açmasın."""
    windows = _basket(n_alpha=9, n_profit_only=0)
    windows.append(_window(n_trades=WFO_MIN_TRADES + 1, pnl=10.0, excess=None))
    v = wfo_verdict(windows)
    assert not v.ok
    # Gerekçe eksik ÖLÇÜMÜ adlandırmalı; "9/10 yetmedi" gibi okunmamalı.
    assert "missing" in v.reason and "alpha" in v.reason


def test_a_nonpositive_penalized_sharpe_still_blocks():
    """Alfa yeterliyken bile kararsız bir seri geçmemeli."""
    v = wfo_verdict(_basket(n_alpha=8, n_profit_only=0, n_loss=2), penalized=-0.3)
    assert not v.ok and "Sharpe" in v.reason
    # ...ve pozitifken engellemez.
    assert wfo_verdict(_basket(n_alpha=8, n_loss=2), penalized=0.3).ok


def test_too_few_trades_is_not_measured_rather_than_failed():
    """Ölçülemeyen ile başarısız ayrı: biri atlanır, diğeri düşürür."""
    thin = [_window(n_trades=WFO_MIN_TRADES - 1, pnl=1.0, excess=0.1)]
    v = wfo_verdict(thin)
    assert not v.measured and v.display == "—"
    assert str(WFO_MIN_TRADES) in v.reason
    assert not wfo_verdict([]).measured


def test_all_three_consumers_read_one_source():
    """Üç yerde üç ayrı sayım, tam da ıraksamanın sebebiydi."""
    import auto.robustness as ar
    import web.routes.agent_backtest as ab

    suite = inspect.getsource(ar.run_full_robustness)
    assert "wfo_verdict(" in suite, "adım satırı kararı okumuyor"

    gate = inspect.getsource(ab._robustness_tally)
    assert "_wfo_verdict(" in gate, "kapı yardımcıyı kullanmıyor"
    assert "positive_ratio" not in gate, "kapıda ikinci bir sayım kalmış"

    scan = inspect.getsource(ab._scan_one_candidate)
    assert "_wfo_verdict(" in scan and ".display" in scan
    assert 'get("pnl", 0) > 0' not in scan, "elenme satırı hâlâ PnL sayıyor"


def test_the_reason_is_printed_exactly_once():
    """Gerekçe TEK yerde basılır: kararın verildiği yerde.

    Ölçülen belirti (koşu 568f2838, tur 1): aynı satır iki kez göründü, çünkü
    hem suite hem kapı basıyordu. Suite'teki kopyanın ikinci bir sakıncası da
    vardı — orada `penalized` ipucu YOK, yani sönümlenmiş-Sharpe bacağında
    kapıyla ıraksayabilirdi.
    """
    import inspect

    import auto.robustness as ar
    import web.routes.agent_backtest as ab

    suite = inspect.getsource(ar.run_full_robustness)
    gate = inspect.getsource(ab._robustness_tally)

    assert "✗ Walk-Forward: {v.reason}" not in suite, "suite gerekçeyi de basıyor"
    assert 'f"  ✗ Walk-Forward: {_wf.reason}"' in gate, "kapı gerekçeyi basmıyor"
    # Görüntülenen oran suite'te KALIR: `display` `penalized`e bağlı değil.
    assert "v.display" in suite


def test_the_rejection_reason_reaches_the_operator():
    """Sessiz `failed += 1` dört yeşil ölçüt ve açıklamasız bir ❌ üretiyordu."""
    import web.routes.agent_backtest as ab

    gate = inspect.getsource(ab._robustness_tally)
    assert re.search(r"✗ Walk-Forward: \{_wf\.reason\}", gate), (
        "WFO reddi operatöre yazılmıyor"
    )


def test_the_profitable_count_survives_as_a_diagnostic():
    """Kârlılık sayısı karar vermiyor ama kayboluyor da değil.

    İkisi arasındaki fark, "boğayı mı taşıyor yoksa alfa mı üretiyor"
    sorusunun doğrudan cevabı — atmak teşhis kaybı olurdu.
    """
    import auto.robustness as ar

    suite = inspect.getsource(ar.run_full_robustness)
    assert "merely profitable" in suite
    v = wfo_verdict(_basket(n_alpha=2, n_profit_only=6, n_loss=2))
    assert v.pnl_positive == 8 and v.alpha_positive == 2


def test_the_step_header_states_the_deciding_criterion():
    """Başlık satırı da kararın ölçüsünü söylemeli, sonuç satırı gibi.

    Ölçülen belirti (koşu ff2bf7de, 2026-08-19, tur 1): sonuç satırı düzeltilmiş
    ve doğru sayıyı basıyordu —

        ✗ Walk-Forward: alpha in only 1/7 windows (<50%)

    ama iki satır YUKARIDAKİ açıklama hâlâ eski ölçütü öğretiyordu:

        📈 Walk-Forward — … ≥50% of windows must have positive PnL.

    Yani operatöre önce yanlış kural söyleniyor, sonra o kurala göre okunamayan
    bir sayı gösteriliyordu. Bir turda ikisini birden okuyan kişi `1/7`'yi
    "7 pencerenin 1'i kârlıymış" diye anlar — oysa 7'sinin çoğu kârlı olabilir.

    Düzeltmenin tek yerde olmaması bu ailenin imzası: aynı ölçüt üç yerde
    (başlık, sonuç, kapı) yazılıyorsa üçü de aynı kaynaktan türemeli ya da
    hiçbiri türemiyorsa hepsi elle senkron tutulmalı — ikincisi çürür.
    """
    import inspect

    import auto.robustness as ar

    suite = inspect.getsource(ar.run_full_robustness)
    header = [
        ln for ln in suite.splitlines() if "Walk-Forward — rolling-window" in ln
    ]
    assert header, "WFO açıklama satırı bulunamadı"

    # Başlığın gövdesi: pf(...) çağrısının tamamı.
    start = suite.index(header[0])
    block = suite[start : start + 500]

    assert "positive PnL" not in block, "başlık hâlâ kârlılığı ölçüt gibi söylüyor"
    assert "beat buy&hold" in block, "başlık asıl ölçütü (al-tut'u geçmek) söylemiyor"
    # Kârlılık sayısı ekranda kalıyor ama ÖLÇÜT olmadığı açıkça yazılmalı.
    assert "merely profitable" in block


def test_alpha_is_read_from_the_cost_matched_field():
    """Kapı, iki bacağı da NET olan alandan okumalı.

    `app_constants` iki karşılaştırma üretiyor ve ikisi aynı sözlükte duruyor:

        excess_return_fraction  → benchmark_cost_basis: gross_buy_and_hold_no_costs
        annualized_alpha        → benchmark_net_cost_basis: round_trip_cost_...

    Birincisinin docstring'i "geriye uyumluluk için duruyor ama KARAR ölçütü
    olamaz" diyordu — büyüklüğü pencere uzunluğuna bağlı ve iki bacağı farklı
    maliyet tabanında. Kapı yine de onu okuyordu; doğru alan iki satır ötedeydi.

    Ölçülen etki (54 artefakt, 973 pencere): ayrışan pencere sayısı 2. Bu test
    davranışı değil KAYNAĞI tutuyor — düzeltme sonuçları değiştirsin diye değil,
    doğru olan bu olduğu için yapıldı.
    """
    import inspect

    import auto.robustness as ar

    src = inspect.getsource(ar.wfo_verdict)
    assert 'get("annualized_alpha")' in src, "kapı maliyet-eşli alanı okumuyor"
    assert 'get("excess_return_fraction")' not in src, (
        "brüt kıyaslı eski alan karara geri sızmış"
    )


def test_a_missing_annualized_alpha_is_fail_closed():
    """Alan bazı pencerelerde HİÇ damgalanmıyor; yokluk olumlu sayılmamalı.

    `_stamp_annualized_comparison` `window_years` ya da benchmark drawdown
    hesaplanamazsa erken dönüyor — o pencerede `excess_return_fraction` var,
    `annualized_alpha` yok. Eksik ölçüm "alfa yok" değil "ölçülemedi"dir ve
    kapıyı açmamalıdır.
    """
    windows = _basket(n_alpha=9, n_profit_only=0)
    blind = _window(n_trades=WFO_MIN_TRADES + 1, pnl=10.0, excess=0.5)
    blind["test_metrics_naive"].pop("annualized_alpha", None)
    windows.append(blind)
    v = wfo_verdict(windows)
    assert not v.ok
    assert "alpha" in v.reason


def test_a_two_window_coin_flip_no_longer_passes():
    """ÖLÇÜLEN VAKA: koşu 4f7849df, aday 1/2 pencere = %50 → WFO'dan GEÇTİ.

    Oran kuralının paydası sınırsızdı: iki geçerli pencerenin birinde al-tut
    geçilince oran tam %50 çıkıyor ve çıta sağlanıyordu. Diskteki 32 adayın
    taranmasında bu, kararı değişen TEK aday — yani kural teorik bir açık
    değil, gerçekleşmiş bir açıktı.

    Ölçüm (3.000 rastgele strateji): payda sınırı yokken rastgele stratejilerin
    %29'u (1-DAY) çıtayı tutturuyor; 10 pencere şartıyla %3-6'ya iniyor. Medyan
    her iki hâlde de %23 — yani eriyen şey beceri değil, az-örneklem gürültüsü.
    """
    v = wfo_verdict(_basket(n_alpha=1, n_profit_only=0, n_loss=1))
    assert v.valid == 2 and v.alpha_positive == 1
    assert v.alpha_ratio == pytest.approx(0.5), "vaka yeniden üretilemedi"
    assert not v.ok, "yazı-tura hâlâ kapıyı açıyor"


def test_the_floor_rejects_even_a_perfect_short_basket():
    """Az pencerede %100 de kanıt değildir — 3/3, 10 pencerelik kanıt sayılmaz."""
    v = wfo_verdict(_basket(n_alpha=3))
    assert v.alpha_ratio == pytest.approx(1.0)
    assert not v.ok


def test_the_floor_reason_is_not_a_performance_verdict():
    """Gerekçe iki farklı reddi ayırmalı: 'kötü' ile 'yargılayamadım'."""
    thin = wfo_verdict(_basket(n_alpha=1, n_loss=1))
    assert "needed to judge" in thin.reason
    assert "not a performance rejection" in thin.reason
    # Gerçek performans reddinin metni karışmamalı.
    real = wfo_verdict(_basket(n_alpha=3, n_loss=9))
    assert "alpha in only" in real.reason and "needed to judge" not in real.reason


def test_an_undersized_basket_is_a_failure_not_a_skip():
    """KRİTİK: kapıda 'ölçülemedi' RET DEĞİL, ATLAMA anlamına geliyor.

    `_robustness_tally` `measured=False` gördüğünde `_skip(...)` çağırıyor ve
    `failed` artmıyor; sıkı modda 3 ölçüt yeterli olduğu için aday kalan
    IS/OOS + çok-sembol + Monte Carlo ile TERFİ EDEBİLİRDİ. Yani yetersiz
    pencereyi "ölçemedim" diye işaretlemek kapıyı GEVŞETİRDİ.

    Bu yüzden `measured` True kalır (ekranda gerçek oran görünür) ve `ok`
    False olur — kanıtlayamayan aday terfi etmez.
    """
    v = wfo_verdict(_basket(n_alpha=1, n_loss=1))
    assert v.measured is True, "atlamaya düşerse aday 3 ölçütle terfi eder"
    assert v.ok is False
    assert v.display == "1/2", "gerçek oran gizlenmemeli"


def test_the_floor_is_registered_as_an_env_knob():
    """Ölçüme dayanan bir sabit, ölçüm değişince ayarlanabilir olmalı."""
    import auto.robustness as ar

    assert ar.WFO_MIN_VALID_WINDOWS == 10
    src = inspect.getsource(ar)
    assert "NAUTILUS_WFO_MIN_WINDOWS" in src


def test_pooled_alpha_turns_unjudgeable_into_a_real_rejection():
    """Payda sınırının altında oy saymak anlamsız — ama ortalama anlamlı olabilir.

    ÖLÇÜLDÜ (arşivdeki 26 aday, 2026-08-20): 25'inin ortalama alfası negatif,
    hiçbiri p<0,10'a ulaşmıyor. Payda sınırının altında kalıp t ≤ −2 ile KESİN
    negatif çıkan 8 aday vardı (ör. 3 pencerede t=−4,60); onlara "yargılayamadım"
    demek yanlıştı — o bir performans reddidir ve öyle raporlanmalı.
    """
    # Sepet DEĞİŞKEN olmalı: `_basket` tek tip pencere üretiyor ve sabit bir
    # seride std sıfırdır — havuzlama (haklı olarak) atlanır. Gerçek WFO
    # pencereleri hiçbir zaman aynı alfayı vermez.
    losses = [-0.18, -0.09, -0.14, -0.21, -0.07, -0.16, -0.12]
    windows = [
        _window(n_trades=WFO_MIN_TRADES + 1, pnl=-100.0, excess=x) for x in losses
    ]
    v = wfo_verdict(windows)
    assert not v.ok
    assert "pooled alpha" in v.reason and "t=" in v.reason, v.reason
    assert "needed to judge" not in v.reason


def test_pooled_alpha_stays_silent_when_the_evidence_is_weak():
    """Belirsiz bir kısa seride eski mesaj kalmalı — uydurma kesinlik üretme."""
    windows = _basket(n_alpha=3, n_loss=3)
    v = wfo_verdict(windows)
    assert not v.ok
    assert "needed to judge" in v.reason, v.reason


def test_pooled_alpha_can_never_open_the_gate():
    """Bu yol YALNIZ ret; geçiş yolu olsaydı boş dağılım kalibrasyonu şarttı.

    Kalibrasyonsuz bir GEÇİŞ ölçütü, dün payda sınırını koyarken kaçındığımız
    hatanın ta kendisi olurdu. Kaynağı tutuyoruz: sınırın altındaki dal
    `ok=False` dışında bir şey döndüremez.
    """
    import inspect

    import auto.robustness as ar

    src = inspect.getsource(ar.wfo_verdict)
    branch = src[src.index("if len(valid) < WFO_MIN_VALID_WINDOWS"):]
    branch = branch[: branch.index("return WfoVerdict") + 400]
    assert "ok=False" in branch and "ok=True" not in branch

    # Mükemmel ama kısa bir sepet de geçmemeli.
    assert not wfo_verdict(_basket(n_alpha=5)).ok


def test_pooled_stats_are_fail_closed_on_a_degenerate_series():
    """std=0 → t tanımsız; sonsuz bir t üretmek yerine havuzlama atlanır."""
    import auto.robustness as ar

    assert ar.pooled_alpha_stats([0.05, 0.05, 0.05]) is None
    assert ar.pooled_alpha_stats([0.05, 0.06]) is None  # n < 3
    out = ar.pooled_alpha_stats([-0.1, -0.2, -0.15, -0.12])
    assert out is not None and out[2] < 0


# ---------------------------------------------------------------------------
# Çıtanın ZORLUĞU da görünür olmalı
# ---------------------------------------------------------------------------


def _bench(ret: float, dd: float):
    return {"benchmark_return_fraction": ret, "benchmark_max_dd": dd}


def test_a_smoothly_rising_window_is_not_winnable_long_only():
    """Pürüzsüz yükselişte piyasadan her çıkış kayıptır — kaldıraçsız geçilemez."""
    from auto.robustness import wfo_window_is_winnable

    assert wfo_window_is_winnable(_bench(0.20, -0.03)) is False
    assert wfo_window_is_winnable(_bench(0.05, -0.18)) is True   # dalgalı
    assert wfo_window_is_winnable(_bench(-0.10, -0.25)) is True  # negatif
    assert wfo_window_is_winnable({"benchmark_return_fraction": 0.1}) is None
    assert wfo_window_is_winnable({}) is None


def test_the_verdict_reports_how_many_windows_were_winnable():
    """ÖLÇÜLDÜ (QQQC 19 yıl): kazanılabilir oran pencere ayarına göre %63→%39.

    Yani %50 çıtası uzun pencerelerde KUSURSUZ zamanlamayla bile aşılamaz. Ve
    pencereyi adayın hızından türettiğimiz için yavaş adaylar tam o rejime
    giriyor. Arşivdeki 14 adayın 3'ünde çıta bu yüzden ulaşılamazdı — geri
    kalan 11'inde ise pencereler yeterliydi ve kusur adaydaydı. Bu ayrım
    ekranda görünmezse ikisi aynı ❌ olarak okunur.
    """
    hard = [
        _window(n_trades=WFO_MIN_TRADES + 1, pnl=1.0, excess=-0.01) for _ in range(12)
    ]
    for w in hard:  # hepsi pürüzsüz yükseliş → kazanılamaz
        w["test_metrics_naive"].update(_bench(0.20, -0.02))
    v = wfo_verdict(hard)
    assert v.winnable == 0
    assert "winnable long-only" in v.reason

    easy = [
        _window(n_trades=WFO_MIN_TRADES + 1, pnl=1.0, excess=-0.01) for _ in range(12)
    ]
    for w in easy:
        w["test_metrics_naive"].update(_bench(0.04, -0.15))
    assert wfo_verdict(easy).winnable == 12


def test_winnability_never_changes_the_decision():
    """Bu bir VEKİL; karar vermez, kararın zorluğunu görünür kılar.

    Karar verseydi kalibrasyonu gerekirdi — ve "kazanılabilir" tanımı ölçülmüş
    bir teorem değil, makul bir yaklaşımdır.
    """
    base = _basket(n_alpha=7, n_loss=5)          # 7/12 → geçer
    for w in base:
        w["test_metrics_naive"].update(_bench(0.20, -0.02))  # hiçbiri kazanılamaz
    v = wfo_verdict(base)
    assert v.winnable == 0
    assert v.ok is True, "vekil kararı ezmemeli"


def test_missing_benchmark_fields_leave_winnable_unknown():
    """Ölçülemeyen zorluk 0 diye raporlanmamalı — 0 bir iddiadır."""
    v = wfo_verdict(_basket(n_alpha=3, n_loss=9))
    assert v.winnable is None
    assert "winnable long-only" not in v.reason
