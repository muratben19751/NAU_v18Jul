"""WFO genetik-algoritma çekirdeği: DETERMİNİZM sözleşmesi + sınır davranışları.

DeepR 2026-08-11 [YÜKSEK]: `wfo_optimizer.py:292-358` (`ga_plan`,
`ga_initial_population`, `_tournament_idx`, `ga_next_population`) ile onlara
girdi üreten `_current_values` / `_sample_dim` / `values_to_dict` testlerde HİÇ
geçmiyordu. Oysa docstring'ler açıkça test edilmesi gereken sözleşmeler yazıyor:

  * "same rng state → same population (determinism contract)"
  * "elit strict-greater ile seçilir (eşitlikte düşük indeks) — sıralı ve
    paralel yollar aynı kazananı üretir"
  * `ga_plan`: "GEN = max(1, round(n_samples/POP)), yarımı YUKARI yuvarlar
    (20/8 → 3)"; boş uzayda `(1, 1)`.

Bu sözleşmeler kırılırsa WFO sonuçları TEKRARLANAMAZ olur: aynı tohumla koşan
iki optimizasyon farklı parametre seçer, `regression_baseline.json` çıpaları
anlamını yitirir ve `test_parallel_exec.py`'nin WFO-seviyesindeki parite
testleri GA-içi ayrışmayı yakalayamaz (fark tek bir turnuva çekilişinde
gizlenebilir).

Testler saf fonksiyonlar üzerinde koşar — backtest çalıştırılmaz, kataloğa veya
studio.db'ye dokunulmaz.

Wiki References: [[webapp_module_map]], [[auto_arama_ekonomisi]], [[deepr_skill]]
"""

from __future__ import annotations

import numpy as np
import pytest

import wfo_optimizer as W
from composer_spec import ComposedStrategySpec, SignalBlock

SEED = 20260811


def _rng(seed: int = SEED):
    return np.random.default_rng(seed)


def _space() -> list[dict]:
    """İki blok parametresi (int) + bir strateji alanı (float) — gerçek
    `build_param_space` çıktısının şekli."""
    return [
        {
            "target": ("block", 0, "fast"),
            "type": "int",
            "lo": 2.0,
            "hi": 50.0,
            "default": 10,
            "label": "ma_cross[0].fast",
        },
        {
            "target": ("block", 0, "slow"),
            "type": "int",
            "lo": 10.0,
            "hi": 200.0,
            "default": 30,
            "label": "ma_cross[0].slow",
        },
        {
            "target": ("spec", "sl_value"),
            "type": "float",
            "lo": 0.5,
            "hi": 5.0,
            "default": 2.0,
            "label": "sl_value",
        },
    ]


def _spec() -> ComposedStrategySpec:
    return ComposedStrategySpec(
        id="ga",
        name="GA probe",
        description="",
        blocks=[
            SignalBlock(type="ma_cross", role="entry", params={"fast": 10, "slow": 30})
        ],
        use_bracket=True,
        sl_value=2.0,
    )


class _StubRng:
    """Turnuva çekilişini tam kontrol için sahte rng.

    `_tournament_idx` yalnızca `integers` çağırır; eşitlik kırma kuralını
    rastgeleliğe bırakmadan sınamak için çekilişi sabitliyoruz.
    """

    def __init__(self, draws):
        self._draws = list(draws)

    def integers(self, _lo, _hi, size=None):
        return np.array(self._draws[: (size if size is not None else len(self._draws))])


# ---------------------------------------------------------------------------
# ga_plan — bütçe eşlemesi
# ---------------------------------------------------------------------------


class TestGaPlan:
    def test_empty_space_collapses_to_a_single_run(self):
        """Aranacak boyut yoksa arama anlamsız → (1, 1).

        Eski davranış n_samples kez AYNI adayı koşturmaktı; bu, boş uzayda
        bütçenin tamamını çöpe atıyordu.
        """
        assert W.ga_plan([], 20) == (1, 1)
        assert W.ga_plan([], 0) == (1, 1)

    def test_docstring_example_rounds_half_up(self, monkeypatch):
        """Docstring çıpası: n_samples=20, POP=8 → GEN=3 (2.5 yukarı yuvarlanır)."""
        monkeypatch.setattr(W, "WFO_POP_SIZE", 8)
        assert W.ga_plan(_space(), 20) == (8, 3)

    @pytest.mark.parametrize(
        "n_samples,expected_gen",
        [
            (11, 1),  # 1.375 → aşağı
            (12, 2),  # 1.500 → tam yarım, YUKARI
            (13, 2),
            (20, 3),  # 2.5 → yukarı
        ],
    )
    def test_half_up_boundary(self, monkeypatch, n_samples, expected_gen):
        monkeypatch.setattr(W, "WFO_POP_SIZE", 8)
        assert W.ga_plan(_space(), n_samples)[1] == expected_gen

    @pytest.mark.parametrize("n_samples", [0, 1, -5])
    def test_generation_count_never_drops_below_one(self, monkeypatch, n_samples):
        """Sıfır nesil = hiç arama; bütçe ne kadar küçülürse küçülsün en az bir
        nesil koşar (aksi hâlde optimize_window boş sonuç döndürürdü)."""
        monkeypatch.setattr(W, "WFO_POP_SIZE", 8)
        assert W.ga_plan(_space(), n_samples)[1] == 1

    def test_population_never_drops_below_one(self, monkeypatch):
        monkeypatch.setattr(W, "WFO_POP_SIZE", 0)
        assert W.ga_plan(_space(), 20) == (1, 20)

    def test_constants_are_read_at_call_time(self, monkeypatch):
        """Env sabitleri monkeypatch'lenince plan ANINDA değişmeli.

        Modül yorumunun sözü ("functions read the values from the module global
        at call time"); erken bağlanma olsaydı testler ve env ayarları etkisiz
        kalırdı.
        """
        monkeypatch.setattr(W, "WFO_POP_SIZE", 4)
        assert W.ga_plan(_space(), 20) == (4, 5)
        monkeypatch.setattr(W, "WFO_POP_SIZE", 10)
        assert W.ga_plan(_space(), 20) == (10, 2)


# ---------------------------------------------------------------------------
# _sample_dim / _current_values / values_to_dict — GA'nın girdi-çıktı kenarları
# ---------------------------------------------------------------------------


class TestSampleDim:
    def test_int_dim_returns_a_python_int_within_inclusive_bounds(self):
        """`rng.integers(lo, hi + 1)` — üst sınır DAHİL.

        `+1` düşerse tarama uzayının en üst değeri hiç denenmez.
        """
        rng = _rng()
        dim = {"type": "int", "lo": 2.0, "hi": 5.0}
        seen = {W._sample_dim(rng, dim) for _ in range(300)}
        assert seen == {2, 3, 4, 5}
        assert all(isinstance(v, int) for v in seen)

    def test_float_dim_stays_within_bounds(self):
        rng = _rng()
        dim = {"type": "float", "lo": 0.5, "hi": 5.0}
        vals = [W._sample_dim(rng, dim) for _ in range(300)]
        assert all(isinstance(v, float) and 0.5 <= v <= 5.0 for v in vals)

    def test_degenerate_int_dim_yields_its_only_value(self):
        """lo == hi (tek aday) çökmemeli, hep aynı değeri vermeli."""
        rng = _rng()
        dim = {"type": "int", "lo": 3.0, "hi": 3.0}
        assert {W._sample_dim(rng, dim) for _ in range(20)} == {3}

    def test_same_seed_same_draw(self):
        dim = {"type": "float", "lo": 0.5, "hi": 5.0}
        a = [W._sample_dim(_rng(), dim) for _ in range(5)]
        b = [W._sample_dim(_rng(), dim) for _ in range(5)]
        assert a == b


class TestCurrentValues:
    def test_reads_block_params_and_spec_fields(self):
        assert W._current_values(_spec(), _space()) == [10, 30, 2.0]

    def test_missing_block_param_falls_back_to_the_dim_default(self):
        """Spec'te olmayan bir parametre GA'yı çökertmez; uzayın varsayılanı
        kullanılır — yeni bir blok parametresi eklendiğinde eski spec'ler
        optimize edilmeye devam eder."""
        spec = _spec()
        del spec.blocks[0].params["fast"]
        assert W._current_values(spec, _space())[0] == 10

    def test_missing_spec_field_falls_back_to_the_dim_default(self):
        space = [
            {
                "target": ("spec", "not_a_field"),
                "type": "float",
                "lo": 0.0,
                "hi": 1.0,
                "default": 0.42,
                "label": "phantom",
            }
        ]
        assert W._current_values(_spec(), space) == [0.42]

    def test_empty_space_yields_no_values(self):
        assert W._current_values(_spec(), []) == []


class TestValuesToDict:
    def test_int_dims_are_rounded_and_floats_kept_to_four_decimals(self):
        out = W.values_to_dict(_space(), [12.6, 30.4, 1.234567])
        assert out == {
            "ma_cross[0].fast": 13,
            "ma_cross[0].slow": 30,
            "sl_value": 1.2346,
        }
        assert isinstance(out["ma_cross[0].fast"], int)

    def test_empty_space_yields_an_empty_report(self):
        assert W.values_to_dict([], []) == {}


# ---------------------------------------------------------------------------
# ga_initial_population — 0. nesil
# ---------------------------------------------------------------------------


class TestGaInitialPopulation:
    def test_individual_zero_is_the_specs_current_values(self):
        """Mevcut spec her zaman yarışa girer: GA en kötü ihtimalle bugünkü
        parametreleri seçer, onları KAYBETMEZ."""
        pop = W.ga_initial_population(_spec(), _space(), _rng(), 6)
        assert pop[0] == W._current_values(_spec(), _space())

    def test_population_has_exactly_pop_size_individuals(self):
        assert len(W.ga_initial_population(_spec(), _space(), _rng(), 6)) == 6

    @pytest.mark.parametrize("pop_size", [1, 0, -3])
    def test_degenerate_pop_size_still_yields_the_current_candidate(self, pop_size):
        """pop_size <= 1 çökmemeli: `max(0, pop_size - 1)` rastgele birey üretir,
        mevcut aday her hâlükârda listede kalır."""
        pop = W.ga_initial_population(_spec(), _space(), _rng(), pop_size)
        assert pop == [W._current_values(_spec(), _space())]

    def test_same_seed_produces_an_identical_population(self):
        """DETERMİNİZM SÖZLEŞMESİ (docstring): aynı rng durumu → aynı popülasyon."""
        a = W.ga_initial_population(_spec(), _space(), _rng(), 8)
        b = W.ga_initial_population(_spec(), _space(), _rng(), 8)
        assert a == b

    def test_a_different_seed_produces_a_different_population(self):
        """Determinizm testi tek başına yeterli değil: rng GERÇEKTEN tüketiliyor
        olmalı, yoksa 'aynı tohum aynı sonuç' sabit bir çıktıyla da sağlanır."""
        a = W.ga_initial_population(_spec(), _space(), _rng(SEED), 8)
        b = W.ga_initial_population(_spec(), _space(), _rng(SEED + 1), 8)
        assert a[1:] != b[1:]

    def test_random_individuals_respect_dim_bounds_and_types(self):
        pop = W.ga_initial_population(_spec(), _space(), _rng(), 30)
        for individual in pop[1:]:
            fast, slow, sl = individual
            assert isinstance(fast, int) and 2 <= fast <= 50
            assert isinstance(slow, int) and 10 <= slow <= 200
            assert isinstance(sl, float) and 0.5 <= sl <= 5.0

    def test_empty_space_yields_a_single_empty_individual(self):
        """Dejenere uzay: her birey boş vektör, çökme yok."""
        pop = W.ga_initial_population(_spec(), [], _rng(), 4)
        assert pop == [[], [], [], []]


# ---------------------------------------------------------------------------
# _tournament_idx — seçim ve eşitlik kırma
# ---------------------------------------------------------------------------


class TestTournamentSelection:
    def test_highest_score_among_the_drawn_wins(self):
        scores = [0.0, 5.0, 1.0, -2.0]
        assert W._tournament_idx(_StubRng([3, 1, 2]), scores) == 1

    def test_ties_are_broken_by_the_lower_index(self):
        """Eşitlikte DÜŞÜK indeks kazanır — sıralı ve paralel yolların aynı
        kazananı üretmesinin tek garantisi bu kural (docstring)."""
        scores = [7.0, 7.0, 7.0, 0.0]
        assert W._tournament_idx(_StubRng([2, 0, 1]), scores) == 0
        assert W._tournament_idx(_StubRng([2, 1]), scores) == 1

    def test_all_failed_candidates_still_select_deterministically(self):
        """Tüm skorlar -inf (her aday hata/az işlem) — eşitlik kuralı devrede,
        en düşük çekilen indeks kazanır; rastgele bir kazanan seçilmez."""
        scores = [float("-inf")] * 4
        assert W._tournament_idx(_StubRng([3, 1, 2]), scores) == 1

    def test_tournament_size_is_capped_by_the_population(self):
        """k > n olduğunda `size=min(k, n)`: popülasyondan fazla çekiş yapılmaz."""
        drawn = []

        class _Recorder:
            def integers(self, _lo, _hi, size=None):
                drawn.append(size)
                return np.array([0, 1])

        W._tournament_idx(_Recorder(), [1.0, 2.0], k=5)
        assert drawn == [2]

    def test_same_seed_selects_the_same_index(self):
        scores = [0.1, 0.9, 0.5, 0.2, 0.7]
        a = [W._tournament_idx(_rng(), scores) for _ in range(3)]
        b = [W._tournament_idx(_rng(), scores) for _ in range(3)]
        assert a == b


# ---------------------------------------------------------------------------
# ga_next_population — elitizm + çaprazlama + mutasyon
# ---------------------------------------------------------------------------


class TestGaNextPopulation:
    def _pop(self):
        return [[10, 30, 2.0], [20, 60, 3.0], [30, 90, 4.0], [40, 120, 1.0]]

    def test_elite_is_the_highest_scoring_individual(self):
        pop = self._pop()
        nxt = W.ga_next_population(_rng(), _space(), pop, [0.1, 0.9, 0.5, 0.2], 4)
        assert nxt[0] == pop[1]

    def test_elite_ties_are_broken_by_the_lower_index(self):
        """`key=(scores[i], -i)` → eşit skorda küçük indeks. Paralel/sıralı
        yolların aynı eliti taşımasının garantisi."""
        pop = self._pop()
        nxt = W.ga_next_population(_rng(), _space(), pop, [0.9, 0.9, 0.9, 0.2], 4)
        assert nxt[0] == pop[0]

    def test_elite_is_copied_not_aliased(self):
        """Elit `list(...)` ile kopyalanır; yeni nesildeki mutasyon eski
        popülasyonun kaydını geriye dönük bozamaz."""
        pop = self._pop()
        nxt = W.ga_next_population(_rng(), _space(), pop, [0.1, 0.9, 0.5, 0.2], 4)
        assert nxt[0] is not pop[1]
        nxt[0][0] = 999
        assert pop[1][0] == 20

    def test_population_size_is_honored(self):
        for size in (1, 2, 7):
            nxt = W.ga_next_population(
                _rng(), _space(), self._pop(), [0.1, 0.9, 0.5, 0.2], size
            )
            assert len(nxt) == size

    def test_pop_size_one_returns_only_the_elite(self):
        """Dejenere bütçe: tek bireylik nesil sonsuz döngüye girmez, elit döner."""
        pop = self._pop()
        assert W.ga_next_population(_rng(), _space(), pop, [0.1, 0.9, 0.5, 0.2], 1) == [
            pop[1]
        ]

    def test_same_seed_produces_an_identical_generation(self):
        """DETERMİNİZM SÖZLEŞMESİ: tüm rastgelelik `rng`den gelir."""
        args = (_space(), self._pop(), [0.1, 0.9, 0.5, 0.2], 6)
        assert W.ga_next_population(_rng(), *args) == W.ga_next_population(
            _rng(), *args
        )

    def test_a_different_seed_produces_different_children(self):
        args = (_space(), self._pop(), [0.1, 0.9, 0.5, 0.2], 6)
        a = W.ga_next_population(_rng(SEED), *args)
        b = W.ga_next_population(_rng(SEED + 1), *args)
        assert a[1:] != b[1:], "rng tüketilmiyor — determinizm testi anlamsızlaşır"

    def test_children_are_clamped_to_the_dimension_bounds(self):
        """Mutasyon ne kadar büyük olursa olsun çocuk [lo, hi] dışına çıkmaz.

        Sınır dışı bir değer `mutate_spec` üzerinden Nautilus'a geçer ve
        backtest ANCAK koştuktan sonra patlar (ör. period=-4).
        """
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(W, "GA_MUT_SIGMA", 1000.0)  # devasa mutasyon
            nxt = W.ga_next_population(
                _rng(), _space(), self._pop(), [0.1, 0.9, 0.5, 0.2], 40
            )
        for fast, slow, sl in nxt[1:]:
            assert 2 <= fast <= 50 and isinstance(fast, int)
            assert 10 <= slow <= 200 and isinstance(slow, int)
            assert 0.5 <= sl <= 5.0
        # Devasa sigma altında sınırların İKİSİ de gerçekten görülmeli —
        # aksi hâlde clamp testi tek yönlü kalırdı.
        fasts = {ind[0] for ind in nxt[1:]}
        assert {2, 50} <= fasts

    def test_int_dimensions_stay_integral_after_mutation(self):
        nxt = W.ga_next_population(
            _rng(), _space(), self._pop(), [0.1, 0.9, 0.5, 0.2], 12
        )
        assert all(isinstance(ind[0], int) and isinstance(ind[1], int) for ind in nxt)

    def test_empty_space_yields_empty_children_without_hanging(self):
        """Dejenere uzayda çaprazlama döngüsü boş vektör üretir ve sonlanır."""
        nxt = W.ga_next_population(_rng(), [], [[]], [0.0], 3)
        assert nxt == [[], [], []]

    def test_all_minus_inf_scores_still_produce_a_generation(self):
        """Her aday başarısız olduğunda GA durmaz: elit yine deterministik
        (en düşük indeks) ve yeni nesil üretilir."""
        pop = self._pop()
        scores = [float("-inf")] * 4
        nxt = W.ga_next_population(_rng(), _space(), pop, scores, 4)
        assert nxt[0] == pop[0]
        assert len(nxt) == 4


# ---------------------------------------------------------------------------
# Uçtan uca: çok nesilli arama tekrarlanabilir mi?
# ---------------------------------------------------------------------------


def _evolve(seed: int, generations: int = 4, pop_size: int = 6) -> list[list]:
    """`optimize_window`'un GA döngüsünün saf (backtest'siz) ikizi.

    Skor, adayın kendisinden deterministik olarak türetilir; böylece test
    yalnızca GA'nın rastgelelik/seçim davranışını ölçer, backtest motorunu değil.
    """
    space, spec = _space(), _spec()
    rng = np.random.default_rng(seed)
    population = W.ga_initial_population(spec, space, rng, pop_size)
    for _ in range(generations):
        scores = [-abs(ind[0] - 21) - abs(ind[1] - 55) for ind in population]
        population = W.ga_next_population(rng, space, population, scores, pop_size)
    return population


class TestMultiGenerationReproducibility:
    def test_same_seed_reproduces_the_whole_search(self):
        """Tohum → sonuç eşlemesi bire bir.

        Bu kırılırsa dün yeşil olan bir WFO koşumu bugün başka parametre seçer
        ve `regression_baseline.json` çıpaları anlamını yitirir.
        """
        assert _evolve(SEED) == _evolve(SEED)

    def test_different_seeds_diverge(self):
        assert _evolve(SEED) != _evolve(SEED + 1)

    def test_the_search_converges_toward_the_optimum(self):
        """GA gerçekten SEÇİYOR mu? Deterministik olup rastgele dolaşan bir
        uygulama da yukarıdaki iki testi geçerdi; bu test seçilim baskısının
        var olduğunu gösterir."""
        space = _space()
        rng = np.random.default_rng(SEED)
        pop = W.ga_initial_population(_spec(), space, rng, 8)

        def _best(population):
            return max(-abs(i[0] - 21) - abs(i[1] - 55) for i in population)

        start = _best(pop)
        for _ in range(12):
            scores = [-abs(i[0] - 21) - abs(i[1] - 55) for i in pop]
            pop = W.ga_next_population(rng, space, pop, scores, 8)
        assert _best(pop) > start

    def test_the_elite_score_never_regresses(self):
        """Elitizm(1) sözleşmesi: en iyi skor nesiller boyunca düşemez."""
        space = _space()
        rng = np.random.default_rng(SEED)
        pop = W.ga_initial_population(_spec(), space, rng, 8)
        best_so_far = None
        for _ in range(10):
            scores = [-abs(i[0] - 21) - abs(i[1] - 55) for i in pop]
            current = max(scores)
            if best_so_far is not None:
                assert current >= best_so_far, "elit kaybedildi"
            best_so_far = current
            pop = W.ga_next_population(rng, space, pop, scores, 8)
