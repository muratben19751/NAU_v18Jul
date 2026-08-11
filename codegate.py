"""AST security gate for user/LLM-generated custom signal-block code.

Extracted from ``agent.py`` so it can be imported at *runtime* by ``composer``
without pulling in the heavy ``anthropic``/``ddgs`` stack. Both the generation
path (``agent.py``) and the on-disk load path (``composer._load_module_from_path``)
validate through here, so stored ``.py`` files are re-checked before they are
ever ``exec``'d — closing the hole where a hand-edited or corrupted block would
run unvalidated at server startup.

Two layers, because this code is exec'd INSIDE the web-server process on the
preview/smoke path (that path never crosses ``sandbox``'s subprocess boundary):
the AST walk decides what may be written as well as read, and
``safe_module_proxy`` hands out read-only views of the injected modules instead
of the live ones. Both draw their allowed names from ``_ALLOWED_ATTRS`` so the
static check and the runtime view cannot drift apart.

Only ``ast`` and ``builtins`` are imported; keep it that way.

Wiki References
---------------
Bkz: [[nau_guvenlik_dayaniklilik_duzeltmeleri]], [[strategy_and_actor]]

The AST allowlist is the only thing standing between generated code and the
server process; the write-context rule is why an allowlisted attribute name is
still not an allowlisted assignment target.
"""

from __future__ import annotations

import ast

# AST node types permitted anywhere in generated code. Everything else (imports,
# try/with/lambda/global/raise, etc.) is rejected by absence.
_ALLOWED_NODES: tuple = (
    ast.Module,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Return,
    ast.If,
    ast.For,
    ast.While,
    ast.Break,
    ast.Continue,
    ast.Pass,
    ast.Assign,
    ast.AugAssign,
    ast.AnnAssign,
    ast.Expr,
    ast.Compare,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.And,
    ast.Or,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.Invert,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.BitAnd,
    ast.BitOr,
    ast.BitXor,
    ast.LShift,
    ast.RShift,
    ast.MatMult,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Del,
    ast.Constant,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.IfExp,
    ast.ListComp,
    ast.DictComp,
    ast.SetComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.Starred,
    ast.keyword,
)

# Attribute names that may appear as `.NAME` — everything else is rejected.
# Covers block/indicator field access + std math/statistics functions + safe
# builtin container mutations.
_ALLOWED_ATTRS: set[str] = {
    # SignalBlock fields
    "params",
    "role",
    "type",
    # dict / list / set method-style (all safe container ops)
    "get",
    "keys",
    "values",
    "items",
    "setdefault",
    "update",
    "pop",
    "append",
    "extend",
    "insert",
    "remove",
    "clear",
    "count",
    "index",
    "add",
    "discard",
    # Indicator common fields (Nautilus)
    "value",
    "upper",
    "lower",
    "middle",
    "initialized",
    # Portfolio helpers (limited)
    "is_net_long",
    "is_net_short",
    "is_flat",
    # math.* / statistics.*
    "pi",
    "e",
    "inf",
    "nan",
    "tau",
    "sqrt",
    "log",
    "log2",
    "log10",
    "exp",
    "pow",
    "sin",
    "cos",
    "tan",
    "atan",
    "atan2",
    "asin",
    "acos",
    "floor",
    "ceil",
    "trunc",
    "fabs",
    "copysign",
    "isfinite",
    "isnan",
    "isinf",
    "mean",
    "median",
    "stdev",
    "variance",
    "pstdev",
    "pvariance",
    "fmean",
    "harmonic_mean",
    "geometric_mean",
    "quantiles",
    # indicators.py (NAU parity library — M27/M33). The module is injected under
    # the name `ind`; the name `indicators` would clash with the evaluate() parameter.
    "calc_rsi",
    "calc_rsi_series",
    "sma",
    "ema",
    "calc_stoch_rsi",
    "calc_atr",
    "calc_adx",
    "calc_volume_change",
    "detect_rsi_divergence",
    "calc_nadaraya_watson",
    "calc_wave_trend",
    # typical key accesses on the dicts returned by calc_* are done via .get
    # (get is already in the list).
}

# Names of built-in callables permitted (used as bare `Name` in `Call`).
_ALLOWED_BUILTINS: set[str] = {
    "abs",
    "min",
    "max",
    "sum",
    "len",
    "round",
    "sorted",
    "range",
    "int",
    "float",
    "bool",
    "str",
    "list",
    "tuple",
    "dict",
    "set",
    "any",
    "all",
    "enumerate",
    "zip",
    "reversed",
    "isinstance",
}

# Names of top-level module references allowed (via `Name` -> `Attribute`).
# `ind` = indicators.py at the repo root (NAU parity library); the loader and
# smoke environment inject it under this name (M27/M33).
_ALLOWED_MODULES: set[str] = {"math", "statistics", "ind"}

# Function parameter names required for the `evaluate` signature.
_REQUIRED_SIGNATURE = ("state", "block", "closes", "indicators", "portfolio")


class GeneratedCodeError(ValueError):
    """Raised when generated/stored code fails AST/security checks."""


class _ReadOnlyModuleProxy:
    """Shallow, read-only stand-in for a module injected into block namespaces.

    Blocks are exec'd inside the web-server process, so injecting the live
    ``math``/``statistics``/``indicators`` module objects means one write reaches
    every other caller in the process. This proxy exposes only the whitelisted
    attribute names and refuses writes, so the AST ban on attribute stores is
    backed by a runtime boundary rather than being the only line of defence.
    """

    def __init__(self, name: str, attrs: dict):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_attrs", attrs)

    def __getattr__(self, item: str):
        attrs = object.__getattribute__(self, "_attrs")
        if item in attrs:
            return attrs[item]
        name = object.__getattribute__(self, "_name")
        raise AttributeError(f"module {name!r} has no attribute {item!r}")

    def __setattr__(self, item: str, value) -> None:
        name = object.__getattribute__(self, "_name")
        raise AttributeError(f"module {name!r} is read-only (cannot set {item!r})")

    def __delattr__(self, item: str) -> None:
        name = object.__getattribute__(self, "_name")
        raise AttributeError(f"module {name!r} is read-only (cannot delete {item!r})")

    def __repr__(self) -> str:
        return f"<read-only module {object.__getattribute__(self, '_name')!r}>"


def safe_module_proxy(module, name: str = "") -> _ReadOnlyModuleProxy:
    """Wrap `module` so blocks can read whitelisted attributes but not write any.

    Only names in ``_ALLOWED_ATTRS`` are forwarded — the same set the AST walk
    permits — so the runtime view and the static check cannot drift apart.
    """
    label = name or getattr(module, "__name__", "module")
    attrs = {a: getattr(module, a) for a in _ALLOWED_ATTRS if hasattr(module, a)}
    return _ReadOnlyModuleProxy(label, attrs)


def has_builtin(name: str) -> bool:
    import builtins

    return hasattr(builtins, name)


def safe_builtins() -> dict:
    """Restricted ``__builtins__`` mapping for exec'ing validated block code.

    Exposes ONLY the whitelisted builtins, plus ``RuntimeError`` (the injected
    loop-budget guard raises it). Both the generation-time smoke test and the
    on-disk loader use this so a codegate miss can't reach
    ``eval``/``exec``/``open``/``getattr`` at runtime — the process boundary is
    not a security sandbox, so the language-level restriction has to hold.
    Dunder attribute access is already rejected at the AST level, so exposing
    ``RuntimeError`` does not enable the ``RuntimeError.__subclasses__()`` escape.
    """
    import builtins

    d = {
        name: getattr(builtins, name)
        for name in _ALLOWED_BUILTINS
        if hasattr(builtins, name)
    }
    d["RuntimeError"] = builtins.RuntimeError
    return d


# Largest exponent a block may raise something to. Every `**` in the 622 stored
# blocks is a small literal — `** 2` for a variance term, `** 0.5` for a standard
# deviation — so this ceiling costs real code nothing.
MAX_POW_EXPONENT = 64


MAX_SHIFT_AMOUNT = 64


def _check_lshift(amount: ast.expr) -> None:
    """Reject `<<` unless the shift amount is a small literal.

    Same DoS class _check_pow guards against, but `_fold_literal`'s "bitwise
    ops cannot grow" assumption (see its ops-dict comment) is true for
    BitAnd/BitOr/BitXor/RShift and FALSE for LShift: `1 << 999999999`
    produces a ~300,000-digit number from two Constant operands, so the
    literal-magnitude ceiling never gets a chance to run on the RESULT —
    computing `a << b` for huge b is itself the expensive step, the same gap
    `_check_pow`'s docstring describes for chained `**`. And `x << y` with y
    a runtime value (DeepR 2026-08-09 [ORTA]) is unbounded at validation
    time for the same reason a variable Pow exponent is: the value is
    attacker-chosen. (LShift chaining itself is not the exponential-growth
    hazard `**` chaining is — each `<<` only adds a bounded number of bits —
    so unlike Pow this needs no companion fold-time check for chained
    literals, only this one guard per node.)

    Takes the shift-amount EXPRESSION, not the node: `x << y` (BinOp.right) and
    `x <<= y` (AugAssign.value) are the same operation in two spellings, and
    ``_check_binary_cost`` feeds both here — see its comment.
    """
    if isinstance(amount, ast.UnaryOp) and isinstance(amount.op, (ast.USub, ast.UAdd)):
        amount = amount.operand
    if not isinstance(amount, ast.Constant) or not isinstance(
        amount.value, (int, float)
    ):
        raise GeneratedCodeError(
            "the shift amount of `<<` must be a numeric literal "
            f"(<= {MAX_SHIFT_AMOUNT}); a computed shift amount is unbounded."
        )
    if isinstance(amount.value, bool) or abs(amount.value) > MAX_SHIFT_AMOUNT:
        raise GeneratedCodeError(
            f"`<<` shift amount {amount.value!r} exceeds the limit of "
            f"{MAX_SHIFT_AMOUNT}"
        )


def _check_pow(exp: ast.expr) -> None:
    """Reject `**` unless the exponent is a small literal.

    `9 ** 9 ** 9` passes every other rule here: Pow is an allowed node, the
    operands are Constants, nothing is imported, no attribute or builtin is
    touched. It is also a single uninterruptible CPython operation, so NONE of
    the guards downstream can stop it — the loop-budget injector only
    instruments While/For/comprehensions (there is no loop here), and the smoke
    harness's `t.join(timeout=2.0)` cannot preempt a thread that holds the GIL
    inside one bytecode. Measured: the join never returns and the whole server
    process wedges until SIGKILL.

    A variable exponent is refused for the same reason: `closes[0] ** x` is
    unbounded at validation time, and the value is attacker-chosen.

    Takes the exponent EXPRESSION, not the node: `x ** y` (BinOp.right) and
    `x **= y` (AugAssign.value) are the same operation in two spellings, and
    ``_check_binary_cost`` feeds both here — see its comment.
    """
    # -2 and similar arrive as UnaryOp(USub, Constant) rather than a Constant.
    if isinstance(exp, ast.UnaryOp) and isinstance(exp.op, (ast.USub, ast.UAdd)):
        exp = exp.operand
    if not isinstance(exp, ast.Constant) or not isinstance(exp.value, (int, float)):
        raise GeneratedCodeError(
            "the exponent of `**` must be a numeric literal "
            f"(<= {MAX_POW_EXPONENT}); a computed exponent is unbounded. "
            "Use math.sqrt / math.log for anything more involved."
        )
    if isinstance(exp.value, bool) or abs(exp.value) > MAX_POW_EXPONENT:
        raise GeneratedCodeError(
            f"`**` exponent {exp.value!r} exceeds the limit of {MAX_POW_EXPONENT}"
        )


# Per-operator cost rules, keyed by the operator node — NOT by the statement
# node that happens to carry it. Python spells the same binary operation two
# ways: `x ** y` is a BinOp and `x **= y` is an AugAssign, and only the first
# is a BinOp. Keying the checks on `type(node.op)` means both spellings (and
# any third one a future grammar adds, as long as it exposes an `op`) walk
# through the SAME rule instead of needing a second patch that someone will
# forget — which is exactly what happened: `**`/`<<` were guarded on BinOp
# only, so `x **= 999999` reached the exec'd namespace unchecked
# (DeepR 2026-08-10 [KRİTİK]).
_COST_CHECKS = {
    ast.Pow: _check_pow,
    ast.LShift: _check_lshift,
}


def _check_binary_cost(node: ast.AST) -> None:
    """Apply the per-operator cost rules to any node that carries an operator.

    Both spellings put the attacker-controlled operand on a different field —
    ``BinOp.right`` vs ``AugAssign.value`` — so the field is normalised here and
    the rule itself only ever sees the operand expression.
    """
    if isinstance(node, ast.BinOp):
        op, operand = node.op, node.right
    elif isinstance(node, ast.AugAssign):
        op, operand = node.op, node.value
    else:
        return
    check = _COST_CHECKS.get(type(op))
    if check is not None:
        check(operand)


# Ceiling on any number a block can spell out with literals alone. The 268
# stored blocks top out at 9999 (periods, thresholds, a 9999 sentinel), so this
# leaves two orders of magnitude of headroom while keeping literal-sized work
# small: the biggest list a block can ask for outright is ~1M elements.
MAX_LITERAL_MAGNITUDE = 1_000_000

_FOLD_DEPTH = 12


def _fold_literal(node: ast.AST, depth: int = 0) -> int | float | None:
    """Value of a literal-only arithmetic expression, or None if not literal.

    Raises GeneratedCodeError as soon as any INTERMEDIATE exceeds
    ``MAX_LITERAL_MAGNITUDE``. Two jobs in one rule:

    - It closes the resource holes ``_check_pow`` does not see. That check only
      constrains the exponent, so a chained base slips through: every ``**`` in
      ``((10**64)**64)**64`` has the literal exponent 64 and passes, yet the
      value is 10**262144 — and one more level costs 64× again (measured: four
      levels = 14.5 s, 55 Mbit). The same gap covers container bombs the AST is
      otherwise blind to, ``list(range(10**9))`` / ``[0] * 10**9``, which the
      loop budget cannot instrument either (no loop node, one C-level call).
    - Rejecting bottom-up keeps VALIDATION itself cheap. Because no operand may
      exceed the ceiling and no exponent may exceed ``MAX_POW_EXPONENT``, the
      largest product this ever computes is 1e6**64 — a few hundred digits. The
      check can never become the bomb it is meant to stop.
    """
    if depth > _FOLD_DEPTH:
        return None

    def _bounded(v: int | float) -> int | float:
        if abs(v) > MAX_LITERAL_MAGNITUDE:
            raise GeneratedCodeError(
                f"literal value {v!r} exceeds the limit of "
                f"{MAX_LITERAL_MAGNITUDE:,}; sizes this large are never a "
                "period, a threshold or a window."
            )
        return v

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            return None
        return _bounded(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        val = _fold_literal(node.operand, depth + 1)
        if val is None:
            return None
        return -val if isinstance(node.op, ast.USub) else val
    if isinstance(node, ast.BinOp):
        left = _fold_literal(node.left, depth + 1)
        right = _fold_literal(node.right, depth + 1)
        if left is None or right is None:
            return None
        ops = {
            ast.Add: lambda a, b: a + b,
            ast.Sub: lambda a, b: a - b,
            ast.Mult: lambda a, b: a * b,
            ast.Pow: lambda a, b: a**b,
        }
        fn = ops.get(type(node.op))
        if fn is None:
            return None  # Div/Mod/bitwise: cannot grow, or not worth folding
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_POW_EXPONENT:
            return None  # _check_pow rejects this node on its own
        try:
            return _bounded(fn(left, right))
        except GeneratedCodeError:
            raise
        except Exception:
            return None  # overflow / type error — not our call to make here
    return None


def validate_generated_code(src: str) -> ast.Module:
    """Parse ``src`` and enforce the whitelist. Returns the AST on success.

    Raises GeneratedCodeError on any violation.
    """
    try:
        tree = ast.parse(src, mode="exec")
    except SyntaxError as e:
        raise GeneratedCodeError(f"syntax error: {e}") from e

    functions: dict[str, ast.FunctionDef] = {}

    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            if node.decorator_list:
                raise GeneratedCodeError(f"decorators are not allowed on `{node.name}`")
            functions[node.name] = node
        else:
            raise GeneratedCodeError(
                f"top-level statement not allowed: {type(node).__name__}. "
                "Only `def` functions are permitted at module scope."
            )

    if "evaluate" not in functions:
        raise GeneratedCodeError("missing `evaluate` function")

    ev = functions["evaluate"]
    arg_names = [a.arg for a in ev.args.args]
    if tuple(arg_names) != _REQUIRED_SIGNATURE:
        raise GeneratedCodeError(
            f"evaluate signature must be {_REQUIRED_SIGNATURE}, got {tuple(arg_names)}"
        )

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise GeneratedCodeError(f"disallowed node: {type(node).__name__}")
        # `**`/`<<` cost rules: dispatched on the OPERATOR, so `x ** y` (BinOp)
        # and `x **= y` (AugAssign) are judged by the same rule. See
        # `_COST_CHECKS`.
        _check_binary_cost(node)
        if isinstance(node, (ast.Constant, ast.UnaryOp, ast.BinOp)):
            _fold_literal(node)
        # Reject any `__dunder__` identifier — blocks the __class__/__globals__ escape.
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                raise GeneratedCodeError(
                    f"disallowed name (leading underscore): {node.id}"
                )
            # Reject any REFERENCE to a non-whitelisted builtin, not just a
            # direct call. Closes the escapes where a dangerous builtin
            # (eval/exec/open/getattr/compile/globals/…) is *named* without being
            # a direct `Name` callee: `[exec][0](...)`, `sorted(data, key=eval)`,
            # `x = eval` then `x(...)`. Whitelisted builtins, allowed module names
            # (math/statistics/ind), helper functions and locals are non-builtins
            # or in the whitelist, so they pass through untouched.
            if node.id not in _ALLOWED_BUILTINS and has_builtin(node.id):
                raise GeneratedCodeError(f"reference to disallowed builtin: {node.id}")
        if isinstance(node, ast.arg):
            if node.arg.startswith("_"):
                raise GeneratedCodeError(
                    f"disallowed arg name (leading underscore): {node.arg}"
                )
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise GeneratedCodeError(f"disallowed attribute access: .{node.attr}")
            if node.attr not in _ALLOWED_ATTRS:
                raise GeneratedCodeError(f"attribute not in whitelist: .{node.attr}")
            # The whitelist above only constrains WHICH attribute is touched, not
            # whether it is read or written. `ind`/`math`/`statistics` are injected
            # into the exec namespace, so `ind.calc_rsi = <lambda>` or `math.pi = 0`
            # would monkey-patch modules shared by the whole server process — every
            # later strategy/backtest in that process would run the poisoned version.
            # Generated blocks are pure functions over their arguments; none of the
            # 233 stored blocks assigns to an attribute. Reads only.
            if not isinstance(node.ctx, ast.Load):
                raise GeneratedCodeError(
                    f"attribute assignment/deletion not allowed: .{node.attr}"
                )
        if isinstance(node, ast.Call):
            # Reject callables that aren't in the builtin/module/local-function whitelist.
            fn = node.func
            if isinstance(fn, ast.Name):
                # Allow: builtins, AND any function defined in the same module
                # (helper functions like `ema_series`, `atr_series`, etc.)
                if fn.id not in _ALLOWED_BUILTINS and fn.id not in functions:
                    raise GeneratedCodeError(f"call to disallowed function: {fn.id}()")
            elif isinstance(fn, ast.Attribute):
                # e.g. math.sqrt(x) or closes.append(x) — the outer walk already
                # validates the .attr against `_ALLOWED_ATTRS` (and rejects any
                # dunder), and the base object can't be a dangerous builtin
                # because bare-Name references to those are now rejected above.
                pass
            else:
                # Callee is a Subscript/Call/etc. — e.g. `[exec][0](...)`,
                # `{0: eval}[0](...)`, `factory()()`. The resolved callable can't
                # be whitelisted statically, so deny outright.
                raise GeneratedCodeError(
                    f"call with non-name callee not allowed: {type(fn).__name__}"
                )
    return tree


# ---------------------------------------------------------------------------
# M25: loop-budget injection — infinite loops of the `while True: pass` class
# pass the AST whitelist (While/For are deliberately allowed; existing blocks in
# the catalog use while). AFTER validation, a step counter that resets at the
# start of every function is injected into the code: on budget overflow, RuntimeError.
# The injected `__loop_budget` name does NOT pass validation (underscore ban)
# but that's fine because the transform is applied after validation — user
# code cannot access this name.
# ---------------------------------------------------------------------------

LOOP_BUDGET_STEPS = 5_000_000

# Module-global counter (list -> subscript mutation does not require `global`) +
# tick helper. While/For guards and comprehension element wrappers use it.
# M1084: comprehensions/generators open a NEW scope, so they cannot write to a
# function-local counter — a module-global + `__budget_tick(elt)` wrapper enforces
# the budget inside comprehensions too. The counter resets at the start of every
# function (evaluate is called on every bar). Note: single-threaded strategy
# execution is assumed (backtest is sequential; smoke runs one at a time).
_BUDGET_PREAMBLE = (
    f"__budget = [{LOOP_BUDGET_STEPS}]\n"
    "def __budget_tick(v):\n"
    f"    __budget[0] -= 1\n"
    "    if __budget[0] < 0:\n"
    "        raise RuntimeError("
    f"'custom block operation budget exceeded ({LOOP_BUDGET_STEPS:,} steps)')\n"
    "    return v\n"
)


class _LoopBudgetInjector(ast.NodeTransformer):
    """Adds a budget tick to every While/For body and comprehension element,
    and a counter reset at the start of every function."""

    def _reset_stmt(self) -> ast.stmt:
        # __budget[0] = N — subscript assignment (no global declaration needed).
        return ast.parse(f"__budget[0] = {LOOP_BUDGET_STEPS}").body[0]

    def _guard_stmts(self) -> list[ast.stmt]:
        return ast.parse(
            "__budget[0] -= 1\n"
            "if __budget[0] < 0:\n"
            "    raise RuntimeError("
            f"'custom block loop budget exceeded ({LOOP_BUDGET_STEPS:,} steps)')"
        ).body

    def _wrap_tick(self, expr: ast.expr) -> ast.expr:
        """expr → __budget_tick(expr) (does not change the value, counts one tick)."""
        return ast.Call(
            func=ast.Name(id="__budget_tick", ctx=ast.Load()),
            args=[expr],
            keywords=[],
        )

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self.generic_visit(node)
        # Budget reset ONLY at the top-level evaluate() entry (once per bar).
        # It used to be placed at the start of EVERY function → a helper called
        # from inside a hot loop refreshed the shared module-global budget on
        # every iteration, the guard never went <0, and the only in-process
        # infinite-loop backstop was renewed (an escaping block would hang the
        # server worker). Helpers SHARE the budget but do not reset it.
        if node.name == "evaluate":
            reset = self._reset_stmt()
            # The injected node carries a small lineno (1-3) from its own
            # ast.parse; fix_missing_locations does NOT override an EXISTING
            # lineno → the budget-error traceback shows the wrong line. Inherit
            # the function's location.
            ast.copy_location(reset, node)
            ast.fix_missing_locations(reset)
            node.body.insert(0, reset)
        return node

    def visit_While(self, node: ast.While):
        self.generic_visit(node)
        guards = self._guard_stmts()
        for g in guards:  # let the budget error show the REAL line of the loop
            ast.copy_location(g, node)
            ast.fix_missing_locations(g)
        node.body = guards + node.body
        return node

    def visit_For(self, node: ast.For):
        self.generic_visit(node)
        guards = self._guard_stmts()
        for g in guards:
            ast.copy_location(g, node)
            ast.fix_missing_locations(g)
        node.body = guards + node.body
        return node

    def _visit_comp(self, node):
        self.generic_visit(node)
        node.elt = self._wrap_tick(node.elt)
        return node

    visit_ListComp = _visit_comp
    visit_SetComp = _visit_comp
    visit_GeneratorExp = _visit_comp

    def visit_DictComp(self, node: ast.DictComp):
        self.generic_visit(node)
        # key and value are separate; wrap the tick around value (once per element).
        node.value = self._wrap_tick(node.value)
        return node


def compile_with_loop_budget(src: str, filename: str = "<custom-block>"):
    """Called AFTER ``validate_generated_code``: transforms the source into a
    budgeted AST and returns a compiled code object. The loader, smoke, and
    preview environment use the same function (generation/runtime parity)."""
    tree = ast.parse(src, mode="exec")
    tree = _LoopBudgetInjector().visit(tree)
    # Budget preamble added at the start of the module (counter + tick helper).
    preamble = ast.parse(_BUDGET_PREAMBLE).body
    tree.body = preamble + tree.body
    ast.fix_missing_locations(tree)
    return compile(tree, filename, "exec")
