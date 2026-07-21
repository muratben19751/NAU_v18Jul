"""Strategy composer routes (visual signal-block builder).

Wiki References
---------------
Bkz: [[strategy_and_actor]], [[order_flow_pipeline]]

The Compose UI reflects the [[strategy_and_actor]] hierarchy.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from agent import propose_composed_strategy
from composer import (
    BLOCK_CATALOG,
    BLOCK_REGISTRY,
    ComposedStrategySpec,
    SignalBlock,
    load_catalog,
    new_spec_id,
    register_custom_from_disk,
    unregister_custom_block,
)
from state import get_state
from wiki_helper import read_wiki_page

try:
    import markdown as _md

    def render_md(txt: str) -> str:
        return _md.markdown(txt, extensions=["fenced_code", "tables"])
except Exception:  # pragma: no cover

    def render_md(txt: str) -> str:
        return f"<pre>{txt}</pre>"


router = APIRouter(prefix="/strategy")


# Draft state per anonymous session (in-memory; ephemeral by design)
_DRAFTS: dict[str, list[SignalBlock]] = {}
COOKIE = "nautlab_sid"
_COOKIE_MAX_AGE = 3600
# Do not let _DRAFTS grow unbounded (every new sid left an entry, never evicted).
# Insertion-order dict → drop the oldest sid (rough LRU; drafts are short-lived).
_MAX_DRAFT_SESSIONS = 500


def _sid(request: Request, response: Response | None = None) -> str:
    sid = request.cookies.get(COOKIE)
    if not sid:
        sid = uuid.uuid4().hex
    # Refresh the cookie on EVERY response (sliding expiry) — previously it was
    # set only the first time, so after 1 hour the draft would expire mid-composition.
    if response is not None:
        response.set_cookie(
            COOKIE, sid, httponly=True, samesite="lax", max_age=_COOKIE_MAX_AGE
        )
    return sid


def _drafts(sid: str) -> list[SignalBlock]:
    if sid not in _DRAFTS and len(_DRAFTS) >= _MAX_DRAFT_SESSIONS:
        # Drop the oldest session (insertion-order) — no unbounded memory growth.
        _DRAFTS.pop(next(iter(_DRAFTS)), None)
    return _DRAFTS.setdefault(sid, [])


def _slugify(label: str) -> str:
    """Convert a user-facing label into a snake_case block name.
    Lowercases, strips accents, keeps [a-z0-9_], collapses runs, trims to 40 chars.
    """
    import re
    import unicodedata

    s = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    s = re.sub(r"_+", "_", s)
    if s and not s[0].isalpha():
        s = "b_" + s
    return s[:40]


def _wiki_html_for(block_type: str) -> tuple[str, str]:
    refs = BLOCK_CATALOG.get(block_type, {}).get("wiki_refs", [])
    active = refs[0] if refs else "wiki/entities/strategy_and_actor.md"
    return active, render_md(read_wiki_page(active))


@router.get("", response_class=HTMLResponse)
async def page(request: Request):
    import custom_block_store as cbs
    from server import get_market_info, templates

    response = HTMLResponse("")
    sid = _sid(request, response)
    drafts = _drafts(sid)
    catalog = load_catalog()
    default_type = next(iter(BLOCK_CATALOG.keys()))
    wiki_active, wiki_html = _wiki_html_for(default_type)

    ctx = {
        "active": "strategy",
        "page_title": "Strategy Composer",
        "market": get_market_info(),
        "block_catalog": BLOCK_CATALOG,
        "default_type": default_type,
        "drafts": drafts,
        "catalog": list(reversed(catalog)),
        "wiki_active": wiki_active,
        "wiki_html": wiki_html,
        "custom_blocks": cbs.list_custom(),
        "options": {
            "entry_logic": "OR",
            "exit_logic": "OR",
            "order_type": "market",
            "limit_offset_bps": 0.0,
            "use_bracket": False,
            "sl_type": "percent",
            "sl_value": 2.0,
            "tp_type": "off",
            "tp_value": 4.0,
            "atr_period": 14,
            "allow_short": False,
            "trade_size_mode": "fixed_usdt",
            "trade_size_percent": 5.0,
            "trade_size_atr_risk": 1.0,
            "trade_size_usdt": 1000.0,
            "trade_size_btc": 0.1,
            "emulate": False,
            "trend_filter": False,
            "trend_interval": "60",
            "trend_ema_period": 50,
        },
    }
    html = templates.get_template("strategy.html").render(request=request, **ctx)
    resp = HTMLResponse(html)
    if not request.cookies.get(COOKIE):
        resp.set_cookie(COOKIE, sid, httponly=True, samesite="lax", max_age=3600)
    return resp


@router.get("/blocks/form", response_class=HTMLResponse)
async def block_form(request: Request, type: str):
    from server import templates

    if type not in BLOCK_CATALOG:
        return HTMLResponse(
            "<div class='empty-state'>Unknown block.</div>", status_code=400
        )
    return templates.TemplateResponse(
        request,
        "fragments/block_form.html",
        {"btype": type, "meta": BLOCK_CATALOG[type]},
    )


@router.get("/wiki", response_class=HTMLResponse)
async def wiki_panel(request: Request, type: str):
    from server import templates

    _, html = _wiki_html_for(type)
    return templates.TemplateResponse(
        request,
        "fragments/wiki_panel.html",
        {"wiki_html": html, "wiki_active": _wiki_html_for(type)[0]},
    )


@router.post("/drafts", response_class=HTMLResponse)
async def add_draft(request: Request):
    from server import templates

    form = await request.form()
    btype = form.get("type")
    role = form.get("role", "entry")
    if btype not in BLOCK_CATALOG:
        return HTMLResponse(
            "<div class='empty-state'>Unknown block.</div>", status_code=400
        )

    params: dict[str, Any] = {}
    for pname, pspec in BLOCK_CATALOG[btype]["params"].items():
        raw = form.get(f"p_{pname}")
        if raw is None:
            continue
        if pspec["type"] == "int":
            try:
                params[pname] = int(raw)
            except (ValueError, TypeError):
                params[pname] = pspec.get("default", 0)
        elif pspec["type"] == "float":
            try:
                params[pname] = float(raw)
            except (ValueError, TypeError):
                params[pname] = pspec.get("default", 0.0)
        else:
            params[pname] = raw

    sid = _sid(request)
    _drafts(sid).append(SignalBlock(type=btype, role=role, params=params))

    return templates.TemplateResponse(
        request,
        "fragments/drafts_list.html",
        {"drafts": _drafts(sid), "block_catalog": BLOCK_CATALOG},
    )


@router.delete("/drafts/{index}", response_class=HTMLResponse)
async def delete_draft(request: Request, index: int):
    from server import templates

    sid = _sid(request)
    drafts = _drafts(sid)
    if 0 <= index < len(drafts):
        drafts.pop(index)
    return templates.TemplateResponse(
        request,
        "fragments/drafts_list.html",
        {"drafts": drafts, "block_catalog": BLOCK_CATALOG},
    )


@router.post("/suggest", response_class=HTMLResponse)
async def suggest(request: Request):
    """Ask Claude to design a full strategy; overwrite drafts with the proposal."""
    from server import templates

    state = get_state()
    history, _, _, _ = state.snapshot()
    catalog = load_catalog()

    proposal, _usage = propose_composed_strategy(history, catalog)

    sid = _sid(request)
    _DRAFTS[sid] = [
        SignalBlock(type=b["type"], role=b["role"], params=b["params"])
        for b in proposal["blocks"]
    ]

    return templates.TemplateResponse(
        request,
        "fragments/suggestion_result.html",
        {
            "drafts": _drafts(sid),
            "block_catalog": BLOCK_CATALOG,
            "name": proposal["name"],
            "description": proposal["description"],
            "options": proposal.get("strategy_options", {}),
        },
    )


@router.post("/save")
async def save(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    trade_size: float = Form(0.1),
    entry_logic: str = Form("OR"),
    exit_logic: str = Form("OR"),
    order_type: str = Form("market"),
    limit_offset_bps: float = Form(0.0),
    use_bracket: str = Form(""),
    sl_type: str = Form("percent"),
    sl_value: float = Form(2.0),
    tp_type: str = Form("off"),
    tp_value: float = Form(4.0),
    atr_period: int = Form(14),
    allow_short: str = Form(""),
    trade_size_mode: str = Form("fixed_usdt"),
    trade_size_percent: float = Form(5.0),
    trade_size_atr_risk: float = Form(1.0),
    trade_size_usdt: float = Form(1000.0),
    emulate: str = Form(""),
    trend_filter: str = Form(""),
    trend_interval: str = Form("60"),
    trend_ema_period: int = Form(50),
):
    sid = _sid(request)
    drafts = _drafts(sid)
    spec = ComposedStrategySpec(
        id=new_spec_id(),
        name=(name or "unnamed").strip(),
        description=description.strip(),
        blocks=list(drafts),
        trade_size=float(trade_size),
        entry_logic=entry_logic if entry_logic in ("OR", "AND") else "OR",
        exit_logic=exit_logic if exit_logic in ("OR", "AND") else "OR",
        order_type=order_type if order_type in ("market", "limit") else "market",
        limit_offset_bps=float(limit_offset_bps),
        use_bracket=bool(use_bracket),
        sl_type=sl_type if sl_type in ("percent", "atr") else "percent",
        sl_value=float(sl_value),
        tp_type=tp_type if tp_type in ("percent", "atr", "off") else "off",
        tp_value=float(tp_value),
        atr_period=int(atr_period),
        allow_short=bool(allow_short),
        trade_size_mode=trade_size_mode
        if trade_size_mode in ("fixed", "fixed_usdt", "percent_equity", "atr_target")
        else "fixed",
        trade_size_percent=float(trade_size_percent),
        trade_size_atr_risk=float(trade_size_atr_risk),
        trade_size_usdt=float(trade_size_usdt),
        emulate=bool(emulate),
        # Multi-timeframe: trade on the main TF + EMA trend confirmation on the
        # trend_interval TF (entries against the trend are suppressed). The engine
        # loads the secondary bar feed only when trend_filter=True; look-ahead safe
        # (Nautilus is event-driven — the secondary bar arrives only WHEN CLOSED).
        trend_filter=bool(trend_filter),
        trend_interval=(trend_interval or "60").strip(),
        trend_ema_period=int(trend_ema_period),
    )
    err = spec.validate()
    if err:
        return RedirectResponse(f"/strategy?error={err}", status_code=303)

    # M14/H1(strategy): a lockless load→append→save was losing strategies under
    # concurrent runs — use the locked append_to_catalog.
    from composer import append_to_catalog

    append_to_catalog(spec)
    _DRAFTS[sid] = []
    return RedirectResponse(f"/strategy?saved={spec.name}", status_code=303)


@router.post("/blocks/generate", response_class=HTMLResponse)
async def generate_custom_block(request: Request):
    """Ask Claude to design a new custom block. Returns a preview fragment.

    On failure returns an error fragment with the failure message.
    """
    import custom_block_store as cbs
    from agent import GeneratedCodeError, propose_custom_block
    from server import templates

    form = await request.form()
    label = (form.get("label") or "").strip()
    description = (form.get("description") or "").strip()
    role_hint = form.get("role_hint") or "entry"

    if not label or not description:
        return HTMLResponse(
            "<div class='empty-state'>Label and description are required.</div>",
            status_code=400,
        )
    if role_hint not in ("entry", "exit", "both"):
        role_hint = "entry"

    try:
        proposal = propose_custom_block(label, description, role_hint)
    except GeneratedCodeError as e:
        return templates.TemplateResponse(
            request,
            "fragments/custom_block_generated.html",
            {
                "error": str(e),
                "label": label,
                "description": description,
                "role_hint": role_hint,
            },
        )
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "fragments/custom_block_generated.html",
            {
                "error": f"{type(e).__name__}: {e}",
                "label": label,
                "description": description,
                "role_hint": role_hint,
            },
        )

    # Ignore Claude's suggested name — always use the user's label as the slug,
    # so the block appears under the exact name they typed.
    user_slug = _slugify(label)
    if not cbs.is_valid_name(user_slug):
        return templates.TemplateResponse(
            request,
            "fragments/custom_block_generated.html",
            {
                "error": f"Label '{label}' could not be converted into a valid name. It must contain at least 2 letters.",
                "label": label,
                "description": description,
                "role_hint": role_hint,
            },
        )
    proposal["name"] = user_slug
    # Preserve the user's exact label in meta.label too.
    if isinstance(proposal.get("meta"), dict):
        proposal["meta"]["label"] = label

    # Reject name collisions with existing blocks upfront.
    if proposal["name"] in BLOCK_REGISTRY:
        return templates.TemplateResponse(
            request,
            "fragments/custom_block_generated.html",
            {
                "error": f"This name ({proposal['name']}) is already in use. Choose a different label.",
                "label": label,
                "description": description,
                "role_hint": role_hint,
            },
        )

    # Run the generated evaluate() on recent BTC 1m closes to produce a
    # signal preview. Non-fatal: if anything fails we just skip the chart.
    chart_data: dict = {}
    try:
        import math as _math
        import statistics as _stats

        import pandas as _pd

        from data import BYBIT_CACHE_DIR

        cache_path = BYBIT_CACHE_DIR / "linear_BTCUSDT_1m.parquet"
        if cache_path.exists():
            df_raw = _pd.read_parquet(cache_path)
            if len(df_raw) >= 300:
                df_raw = df_raw.iloc[-300:]
            closes = [float(x) for x in df_raw["close"].tolist()]
            highs = [float(x) for x in df_raw["high"].tolist()]
            lows = [float(x) for x in df_raw["low"].tolist()]
            volumes = [float(x) for x in df_raw["volume"].tolist()]
            dates = [str(df_raw.index[i])[:16] for i in range(len(df_raw))]

        import builtins as _builtins_mod

        import indicators as _ind_mod
        from codegate import (
            _ALLOWED_BUILTINS,
            compile_with_loop_budget,
            has_builtin,
            validate_generated_code,
        )

        # H490/M527: the preview must be at PARITY with the production smoke/runtime —
        # previously it injected bare function names (sqrt, mean...), so blocks
        # reading math.*/ind.*/OHLC silently produced no signals; and a budgetless
        # plain compile could freeze the server (event loop) in a data-dependent
        # infinite loop. Now: validate with codegate → inject full math/statistics/ind
        # modules → compile with a loop budget → provide real OHLCV.
        validate_generated_code(proposal["code"])  # dunder/import/loop gates
        _ALLOWED: dict = {
            "__builtins__": {
                k: getattr(_builtins_mod, k)
                for k in _ALLOWED_BUILTINS
                if has_builtin(k)
            },
            "math": _math,
            "statistics": _stats,
            "ind": _ind_mod,
        }
        ns: dict = {}
        exec(compile_with_loop_budget(proposal["code"], "<preview>"), _ALLOWED, ns)
        for k, v in ns.items():
            if callable(v) and not k.startswith("_"):
                _ALLOWED[k] = v
        # M1084: budget preamble names landed in ns; functions look them up in globals.
        for _bk in ("__budget", "__budget_tick"):
            if _bk in ns:
                _ALLOWED[_bk] = ns[_bk]
        ev = ns.get("evaluate")
        if ev is None:
            raise ValueError("evaluate() function not found")

        class _Block:
            def __init__(self, params, role):
                self.params = params
                self.role = role
                self.type = "custom"

        class _Port:
            def is_net_long(self, *a):
                return False

            def is_net_short(self, *a):
                return False

            def is_flat(self, *a):
                return True

        default_params = {
            k: v.get("default")
            for k, v in (proposal.get("meta") or {}).get("params", {}).items()
        }
        block, port, state = _Block(default_params, role_hint), _Port(), {}
        WINDOW = 150
        start_i = max(0, len(closes) - WINDOW)
        signals = []
        for i in range(start_i, len(closes)):
            # M527: provide highs/lows/volumes to indicators like the runtime does.
            _ind_dict = {
                "highs": highs[: i + 1],
                "lows": lows[: i + 1],
                "volumes": volumes[: i + 1],
            }
            try:
                sig = ev(state, block, closes[: i + 1], _ind_dict, port)
            except Exception:
                sig = None
            signals.append({"i": i - start_i, "sig": sig})
        chart_data = {
            "closes": closes[start_i:],
            "dates": dates[start_i:],
            "signals": signals,
        }
    except Exception:
        pass

    return templates.TemplateResponse(
        request,
        "fragments/custom_block_generated.html",
        {
            "proposal": proposal,
            "label": label,
            "description": description,
            "role_hint": role_hint,
            "chart_data": chart_data,
        },
    )


@router.post("/blocks/save-custom", response_class=HTMLResponse)
async def save_custom_block(
    request: Request,
    name: str = Form(...),
    meta_json: str = Form(...),
    code: str = Form(...),
    prompt: str = Form(""),
):
    """Persist a validated custom block to disk and register it live."""
    import json as _json

    import custom_block_store as cbs
    from agent import _test_execute_generated
    from codegate import GeneratedCodeError, validate_generated_code

    if name in BLOCK_REGISTRY:
        return HTMLResponse(
            f"<div class='empty-state'>Name '{name}' is already registered.</div>",
            status_code=409,
        )
    if not cbs.is_valid_name(name):
        return HTMLResponse(
            f"<div class='empty-state'>Invalid name: {name}. Lowercase snake_case required.</div>",
            status_code=400,
        )

    try:
        meta = _json.loads(meta_json)
    except Exception as e:
        return HTMLResponse(
            f"<div class='empty-state'>meta JSON error: {e}</div>", status_code=400
        )

    # Re-validate defense-in-depth — form input could be tampered with.
    # M591: require_max_lookback=True — the M16 requirement on the production path
    # must also apply at this layer precisely to catch code tampered with via the
    # form (it was being bypassed with the default False).
    try:
        validate_generated_code(code)
        _test_execute_generated(code, meta=meta, require_max_lookback=True)
    except GeneratedCodeError as e:
        return HTMLResponse(
            f"<div class='empty-state'>Code rejected: {e}</div>", status_code=400
        )

    try:
        cbs.save_custom(name, meta, code, prompt=prompt)
        register_custom_from_disk(name)
    except Exception as e:
        return HTMLResponse(
            f"<div class='empty-state'>Save error: {type(e).__name__}: {e}</div>",
            status_code=500,
        )

    # Full page reload — the block-type dropdown lives in multiple fragments.
    resp = HTMLResponse("")
    resp.headers["HX-Redirect"] = "/strategy?saved_block=" + name
    return resp


@router.delete("/blocks/custom/{name}", response_class=HTMLResponse)
async def delete_custom_block(request: Request, name: str):
    import custom_block_store as cbs
    from server import templates

    if name in BLOCK_REGISTRY and BLOCK_REGISTRY[name].get("builtin"):
        return HTMLResponse(
            f"<div class='empty-state'>Built-in block cannot be deleted: {name}</div>",
            status_code=400,
        )
    # M621: when a block was deleted, the specs using it were silently filtered
    # out and PERMANENTLY removed from the catalog on the next load_catalog. If
    # there is a dependent strategy, refuse to delete (unless explicitly requested
    # with force=1).
    force = request.query_params.get("force") == "1"
    if not force:
        from composer import load_catalog

        dependents = [
            s.name for s in load_catalog() if any(b.type == name for b in s.blocks)
        ]
        if dependents:
            preview = ", ".join(dependents[:5]) + ("…" if len(dependents) > 5 else "")
            return HTMLResponse(
                f"<div class='empty-state'>⚠ {len(dependents)} strategies use the "
                f"'{name}' block ({preview}). Deleting it also permanently deletes "
                f"those strategies. To delete anyway, add ?force=1.</div>",
                status_code=409,
            )
    cbs.delete_custom(name)
    unregister_custom_block(name)
    return templates.TemplateResponse(
        request,
        "fragments/custom_blocks_list.html",
        {"custom_blocks": cbs.list_custom()},
    )


@router.delete("/{spec_id}", response_class=HTMLResponse)
async def delete_spec(request: Request, spec_id: str):
    # M634: a lockless load→filter→save was racing with concurrent appends —
    # use the locked mutate_catalog (deletion is atomic).
    from composer import mutate_catalog
    from server import templates

    mutate_catalog(lambda cat: [s for s in cat if s.id != spec_id])
    catalog = load_catalog()
    return templates.TemplateResponse(
        request,
        "fragments/catalog_list.html",
        {"catalog": list(reversed(catalog))},
    )
