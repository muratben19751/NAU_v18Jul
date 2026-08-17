"""`pass_rate`'in paydası kaç BAĞIMSIZ gözleme denk geliyor.

Oran, paydadaki sembollerin bağımsız olduğunu varsayar. Aynı faktöre binen
semboller tek gözlemi birkaç kez saymaktır ve bu hem GEÇME hem RET kararını
olduğundan güçlü gösterir — koşu 755b7880 tur 2'de iki aday `1/5 · %20` ile
`✗ Symbol specific` damgası yedi, oysa payda fiilen ~2,3 bağımsız gözlemdi.

Ölçüm (testin kendi penceresi, son 730 gün / 502 işlem günü, 1-DAY):

    SPY · IWM · AAPL · MSFT · NVDA   nominal 5 → etkin 2,32 · PC1 %60
    QQQ eklenince 6'lı sepet 2,17'ye DÜŞÜYOR (QQQ↔SPY = 0,95)

Buradaki testler sentetik seriler kullanır: bağımsızlık iddiası ölçülebilir bir
matematiksel özellik, piyasa verisine bağlanırsa test veri kataloğunun hâline
bağımlı olur ve asıl iddiayı sınamaz.

Wiki References
---------------
See: [[multi_symbol_generalization]], [[nau_auto_kosusu_755b7880_2026_08_17]]
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest_robustness import effective_symbol_count


def _series(n: int, *, seed: int, shared=None, w: float = 0.0):
    """Kendi gürültüsü + (isteğe bağlı) ortak faktörden pay taşıyan fiyat serisi."""
    rng = np.random.default_rng(seed)
    own = rng.normal(0, 0.01, n)
    r = own if shared is None else w * shared + (1 - w) * own
    return pd.Series(100.0 * np.exp(np.cumsum(r)))


class TestItCountsIndependence:
    def test_independent_symbols_score_near_nominal(self):
        """Bağımsız seriler → etkin sayı nominale yaklaşmalı."""
        closes = {f"S{i}": _series(400, seed=i) for i in range(5)}

        n = effective_symbol_count(closes)

        assert n is not None
        assert n > 4.0, f"bağımsız 5 sembol {n} çıktı — ölçü bağımsızlığı görmüyor"

    def test_identical_symbols_collapse_to_one(self):
        """Aynı seriyi beş kez saymak bir gözlemdir."""
        base = _series(400, seed=7)
        closes = {f"S{i}": base.copy() for i in range(5)}

        n = effective_symbol_count(closes)

        assert n is not None
        assert n == pytest.approx(1.0, abs=0.05)

    def test_a_shared_factor_lands_in_between(self):
        """Ortak faktör payı arttıkça etkin sayı DÜŞMELİ — yön testi."""
        rng = np.random.default_rng(99)
        shared = rng.normal(0, 0.01, 400)
        weak = {f"S{i}": _series(400, seed=i, shared=shared, w=0.2) for i in range(5)}
        strong = {f"S{i}": _series(400, seed=i, shared=shared, w=0.8) for i in range(5)}

        n_weak = effective_symbol_count(weak)
        n_strong = effective_symbol_count(strong)

        assert n_strong < n_weak
        assert 1.0 <= n_strong <= 5.0 and 1.0 <= n_weak <= 5.0

    def test_a_redundant_addition_can_lower_the_count(self):
        """Ölçülen ters sonuç: sepete KOPYA eklemek bağımsızlığı düşürür.

        QQQ↔SPY korelasyonu 0,95; "peer ekleyelim" hamlesi paydayı büyütür,
        kanıtı büyütmez. Nominal artarken etkin düşebiliyor — sayının kendisi
        bunu göstermezse kimse fark etmez.
        """
        base = {f"S{i}": _series(400, seed=i) for i in range(3)}
        with_twin = {**base, "S0_twin": base["S0"].copy()}

        n_base = effective_symbol_count(base)
        n_twin = effective_symbol_count(with_twin)

        assert n_twin < n_base, "kopya eklemek etkin sayıyı düşürmeliydi"


class TestItRefusesToGuess:
    @pytest.mark.parametrize(
        "closes",
        [
            {},
            {"S0": _series(400, seed=1)},
            {"S0": _series(10, seed=1), "S1": _series(10, seed=2)},
            {"S0": None, "S1": None},
        ],
    )
    def test_unmeasurable_returns_none_not_a_number(self, closes):
        """Uydurulmuş bağımsızlık, ölçülmemiş olandan kötüdür.

        Özellikle 0 ya da nominal DÖNMEMELİ: ikisi de aşağı akışta gerçek bir
        ölçüm gibi okunur.
        """
        assert effective_symbol_count(closes) is None

    def test_a_constant_series_does_not_raise(self):
        """Sıfır varyans → korelasyon NaN. Teşhis bir koşuyu düşürmemeli."""
        closes = {
            "S0": pd.Series([100.0] * 400),
            "S1": _series(400, seed=3),
        }

        assert effective_symbol_count(closes) is None


class TestItIsDiagnosticOnly:
    def test_the_result_dict_carries_it_next_to_pass_rate(self):
        """Sayı `pass_rate`'in YANINDA durmalı — kararın yerine değil."""
        import inspect

        import backtest_robustness as br

        src = inspect.getsource(br.run_multi_symbol)

        assert '"effective_symbols"' in src
        assert '"pass_rate"' in src

    def test_the_label_is_decided_without_it(self):
        """Etiket eşikleri yalnız pass_rate okur; etkin sayı karara girmez.

        Girdiği an kapı sessizce değişir ve "teşhis ekledim" diyerek kapıyı
        oynatmış olurum — ölçmek için ölçtüğünü bozmak.
        """
        import inspect

        import backtest_robustness as br

        src = inspect.getsource(br.run_multi_symbol)
        decision = src[src.index("if n_valid == 0:") : src.index("sharpes = [")]

        assert "effective" not in decision, (
            "etiket kararı etkin sembol sayısını okuyor — bu bir teşhis olmalıydı"
        )
