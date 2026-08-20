"""AUTO robustluk suite'i — Multi-Symbol → IS/OOS → Walk-Forward → Monte Carlo.

Buradaki kod eskiden `web/routes/agent_backtest.py` içindeydi ve `sandbox.py`'nin
robustluk child'ı onu `import web.routes.agent_backtest as ab` ile çekip
`ab._IPC_Q = q` diyerek başka bir modülün private global'ini dışarıdan set
ediyordu. Yani HTTP servis etmeyen bir worker süreci sırf bu fonksiyon için tüm
FastAPI route ağacını yüklüyordu ve bağımlılık yönü tersineydi (DeepR 2026-08-11
[YÜKSEK]).

İlerleme bildirimi artık modül-global bir kuyruk yerine açık bir `progress_fn`
parametresi: parent süreçte route katmanı `_add_step`'e bağlar, child süreçte
`sandbox` kuyruğa yazan bir kapanış (closure) verir. Davranış birebir aynı —
eski `_add_step` zaten child'da `q.put(("progress", msg))` yapıp dönüyordu — ama
artık iki katman arasında paylaşılan tek şey bir fonksiyon imzası.

Kapı mantığı (`_robustness_passed`) bilinçli olarak burada DEĞİL: o, aday
seçimiyle iç içe geçmiş bir AUTO politikası ve hâlâ route/worker katmanında
yaşıyor. Bu modül yalnız ölçümü üretir, kararı vermez.

Wiki References
---------------
See: [[auto_kapi_ve_geri_bildirim]] (kapı hangi seriyi okur — `wfo_test`, neden
`test_metrics_naive`), [[multi_symbol_generalization]] (dikiş-farkındalıklı peer
dışlama), [[webapp_module_map]], [[backtesting_guide]],
[[nau_holdout_dogrulama_turu_2026_08_18]] (peer penceresinin mühür çapası ve
WFO penceresinin adayın hızından türetilmesi — `wfo_window_months`)
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from app_constants import MIN_DECISION_TRADES

# L2: Monte Carlo drawdown limits (%).  The median describes the typical path;
# strict publication also has to reject a materially unsafe adverse tail.
MC_DD_LIMIT = -25.0
MC_DD_TAIL_LIMIT = -35.0

# Liquid peer basket for multi-symbol robustness on external (US equity) runs.
# Üç endeks ETF'i + dört mega-cap: birincil enstrüman bunlardan birini dışlasa
# bile (bkz. peer_exclusions) geriye PEER_SAMPLE_SIZE kadarı kalsın diye yedekli.
#
# Venue eki BURADA BAĞLAYICI DEĞİL — bkz. resolve_peer_ids. Sepet uzun süre
# "SPY.ARCA"/"IWM.ARCA" yazdı (gerçek dünyada doğru: ikisi de NYSE Arca'da
# listeli) ama ingest her enstrümanı kendi damgasıyla yazıyor ve bu kutuda 16
# enstrümanın 16'sı `.NASDAQ`. Eşleşmeyen id aşağıdaki veri filtresinden
# SESSİZCE düşüyordu: beş peer'lık sepet fiilen üçle, QQQC koşusunda ikiyle
# çalışıyordu ve hiçbir yerde uyarı yoktu (ölçüldü 2026-08-16, koşu 392287b2).
EXTERNAL_PEER_BASKET = [
    "SPY.NASDAQ",
    "QQQ.NASDAQ",
    "IWM.NASDAQ",
    "AAPL.NASDAQ",
    "MSFT.NASDAQ",
    "NVDA.NASDAQ",
    "GOOGL.NASDAQ",
]

# Kaç peer'a kadar test edilir. 3 iken eşik çözünürlüğü sorunluydu: geçme kuralı
# `pass_rate >= 0.7`, yani 2 peer'da pratikte 2/2 zorunlu ve ara bant
# ("⚠ Limited") tek sembollük gürültüyle belirleniyordu. 5'te 4/5 geçer, 3/5
# "Limited" olur — kapı aynı sertlikte ama kararı tek bir zayıf akrana bağlı değil.
PEER_SAMPLE_SIZE = 5


def bare_ticker(instrument_id: str) -> str:
    """ "QQQ.NASDAQ" → "QQQ" (manifest anahtarları venue taşımaz).

    YALNIZ son nokta atılır: 'BRK.A.NASDAQ' → 'BRK.A'. İlk noktadan bölmek
    data.py'de bir kez yapıldı ve noktalı sembollerin manifest kaydını
    kaybettirdi (M1260) — aynı hatayı ikinci kez yazmayalım.
    """
    return (instrument_id or "").rsplit(".", 1)[0].strip().upper()


def resolve_peer_ids(basket: list[str]) -> list[str]:
    """Sepet id'lerini KATALOĞUN kullandığı venue'ya çevirir.

    Eşleştirme bare ticker üzerinden yapılır çünkü venue eki ingest'e göre
    değişiyor ve sabit yazmak sessiz düşmeye yol açıyor (bkz. sepetin yorumu).
    Katalogda karşılığı olmayan girdi olduğu gibi bırakılır — aşağıdaki veri
    filtresi zaten eleyecek, ama sepeti burada kırpmak "neden 2 peer?" sorusunun
    izini siler.

    Katalog okunamazsa sepet değişmeden döner: bir peer listesi uğruna
    robustness koşusu traceback etmez.
    """
    from data import list_external_instruments

    try:
        catalog = [r["instrument_id"] for r in list_external_instruments()]
    except Exception:
        return list(basket)
    known = set(catalog)
    # İlk gören kazanır: aynı ticker iki venue'da varsa (ör. hem .NASDAQ hem
    # .ARCA ingest edilmişse) sözlük kurma yönü sessiz bir tercih yapar. Katalog
    # sırası en azından SABİT; sepette yazılı id'nin kendisi katalogdaysa da o
    # kazanır — yazarın açık tercihini tahmine bırakmayalım.
    by_ticker: dict[str, str] = {}
    for cid in catalog:
        by_ticker.setdefault(bare_ticker(cid), cid)
    return [p if p in known else by_ticker.get(bare_ticker(p), p) for p in basket]


def peer_exclusions(instrument_id: str) -> set[str]:
    """Multi-symbol testinden dışlanacak bare ticker'lar.

    Düz `p != symbol` karşılaştırması DİKİLMİŞ serileri tanımıyordu: QQQC,
    `stitch:QQQ+QQQQ` ile üretilmiş sürekli bir seridir, dolayısıyla bir QQQC
    koşusunda peer olarak QQQ seçmek "başka bir enstrümanda da çalışıyor mu"
    sorusunu sormuyor — aynı veriyi ikinci kez soruyor. Genelleşebilirlik testi
    diye görünen şey, testin kendisinin tekrarıydı (DeepR 2026-08-10).

    Dışlama iki yönlü: dikilmiş seri bileşenlerini dışlar, bileşen de kendisini
    içeren dikişi dışlar (QQQ koşusunda QQQC peer olamaz).
    """
    from data import external_manifest

    me = bare_ticker(instrument_id)
    out = {me}
    try:
        manifest = external_manifest() or {}
    except Exception:
        return out

    def _components(entry: dict) -> set[str]:
        src = str((entry or {}).get("source") or "")
        if not src.startswith("stitch:"):
            return set()
        return {
            p.strip().upper() for p in src[len("stitch:") :].split("+") if p.strip()
        }

    out |= _components(manifest.get(me) or {})
    for ticker, entry in manifest.items():
        if me in _components(entry):
            out.add(str(ticker).strip().upper())
    return out


def wfo_test(w: dict) -> dict:
    """A WFO window's DECISION metric — the metric of the spec that gets SAVED.

    ``run_walk_forward`` re-optimizes the parameters on every window's train
    slice (GA, backtest_robustness.py) and reports two OOS results:

    - ``test_metrics``       — the RE-OPTIMIZED spec on the test slice
    - ``test_metrics_naive`` — the UNCHANGED spec (what the agent appends to
      the catalog and what the user would actually run)

    The gate used to read the first one, so the certificate went to a strategy
    re-fitted every 3 months while the deployed artifact was never re-fitted.
    Measured on run 1376c812: one candidate scored a penalized OOS Sharpe of
    −0.069 optimized (the pass threshold is >0) against −0.896 naive on the same
    windows — a pass would have shipped something a full point worse than the
    number that authorized it.

    Fallback: when the spec has no optimizable numeric parameter the WFO builds
    an empty search space, ``naive_result`` is never run and ``test_metrics`` IS
    the unoptimized run — so falling back to it keeps the same meaning.
    """
    return (w.get("test_metrics_naive") or {}) or (w.get("test_metrics") or {})


# Peer eşiğiyle AYNI sayı, tek kaynaktan. Burası 3 derken çok-sembol kapısı 5
# diyordu: aynı soruya ("bu kadar işlemden sonuç çıkar mı?") iki cevap. Ölçüm
# ve gerekçe app_constants.MIN_DECISION_TRADES'te.
WFO_MIN_TRADES = MIN_DECISION_TRADES


# Oranın PAYDASI için alt sınır. "Geçerli pencerelerin ≥%50'si al-tut'u geçmeli"
# kuralı, paydaya sınır konmazsa 2 pencerenin 1'iyle sağlanır — yani yazı-tura.
#
# ÖLÇÜLDÜ (3.000 rastgele strateji, QQQC 22 yıl, 2026-08-20): çıtayı tutturan
# rastgele strateji oranı, payda sınırına göre —
#
#   en az pencere │ 1-HOUR │ 1-DAY
#   ──────────────┼────────┼──────
#     1 (eski hâl)│   %10  │  %29
#     5           │    %4  │  %14
#    10           │    %3  │   %6
#    15           │    %2  │   %1
#
# Medyan her iki seride de %23'te SABİT kaldı (beceriksizliğin gerçek seviyesi);
# eriyen yalnız kuyruktu, yani yüksek oranlar beceri değil az-örneklem gürültüsü.
# 15 daha güvenli ama gerçek adayların çoğunun 7-17 penceresi var; 10, yanlış
# geçişi %3-6'ya indirirken adayları ölçülebilir bırakıyor.
WFO_MIN_VALID_WINDOWS = int(os.environ.get("NAUTILUS_WFO_MIN_WINDOWS", "10"))


# WFO penceresi TAKVİMDE sabit (6 ay eğitim / 2 ay test / 3 ay adım), eşiği ise
# SAYIMDA — mühürlü kapıyla birebir aynı birim uyuşmazlığı. Dönüşüm katsayısı
# yine stratejinin işlem hızı ve yine serbest bir değişken.
#
# ÖLÇÜLDÜ (diskteki AUTO artefaktları, 178 WFO penceresi): pencere başına işlem
# dağılımı {0: 70, 1: 88, 2: 20}. Hiçbir pencere 3'e bile ulaşmamış, yani
# ölçütün eşiği (5) hiçbir koşuda ulaşılabilir olmamış: WFO fiilen hiç
# konuşmadı — ama GA maliyeti her koşuda ödendi. Kapı bunu dürüstçe "ölçülmedi"
# sayıyor (bkz. agent_backtest `_skip("Walk-Forward", ...)`), yani sonuç yanlış
# değil YOK; ödenen bedel gerçek.
#
# Taban KORUNUYOR: kısa TF'lerde (1m/5m kripto) pencere zaten yeterince bar
# taşıyor ve davranış değişmiyor — genişletme yalnız gerektiğinde devreye girer.
WFO_BASE_TRAIN_MONTHS = 6
WFO_BASE_TEST_MONTHS = 2
WFO_BASE_STEP_MONTHS = 3
# Pencereyi büyütmenin bedeli pencere SAYISI. Altına inilmeyecek sayı: bir
# oranın ("pencerelerin %50'si pozitif") altında en az bu kadar gözlem olmalı,
# yoksa ölçüt bu kez de az-örneklemden konuşamaz.
WFO_MIN_WINDOWS = 8


def wfo_window_months(
    bars_df, n_trades: int | None
) -> tuple[int, int, int, str | None]:
    """(train, test, step) ay + uyarı — pencere adayın ÖLÇÜLEN hızından türer.

    Soru şu: ``WFO_MIN_TRADES`` girişin bu adayın kendi hızıyla düşebileceği
    en dar test penceresi kaç ay? Cevap tabandan küçükse taban kullanılır
    (kısa TF davranışı değişmez); büyükse pencere büyür ve eğitim/adım aynı
    oranda (3× ve 1,5×) ölçeklenir — böylece "6/2/3"ün şekli korunur.

    İki sınır var:

    * ``WFO_MIN_WINDOWS`` — pencereyi büyütmek pencere sayısını düşürür. Oranı
      8 gözlemin altına indirecek bir genişleme yapılmaz; o noktada uyarı
      verilir, çünkü ölçüt yine susacaktır ve bunu ÖNDEN söylemek zincirin
      sonunda "0/0 geçerli pencere" yazmaktan iyidir.
    * Hız ölçülemiyorsa (``n_trades`` yok ya da 0) taban kullanılır —
      uydurulmuş bir oran, bilinen bir sabitten kötüdür.

    Uyarı yalnız uyarıdır; hiçbir eşiği oynatmaz.
    """
    base = (WFO_BASE_TRAIN_MONTHS, WFO_BASE_TEST_MONTHS, WFO_BASE_STEP_MONTHS)
    try:
        n_bars = len(bars_df)
        span_days = (bars_df.index[-1] - bars_df.index[0]).days
    except Exception:
        return (*base, None)
    if not n_bars or span_days <= 0 or not n_trades:
        return (*base, None)

    total_months = span_days / 30.44
    bars_per_month = n_bars / total_months
    rate = n_trades / n_bars  # giriş / bar
    need_bars = WFO_MIN_TRADES / rate
    want_test = max(WFO_BASE_TEST_MONTHS, math.ceil(need_bars / bars_per_month))
    if want_test == WFO_BASE_TEST_MONTHS:
        return (*base, None)

    # Bir T aylık test penceresi için toplam gereksinim:
    #   3T (eğitim) + T (test) + 1,5T × (W−1) adım  ≤  total_months
    def _windows_for(t: int) -> int:
        train, step = 3 * t, max(1, round(1.5 * t))
        return int((total_months - train - t) // step) + 1

    test_m = want_test
    while test_m > WFO_BASE_TEST_MONTHS and _windows_for(test_m) < WFO_MIN_WINDOWS:
        test_m -= 1
    train_m, step_m = 3 * test_m, max(1, round(1.5 * test_m))

    expected = test_m * bars_per_month * rate
    note = None
    if expected < WFO_MIN_TRADES:
        # Cümledeki her sayı DOĞRULANABİLİR olmalı: pencere sayısı da yazılıyor,
        # çünkü genişleme iki ayrı sınırdan biriyle durmuş olabilir (taban ya da
        # WFO_MIN_WINDOWS) ve operatör hangisi olduğunu görmeden karar veremez.
        note = (
            f"Walk-Forward: this candidate opened an entry every {1 / rate:,.0f} "
            f"bars — a {test_m}-month test window ({_windows_for(test_m)} windows) "
            f"yields ~{expected:.1f} entries, under the {WFO_MIN_TRADES} a window "
            "needs to count. The criterion will probably stay silent, and the "
            "GA cost is paid either way"
        )
    return train_m, test_m, step_m, note


def valid_wfo_windows(wfo: list[dict]) -> list[dict]:
    """WFO windows with enough trades in their decision metric to count."""
    def _trades(w: dict) -> int:
        tm = wfo_test(w)
        if "n_trades" in tm and tm["n_trades"] is not None:
            try:
                return int(tm["n_trades"])
            except (ValueError, TypeError):
                return 0
        return int(w.get("test_n_trades") or 0)

    return [w for w in wfo if _trades(w) >= WFO_MIN_TRADES]


class WfoVerdict(NamedTuple):
    """WFO ölçütünün kararı VE o kararın sayıları — tek kaynaktan.

    Ekrana basılan sayı ile karar veren sayı ayrı yerlerde hesaplanınca
    ıraksadılar ve bunu kimse fark etmedi, çünkü ekran makul görünmeye devam
    etti. ÖLÇÜLDÜ (koşu 8aa18365, tur 3, artefaktdan sayıldı):

        geçerli pencere            60
        PnL'i pozitif  (ekranda)   37/60 = %62   ← çıtayı geçiyor
        al-tut'u geçen (kapıda)    28/60 = %47   ← çıtanın altında

    Operatör dört ölçütü de yeşil, sonucu ❌ gördü — gerekçesi ekranda olmayan
    bir ret. 22 yıllık boğada bu iki sayı sistematik ıraksıyor: bir pencerede
    kâr etmek kolay, al-tut'u geçmek değil.

    Bu depoda ilke zaten yazılıydı (`_holdout_promotion_verdict`: "gerekçe
    metni boolean ile TAM OLARAK aynı bayraklardan türetilmelidir"); burası onu
    uygulamıyordu. Artık hem kapı hem iki ekran satırı bu tek çağrıyı okuyor.

    ``pnl_positive`` bilerek DURUYOR ama karar vermiyor: teşhis olarak değerli
    (ikisi arasındaki fark "boğayı mı taşıyor, alfa mı üretiyor" sorusunun
    cevabı), karar ölçütü olarak yanıltıcı.
    """

    measured: bool
    valid: int
    alpha_positive: int
    pnl_positive: int
    alpha_ratio: float | None
    penalized_sharpe: float | None
    ok: bool
    reason: str

    @property
    def display(self) -> str:
        """Ekrana basılacak oran — KARARIN saydığı sayı."""
        return f"{self.alpha_positive}/{self.valid}" if self.measured else "—"


def wfo_verdict(wfo: list[dict], *, penalized: float | None = None) -> WfoVerdict:
    """WFO ölçütünü uygula ve kararı sayılarıyla birlikte döndür.

    ``penalized``: çağıranın payload'dan çözdüğü sönümlenmiş OOS Sharpe
    (``oos_sharpe_naive_penalized`` vb.). Verilmezse geçerli pencerelerin
    Sharpe'larından hesaplanır — hangi serinin okunacağı payload'ın
    kökenine bağlı olduğu için çözüm çağıranda kalıyor.

    Kural: geçerli pencerelerin en az yarısı AL-TUT'U GEÇMELİ, ve sönümlenmiş
    OOS Sharpe biliniyorsa pozitif olmalı. Eksik/sonsuz alfa fail-closed:
    ölçülemeyen alfa, olumlu sayılmaz.

    Alfa ``annualized_alpha``'dan okunur, ``excess_return_fraction``'dan DEĞİL.
    İkisi de aynı sözlükte duruyor ama birincisi bu iş için yazıldı: iki bacak
    da net (kıyasa ``BENCHMARK_ROUND_TRIP_COST_FRACTION`` düşülür, temettü
    eklenir) ve yıllıklandırılmış olduğu için pencere uzunluğundan bağımsız.
    İkincisi `app_constants` içinde "geriye uyumluluk için duruyor ama KARAR
    ölçütü olamaz" diye belgelenmişti — kapı yine de onu okuyordu.

    Ölçülen etki (54 artefakt, 973 pencere): iki alanın ayrıştığı pencere
    sayısı **2**. Yani bu bir doğruluk düzeltmesi, kalibrasyon değil — maliyet
    %0,02, pencere getirileri ±%1-20 bandında. Bu sayı burada yazılı ki
    "kapıyı düzelttik, artık adaylar geçer" beklentisi doğmasın.
    """
    valid = valid_wfo_windows(wfo or [])
    if not wfo or not valid:
        return WfoVerdict(
            measured=False,
            valid=0,
            alpha_positive=0,
            pnl_positive=0,
            alpha_ratio=None,
            penalized_sharpe=None,
            ok=False,
            reason=(
                "no windows"
                if not wfo
                else f"no valid window with ≥{WFO_MIN_TRADES} trades"
            ),
        )

    pnl_positive = sum(1 for w in valid if (wfo_test(w).get("pnl") or 0) > 0)

    excess = []
    for w in valid:
        try:
            excess.append(float(wfo_test(w).get("annualized_alpha")))
        except (TypeError, ValueError):
            excess.append(float("nan"))
    if any(not math.isfinite(v) for v in excess):
        return WfoVerdict(
            measured=True,
            valid=len(valid),
            alpha_positive=0,
            pnl_positive=pnl_positive,
            alpha_ratio=None,
            penalized_sharpe=penalized,
            ok=False,
            reason="missing slice-local annualized alpha",
        )

    alpha_positive = sum(1 for v in excess if v > 0)
    ratio = alpha_positive / len(valid)

    # Payda yetersizse oran anlamsız. Bu bir PERFORMANS reddi değil, KANITLAMA
    # eksikliğidir — ama sonucu yine rettir: kapıda "ölçülemedi" `_skip`e düşer,
    # `failed` artmaz ve aday kalan 3 ölçütle TERFİ EDEBİLİRDİ. Yetersiz
    # kanıtın terfiyle ödüllendirilmemesi için `measured` True kalır (ekranda
    # gerçek oran görünsün) ve `ok` False olur.
    if len(valid) < WFO_MIN_VALID_WINDOWS:
        return WfoVerdict(
            measured=True,
            valid=len(valid),
            alpha_positive=alpha_positive,
            pnl_positive=pnl_positive,
            alpha_ratio=ratio,
            penalized_sharpe=penalized,
            ok=False,
            reason=(
                f"only {len(valid)} valid windows — "
                f"{WFO_MIN_VALID_WINDOWS} needed to judge "
                f"(not a performance rejection)"
            ),
        )

    pen = penalized
    if pen is None:
        sh = []
        for w in valid:
            raw = wfo_test(w).get("sharpe")
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(v):
                sh.append(v)
        if len(sh) >= 2:
            mean = sum(sh) / len(sh)
            var = sum((x - mean) ** 2 for x in sh) / len(sh)
            pen = mean - 0.5 * (var**0.5)
    try:
        pen = float(pen) if pen is not None else None
        if pen is not None and not math.isfinite(pen):
            pen = None
    except (TypeError, ValueError):
        pen = None

    if ratio < 0.5:
        reason = f"alpha in only {alpha_positive}/{len(valid)} windows (<50%)"
        ok = False
    elif pen is not None and pen <= 0:
        reason = f"penalized OOS Sharpe {pen:.2f} ≤ 0"
        ok = False
    else:
        reason = "passed"
        ok = True

    return WfoVerdict(
        measured=True,
        valid=len(valid),
        alpha_positive=alpha_positive,
        pnl_positive=pnl_positive,
        alpha_ratio=ratio,
        penalized_sharpe=pen,
        ok=ok,
        reason=reason,
    )


def multi_symbol_definitive_failure(ms: dict | None) -> bool:
    """True only for an evaluated, explicit symbol-specific rejection."""

    return "✗" in str((ms or {}).get("generalization_label", ""))


def split_definitive_failure(split: dict | None) -> bool:
    """True when IS/OOS has already produced an explicit rejection."""
    return "✗" in str((split or {}).get("overfitting_label", ""))


def run_full_robustness(
    spec,
    bars_df,
    instrument,
    bar_type,
    venue,
    trades: list,
    symbol: str = "BTCUSDT",
    interval: str = "1",
    category: str = "linear",
    source: str = "bybit",
    *,
    progress_fn: Callable[[str], None] | None = None,
) -> dict:
    """Run robustness in the order Multi-Symbol → IS/OOS → WFO → MC.

    ``source="external"``: ``symbol`` is an external catalog instrument id
    (e.g. "QQQ.NASDAQ"), ``interval`` is the catalog DSL ("1-DAY"); the
    multi-symbol universe is chosen from EXTERNAL_PEER_BASKET.

    ``progress_fn`` receives every human-readable step line. Eskiden bu, modül
    global'i `_IPC_Q`'nün varlığına göre dallanan `_add_step` idi; artık
    çağıranın verdiği bir fonksiyon — sandbox child'ı kuyruğa yazar, in-process
    bir çağıran koşu durumuna yazar. Verilmezse adımlar sessizce düşer (suite
    yine tam koşar).

    ÖNEMLİ: satır başlarındaki glyph'ler (🌐 📊 📈 🎲) sözleşmenin parçası —
    `web/routes/agent_backtest.py:_make_rob_progress` alt-fazları bunları
    ayrıştırarak açıp kapatıyor. Değiştirilmemeli.

    When NAUTILUS_PARALLEL=1 (default), independent backtest units
    (multi-symbol, IS/OOS pair, WFO window×candidate) are distributed to a
    process pool; if the pool can't be set up or a stage blows up in the pool,
    that stage is re-run on the untouched sequential path. NAUTILUS_PARALLEL=0 →
    fully sequential (old behavior).
    """
    import shutil as _shutil

    from backtest import STARTING_CASH
    from backtest_robustness import (
        run_insample_oos_split,
        run_monte_carlo,
        run_multi_symbol,
        run_walk_forward,
    )

    pf = progress_fn if progress_fn is not None else (lambda _msg: None)

    # ── Parallel pool (optional) ──────────────────────────────────────────────
    pool = None
    run_many = None
    snapshot_path = None
    try:
        from parallel_exec import (
            BacktestPool,
            get_worker_count,
            make_snapshot,
            parallel_enabled,
        )

        if parallel_enabled():
            # Warm the trend-filter cache BEFORE fan-out: on a cold cache multiple
            # workers race to write the same parquet (data.py to_parquet).
            if getattr(spec, "trend_filter", False):
                try:
                    if source == "external":
                        from data import load_external_bars

                        load_external_bars(
                            symbol, getattr(spec, "trend_interval", "1-DAY")
                        )
                    else:
                        from data import load_bybit_bars

                        load_bybit_bars(
                            symbol=symbol,
                            interval=getattr(spec, "trend_interval", "60"),
                            category=category,
                            start=bars_df.index[0].to_pydatetime(),
                            end=bars_df.index[-1].to_pydatetime(),
                        )
                except Exception as warm_exc:
                    pf(f"  ⚠ Could not warm trend cache: {warm_exc}")
            snapshot_path = make_snapshot(bars_df)
            pool_recipe = (
                {"source": "external", "instrument_id": symbol, "granularity": interval}
                if source == "external"
                else {"symbol": symbol, "interval": interval, "category": category}
            )
            pool = BacktestPool(
                snapshot_path,
                pool_recipe,
                max_workers=get_worker_count(),
            )
            run_many = pool.run_units
            pf(
                f"⚡ Parallel mode: {pool.max_workers} worker processes "
                "(can be disabled with NAUTILUS_PARALLEL=0)"
            )
    except Exception as pool_exc:
        pf(f"⚠ Could not set up parallel pool ({pool_exc}) — sequential mode")
        run_many = None

    def _stage(label, fn, /, *args, **kwargs):
        """Run a robustness stage with the pool (if any); on a pool error re-run
        the same stage on the sequential path. The sequential path is always up."""
        if run_many is not None:
            try:
                return fn(*args, run_many=run_many, **kwargs)
            except Exception as par_exc:
                pf(
                    f"  ⚠ {label} parallel stage failed "
                    f"({type(par_exc).__name__}) — re-running sequentially"
                )
        return fn(*args, **kwargs)

    try:
        # 1) Multi-Symbol — cheapest test, eliminates fast (saves IS/OOS and WFO time up front)
        if source == "external":
            from data import _external_bar_dir

            # First filter peers that HAVE data at this granularity, THEN clip to
            # PEER_SAMPLE_SIZE (scoring is already tolerant). The reverse order
            # used to never try the 4th/5th peer if the first 3 peers had no data.
            # Dışlama dikiş-farkındalıklı: QQQC = stitch:QQQ+QQQQ olduğu için
            # düz `p != symbol` QQQ'yu peer sanıyordu (bkz. peer_exclusions).
            # Venue çözümü filtreden ÖNCE: sepet gerçek dünyanın venue'sunu
            # yazarken katalog başka damga kullanabiliyor (bkz. resolve_peer_ids).
            excluded = peer_exclusions(symbol)
            resolved = resolve_peer_ids(EXTERNAL_PEER_BASKET)
            eligible = [
                p
                for p in resolved
                if bare_ticker(p) not in excluded
                and _external_bar_dir(p, interval) is not None
            ]
            other_symbols = eligible[:PEER_SAMPLE_SIZE]
            # Havuz hedefin altında kaldıysa SÖYLE. Sessiz düşme tam olarak bu
            # kapının 5 peer sanılırken 2 ile karar vermesine yol açmıştı.
            if len(other_symbols) < PEER_SAMPLE_SIZE:
                pf(
                    f"  ⚠ Multi-Symbol havuzu {len(other_symbols)}/"
                    f"{PEER_SAMPLE_SIZE} peer — {interval} için veri bulunan ve "
                    f"dışlanmayan başka enstrüman yok; kapı bu daralmış "
                    f"örneklemle karar verecek."
                )
            # 365 calendar days ≈ 252 equity bars — too few for the _MIN_TRADES threshold; use 730.
            ms_days = 730
        else:
            other_symbols = [
                s for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT") if s != symbol
            ]
            ms_days = 365  # 180→365: more trades → statistical reliability
        pf(
            f"🌐 Multi-Symbol — the strategy is also being tested on {', '.join(other_symbols)}. "
            "Is it generalizable or specific only to this symbol?"
        )
        ms = _stage(
            "Multi-Symbol",
            run_multi_symbol,
            spec,
            primary_symbol=symbol,
            symbols=other_symbols,
            interval=interval,
            category=category,
            days=ms_days,
            progress_fn=pf,
            source=source,
            # Peer penceresi MÜHRÜN peşinden gitsin. `bars_df` çağırana göre
            # zaten mühürlenmiş (kırpılmış) eğitim çerçevesi; son barı vermek,
            # mühür kuralı ne zaman değişirse değişsin peer testinin onunla
            # birlikte geriye kaymasını sağlar — sabit bir tarih yazmak, bir
            # sonraki mühür değişikliğinde aynı sızıntıyı geri getirirdi.
            end_anchor=(bars_df.index[-1] if len(bars_df) else None),
        )
        pf(
            f"  → positive on {ms.get('symbols_positive', 0)}/{ms.get('symbols_valid', 0)} symbols · "
            f"{ms.get('generalization_label', '?')}"
        )

        # A definitive symbol-specific rejection already fixes the final gate
        # result. Do not spend minutes on IS/OOS + WFO for an outcome that cannot
        # change. Return explicit skipped sections so the audit log remains clear.
        if multi_symbol_definitive_failure(ms):
            reason = "skipped after definitive multi-symbol rejection"
            pf(f"  ⏭ {reason}: IS/OOS, Walk-Forward and Monte Carlo")
            return {
                "split": {"error": reason},
                "wfo_windows": [],
                "mc": {"error": reason},
                "multi_symbol": ms,
                "short_circuit": "multi_symbol",
            }

        # 2) IS/OOS Split
        pf(
            "📊 IS/OOS Split — 70% of data for training, 30% real OOS test. "
            "The OOS/IS Sharpe ratio measures overfitting (≥0.7 = robust)."
        )
        split = _stage(
            "IS/OOS",
            run_insample_oos_split,
            spec,
            bars_df,
            instrument,
            bar_type,
            venue,
            split_pct=0.7,
            progress_fn=pf,
        )
        sp = split or {}
        is_m = sp.get("in_sample_metrics") or {}
        oos_m = sp.get("oos_metrics") or {}
        pf(
            f"  IS result: PnL={is_m.get('pnl', 0):+.2f} · "
            f"Sharpe={is_m.get('sharpe', float('nan')):.2f} · "
            f"{is_m.get('n_trades', 0)} trade | "
            f"OOS result: PnL={oos_m.get('pnl', 0):+.2f} · "
            f"Sharpe={oos_m.get('sharpe', float('nan')):.2f} · "
            f"{oos_m.get('n_trades', 0)} trade"
        )
        pf(f"  → Overfitting score: {sp.get('overfitting_label', '?')}")

        # In strict AUTO, an explicit split rejection guarantees that the final
        # gate cannot pass. WFO and Monte Carlo cannot reverse that measured
        # failure, so avoid the remaining expensive work.
        if split_definitive_failure(split):
            reason = "skipped after definitive IS/OOS rejection"
            pf(f"  ⏭ {reason}: Walk-Forward and Monte Carlo")
            return {
                "split": split,
                "wfo_windows": [],
                "mc": {"error": reason},
                "multi_symbol": ms,
                "short_circuit": "is_oos",
            }

        # 3) Walk-Forward
        # Pencere ADAYIN HIZINDAN türetiliyor (bkz. wfo_window_months): sabit
        # 6/2/3 ay, günlük barda test penceresini ~42 bara indiriyordu ve
        # `WFO_MIN_TRADES` oraya asla düşmüyordu.
        _wf_train, _wf_test, _wf_step, _wf_note = wfo_window_months(
            bars_df, len(trades) if trades else None
        )
        if _wf_note:
            pf(f"  ⚠ {_wf_note}")
        # Açıklama, KARARIN ölçüsünü söylemeli. Eskiden "positive PnL" yazıyordu
        # ama kapı `wfo_verdict` ile AL-TUT'U GEÇEN pencereleri sayıyor; 22 yıllık
        # boğada ikisi sistematik ıraksıyor (ölçüldü: 37 kârlı / 28 alfalı, 60
        # pencere) ve operatör ekrandaki oranı yanlış ölçüte göre okuyordu.
        pf(
            f"📈 Walk-Forward — rolling-window OOS test. Each window has {_wf_train} "
            f"months training + {_wf_test} months test. "
            "≥50% of windows must beat buy&hold (positive excess return); "
            "merely profitable windows do not count."
        )
        wfo = _stage(
            "Walk-Forward",
            run_walk_forward,
            spec,
            bars_df,
            instrument,
            bar_type,
            venue,
            train_months=_wf_train,
            test_months=_wf_test,
            step_months=_wf_step,
            progress_fn=pf,
        )
        if wfo:
            # Reported on the SAME series the gate decides on (wfo_test): the
            # unchanged spec's OOS windows. Showing the re-optimized count next
            # to a naive verdict made the two disagree on screen.
            #
            # ...ve AYNI ÖLÇÜMLE: bu satır "PnL'i pozitif pencere" sayarken kapı
            # "al-tut'u geçen pencere" sayıyordu. Ölçüldü (koşu 8aa18365):
            # ekranda 37/60 (%62, çıtayı geçiyor), kapıda 28/60 (%47, düşüyor).
            # Artık ikisi de `wfo_verdict`'i okuyor; kârlılık sayısı TEŞHİS
            # olarak yanında duruyor, çünkü aradaki fark "boğayı mı taşıyor,
            # alfa mı üretiyor" sorusunun cevabı.
            v = wfo_verdict(wfo)
            valid_wfo = valid_wfo_windows(wfo)
            avg_pnl = (
                sum(wfo_test(w).get("pnl", 0) for w in valid_wfo) / len(valid_wfo)
                if valid_wfo
                else 0.0
            )
            pos_opt = sum(
                1 for w in wfo if (w.get("test_metrics") or {}).get("pnl", 0) > 0
            )
            pf(
                f"  → {v.display} valid windows beat buy&hold (saved spec) · "
                f"{v.pnl_positive}/{v.valid} merely profitable · "
                f"average test PnL={avg_pnl:+.2f} USDT · "
                f"{pos_opt}/{len(wfo)} when re-optimized per window (diagnostic)"
            )
            # RET GEREKÇESİ BURADA BASILMAZ, kapıda basılır. İki sebep:
            #
            # 1. Karar kapıda veriliyor; gerekçeyi kararın verildiği yerden
            #    yazmak bugün üç kez düzeltilen ilkenin ta kendisi.
            # 2. Buradaki çağrı `penalized` ipucunu VEREMİYOR — sönümlenmiş OOS
            #    Sharpe payload'ın toplu alanlarından (`oos_sharpe_naive_penalized`)
            #    çözülüyor ve o alanlar bu aşamada henüz yok. Yani bu noktadaki
            #    `ok`/`reason`, kapının vereceği hükümle Sharpe bacağında
            #    IRAKSAYABİLİR. Yalnız `display` güvenli: alfa oranı `penalized`e
            #    bağlı değil.
            #
            # Ölçülen belirti (koşu 568f2838, tur 1): aynı gerekçe iki kez
            # basılıyordu, çünkü satır hem burada hem kapıda ekliydi.

        # 4) Monte Carlo (already vectorized numpy — no pool needed)
        mc: dict = {"error": "No trade data."}
        if trades:
            pf(
                f"🎲 Monte Carlo — shuffles the sequence of {len(trades)} trades {300} times to "
                f"measure the luck factor. Median DD < {MC_DD_LIMIT:.0f}% is risky."
            )
            mc = run_monte_carlo(
                trades,
                n_sims=300,
                starting_cash=STARTING_CASH,
                progress_fn=pf,
            )
            if not mc.get("error"):
                pf(
                    f"  → Median final: ${mc.get('median_final', 0):,.0f} · "
                    f"p5 scenario: ${mc.get('p5_final', 0):,.0f} · "
                    f"Median max DD: {mc.get('max_dd_p50', 0):.1f}%"
                )
        else:
            pf("  ⚠ Monte Carlo skipped — no trades were opened in the backtest")

        return {"split": split, "wfo_windows": wfo, "mc": mc, "multi_symbol": ms}
    finally:
        if pool is not None:
            pool.shutdown()
        if snapshot_path is not None:
            _shutil.rmtree(Path(snapshot_path).parent, ignore_errors=True)
