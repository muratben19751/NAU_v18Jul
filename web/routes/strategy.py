"""Strategy composer routes (visual signal-block builder).

Wiki References
---------------
Bkz: [[strategy_and_actor]], [[order_flow_pipeline]]

The Compose UI reflects the [[strategy_and_actor]] hierarchy.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from agent import propose_composed_strategy
from composer import (
    BLOCK_CATALOG,
    BLOCK_REGISTRY,
    SignalBlock,
    build_spec,
    load_catalog,
    register_custom_from_disk,
    unregister_custom_block,
)
from state import get_state
from web.shared import SESSION_COOKIE, ChatStore, render_md, session_id
from wiki_helper import read_wiki_page

router = APIRouter(prefix="/strategy")


# Multi-turn "AI ile düzenle" sohbet store'ları (web.shared.ChatStore — backtest'in
# "AI ile iyileştir" deseni genelleştirildi). A: custom block KODU, B: draft LİSTESİ.
# Ayrı örnekler — payload şemaları farklı; LLM çağrısı ASLA lock altında değil.
_BLOCK_CHAT = ChatStore()
_DRAFTS_CHAT = ChatStore()


# Draft state per anonymous session (in-memory; ephemeral by design)
_DRAFTS: dict[str, list[SignalBlock]] = {}
# _sid/COOKIE now live in web.shared (session_id/SESSION_COOKIE) so backtest.py
# can share the same session dimension. Thin aliases keep this module's existing
# call-sites (and tests) unchanged.
COOKIE = SESSION_COOKIE
_COOKIE_MAX_AGE = 3600
# Do not let _DRAFTS grow unbounded (every new sid left an entry, never evicted).
# Insertion-order dict → drop the oldest sid (rough LRU; drafts are short-lived).
_MAX_DRAFT_SESSIONS = 500


def _sid(request: Request, response: Response | None = None) -> str:
    return session_id(request, response)


def _sid_cookie(resp: Response, sid: str) -> Response:
    """Write the session cookie onto the response we actually return.

    FastAPI does NOT merge an injected ``Response`` param's cookies into a
    Response object returned from the handler (only into non-Response returns
    like dicts). Since every draft route returns a TemplateResponse, the cookie
    must be set on THAT object — otherwise a cookie-less first request (curl,
    tests, a fresh client hitting /strategy/drafts before /studio) never gets a
    ``nautlab_sid`` and its drafts vanish on the next call.
    """
    resp.set_cookie(
        SESSION_COOKIE, sid, httponly=True, samesite="lax", max_age=_COOKIE_MAX_AGE
    )
    return resp


def _drafts(sid: str) -> list[SignalBlock]:
    if sid not in _DRAFTS and len(_DRAFTS) >= _MAX_DRAFT_SESSIONS:
        # Drop the oldest session (insertion-order) — no unbounded memory growth.
        _DRAFTS.pop(next(iter(_DRAFTS)), None)
    return _DRAFTS.setdefault(sid, [])


# ── Advanced-option round-trip helpers ──────────────────────────────────────
# strategy_preview.html + advanced_options.html both read an ``options`` dict.
# These two helpers are inverses: form → options (for live preview during
# compose) and spec → options (for loading a saved strategy back into the form).
# The keys mirror the fields POST /strategy/save reads, so a round-trip is lossless
# for everything the save form actually persists.
_OPT_BOOL_KEYS = ("use_bracket", "allow_short", "emulate", "trend_filter")


def _as_bool(v: Any) -> bool:
    """Checkbox/string truthiness — mirrors composer.build_spec's coercion."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "on", "yes")


def _num(v: Any, default: float, *, cast=float):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default


def _options_from_form(form) -> dict:
    """Build the preview ``options`` dict from posted #save-form fields.

    ``form`` is a Starlette FormData (or any ``.get``-able mapping). Missing
    fields fall back to the same defaults studio.py seeds, so a partial form
    (e.g. before Advanced Options was touched) is safe.
    """
    g = form.get
    return {
        "entry_logic": g("entry_logic") or "OR",
        "exit_logic": g("exit_logic") or "OR",
        "order_type": g("order_type") or "market",
        "limit_offset_bps": _num(g("limit_offset_bps"), 0.0),
        "use_bracket": _as_bool(g("use_bracket")),
        "sl_type": g("sl_type") or "percent",
        "sl_value": _num(g("sl_value"), 2.0),
        "tp_type": g("tp_type") or "off",
        "tp_value": _num(g("tp_value"), 4.0),
        "atr_period": _num(g("atr_period"), 14, cast=int),
        "allow_short": _as_bool(g("allow_short")),
        "trade_size_mode": g("trade_size_mode") or "fixed_usdt",
        "trade_size_percent": _num(g("trade_size_percent"), 5.0),
        "trade_size_atr_risk": _num(g("trade_size_atr_risk"), 1.0),
        "trade_size_usdt": _num(g("trade_size_usdt"), 1000.0),
        # advanced_options.html/preview read ``trade_size_btc``; the form input
        # is named ``trade_size``.
        "trade_size_btc": _num(g("trade_size"), 0.1),
        "emulate": _as_bool(g("emulate")),
        "trend_filter": _as_bool(g("trend_filter")),
        "trend_interval": g("trend_interval") or "60",
        "trend_ema_period": _num(g("trend_ema_period"), 50, cast=int),
    }


def _options_from_spec(spec) -> dict:
    """Reconstruct the ``options`` dict from a saved ComposedStrategySpec — the
    inverse of _options_from_form, used when editing a saved strategy."""
    return {
        "entry_logic": spec.entry_logic,
        "exit_logic": spec.exit_logic,
        "order_type": spec.order_type,
        "limit_offset_bps": spec.limit_offset_bps,
        "use_bracket": spec.use_bracket,
        "sl_type": spec.sl_type,
        "sl_value": spec.sl_value,
        "tp_type": spec.tp_type,
        "tp_value": spec.tp_value,
        "atr_period": spec.atr_period,
        "allow_short": spec.allow_short,
        "trade_size_mode": spec.trade_size_mode,
        "trade_size_percent": spec.trade_size_percent,
        "trade_size_atr_risk": spec.trade_size_atr_risk,
        "trade_size_usdt": spec.trade_size_usdt,
        "trade_size_btc": spec.trade_size,
        "emulate": spec.emulate,
        "trend_filter": spec.trend_filter,
        "trend_interval": spec.trend_interval,
        "trend_ema_period": spec.trend_ema_period,
    }


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


def _preview_signals(code: str, meta: dict, role_hint: str) -> dict:
    """Run a validated evaluate() over recent BTC 1m closes to build a signal
    preview chart. Non-fatal: any failure returns {} (the caller skips the chart).

    Parity with the runtime/loader (H490/M527): codegate-validate → inject full
    math/statistics/ind modules → compile with a loop budget → feed real OHLCV.
    Shared by both the generate and the AI-edit flows (single source of truth).
    """
    try:
        import builtins as _builtins_mod
        import math as _math
        import statistics as _stats

        import pandas as _pd

        import indicators as _ind_mod
        from codegate import (
            _ALLOWED_BUILTINS,
            compile_with_loop_budget,
            has_builtin,
            validate_generated_code,
        )
        from data import BYBIT_CACHE_DIR

        cache_path = BYBIT_CACHE_DIR / "linear_BTCUSDT_1m.parquet"
        if not cache_path.exists():
            return {}
        df_raw = _pd.read_parquet(cache_path)
        if len(df_raw) >= 300:
            df_raw = df_raw.iloc[-300:]
        closes = [float(x) for x in df_raw["close"].tolist()]
        highs = [float(x) for x in df_raw["high"].tolist()]
        lows = [float(x) for x in df_raw["low"].tolist()]
        volumes = [float(x) for x in df_raw["volume"].tolist()]
        dates = [str(df_raw.index[i])[:16] for i in range(len(df_raw))]

        validate_generated_code(code)  # dunder/import/loop gates
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
        exec(compile_with_loop_budget(code, "<preview>"), _ALLOWED, ns)
        for k, v in ns.items():
            if callable(v) and not k.startswith("_"):
                _ALLOWED[k] = v
        # M1084: budget preamble names landed in ns; functions look them up in globals.
        for _bk in ("__budget", "__budget_tick"):
            if _bk in ns:
                _ALLOWED[_bk] = ns[_bk]
        ev = ns.get("evaluate")
        if ev is None:
            return {}

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
            k: v.get("default") for k, v in (meta or {}).get("params", {}).items()
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
        return {
            "closes": closes[start_i:],
            "dates": dates[start_i:],
            "signals": signals,
        }
    except Exception:
        return {}


@router.get("", response_class=HTMLResponse)
async def page(request: Request):
    # Merged into the unified Studio (Faz 4). The Composer now lives as the
    # "Compose" tab of /studio; this root path redirects there (deep-links and
    # bookmarks keep working). The /strategy/* HTMX endpoints below are unchanged.
    return RedirectResponse("/studio", status_code=307)


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

    return _sid_cookie(
        templates.TemplateResponse(
            request,
            "fragments/drafts_list.html",
            {
                "drafts": _drafts(sid),
                "block_catalog": BLOCK_CATALOG,
                "options": _options_from_form(form),
                "name": form.get("name") or "",
            },
        ),
        sid,
    )


@router.delete("/drafts/{index}", response_class=HTMLResponse)
async def delete_draft(request: Request, index: int):
    from server import templates

    form = await request.form()
    sid = _sid(request)
    drafts = _drafts(sid)
    if 0 <= index < len(drafts):
        drafts.pop(index)
    return _sid_cookie(
        templates.TemplateResponse(
            request,
            "fragments/drafts_list.html",
            {
                "drafts": drafts,
                "block_catalog": BLOCK_CATALOG,
                "options": _options_from_form(form),
                "name": form.get("name") or "",
            },
        ),
        sid,
    )


@router.post("/suggest", response_class=HTMLResponse)
async def suggest(request: Request):
    """Ask Claude to design a full strategy; overwrite drafts with the proposal.

    The user's current Description text (if any) is forwarded as a ``hint`` so
    the proposal reflects their intent, and is PRESERVED — a description the
    user typed is never overwritten by Claude's suggestion (only an empty
    field is filled).
    """
    from server import templates

    form = await request.form()
    user_desc = (form.get("description") or "").strip()

    state = get_state()
    history, _, _, _ = state.snapshot()
    catalog = load_catalog()

    proposal, _usage = propose_composed_strategy(history, catalog, hint=user_desc)

    sid = _sid(request)
    _DRAFTS[sid] = [
        SignalBlock(type=b["type"], role=b["role"], params=b["params"])
        for b in proposal["blocks"]
    ]

    # Preserve the user's own description; only fall back to Claude's when the
    # field was left empty.
    description = user_desc or proposal["description"]

    return _sid_cookie(
        templates.TemplateResponse(
            request,
            "fragments/suggestion_result.html",
            {
                "drafts": _drafts(sid),
                "block_catalog": BLOCK_CATALOG,
                "name": proposal["name"],
                "description": description,
                "options": proposal.get("strategy_options", {}),
            },
        ),
        sid,
    )


# ── B) Strateji taslağının BLOK LİSTESİNİ AI ile çok-turlu sohbetle düzenleme ─
def _blocks_to_dicts(blocks: list[SignalBlock]) -> list[dict]:
    return [{"type": b.type, "role": b.role, "params": b.params} for b in blocks]


def _render_drafts_chat(request, conv: dict, conv_id: str, options=None, name=""):
    from server import templates

    working = [
        SignalBlock(type=b["type"], role=b["role"], params=b["params"])
        for b in conv["working_blocks"]
    ]
    return templates.TemplateResponse(
        request,
        "fragments/drafts_chat_thread.html",
        {
            "stage": "chat",
            "conv_id": conv_id,
            "messages": conv["messages"],
            "preview_blocks": working,
            "block_catalog": BLOCK_CATALOG,
            "error": conv.get("last_error") or "",
            # Live Strategy Preview (OOB) reflects the PROPOSED blocks + the
            # user's current name/options while the chat is open.
            "drafts": working,
            "options": options or {},
            "name": name or "",
        },
    )


@router.post("/drafts/chat/new", response_class=HTMLResponse)
async def drafts_chat_new(request: Request):
    """Mevcut draft blok listesi için AI-düzenleme sohbeti başlat."""
    import asyncio
    import json as _json

    form = await request.form()
    sid = _sid(request)
    blocks = _blocks_to_dicts(_drafts(sid))
    if not blocks:
        return HTMLResponse(
            "<div class='empty-state'>Önce en az bir blok ekle, sonra AI ile düzenle.</div>",
            status_code=400,
        )

    from agent import chat_edit_blocks

    embedded = (
        "Bu strateji taslağının blok listesini düzenlemek istiyorum.\n\n"
        f"Mevcut bloklar (JSON):\n{_json.dumps(blocks, ensure_ascii=False)}\n\n"
        "Ne yapmak istediğimi soracağım."
    )
    first_user = {
        "role": "user",
        "content": embedded,
        "display": "Blok listesini AI ile düzenlemek istiyorum.",
    }
    reply = await asyncio.to_thread(chat_edit_blocks, blocks, [first_user])

    conv = {
        "sid": sid,
        "messages": [
            first_user,
            {"role": "assistant", "content": reply["text"]},
        ],
        "working_blocks": reply["blocks"],
        "last_error": reply.get("error") or "",
    }
    conv_id = _DRAFTS_CHAT.new(conv)
    return _render_drafts_chat(
        request, conv, conv_id, _options_from_form(form), form.get("name") or ""
    )


@router.post("/drafts/chat", response_class=HTMLResponse)
async def drafts_chat_turn(
    request: Request,
    conv_id: str = Form(""),
    message: str = Form(""),
):
    """Draft-düzenleme sohbetine bir tur ekle."""
    import asyncio

    from server import templates

    form = await request.form()
    opts = _options_from_form(form)
    name = form.get("name") or ""
    msg = (message or "").strip()
    conv = _DRAFTS_CHAT.get(conv_id)
    if conv is None:
        return templates.TemplateResponse(
            request, "fragments/drafts_chat_thread.html", {"stage": "expired"}
        )
    if not msg:
        return _render_drafts_chat(request, conv, conv_id, opts, name)

    from agent import chat_edit_blocks

    history = list(conv["messages"])
    user_turn = {"role": "user", "content": msg, "display": msg}
    reply = await asyncio.to_thread(
        chat_edit_blocks, conv["working_blocks"], history + [user_turn]
    )

    def _mutate(c):
        c["messages"].append(user_turn)
        c["messages"].append({"role": "assistant", "content": reply["text"]})
        if reply.get("changed"):
            c["working_blocks"] = reply["blocks"]
        c["last_error"] = reply.get("error") or ""

    updated = _DRAFTS_CHAT.commit(conv_id, _mutate)
    if updated is None:
        return templates.TemplateResponse(
            request, "fragments/drafts_chat_thread.html", {"stage": "expired"}
        )
    return _render_drafts_chat(request, updated, conv_id, opts, name)


@router.post("/drafts/chat/apply", response_class=HTMLResponse)
async def drafts_chat_apply(request: Request, conv_id: str = Form("")):
    """Sohbette düzenlenen blok listesini gerçek draft'a uygula."""
    from server import templates

    form = await request.form()
    conv = _DRAFTS_CHAT.get(conv_id)
    if conv is None:
        return templates.TemplateResponse(
            request, "fragments/drafts_chat_thread.html", {"stage": "expired"}
        )
    sid = _sid(request)
    _DRAFTS[sid] = [
        SignalBlock(type=b["type"], role=b["role"], params=b["params"])
        for b in conv["working_blocks"]
    ]
    return _sid_cookie(
        templates.TemplateResponse(
            request,
            "fragments/drafts_list.html",
            {
                "drafts": _drafts(sid),
                "block_catalog": BLOCK_CATALOG,
                "options": _options_from_form(form),
                "name": form.get("name") or "",
            },
        ),
        sid,
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
    edit_spec_id: str = Form(""),
):
    from server import templates

    sid = _sid(request)
    drafts = _drafts(sid)
    spec = build_spec(
        name=name,
        description=description,
        blocks=list(drafts),
        trade_size=trade_size,
        entry_logic=entry_logic,
        exit_logic=exit_logic,
        order_type=order_type,
        limit_offset_bps=limit_offset_bps,
        use_bracket=use_bracket,
        sl_type=sl_type,
        sl_value=sl_value,
        tp_type=tp_type,
        tp_value=tp_value,
        atr_period=atr_period,
        allow_short=allow_short,
        trade_size_mode=trade_size_mode,
        trade_size_percent=trade_size_percent,
        trade_size_atr_risk=trade_size_atr_risk,
        trade_size_usdt=trade_size_usdt,
        emulate=emulate,
        # Multi-timeframe: trade on the main TF + EMA trend confirmation on the
        # trend_interval TF (entries against the trend are suppressed). The engine
        # loads the secondary bar feed only when trend_filter=True; look-ahead safe
        # (Nautilus is event-driven — the secondary bar arrives only WHEN CLOSED).
        trend_filter=trend_filter,
        trend_interval=trend_interval,
        trend_ema_period=trend_ema_period,
    )
    err = spec.validate()
    if err:
        # HTMX in-place error banner (was a full-page redirect to /strategy?error=).
        return templates.TemplateResponse(
            request,
            "fragments/save_result.html",
            {"ok": False, "error": err},
            status_code=400,
        )

    # M14/H1(strategy): a lockless load→append→save was losing strategies under
    # concurrent runs — use the locked append_to_catalog / mutate_catalog.
    from composer import append_to_catalog, load_catalog, mutate_catalog

    eid = edit_spec_id.strip()
    if eid and any(s.id == eid for s in load_catalog()):
        # Edit (overwrite) mode: keep the original id and replace the record
        # in place so the catalog keeps ONE entry. build_spec always mints a
        # fresh id, so pin it back to the edited spec's id.
        spec.id = eid
        mutate_catalog(lambda cat: [spec if s.id == eid else s for s in cat])
        overwrote = True
    else:
        # New strategy (also the "⎘ Kopyala" path — edit_spec_id is blank there,
        # or the original was deleted meanwhile → safe append).
        append_to_catalog(spec)
        overwrote = False
    _DRAFTS[sid] = []

    # HTMX response (was RedirectResponse to /strategy?saved=…, which forced a
    # full-page reload that wiped the studio mode / open chat / backtest panel).
    # Return the saved banner + OOB-refresh the catalog list and reset the
    # composer form (drafts cleared, edit-spec-id blanked).
    return _sid_cookie(
        templates.TemplateResponse(
            request,
            "fragments/save_result.html",
            {
                "ok": True,
                "spec": spec,
                "overwrote": overwrote,
                "catalog": list(reversed(load_catalog())),
                "drafts": [],
                "block_catalog": BLOCK_CATALOG,
                "options": {},
                "name": "My Strategy",
            },
        ),
        sid,
    )


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
    chart_data = _preview_signals(
        proposal["code"], proposal.get("meta") or {}, role_hint
    )

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


# ── A) Custom block KODUNU AI ile çok-turlu sohbetle düzenleme ──────────────
def _dependent_spec_names(block_name: str) -> list[str]:
    """Bu custom block tipini kullanan kayıtlı strateji adları (uyarı için)."""
    return [
        s.name for s in load_catalog() if any(b.type == block_name for b in s.blocks)
    ]


def _render_block_chat(request, conv: dict, conv_id: str):
    """block_chat_thread.html'i mevcut sohbet durumuyla render et."""
    from server import templates

    name = conv["name"]
    return templates.TemplateResponse(
        request,
        "fragments/block_chat_thread.html",
        {
            "stage": "chat",
            "conv_id": conv_id,
            "name": name,
            "messages": conv["messages"],
            "proposal": {
                "name": name,
                "meta": conv["working_meta"],
                "code": conv["working_code"],
            },
            "chart_data": conv.get("chart_data") or {},
            "error": conv.get("last_error") or "",
            "dependents": _dependent_spec_names(name),
        },
    )


@router.post("/blocks/{name}/chat/new", response_class=HTMLResponse)
async def block_chat_new(request: Request, name: str):
    """Bir custom block için AI-düzenleme sohbeti başlat."""
    import asyncio

    import custom_block_store as cbs

    info = cbs.get_custom(name)
    if info is None:
        return HTMLResponse(
            f"<div class='empty-state'>Custom block bulunamadı: {name}</div>",
            status_code=404,
        )
    if name in BLOCK_REGISTRY and BLOCK_REGISTRY[name].get("builtin"):
        return HTMLResponse(
            "<div class='empty-state'>Yerleşik (built-in) bloklar düzenlenemez.</div>",
            status_code=400,
        )
    # get_custom KODU döndürmez — diskten oku (composer.py:1101 deseni, UTF-8 şart).
    try:
        code = cbs.module_path(name).read_text(encoding="utf-8")
    except OSError as e:
        return HTMLResponse(
            f"<div class='empty-state'>Blok kodu okunamadı: {e}</div>", status_code=500
        )
    meta = info.get("meta") or {}

    # İlk user mesajı: modele mevcut kod+meta gömülü; ekranda kısa görünüm (display).
    import json as _json

    from agent import chat_edit_block

    embedded = (
        f"Bu bloğu düzenlemek istiyorum. Blok adı (name, DEĞİŞMEZ): {name}\n\n"
        f"Mevcut meta (JSON):\n{_json.dumps(meta, ensure_ascii=False)}\n\n"
        f"Mevcut kod:\n{code}\n\n"
        "Önce ne yapmak istediğimi soracağım; hazır ol."
    )
    first_user = {
        "role": "user",
        "content": embedded,
        "display": "Bu bloğu AI ile düzenlemek istiyorum.",
    }
    reply = await asyncio.to_thread(chat_edit_block, name, meta, code, [first_user])

    conv = {
        "name": name,
        "messages": [
            first_user,
            {"role": "assistant", "content": reply["text"]},
        ],
        "working_meta": reply["meta"],
        "working_code": reply["code"],
        "last_error": reply.get("error") or "",
        "chart_data": _preview_signals(reply["code"], reply["meta"], "entry")
        if reply.get("changed")
        else {},
    }
    conv_id = _BLOCK_CHAT.new(conv)
    return _render_block_chat(request, conv, conv_id)


@router.post("/blocks/chat", response_class=HTMLResponse)
async def block_chat_turn(
    request: Request,
    conv_id: str = Form(""),
    message: str = Form(""),
):
    """Custom block düzenleme sohbetine bir tur ekle."""
    import asyncio

    from server import templates

    msg = (message or "").strip()
    conv = _BLOCK_CHAT.get(conv_id)
    if conv is None:
        return templates.TemplateResponse(
            request, "fragments/block_chat_thread.html", {"stage": "expired"}
        )
    if not msg:
        return _render_block_chat(request, conv, conv_id)

    from agent import chat_edit_block

    history = list(conv["messages"])
    user_turn = {"role": "user", "content": msg, "display": msg}
    reply = await asyncio.to_thread(
        chat_edit_block,
        conv["name"],
        conv["working_meta"],
        conv["working_code"],
        history + [user_turn],
    )
    chart = (
        _preview_signals(reply["code"], reply["meta"], "entry")
        if reply.get("changed")
        else None
    )

    def _mutate(c):
        c["messages"].append(user_turn)
        c["messages"].append({"role": "assistant", "content": reply["text"]})
        if reply.get("changed"):
            c["working_meta"] = reply["meta"]
            c["working_code"] = reply["code"]
            c["chart_data"] = chart
        c["last_error"] = reply.get("error") or ""

    updated = _BLOCK_CHAT.commit(conv_id, _mutate)
    if updated is None:
        return templates.TemplateResponse(
            request, "fragments/block_chat_thread.html", {"stage": "expired"}
        )
    return _render_block_chat(request, updated, conv_id)


@router.post("/blocks/chat/save", response_class=HTMLResponse)
async def block_chat_save(request: Request, conv_id: str = Form("")):
    """Sohbette düzenlenen bloğu kaydet. save-custom'ın 409 kontrolünü ATLAR
    (aynı isimle üzerine yazmak edit'in amacıdır); güvenlik doğrulaması aynen kalır.
    """
    import custom_block_store as cbs
    from agent import _test_execute_generated
    from codegate import GeneratedCodeError, validate_generated_code

    conv = _BLOCK_CHAT.get(conv_id)
    if conv is None:
        return HTMLResponse(
            "<div class='empty-state'>Sohbet süresi doldu. Lütfen yeniden başlat.</div>",
            status_code=409,
        )
    name = conv["name"]
    meta = conv["working_meta"]
    code = conv["working_code"]

    if not cbs.is_valid_name(name):
        return HTMLResponse(
            f"<div class='empty-state'>Geçersiz ad: {name}</div>", status_code=400
        )
    # Defense-in-depth: kaydetmeden önce tekrar doğrula (save-custom:583-585 ile aynı).
    try:
        validate_generated_code(code)
        _test_execute_generated(code, meta=meta, require_max_lookback=True)
    except GeneratedCodeError as e:
        return HTMLResponse(
            f"<div class='empty-state'>Kod reddedildi: {e}</div>", status_code=400
        )

    try:
        cbs.save_custom(name, meta, code, prompt="AI ile düzenlendi")
        # Diskteki eski modülü at, yeniyi import et (davranış tazelensin).
        unregister_custom_block(name)
        register_custom_from_disk(name)
    except Exception as e:
        return HTMLResponse(
            f"<div class='empty-state'>Kaydetme hatası: {type(e).__name__}: {e}</div>",
            status_code=500,
        )

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
        {"custom_blocks": cbs.list_custom(include_ephemeral=False)},
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


@router.post("/{spec_id}/edit", response_class=HTMLResponse)
async def edit_spec(request: Request, spec_id: str, mode: str = "overwrite"):
    """Load a saved strategy back into the composer for editing.

    ``mode=overwrite`` (✎) pins the hidden ``edit_spec_id`` so the next Save
    updates the SAME catalog record. ``mode=copy`` (⎘) leaves it blank so Save
    creates a new record and the original is preserved.

    Returns the drafts list (→ #drafts, carries the Strategy Preview OOB) plus
    OOB swaps for name/description/advanced-options/edit-spec-id — mirroring the
    /suggest → suggestion_result.html pattern.
    """
    from server import templates

    spec = next((s for s in load_catalog() if s.id == spec_id), None)
    if spec is None:
        return HTMLResponse(
            "<div class='empty-state'>Strateji bulunamadı (silinmiş olabilir).</div>",
            status_code=404,
        )

    sid = _sid(request)
    _DRAFTS[sid] = [
        SignalBlock(type=b.type, role=b.role, params=dict(b.params))
        for b in spec.blocks
    ]
    options = _options_from_spec(spec)
    edit_id = spec.id if mode != "copy" else ""

    return _sid_cookie(
        templates.TemplateResponse(
            request,
            "fragments/edit_result.html",
            {
                "drafts": _drafts(sid),
                "block_catalog": BLOCK_CATALOG,
                "name": spec.name,
                "description": spec.description,
                "options": options,
                "edit_spec_id": edit_id,
            },
        ),
        sid,
    )
