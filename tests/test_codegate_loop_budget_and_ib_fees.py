"""Unit tests for two security/correctness-critical paths the DeepR audit
(2026-08-08) found with zero test coverage:

1. codegate.compile_with_loop_budget / _LoopBudgetInjector — the AST
   transform that stops a runaway custom-block (infinite while/for loop)
   from hanging the backtest process. If this silently stopped injecting
   guards, a generated block could hang the server with no test catching it.
2. backtest.IBFixedFeeModel.get_commission — the per-share/min/cap commission
   formula for US-equity fills. A sign/clamp bug here would silently corrupt
   every equity backtest's PnL.
"""

from __future__ import annotations

import pytest
from nautilus_trader.model.currencies import USD

from backtest import IBFixedFeeModel
from codegate import compile_with_loop_budget


def _run(src: str) -> dict:
    # Single namespace (globals is locals): a function defined by exec'd code
    # always resolves free names via its OWN __globals__, which exec sets to
    # whatever dict is passed as `globals` — a separate locals dict would
    # leave __budget (bound by the preamble as a top-level name) invisible to
    # the injected guards inside evaluate()/helpers at call time.
    ns: dict = {}
    code = compile_with_loop_budget(src)
    exec(code, ns)  # noqa: S102 — exact code path the sandbox uses
    return ns


class _small_budget:
    """Context manager: shrink codegate.LOOP_BUDGET_STEPS for the duration.

    The reset statement inserted at evaluate()'s entry (_reset_stmt) AND the
    inline While/For guards (_guard_stmts) both bake LOOP_BUDGET_STEPS in
    directly — patching _BUDGET_PREAMBLE alone does NOT shrink the effective
    budget, because evaluate()'s own reset (`__budget[0] = LOOP_BUDGET_STEPS`)
    overwrites whatever the preamble set the instant evaluate() is called.
    LOOP_BUDGET_STEPS is read at AST-BUILD time (inside compile_with_loop_budget,
    via _LoopBudgetInjector), so it must be patched before that call, not after.
    """

    def __init__(self, n: int):
        self.n = n

    def __enter__(self):
        import codegate as cg

        self._cg = cg
        self._orig = cg.LOOP_BUDGET_STEPS
        cg.LOOP_BUDGET_STEPS = self.n
        return self

    def __exit__(self, *exc):
        self._cg.LOOP_BUDGET_STEPS = self._orig
        return False


class TestLoopBudgetInjector:
    def test_normal_bounded_loop_runs_fine(self):
        src = (
            "def evaluate():\n"
            "    total = 0\n"
            "    for i in range(10):\n"
            "        total += i\n"
            "    return total\n"
        )
        ns = _run(src)
        assert ns["evaluate"]() == 45

    def test_while_loop_within_budget_runs_fine(self):
        src = (
            "def evaluate():\n"
            "    i = 0\n"
            "    while i < 10:\n"
            "        i += 1\n"
            "    return i\n"
        )
        ns = _run(src)
        assert ns["evaluate"]() == 10

    def test_comprehension_within_budget_runs_fine(self):
        src = "def evaluate():\n    return [x * 2 for x in range(10)]\n"
        ns = _run(src)
        assert ns["evaluate"]() == [x * 2 for x in range(10)]

    def test_infinite_while_loop_raises_runtime_error(self):
        with _small_budget(50):
            src = (
                "def evaluate():\n"
                "    i = 0\n"
                "    while True:\n"
                "        i += 1\n"
                "    return i\n"
            )
            ns = _run(src)
            with pytest.raises(RuntimeError, match="loop budget exceeded"):
                ns["evaluate"]()

    def test_infinite_for_loop_raises(self):
        with _small_budget(20):
            src = (
                "def evaluate():\n"
                "    total = 0\n"
                "    for i in range(10_000_000):\n"
                "        total += i\n"
                "    return total\n"
            )
            ns = _run(src)
            with pytest.raises(RuntimeError, match="loop budget exceeded"):
                ns["evaluate"]()

    def test_runaway_comprehension_raises(self):
        with _small_budget(20):
            src = "def evaluate():\n    return [x for x in range(10_000_000)]\n"
            ns = _run(src)
            with pytest.raises(RuntimeError, match="operation budget exceeded"):
                ns["evaluate"]()

    @pytest.mark.parametrize(
        "expr",
        [
            # Liste: `elt` yalnız süzgeçten GEÇEN öğe için değerlendirilir.
            "[x for x in range(10_000_000) if x < 0]",
            "{x for x in range(10_000_000) if x < 0}",
            "{x: x for x in range(10_000_000) if x < 0}",
            "sum(1 for x in range(10_000_000) if x < 0)",
            # İç içe generator: iç süzgeç dıştaki her öğe için yeniden koşar.
            "[x for x in range(10_000) for y in range(10_000) if y < 0]",
        ],
    )
    def test_a_comprehension_filter_that_rejects_everything_still_ticks(self, expr):
        """Süzgeç her şeyi elerse `elt` HİÇ çalışmaz — tik oradan gelemez.

        Bütçe yalnız elemente bağlıyken bu bir kaçış yoluydu: ölçüldü
        (2026-08-17), `[i for i in range(10**7) if i < 0]` 0,14 saniyede,
        `(n:=n+1)` sayacıyla ON MİLYON adım atarak, bütçe hiç tetiklenmeden
        tamamlanıyordu. While/For'da böyle bir delik yok (guard'lar gövdede);
        delik yalnız "iterasyon" ile "element"in ayrıştığı yerde.
        """
        with _small_budget(20):
            ns = _run(f"def evaluate():\n    return {expr}\n")
            with pytest.raises(RuntimeError, match="operation budget exceeded"):
                ns["evaluate"]()

    @pytest.mark.parametrize(
        "expr,expected",
        [
            ("[i for i in range(20) if i % 2 == 0]", list(range(0, 20, 2))),
            ("{i: i * 2 for i in range(5) if i > 2}", {3: 6, 4: 8}),
            ("sorted({i % 3 for i in range(10) if i != 4})", [0, 1, 2]),
            ("sum(i for i in range(10) if i > 7)", 17),
            ("[x * y for x in range(3) if x for y in range(3) if y]", [1, 2, 2, 4]),
        ],
    )
    def test_ticking_the_filter_does_not_change_what_it_filters(self, expr, expected):
        """`__budget_tick` değeri olduğu gibi döndürür — doğruluk değeri korunur.

        Süzgeci sarmalamak sonucu değiştirseydi, düzeltme bütçeyi kapatıp
        stratejinin mantığını bozardı; sessiz bir yanlış, gürültülü bir
        aşımdan kötüdür.
        """
        ns = _run(f"def evaluate():\n    return {expr}\n")

        assert ns["evaluate"]() == expected

    def test_budget_resets_between_evaluate_calls(self):
        # Regression guard for the documented invariant (see visit_FunctionDef):
        # the counter resets at every evaluate() ENTRY — so a small budget that
        # would be exhausted by two calls' worth of work in a row must still
        # let each individual call finish on its own.
        with _small_budget(15):
            src = (
                "def evaluate():\n"
                "    total = 0\n"
                "    for i in range(10):\n"
                "        total += i\n"
                "    return total\n"
            )
            ns = _run(src)
            first = ns["evaluate"]()
            second = ns["evaluate"]()  # would fail if the 1st call's spend carried over
            assert first == second == 45

    def test_nested_helper_shares_budget_but_does_not_reset_it(self):
        # helper() alone loops 10 times (within a 15-tick budget), but
        # evaluate() calls it twice — 20 total ticks against a budget of 15
        # that resets only once (at evaluate()'s own entry, per
        # visit_FunctionDef's "ONLY at the top-level evaluate() entry"
        # comment). If helper() wrongly reset the shared counter on its own
        # entry too, this would NOT raise.
        with _small_budget(15):
            src = (
                "def helper():\n"
                "    total = 0\n"
                "    for i in range(10):\n"
                "        total += i\n"
                "    return total\n"
                "def evaluate():\n"
                "    return helper() + helper()\n"
            )
            ns = _run(src)
            with pytest.raises(RuntimeError, match="loop budget exceeded"):
                ns["evaluate"]()


class TestIBFixedFeeModelGetCommission:
    class _FakeInstrument:
        quote_currency = USD

    def test_per_share_fee_above_minimum(self):
        model = IBFixedFeeModel(
            per_share_usd=0.005, min_per_order_usd=1.0, max_pct_of_trade=0.01
        )
        commission = model.get_commission(
            order=None, fill_qty=1000, fill_px=50.0, instrument=self._FakeInstrument()
        )
        # 1000 * 0.005 = 5.0, above the $1 minimum, below 1% of $50,000 notional
        assert float(commission.as_double()) == pytest.approx(5.0)

    def test_small_order_clamped_to_minimum(self):
        model = IBFixedFeeModel(
            per_share_usd=0.005, min_per_order_usd=1.0, max_pct_of_trade=0.01
        )
        commission = model.get_commission(
            order=None, fill_qty=10, fill_px=50.0, instrument=self._FakeInstrument()
        )
        # 10 * 0.005 = 0.05, below the $1 minimum -> clamped up to $1
        assert float(commission.as_double()) == pytest.approx(1.0)

    def test_large_order_capped_at_percent_of_notional(self):
        model = IBFixedFeeModel(
            per_share_usd=1.0, min_per_order_usd=1.0, max_pct_of_trade=0.01
        )
        # 1000 shares * $1/share = $1000 raw commission, but notional is only
        # 1000 * $0.50 = $500 -> 1% cap = $5, well below the raw $1000.
        commission = model.get_commission(
            order=None, fill_qty=1000, fill_px=0.50, instrument=self._FakeInstrument()
        )
        assert float(commission.as_double()) == pytest.approx(5.0)

    def test_zero_notional_not_capped_to_zero(self):
        # notional <= 0 must NOT apply the percentage cap (that would zero out
        # the commission on a zero-price fill) — the minimum still applies.
        model = IBFixedFeeModel(
            per_share_usd=0.005, min_per_order_usd=1.0, max_pct_of_trade=0.01
        )
        commission = model.get_commission(
            order=None, fill_qty=100, fill_px=0.0, instrument=self._FakeInstrument()
        )
        assert float(commission.as_double()) == pytest.approx(1.0)

    def test_uses_instrument_quote_currency(self):
        model = IBFixedFeeModel()
        commission = model.get_commission(
            order=None, fill_qty=100, fill_px=50.0, instrument=self._FakeInstrument()
        )
        assert str(commission.currency) == "USD"
