"""D set regressions: H8 runtime injection, M25 loop budget,
M27/M33 indicators.py integration + new builtin blocks, L14 bollinger mode,
L25 block isolation.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

MATH_BLOCK = """\
def max_lookback(params):
    return 30

def evaluate(state, block, closes, indicators, portfolio):
    if len(closes) < 25:
        return None
    m = statistics.mean(closes[-20:])
    s = math.sqrt(max(0.0, closes[-1]))
    if closes[-1] > m and s > 0:
        return "long"
    return None
"""

IND_BLOCK = """\
def max_lookback(params):
    return 60

def evaluate(state, block, closes, indicators, portfolio):
    if len(closes) < 40:
        return None
    rsi = ind.calc_rsi(closes, 14)
    if rsi < 40:
        return "long"
    if rsi > 60:
        return "short"
    return None
"""

LOOP_BLOCK = """\
def max_lookback(params):
    return 10

def evaluate(state, block, closes, indicators, portfolio):
    x = 0
    while x >= 0:
        x = x + 1
    return None
"""

# Infinite loop inside a HELPER — deep-review fix: the budget reset must only
# be at the start of evaluate(). Previously it was placed at the start of EVERY
# function, so a helper called from inside a loop refreshed the budget on every
# iteration and renewed the backstop (the budget never went <0 → the worker hung).
HELPER_LOOP_BLOCK = """\
def max_lookback(params):
    return 10

def spin(n):
    x = 0
    while x >= 0:
        x = x + 1
    return x

def evaluate(state, block, closes, indicators, portfolio):
    return spin(1)
"""


def _load(tmp_path: Path, name: str, src: str):
    from composer import _load_module_from_path

    p = tmp_path / f"{name}.py"
    p.write_text(src, encoding="utf-8")
    return _load_module_from_path(name, p)


CLOSES = [100.0 + i * 0.1 for i in range(300)]
INDICATORS = {
    "volumes": [1000.0] * 300,
    "highs": [c + 0.5 for c in CLOSES],
    "lows": [c - 0.5 for c in CLOSES],
}

# ── Depo fixture'ı ────────────────────────────────────────────────────────
# Aşağıdaki üç test eskiden geliştiricinin GERÇEK
# ~/.cache/nautilus_web_app/custom_blocks deposunu kullanıyordu (DeepR
# 2026-08-11 [ORTA], test hijyeni): biri oraya `.py` + registry kaydı YAZIYOR,
# ikisi de deponun İÇERİĞİNE göre farklı davranıyordu. Sonuç: temiz bir
# makinede/CI'da `assert checked >= 1` skip değil FAIL veriyor, dolu bir
# makinede ise her geliştiricide farklı sayıda ve tamamen keyfi (LLM üretimi)
# kod exec ediliyordu — yani testler deterministik değildi ve gerçek veriyi
# kirletiyordu. Depoyu tmp_path'e yönlendirip temsilci blokları repo içinde
# sabitlemek testin NİYETİNİ korur ("gerçek blok şekilleri hâlâ çalışıyor mu")
# ama sonucunu makineden bağımsız kılar. Aynı monkeypatch deseni üç kardeş
# testte zaten var (test_ohlc_blocks.py:41, test_volume_blocks.py:159,
# test_perf_equivalence.py:199).

# statistics.stdev kullanan, depodaki gerçek "volatilite filtresi" bloklarının
# temsilcisi.
STATS_BLOCK = """\
def max_lookback(params):
    return 40

def evaluate(state, block, closes, indicators, portfolio):
    if len(closes) < 30:
        return None
    sd = statistics.stdev(closes[-30:])
    if sd <= 0:
        return None
    return "long" if closes[-1] > closes[-30] else "short"
"""

# Kod-üretim tavanına (codegate `**`/`<<` denetimi) en çok yaklaşan gerçek
# şekiller: 9999 sentinel, varyans terimi, stdev üssü, epsilon.
SENTINEL_BLOCK = """\
def max_lookback(params):
    return 20

def evaluate(state, block, closes, indicators, portfolio):
    best = 9999.0
    var = (closes[-1] - closes[-2]) ** 2
    root = closes[-1] ** 0.5
    eps = 1e-9 + root
    if var < best and eps > 0:
        return None
    return None
"""

# {isim: (kaynak, meta)} — depoya tohumlanan temsilciler.
_STORE_FIXTURE_BLOCKS = {
    "fx_math_block": (MATH_BLOCK, {"label": "fx math", "params": {}}),
    "fx_stats_block": (STATS_BLOCK, {"label": "fx stats", "params": {}}),
    # math/statistics KULLANMAYAN blok: math testinin filtresinin hâlâ
    # ayıkladığını kanıtlar (aksi hâlde `checked` sayısı sessizce şişerdi).
    "fx_ind_block": (
        IND_BLOCK,
        {"label": "fx ind", "params": {"period": {"type": "int", "default": 14}}},
    ),
    "fx_sentinel_block": (SENTINEL_BLOCK, {"label": "fx sentinel", "params": {}}),
}


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """`custom_block_store`'un kökünü tmp_path'e yönlendirir ve döndürür.

    Testler artık ne geliştiricinin deposunu okur ne de ona yazar.
    """
    import custom_block_store as cbs

    store = tmp_path / "custom_blocks"
    store.mkdir()
    monkeypatch.setattr(cbs, "STORE_DIR", store)
    monkeypatch.setattr(cbs, "REGISTRY_FILE", store / "registry.json")
    return cbs


@pytest.fixture
def seeded_store(isolated_store):
    """İzole depoyu temsilci bloklarla doldurur (bkz. `_STORE_FIXTURE_BLOCKS`)."""
    for name, (src, meta) in _STORE_FIXTURE_BLOCKS.items():
        isolated_store.save_custom(name, meta, src)
    return isolated_store


def test_store_fixture_never_points_at_the_real_cache(seeded_store, tmp_path):
    """Çıpa: fixture'ın monkeypatch'i düşerse bu test kırmızıya döner.

    Yoksa aşağıdaki testler sessizce yeniden geliştiricinin gerçek
    `~/.cache/nautilus_web_app/custom_blocks` deposuna yazmaya başlar ve
    kimse fark etmez — bu düzeltmenin ilk sebebi tam olarak buydu.
    """
    real = Path.home() / ".cache" / "nautilus_web_app" / "custom_blocks"
    assert seeded_store.STORE_DIR != real
    assert seeded_store.REGISTRY_FILE.parent != real
    assert tmp_path in seeded_store.STORE_DIR.parents
    # Tohumlama gerçekten tmp'ye düştü mü (boş depo üzerinde "geçti" demesin).
    assert seeded_store.module_path("fx_math_block").exists()
    assert len(seeded_store.list_custom()) == len(_STORE_FIXTURE_BLOCKS)


class TestRuntimeInjection:
    """H8: the loader must do the same injection as the smoke environment."""

    def test_math_statistics_available_at_runtime(self, tmp_path):
        mod = _load(tmp_path, "t_math", MATH_BLOCK)
        # Previously: NameError: name 'math' is not defined (silent no-op).
        out = mod.evaluate({}, SimpleNamespace(params={}), CLOSES, INDICATORS, None)
        assert out in (None, "long", "short", "exit")

    def test_ind_library_available_at_runtime(self, tmp_path):
        mod = _load(tmp_path, "t_ind", IND_BLOCK)
        out = mod.evaluate({}, SimpleNamespace(params={}), CLOSES, INDICATORS, None)
        assert out in (None, "long", "short", "exit")

    def test_existing_catalog_math_blocks_no_nameerror(self, seeded_store):
        """Depodaki math/statistics kullanan bloklar NameError atmadan çalışmalı.

        Depo izole + tohumlu (bkz. `seeded_store`): eskiden geliştiricinin
        gerçek deposunu geziyordu, yani testin ne doğruladığı makineye göre
        değişiyordu. Sayı artık kesin: dört temsilciden math/statistics
        kullanan İKİSİ koşar, `ind.` kullanan biri filtreyle elenir.
        """
        cbs = seeded_store
        from composer import _load_module_from_path

        checked = 0
        for info in cbs.list_custom():
            name = info["name"]
            path = cbs.module_path(name)
            try:
                src = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if "math." not in src and "statistics." not in src:
                continue
            mod = _load_module_from_path(name, path)
            defaults = {
                k: v.get("default")
                for k, v in (info["meta"].get("params") or {}).items()
                if isinstance(v, dict)
            }
            out = mod.evaluate(
                {}, SimpleNamespace(params=defaults), CLOSES, dict(INDICATORS), None
            )
            assert out in (None, "long", "short", "exit"), name
            checked += 1
        # Kesin sayı: filtre bozulup `ind.` bloğunu da içeri alırsa ya da
        # tohumlama sessizce boş kalırsa test kırmızıya döner.
        assert checked == 2, f"beklenen 2 math/statistics bloğu, bulunan {checked}"


class TestLoopBudget:
    """M25: an infinite loop must raise RuntimeError instead of leaking a thread."""

    def test_infinite_loop_trips_budget(self, tmp_path):
        mod = _load(tmp_path, "t_loop", LOOP_BLOCK)
        with pytest.raises(RuntimeError, match="loop budget exceeded"):
            mod.evaluate({}, SimpleNamespace(params={}), CLOSES, INDICATORS, None)

    def test_infinite_loop_in_helper_trips_budget(self, tmp_path):
        # The budget reset is only at the start of evaluate() — an infinite loop
        # inside a helper must consume the shared budget and be caught (the helper
        # does NOT reset on each call).
        mod = _load(tmp_path, "t_helper_loop", HELPER_LOOP_BLOCK)
        with pytest.raises(RuntimeError, match="loop budget exceeded"):
            mod.evaluate({}, SimpleNamespace(params={}), CLOSES, INDICATORS, None)

    def test_smoke_rejects_infinite_loop(self):
        from agent import _test_execute_generated
        from codegate import GeneratedCodeError

        with pytest.raises(GeneratedCodeError):
            _test_execute_generated(LOOP_BLOCK)

    def test_smoke_requires_max_lookback_for_new_blocks(self):
        from agent import _test_execute_generated
        from codegate import GeneratedCodeError

        src = (
            "def evaluate(state, block, closes, indicators, portfolio):\n"
            "    return None\n"
        )
        # Old blocks (require=False) pass…
        _test_execute_generated(src)
        # …mandatory in new generation (M16).
        with pytest.raises(GeneratedCodeError, match="max_lookback"):
            _test_execute_generated(src, require_max_lookback=True)


class TestCostGate:
    """The COST axis of the allowlist: work that no downstream guard can stop.

    The loop budget only instruments While/For/comprehensions, and the smoke
    harness's `join(timeout=2.0)` cannot preempt a thread holding the GIL inside
    one bytecode — so anything expensive without a loop node has to be refused
    statically, at validation time.
    """

    @staticmethod
    def _src(body: str) -> str:
        return (
            "def max_lookback(params):\n"
            "    return 10\n"
            "def evaluate(state, block, closes, indicators, portfolio):\n"
            f"{body}"
            "    return None\n"
        )

    @pytest.mark.parametrize(
        "body",
        [
            "    x = 9 ** 9 ** 9\n",  # the classic: exponent is itself a Pow
            "    x = 3 ** (3 * 10 ** 7 + len(closes))\n",  # computed exponent
            "    x = closes[-1] ** len(closes)\n",  # variable exponent
            "    x = 2 ** 65\n",  # literal, over MAX_POW_EXPONENT
        ],
    )
    def test_pow_bomb_rejected(self, body):
        from codegate import GeneratedCodeError, validate_generated_code

        with pytest.raises(GeneratedCodeError, match="exponent"):
            validate_generated_code(self._src(body))

    @pytest.mark.parametrize(
        "body",
        [
            # Every `**` here has the literal exponent 64, so the exponent rule
            # alone lets it through — the VALUE is what explodes (10**262144,
            # and 64x more per extra level).
            "    x = ((10 ** 64) ** 64) ** 64\n",
            # No loop node to instrument, one C-level call, ~8GB.
            "    x = list(range(10 ** 9))\n",
            "    x = sum(range(10 ** 9))\n",
            "    x = [0] * 10 ** 9\n",
            "    x = 1e9 * 1e9\n",
        ],
    )
    def test_oversized_literal_rejected(self, body):
        from codegate import GeneratedCodeError, validate_generated_code

        with pytest.raises(GeneratedCodeError, match="literal value"):
            validate_generated_code(self._src(body))

    @pytest.mark.parametrize(
        "body",
        [
            # DeepR 2026-08-09 [ORTA]: Pow had a variable-exponent guard,
            # LShift did not — same DoS shape, one uninterruptible operation.
            "    x = closes[-1] << len(closes)\n",  # variable shift amount
            "    x = 1 << (3 * 10 ** 7 + len(closes))\n",  # computed shift amount
            "    x = 1 << 65\n",  # literal, over MAX_SHIFT_AMOUNT
            # _fold_literal treats bitwise ops as "cannot grow" (true for
            # BitAnd/BitOr/BitXor/RShift, false for LShift) — without
            # _check_lshift this fully-literal bomb reaches the `<<`
            # computation itself before any ceiling can reject it.
            "    x = 1 << 999999999\n",
        ],
    )
    def test_lshift_bomb_rejected(self, body):
        from codegate import GeneratedCodeError, validate_generated_code

        with pytest.raises(GeneratedCodeError, match="shift amount"):
            validate_generated_code(self._src(body))

    def test_lshift_bomb_validation_is_itself_cheap(self):
        """Mirrors test_validation_of_a_bomb_is_itself_cheap for **: rejecting
        the shift amount happens before `1 << 999999999` is ever computed."""
        import time

        from codegate import GeneratedCodeError, validate_generated_code

        src = self._src("    x = 1 << 999999999\n")
        t0 = time.perf_counter()
        with pytest.raises(GeneratedCodeError):
            validate_generated_code(src)
        assert time.perf_counter() - t0 < 1.0

    def test_small_literal_lshift_still_passes(self):
        from codegate import validate_generated_code

        validate_generated_code(self._src("    x = 1 << 4\n"))  # e.g. a bit flag

    @pytest.mark.parametrize(
        "body",
        [
            # DeepR 2026-08-10 [KRİTİK]: `**`/`<<` maliyet denetimi YALNIZCA
            # ast.BinOp düğümünde çalışıyordu. `x **= y` ise ast.AugAssign —
            # BinOp DEĞİL — yani aynı işlemin ikinci sözdizimi hiçbir kuraldan
            # geçmiyordu. Döngü düğümü olmadığı için loop-budget hiç tick
            # atmıyor, smoke'un join(timeout=2.0)'ı GIL'i tutan tek bytecode'u
            # kesemiyor: blok önizleme yolu (web/routes/strategy.py:246, :893)
            # web-server sürecinin İÇİNDE exec ettiği için tek satır tüm
            # uygulamayı dondurabiliyordu.
            "    x = 2\n    x **= 999999\n",  # sabit üs, tavanın üstünde
            "    x = 2\n    x **= 65\n",  # sabit üs, tavanın 1 üstünde
            "    x = 2\n    x **= len(closes)\n",  # değişken üs: sınırsız
            "    x = 2\n    x **= 3 * 10 ** 5 + len(closes)\n",  # hesaplanmış üs
        ],
    )
    def test_augassign_pow_bomb_rejected(self, body):
        from codegate import GeneratedCodeError, validate_generated_code

        with pytest.raises(GeneratedCodeError, match="exponent"):
            validate_generated_code(self._src(body))

    @pytest.mark.parametrize(
        "body",
        [
            # `<<=` de aynı boşluktan geçiyordu; `1 << 999999` tek başına
            # ~300k basamaklı sayı üretir (bkz. _check_lshift docstring'i).
            "    x = 2\n    x <<= 999999\n",
            "    x = 2\n    x <<= 65\n",
            "    x = 2\n    x <<= len(closes)\n",
            "    x = 2\n    x <<= 3 * 10 ** 5 + len(closes)\n",
        ],
    )
    def test_augassign_lshift_bomb_rejected(self, body):
        from codegate import GeneratedCodeError, validate_generated_code

        with pytest.raises(GeneratedCodeError, match="shift amount"):
            validate_generated_code(self._src(body))

    @pytest.mark.parametrize(
        "expr_body,aug_body",
        [
            ("    x = 2\n    x = x ** 65\n", "    x = 2\n    x **= 65\n"),
            ("    x = 2\n    x = x << 65\n", "    x = 2\n    x <<= 65\n"),
            (
                "    x = 2\n    x = x ** len(closes)\n",
                "    x = 2\n    x **= len(closes)\n",
            ),
            (
                "    x = 2\n    x = x << len(closes)\n",
                "    x = 2\n    x <<= len(closes)\n",
            ),
            # Gerçek blok şekilleri: iki sözdizimi de KABUL almalı — kural
            # sıkılaşırken meşru kod kaybolmasın.
            ("    x = 2\n    x = x ** 2\n", "    x = 2\n    x **= 2\n"),
            ("    x = 2\n    x = x << 4\n", "    x = 2\n    x <<= 4\n"),
        ],
    )
    def test_binop_and_augassign_get_the_same_verdict(self, expr_body, aug_body):
        """Aynı işlem, iki sözdizimi, tek kural.

        Denetimin düğüm tipine değil "operatör + sağ operand" ikilisine bağlı
        olduğunun kanıtı: `a ** b` neyi reddediyorsa `a **= b` de aynısını,
        neyi kabul ediyorsa yine aynısını yapmalı. Bu eşleştirme, ileride
        birinin yalnız bir kolu yamamasına karşı kalıcı bir çıpa.
        """
        from codegate import GeneratedCodeError, validate_generated_code

        def _verdict(body: str) -> str:
            try:
                validate_generated_code(self._src(body))
                return "ok"
            except GeneratedCodeError as e:
                # Mesajın kendisi de eşleşmeli: aynı kuraldan, aynı gerekçeyle.
                return f"rejected: {e}"

        assert _verdict(expr_body) == _verdict(aug_body)

    @pytest.mark.parametrize(
        "body",
        [
            "    x = (closes[-1] - closes[-2]) ** 2\n",  # variance term
            "    x = closes[-1] ** 0.5\n",  # stdev
            "    x = 1e-9 + closes[-1]\n",  # epsilon
            "    x = 9999.0 if closes else 0.0\n",  # sentinel (the on-disk max)
            "    x = [c for c in range(len(closes))]\n",  # data-sized range
            "    x = 60 * 24 * 365\n",  # bars per year — folds to 525600
        ],
    )
    def test_real_block_shapes_still_pass(self, body):
        from codegate import validate_generated_code

        validate_generated_code(self._src(body))

    def test_validation_of_a_bomb_is_itself_cheap(self):
        """The check must not become the bomb: rejecting is bottom-up, so no
        intermediate above the ceiling is ever computed."""
        import time

        from codegate import GeneratedCodeError, validate_generated_code

        src = self._src("    x = (((((10 ** 64) ** 64) ** 64) ** 64) ** 64)\n")
        t0 = time.perf_counter()
        with pytest.raises(GeneratedCodeError):
            validate_generated_code(src)
        assert time.perf_counter() - t0 < 1.0

    def test_stored_blocks_all_survive_the_ceiling(self, seeded_store):
        """Tavan gerçek kod şekillerine göre kalibre: depodaki her blok geçmeli.

        Temsilciler (9999 sentinel, varyans terimi, stdev üssü, epsilon) repo
        içinde sabit; eskiden geliştiricinin deposundaki keyfi LLM üretimi kod
        doğrulanıyordu, yani tavan kalibrasyonunu neyin koruduğu belirsizdi.
        """
        cbs = seeded_store
        from codegate import validate_generated_code

        checked = 0
        for info in cbs.list_custom():
            path = cbs.module_path(info["name"])
            try:
                src = path.read_text(encoding="utf-8")
            except OSError:
                continue
            validate_generated_code(src)
            checked += 1
        assert checked == len(_STORE_FIXTURE_BLOCKS), (
            f"tohumlanan {len(_STORE_FIXTURE_BLOCKS)} bloğun {checked} tanesi okundu"
        )


def _fake_strategy(closes, highs=None, lows=None):
    return SimpleNamespace(
        _highs=highs or [c + 0.5 for c in closes],
        _lows=lows or [c - 0.5 for c in closes],
        _prev_state={},
        _indicators={},
        _volumes=[1000.0] * len(closes),
    )


class TestNewBuiltinBlocks:
    """M27: 4 new builtins built on top of the NAU parity library."""

    def test_registered_as_builtin(self):
        from composer import BLOCK_REGISTRY

        for name in (
            "adx_threshold",
            "stoch_rsi_cross",
            "wave_trend_cross",
            "donchian_channel",
        ):
            assert name in BLOCK_REGISTRY, name
            assert BLOCK_REGISTRY[name]["builtin"] is True

    def test_adx_parity_with_ind(self):
        import indicators as ind
        from composer import _eval_adx_threshold

        closes = [100 + ((i * 7) % 13) - 6 + i * 0.05 for i in range(120)]
        strat = _fake_strategy(closes)
        block = SimpleNamespace(
            params={"period": 14, "threshold": 0.0}, role="entry", type="adx_threshold"
        )
        out = _eval_adx_threshold(strat, 0, block, closes)
        res = ind.calc_adx(strat._highs, strat._lows, closes, 14)
        assert res is not None
        expected = "long" if res["plusDI"] > res["minusDI"] else "short"
        assert out == expected  # threshold=0 → direction exactly from calc_adx

    def test_donchian_breakout_and_revert(self):
        from composer import _eval_donchian_channel

        closes = [100.0] * 30 + [105.0]  # last bar breaks above the previous 30 bars
        strat = _fake_strategy(closes)
        blk = SimpleNamespace(
            params={"period": 20, "mode": "breakout"},
            role="entry",
            type="donchian_channel",
        )
        assert _eval_donchian_channel(strat, 0, blk, closes) == "long"
        blk.params["mode"] = "revert"
        assert _eval_donchian_channel(strat, 0, blk, closes) == "short"

    def test_wave_trend_and_stoch_smoke(self):
        from composer import _eval_stoch_rsi_cross, _eval_wave_trend_cross

        closes = [100 + ((i * 11) % 17) - 8 + i * 0.02 for i in range(200)]
        strat = _fake_strategy(closes)
        for fn, typ in (
            (_eval_stoch_rsi_cross, "stoch_rsi_cross"),
            (_eval_wave_trend_cross, "wave_trend_cross"),
        ):
            blk = SimpleNamespace(params={}, role="entry", type=typ)
            # Two bars in a row: the first fills prev-state, the second looks for a cross.
            fn(strat, 0, blk, closes[:-1])
            out = fn(strat, 0, blk, closes)
            assert out in (None, "long", "short")


class TestBollingerMode:
    """L14: legacy default is preserved; breakout/revert can produce short."""

    def _strat_with_bb(self, upper, lower):
        bb = SimpleNamespace(initialized=True, upper=upper, lower=lower)
        return SimpleNamespace(_indicators={0: {"bb": bb}}, _prev_state={})

    def test_legacy_default_both_bands_long(self):
        from composer import _eval_bollinger_break

        strat = self._strat_with_bb(upper=110.0, lower=90.0)
        up = SimpleNamespace(params={"side": "upper"}, role="entry", type="b")
        dn = SimpleNamespace(params={"side": "lower"}, role="entry", type="b")
        assert _eval_bollinger_break(strat, 0, up, [111.0]) == "long"
        assert _eval_bollinger_break(strat, 0, dn, [89.0]) == "long"

    def test_breakout_and_revert_sides(self):
        from composer import _eval_bollinger_break

        strat = self._strat_with_bb(upper=110.0, lower=90.0)
        up_b = SimpleNamespace(
            params={"side": "upper", "mode": "breakout"}, role="entry", type="b"
        )
        dn_b = SimpleNamespace(
            params={"side": "lower", "mode": "breakout"}, role="entry", type="b"
        )
        up_r = SimpleNamespace(
            params={"side": "upper", "mode": "revert"}, role="entry", type="b"
        )
        assert _eval_bollinger_break(strat, 0, up_b, [111.0]) == "long"
        assert _eval_bollinger_break(strat, 0, dn_b, [89.0]) == "short"
        assert _eval_bollinger_break(strat, 0, up_r, [111.0]) == "short"


class TestBlockIsolation:
    """L25: a custom block must not be able to mutate the spec's live params dict."""

    def test_params_mutation_stays_in_copy(self, isolated_store):
        """Bloğun `block.params.update(...)` yazısı spec'in canlı dict'ine sızmamalı.

        Depo izole (bkz. `isolated_store`): bu test eskiden geliştiricinin
        gerçek deposuna `t_isolation_x.py` + registry kaydı yazıyordu ve
        temizliği `finally` içindeki `except Exception: pass`'e bağlıydı — test
        ortada patlarsa dosya kalıcı oluyordu. `tmp_path`'i parametre olarak
        alıp hiç kullanmaması zaten kazayı belli ediyordu.
        """
        from composer import (
            BLOCK_REGISTRY,
            register_custom_from_disk,  # noqa: F401 (registration path)
        )

        src = (
            "def max_lookback(params):\n"
            "    return 10\n"
            "def evaluate(state, block, closes, indicators, portfolio):\n"
            '    block.params.update({"period": 999})\n'
            "    return None\n"
        )
        cbs = isolated_store

        name = "t_isolation_x"
        meta = {"label": "t", "params": {"period": {"type": "int", "default": 14}}}
        cbs.save_custom(name, meta, src)
        try:
            register_custom_from_disk(name)
            entry = BLOCK_REGISTRY[name]
            real_block = SimpleNamespace(params={"period": 14}, role="entry", type=name)
            strat = _fake_strategy(CLOSES)
            strat._buf_cap = 60
            strat.portfolio = SimpleNamespace(
                is_net_long=lambda _i: False,
                is_net_short=lambda _i: False,
                is_flat=lambda _i: True,
            )
            strat._iid = lambda: "X"
            entry["eval"](strat, 0, real_block, CLOSES)
            # The real spec params MUST NOT change (the copy was mutated).
            assert real_block.params["period"] == 14
        finally:
            from composer import unregister_custom_block

            unregister_custom_block(name)
            try:
                cbs.delete_custom(name)
            except Exception:
                pass


class TestNadarayaWatsonWindowing:
    """DeepR 2026-08-08 [YÜKSEK]: the O(n²) double loop now caps each row's
    j-range to a ±6*bandwidth radius (Gaussian tail is ~0 past that). These
    pin the windowed result against the original unwindowed math so a future
    edit cannot silently reintroduce the full O(n²) scan or drift the output.
    """

    @staticmethod
    def _reference(closes, bandwidth=6, multiplier=3.0):
        import math

        n = len(closes)
        y_hat = []
        for i in range(n):
            weight_sum = value_sum = 0.0
            for j in range(n):
                dist = (i - j) / bandwidth
                w = math.exp(-0.5 * dist * dist)
                weight_sum += w
                value_sum += w * closes[j]
            y_hat.append(value_sum / weight_sum if weight_sum > 1e-10 else closes[i])
        residual_sq_sum = sum((closes[i] - y_hat[i]) ** 2 for i in range(n))
        std = math.sqrt(residual_sq_sum / n)
        reg = y_hat[-1]
        half_band = multiplier * std
        pos = (
            max(-1.0, min(1.0, (closes[-1] - reg) / half_band))
            if half_band > 1e-10
            else 0.0
        )
        slope_len = min(5, n - 1)
        slope = y_hat[-1] - y_hat[-1 - slope_len]
        threshold = std * 0.1
        trend = "up" if slope > threshold else "down" if slope < -threshold else "flat"
        return {
            "regression": reg,
            "upper": reg + multiplier * std,
            "lower": reg - multiplier * std,
            "position": pos,
            "trend": trend,
        }

    @pytest.mark.parametrize("bandwidth", [2, 6, 8, 20, 200])
    def test_windowed_matches_unwindowed_reference(self, bandwidth):
        import random

        from indicators import calc_nadaraya_watson

        rng = random.Random(42)
        closes = [100 + rng.gauss(0, 2) for _ in range(300)]

        windowed = calc_nadaraya_watson(closes, bandwidth=bandwidth)
        reference = self._reference(closes, bandwidth=bandwidth)

        assert windowed["trend"] == reference["trend"]
        for key in ("regression", "upper", "lower", "position"):
            assert windowed[key] == pytest.approx(reference[key], abs=1e-6)

    def test_short_series_below_the_window_radius_is_unaffected(self):
        """n < 2*radius+1 (the whole series fits inside one window) must give
        the exact same result as before — this is the everyday case at the
        registry default bandwidth=8 with a warmup-sized series."""
        import random

        from indicators import calc_nadaraya_watson

        rng = random.Random(7)
        closes = [50 + rng.gauss(0, 1) for _ in range(35)]

        windowed = calc_nadaraya_watson(closes, bandwidth=8)
        reference = self._reference(closes, bandwidth=8)

        for key in ("regression", "upper", "lower", "position", "trend"):
            if isinstance(windowed[key], float):
                assert windowed[key] == pytest.approx(reference[key], abs=1e-9)
            else:
                assert windowed[key] == reference[key]


class TestSmaRunningSum:
    """DeepR 2026-08-08 [ORTA]: sma() re-summed the whole window on every
    step (O(n*period)); now a running sum (O(n)), matching ema()'s existing
    shape. Pins the new output against the original re-sum-per-step math."""

    @staticmethod
    def _reference(values, period):
        result = []
        for i in range(period - 1, len(values)):
            s = 0.0
            for j in range(i - period + 1, i + 1):
                s += values[j]
            result.append(s / period)
        return result

    @pytest.mark.parametrize("period", [1, 2, 3, 20, 50])
    def test_matches_the_original_resum_per_step_reference(self, period):
        import random

        from indicators import sma

        rng = random.Random(3)
        values = [100 + rng.gauss(0, 5) for _ in range(300)]

        got = sma(values, period)
        expected = self._reference(values, period)

        assert len(got) == len(expected)
        for g, e in zip(got, expected, strict=True):
            assert g == pytest.approx(e, abs=1e-9)

    def test_shorter_than_period_returns_empty(self):
        from indicators import sma

        assert sma([1.0, 2.0], 5) == []
