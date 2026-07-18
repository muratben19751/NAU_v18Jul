"""AST security gate for user/LLM-generated custom signal-block code.

Extracted from ``agent.py`` so it can be imported at *runtime* by ``composer``
without pulling in the heavy ``anthropic``/``ddgs`` stack. Both the generation
path (``agent.py``) and the on-disk load path (``composer._load_module_from_path``)
validate through here, so stored ``.py`` files are re-checked before they are
ever ``exec``'d — closing the hole where a hand-edited or corrupted block would
run unvalidated at server startup.

Only ``ast`` and ``builtins`` are imported; keep it that way.
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
    # indicators.py (NAU parite kütüphanesi — M27/M33). Modül `ind` adıyla
    # enjekte edilir; `indicators` adı evaluate() parametresiyle çakışırdı.
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
    # calc_* dönüş dict'lerinin tipik anahtar erişimleri .get ile yapılır
    # (get zaten listede).
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
# `ind` = repo kökündeki indicators.py (NAU parite kütüphanesi); yükleyici ve
# smoke ortamı bu adla enjekte eder (M27/M33).
_ALLOWED_MODULES: set[str] = {"math", "statistics", "ind"}

# Function parameter names required for the `evaluate` signature.
_REQUIRED_SIGNATURE = ("state", "block", "closes", "indicators", "portfolio")


class GeneratedCodeError(ValueError):
    """Raised when generated/stored code fails AST/security checks."""


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
# M25: döngü-bütçesi enjeksiyonu — `while True: pass` sınıfı sonsuz döngüler
# AST whitelist'ini geçer (While/For bilinçli serbest; katalogdaki mevcut
# bloklar while kullanıyor). Doğrulama SONRASI koda, her fonksiyon başında
# sıfırlanan bir adım sayacı enjekte edilir: bütçe aşımında RuntimeError.
# Enjekte edilen `__loop_budget` adı doğrulamadan GEÇMEZ (underscore yasağı)
# ama transform doğrulamadan sonra uygulandığı için sorun değil — kullanıcı
# kodu bu ada erişemez.
# ---------------------------------------------------------------------------

LOOP_BUDGET_STEPS = 5_000_000

# Modül-global sayaç (liste → subscript mutasyonu `global` gerektirmez) +
# tick yardımcısı. While/For guard'ları ve comprehension element sarmaları
# bunu kullanır. M1084: comprehension/generator'lar YENİ scope açtığından
# fonksiyon-yerel bir sayaca yazamaz — modül-global + `__budget_tick(elt)`
# sarması bütçeyi comprehension içinde de uygular. Sayaç her fonksiyon
# başında (evaluate her barda çağrılır) sıfırlanır. Not: tek-thread'li strateji
# yürütmesi varsayılır (backtest sıralı; smoke tek-tek).
_BUDGET_PREAMBLE = (
    f"__budget = [{LOOP_BUDGET_STEPS}]\n"
    "def __budget_tick(v):\n"
    f"    __budget[0] -= 1\n"
    "    if __budget[0] < 0:\n"
    "        raise RuntimeError("
    f"'custom blok işlem bütçesi aşıldı ({LOOP_BUDGET_STEPS:,} adım)')\n"
    "    return v\n"
)


class _LoopBudgetInjector(ast.NodeTransformer):
    """Her While/For gövdesine ve comprehension element'ine bütçe-tick'i,
    her fonksiyon başına sayaç sıfırlaması ekler."""

    def _reset_stmt(self) -> ast.stmt:
        # __budget[0] = N — subscript ataması (global bildirimi gerekmez).
        return ast.parse(f"__budget[0] = {LOOP_BUDGET_STEPS}").body[0]

    def _guard_stmts(self) -> list[ast.stmt]:
        return ast.parse(
            "__budget[0] -= 1\n"
            "if __budget[0] < 0:\n"
            "    raise RuntimeError("
            f"'custom blok döngü bütçesi aşıldı ({LOOP_BUDGET_STEPS:,} adım)')"
        ).body

    def _wrap_tick(self, expr: ast.expr) -> ast.expr:
        """expr → __budget_tick(expr) (değeri değiştirmez, tek tick sayar)."""
        return ast.Call(
            func=ast.Name(id="__budget_tick", ctx=ast.Load()),
            args=[expr],
            keywords=[],
        )

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self.generic_visit(node)
        # Bütçe sıfırlaması YALNIZ top-level evaluate() girişinde (bar başına bir
        # kez). Eskiden HER fonksiyonun başına konuyordu → sıcak döngü içinden
        # çağrılan bir helper paylaşılan module-global bütçeyi her iterasyonda
        # tazeliyor, guard hiç <0 olmuyor ve tek in-process sonsuz-döngü backstop'u
        # yeniliyordu (kaçak blok server worker'ını asardı). Helper'lar bütçeyi
        # PAYLAŞIR ama sıfırlamaz.
        if node.name == "evaluate":
            reset = self._reset_stmt()
            # Enjekte edilen node kendi ast.parse'ından küçük lineno (1-3) taşır;
            # fix_missing_locations MEVCUT lineno'yu ezmez → bütçe-hata traceback'i
            # yanlış satır gösterir. Fonksiyon konumunu miras al.
            ast.copy_location(reset, node)
            ast.fix_missing_locations(reset)
            node.body.insert(0, reset)
        return node

    def visit_While(self, node: ast.While):
        self.generic_visit(node)
        guards = self._guard_stmts()
        for g in guards:  # bütçe-hatası döngünün GERÇEK satırını göstersin
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
        # key ve value ayrı; tick'i value'ya sar (her eleman bir kez).
        node.value = self._wrap_tick(node.value)
        return node


def compile_with_loop_budget(src: str, filename: str = "<custom-block>"):
    """``validate_generated_code`` SONRASI çağrılır: kaynağı bütçeli AST'ye
    dönüştürüp derlenmiş kod nesnesi döndürür. Yükleyici, smoke ve önizleme
    ortamı aynı fonksiyonu kullanır (üretim/çalışma-zamanı paritesi)."""
    tree = ast.parse(src, mode="exec")
    tree = _LoopBudgetInjector().visit(tree)
    # Bütçe preamble'ını modül başına ekle (sayaç + tick yardımcısı).
    preamble = ast.parse(_BUDGET_PREAMBLE).body
    tree.body = preamble + tree.body
    ast.fix_missing_locations(tree)
    return compile(tree, filename, "exec")
