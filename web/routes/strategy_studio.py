"""Strategy Studio — visual strategy builder routes.

Wiki References
---------------
See: [[strategy_studio]], [[strategy_and_actor]], [[backtesting_guide]], [[portfolio]]

Mounted on the main app in ``server.py``. Owns ``/studio/{strategy_id}`` and
its HTMX action endpoints; the plain ``/studio`` entry point belongs to the
Composer+Backtest page in ``web/routes/studio.py``.

Templates live in ``web/templates/studio/``, static assets in
``web/static/studio.{css,js}`` — both served by the host app's existing mounts.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections import OrderedDict

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from strategy_studio.ai import (
    GuardrailReject,
    HttpAnthropicClient,
    Suggestion,
    SuggestionFailure,
    apply_suggestion,
    build_prompt,
    evaluate_trial,
    improved,
    parse_suggestion,
)
from strategy_studio.backtest import (
    BacktestMetrics,
    NautilusBacktestAdapter,
    StubBacktestAdapter,
    UnsupportedStrategy,
)
from strategy_studio.compiler import CompileError, compile_strategy
from strategy_studio.deploy import (
    DEFAULT_GATE_DSR,
    DeployBlocked,
    DeployConfig,
    prepare_deployment,
)
from strategy_studio.mutations import (
    MutationError,
    RuleNotFound,
    add_rule,
    delete_rule,
    find_rule,
    set_block_attr,
    set_optimize_range,
    set_regime_else,
    toggle_instrument,
    toggle_optimize,
    update_allocation,
    update_risk,
    update_rule_param,
)
from strategy_studio.optimizer import (
    OPTIMIZER_MAX_RUNS,
    STUB_MAX_EVALS,
    OptResult,
    StubWalkForwardOptimizer,
    apply_params,
)
from strategy_studio.registry import INDICATOR_REGISTRY, library_by_category
from strategy_studio.schema import StrategyDefinition
from strategy_studio.store import StrategyStore

router = APIRouter()

store = StrategyStore()


def _tpl():
    """Host app's Jinja environment (late import: server imports this module)."""
    from server import templates

    return templates

def _adapter_for(env_var: str):
    """Stub unless `env_var` selects the real engine.

    The stub is the default everywhere so the studio stays usable — and the
    suite stays offline — without market data. Both satisfy BacktestAdapter.
    """
    return (NautilusBacktestAdapter()
            if os.environ.get(env_var, "").lower() == "nautilus"
            else StubBacktestAdapter())


# One run per click — STUDIO_BACKTEST=nautilus is safe to flip on its own.
ADAPTER = _adapter_for("STUDIO_BACKTEST")

# Sweeps and AI trials fan out: the optimizer calls run() once per combination
# (capped at OPTIMIZER_MAX_RUNS) and the AI loop once per suggestion, while
# NautilusBacktestAdapter itself costs 1 + walkforward.folds engine runs per
# call. Sharing ADAPTER would therefore turn a 100-combination sweep into ~700
# Nautilus runs the moment the single-run switch was flipped. Its own switch
# keeps the layers independently verifiable; set STUDIO_BACKTEST_OPT=nautilus
# only once a sweep at real-engine cost is actually what you want.
TRIAL_ADAPTER = _adapter_for("STUDIO_BACKTEST_OPT")

# Ceiling on engine runs for one sweep once TRIAL_ADAPTER is the real engine.
ENGINE_SWEEP_MAX_RUNS = int(os.environ.get("STUDIO_OPT_MAX_ENGINE_RUNS", "200"))

# INTEGRATION POINT: swap for your real walk-forward optimizer (optimizer.py)
OPTIMIZER = StubWalkForwardOptimizer(adapter=TRIAL_ADAPTER)
# INTEGRATION POINT: swap for the LLM client of your existing loop (ai.py)
LLM = HttpAnthropicClient()

_BLOCK_TEMPLATE = {
    "entry": ("studio/_rule_group.html",
              {"block_kind": "entry", "title": "Long conditions"}),
    "exit": ("studio/_rule_group.html",
             {"block_kind": "exit", "title": "Exit conditions"}),
    "regime": ("studio/_regime_block.html", {}),
    "sub_entry": ("studio/_regime_block.html", {}),
    "sub_exit": ("studio/_regime_block.html", {}),
    "risk": ("studio/_risk_block.html", {}),
    "allocation": ("studio/_allocation_block.html", {}),
}


def _ctx(request: Request, defn: StrategyDefinition, **extra) -> dict:
    return {"request": request, "defn": defn,
            "registry": INDICATOR_REGISTRY, **extra}


def _side_ctx(request: Request, defn: StrategyDefinition, **extra) -> dict:
    opt_run = store.latest_opt(defn.id)
    opt_results = None
    if opt_run and opt_run["status"] == "done" and opt_run["results"]:
        opt_results = [OptResult(**r) for r in json.loads(opt_run["results"])]
    iters = []
    for row in store.iterations(defn.id):
        it = dict(row)
        try:
            it["sugg"] = Suggestion.model_validate_json(row["suggestion"])
        except Exception:  # noqa: BLE001 — failed suggestions store raw notes
            it["sugg"] = None
        it["trial"] = (BacktestMetrics.from_json(row["trial_metrics"])
                       if row["trial_metrics"] else None)
        it["base"] = (BacktestMetrics.from_json(row["baseline"])
                      if row["baseline"] else None)
        iters.append(it)
    return _ctx(request, defn, opt_run=opt_run, opt_results=opt_results,
                max_runs=OPTIMIZER_MAX_RUNS,
                ai_loop=store.latest_loop(defn.id), ai_iters=iters, **extra)


def _render_side(request: Request, defn: StrategyDefinition,
                 oob: bool = False) -> str:
    return _tpl().get_template("studio/_side_panel.html").render(
        _side_ctx(request, defn, oob=oob))


def _render_block(request: Request, defn: StrategyDefinition, block: str,
                  oob_side: bool = True) -> HTMLResponse:
    """Re-render one block; append the side panel out-of-band so sweep
    size / param cards stay in sync after any mutation."""
    html = _block_html(request, defn, block)
    if oob_side:
        html += _render_side(request, defn, oob=True)
    return HTMLResponse(html)


def _block_html(request: Request, defn: StrategyDefinition, block: str,
                oob: bool = False) -> str:
    if block in ("sub_entry", "sub_exit"):
        block = "regime"                     # sub rules live inside the card
    tpl, extra = _BLOCK_TEMPLATE[block]
    ctx = _ctx(request, defn, **extra)
    if block == "regime":
        ctx["regime"] = defn.regime
    elif block == "risk":
        ctx["risk"] = defn.risk
    elif block == "allocation":
        ctx["allocation"] = defn.allocation
    else:
        ctx["group"] = defn.entry if block == "entry" else defn.exit
    ctx["ghosts"] = _ghost_ctx(defn, block)
    html = _tpl().get_template(tpl).render(ctx)
    if oob:
        html = html.replace(f'id="block-{block}"',
                            f'id="block-{block}" hx-swap-oob="true"', 1)
    return html


def _ghost_of(row: dict) -> dict:
    return {
        "id": row["id"],
        "source": row["source"],
        "s": Suggestion.model_validate_json(row["suggestion"]),
        "trial": (BacktestMetrics.from_json(row["trial_metrics"])
                  if row["trial_metrics"] else None),
        "base": (BacktestMetrics.from_json(row["baseline"])
                 if row["baseline"] else None),
    }


def _ghost_ctx(defn: StrategyDefinition, block: str) -> list[dict]:
    return [_ghost_of(r) for r in store.pending_suggestions(defn.id, block)]


def _ghosts_by_block(defn: StrategyDefinition) -> dict[str, list[dict]]:
    """All pending ghosts in ONE query, grouped by block.

    The page renders seven blocks; asking per block turned a page load into
    seven identical-shaped queries (plus a fresh sqlite connection each).
    """
    grouped: dict[str, list[dict]] = {b: [] for b in _BLOCK_TEMPLATE}
    for row in store.pending_suggestions(defn.id):
        grouped.setdefault(row["block"], []).append(_ghost_of(row))
    return grouped


def _block_of_owner(defn: StrategyDefinition, owner: str) -> str:
    if owner == "risk":
        return "risk"
    block, *_ = find_rule(defn, owner)
    return block


def _load_working(strategy_id: str) -> StrategyDefinition:
    try:
        defn, _ = store.working_copy(strategy_id)
    except KeyError:
        raise HTTPException(404, f"strategy '{strategy_id}' not found")
    return defn


# ── pages ────────────────────────────────────────────────────────

@router.get("/studio/{strategy_id}", response_class=HTMLResponse)
def studio_page(request: Request, strategy_id: str,
                version: int | None = None):
    try:
        if version is not None:
            defn, is_draft = store.load(strategy_id, version), False
        else:
            defn, is_draft = store.working_copy(strategy_id)
    except KeyError:
        raise HTTPException(404, f"strategy '{strategy_id}' not found")
    run = store.latest_run(strategy_id)
    metrics = spark = None
    if run and run["status"] == "done" and run["metrics"]:
        metrics = BacktestMetrics.from_json(run["metrics"])
        spark = _spark_path(metrics.equity_curve)
    return _tpl().TemplateResponse(request, "studio/page.html", _side_ctx(
        request, defn, library=library_by_category(), is_draft=is_draft,
        initial_run=run, initial_metrics=metrics, initial_spark=spark,
        ghosts_by_block=_ghosts_by_block(defn)))


@router.get("/studio/{strategy_id}/history")
def studio_history(strategy_id: str):
    return store.history(strategy_id)


@router.get("/healthz")
def healthz():
    return {"ok": True}


# ── mutations (Phase 2) ──────────────────────────────────────────

@router.post("/studio/{strategy_id}/blocks/{block}/rules")
def route_add_rule(request: Request, strategy_id: str, block: str,
                   indicator: str = Form(...),
                   as_filter: bool = Form(False)):
    defn = _load_working(strategy_id)
    try:
        add_rule(defn, block, indicator, as_filter)
    except MutationError as e:
        return PlainTextResponse(str(e), status_code=422)
    store.save_draft(defn)
    return _render_block(request, defn, block)


@router.patch("/studio/{strategy_id}/rules/{rule_id}")
def route_edit_rule(request: Request, strategy_id: str, rule_id: str,
                    param: str = Form(...), value: str = Form(...)):
    defn = _load_working(strategy_id)
    try:
        update_rule_param(defn, rule_id, param, value)
        block, *_ = find_rule(defn, rule_id)
    except RuleNotFound as e:
        raise HTTPException(404, str(e))
    except MutationError as e:
        return PlainTextResponse(str(e), status_code=422)
    store.save_draft(defn)
    return _render_block(request, defn, block)


@router.delete("/studio/{strategy_id}/rules/{rule_id}")
def route_delete_rule(request: Request, strategy_id: str, rule_id: str):
    defn = _load_working(strategy_id)
    try:
        block = delete_rule(defn, rule_id)
    except RuleNotFound as e:
        raise HTTPException(404, str(e))
    except MutationError as e:
        return PlainTextResponse(str(e), status_code=422)
    store.save_draft(defn)
    return _render_block(request, defn, block)


@router.patch("/studio/{strategy_id}/blocks/{block}")
def route_block_attr(request: Request, strategy_id: str, block: str,
                     match: str | None = Form(None),
                     evaluate: str | None = Form(None),
                     else_mode: str | None = Form(None)):
    defn = _load_working(strategy_id)
    try:
        if else_mode is not None:
            if block != "regime":
                return PlainTextResponse(
                    "else_mode only applies to the regime block",
                    status_code=422)
            set_regime_else(defn, else_mode)
        else:
            set_block_attr(defn, block, match=match, evaluate=evaluate)
    except MutationError as e:
        return PlainTextResponse(str(e), status_code=422)
    store.save_draft(defn)
    return _render_block(request, defn, block, oob_side=False)


@router.patch("/studio/{strategy_id}/allocation")
def route_allocation(request: Request, strategy_id: str,
                     name: str = Form(...), value: str = Form(...)):
    defn = _load_working(strategy_id)
    try:
        update_allocation(defn, name, value)
    except MutationError as e:
        return PlainTextResponse(str(e), status_code=422)
    store.save_draft(defn)
    return _render_block(request, defn, "allocation", oob_side=False)


@router.patch("/studio/{strategy_id}/risk")
def route_risk(request: Request, strategy_id: str, value: str = Form(...),
               name: str | None = Form(None), param: str | None = Form(None)):
    # The rule endpoints call this field `param`; risk historically called it
    # `name`. Accept both so one vocabulary works across the whole surface.
    field = name or param
    if not field:
        return PlainTextResponse("missing 'name' (or 'param')", status_code=422)
    defn = _load_working(strategy_id)
    try:
        update_risk(defn, field, value)
    except MutationError as e:
        return PlainTextResponse(str(e), status_code=422)
    store.save_draft(defn)
    return _render_block(request, defn, "risk")


@router.patch("/studio/{strategy_id}/instruments/{symbol}")
def route_instrument(request: Request, strategy_id: str, symbol: str):
    defn = _load_working(strategy_id)
    try:
        toggle_instrument(defn, symbol)
    except MutationError as e:
        return PlainTextResponse(str(e), status_code=422)
    store.save_draft(defn)
    html = _tpl().get_template("studio/_instruments.html").render(
        _ctx(request, defn))
    return HTMLResponse(html)


@router.post("/studio/{strategy_id}/save")
def route_save(request: Request, strategy_id: str):
    try:
        store.promote_draft(strategy_id)
    except KeyError:
        return PlainTextResponse("nothing to save", status_code=422)
    defn = store.load(strategy_id)
    html = _tpl().get_template("studio/_strategy_name.html").render(
        _ctx(request, defn, is_draft=False))
    return HTMLResponse(html)


def _render_footer(request: Request, defn: StrategyDefinition,
                   run: dict | None) -> HTMLResponse:
    metrics = spark = None
    if run and run["status"] == "done" and run["metrics"]:
        metrics = BacktestMetrics.from_json(run["metrics"])
        spark = _spark_path(metrics.equity_curve)
    return HTMLResponse(_tpl().get_template(
        "studio/_footer_metrics.html").render(_ctx(
            request, defn, run=run, metrics=metrics, spark=spark)))


def _spark_path(curve: list[float], w: int = 260, h: int = 36) -> str:
    """Downsample the equity curve to an SVG path (<=260 points)."""
    if len(curve) < 2:
        return ""
    step = max(1, -(-len(curve) // w))  # ceil division => <= w points
    pts = curve[::step]
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1e-9
    coords = [
        f"{i * w / (len(pts) - 1):.1f},{h - 4 - (v - lo) / rng * (h - 8):.1f}"
        for i, v in enumerate(pts)
    ]
    return "M" + " L".join(coords)


def _execute_run(run_id: str, defn: StrategyDefinition) -> None:
    """Runs in a background task; all outcomes land in studio_runs."""
    try:
        compiled = compile_strategy(defn)
        metrics = ADAPTER.run(compiled)
        store.finish_run(run_id, metrics.to_json())
    except Exception as e:  # noqa: BLE001 — surface anything to the UI
        store.fail_run(run_id, str(e))


@router.post("/studio/{strategy_id}/backtest")
def route_backtest(request: Request, strategy_id: str,
                   background_tasks: BackgroundTasks):
    try:
        defn, is_draft = store.working_copy(strategy_id)
    except KeyError:
        raise HTTPException(404, f"strategy '{strategy_id}' not found")
    # compile FIRST so config errors surface immediately, not in the task
    try:
        compile_strategy(defn)
    except CompileError as e:
        resp = PlainTextResponse(str(e), status_code=422)
        if e.rule_id:
            resp.headers["X-Rule-Id"] = e.rule_id
        return resp
    run_id = uuid.uuid4().hex[:12]
    store.create_run(run_id, strategy_id, defn.version, is_draft)
    background_tasks.add_task(_execute_run, run_id, defn)
    return _render_footer(request, defn, store.latest_run(strategy_id))


@router.get("/studio/{strategy_id}/runs/latest/folds")
def route_run_folds(request: Request, strategy_id: str):
    run = store.latest_run(strategy_id)
    if not run or run["status"] != "done" or not run["metrics"]:
        return HTMLResponse(
            '<p class="opt-note">No completed run yet.</p>')
    metrics = BacktestMetrics.from_json(run["metrics"])
    return HTMLResponse(_tpl().get_template(
        "studio/_results_pane.html").render(
            _ctx(request, _load_working(strategy_id),
                 metrics=metrics, run=run)))


@router.get("/studio/{strategy_id}/runs/latest")
def route_latest_run(request: Request, strategy_id: str):
    defn = _load_working(strategy_id)
    return _render_footer(request, defn, store.latest_run(strategy_id))


@router.patch("/studio/{strategy_id}/opt/toggle")
def route_opt_toggle(request: Request, strategy_id: str,
                     owner: str = Form(...), param: str = Form(...)):
    defn = _load_working(strategy_id)
    try:
        toggle_optimize(defn, owner, param)
        block = _block_of_owner(defn, owner)
    except RuleNotFound as e:
        raise HTTPException(404, str(e))
    except MutationError as e:
        return PlainTextResponse(str(e), status_code=422)
    store.save_draft(defn)
    html = _render_side(request, defn)
    tpl, extra = _BLOCK_TEMPLATE[block]
    ctx = _ctx(request, defn, **extra)
    if block == "risk":
        ctx["risk"] = defn.risk
    elif block == "regime":
        ctx["regime"] = defn.regime
    else:
        ctx["group"] = defn.entry if block == "entry" else defn.exit
    blk = _tpl().get_template(tpl).render(ctx)
    blk = blk.replace(f'id="block-{block}"',
                      f'id="block-{block}" hx-swap-oob="true"', 1)
    return HTMLResponse(html + blk)


@router.patch("/studio/{strategy_id}/opt/range")
def route_opt_range(request: Request, strategy_id: str,
                    owner: str = Form(...), param: str = Form(...),
                    min_v: str = Form(..., alias="min"),
                    step_v: str = Form(..., alias="step"),
                    max_v: str = Form(..., alias="max")):
    defn = _load_working(strategy_id)
    try:
        set_optimize_range(defn, owner, param, min_v, step_v, max_v)
    except RuleNotFound as e:
        raise HTTPException(404, str(e))
    except MutationError as e:
        return PlainTextResponse(str(e), status_code=422)
    store.save_draft(defn)
    return HTMLResponse(_render_side(request, defn))


def _execute_opt(run_id: str, defn: StrategyDefinition) -> None:
    try:
        results = OPTIMIZER.run(defn)
        store.finish_opt(run_id, json.dumps([r.to_dict() for r in results]))
    except Exception as e:  # noqa: BLE001
        store.fail_opt(run_id, str(e))


@router.post("/studio/{strategy_id}/optimize")
def route_optimize(request: Request, strategy_id: str,
                   background_tasks: BackgroundTasks):
    try:
        defn, is_draft = store.working_copy(strategy_id)
    except KeyError:
        raise HTTPException(404, f"strategy '{strategy_id}' not found")
    sweep = defn.sweep_size()
    if sweep == 0:
        return PlainTextResponse(
            "no parameters marked for optimization", status_code=422)
    total = sweep * defn.walkforward.folds
    if total > OPTIMIZER_MAX_RUNS:
        return PlainTextResponse(
            f"sweep of {total:,} runs exceeds the optimizer limit "
            f"({OPTIMIZER_MAX_RUNS:,}) — narrow some ranges",
            status_code=422)
    # A sweep on the real engine is a different order of cost: the optimizer
    # samples up to STUB_MAX_EVALS combinations and each one costs
    # 1 + walkforward.folds engine runs. Refuse before starting rather than
    # letting a background task grind for hours.
    if isinstance(TRIAL_ADAPTER, NautilusBacktestAdapter):
        engine_runs = min(sweep, STUB_MAX_EVALS) * (1 + defn.walkforward.folds)
        if engine_runs > ENGINE_SWEEP_MAX_RUNS:
            return PlainTextResponse(
                f"sweep would take {engine_runs:,} engine runs, over the "
                f"{ENGINE_SWEEP_MAX_RUNS:,} limit for STUDIO_BACKTEST_OPT="
                "nautilus — narrow the ranges, cut folds, or raise "
                "STUDIO_OPT_MAX_ENGINE_RUNS",
                status_code=422)
    try:
        compile_strategy(defn)
    except CompileError as e:
        return PlainTextResponse(str(e), status_code=422)
    run_id = uuid.uuid4().hex[:12]
    store.create_opt(run_id, strategy_id, defn.version, is_draft)
    background_tasks.add_task(_execute_opt, run_id, defn)
    return HTMLResponse(_render_side(request, defn))


@router.get("/studio/{strategy_id}/optimize/panel")
def route_opt_panel(request: Request, strategy_id: str):
    defn = _load_working(strategy_id)
    return HTMLResponse(_render_side(request, defn))


@router.post("/studio/{strategy_id}/optimize/apply")
def route_opt_apply(strategy_id: str, rank: int = Form(...)):
    defn = _load_working(strategy_id)
    opt = store.latest_opt(strategy_id)
    if not opt or opt["status"] != "done" or not opt["results"]:
        return PlainTextResponse("no completed optimization", status_code=422)
    results = [OptResult(**r) for r in json.loads(opt["results"])]
    hit = next((r for r in results if r.rank == rank), None)
    if hit is None:
        return PlainTextResponse(f"rank {rank} not found", status_code=422)
    try:
        apply_params(defn, hit.params)
    except MutationError as e:
        return PlainTextResponse(str(e), status_code=422)
    store.save_draft(defn)
    return Response(headers={"HX-Refresh": "true"})


def _baseline_metrics(strategy_id: str) -> BacktestMetrics | None:
    """Last recorded run — the deploy gate's baseline.

    Deliberately reads what the user actually ran, not a freshly measured
    number: the gate must judge a real engine run they triggered themselves.
    """
    run = store.latest_run(strategy_id)
    if run and run["status"] == "done" and run["metrics"]:
        return BacktestMetrics.from_json(run["metrics"])
    return None


# Trial baselines, keyed by (engine, definition). LRU-bounded: the AI loop
# walks through many definitions, and a clear-all would make a loop over
# MAX+1 definitions miss every single time.
_TRIAL_BASELINE_CACHE: OrderedDict[tuple[str, str], BacktestMetrics] = OrderedDict()
_TRIAL_BASELINE_CACHE_MAX = 64


def _trial_baseline(defn: StrategyDefinition) -> BacktestMetrics | None:
    """Baseline measured on the *same* engine the trial will run on.

    evaluate_trial's guardrails compare trial against baseline. Taking the
    baseline from latest_run would compare a TRIAL_ADAPTER trial against
    whatever engine produced that stored run — with the two engine switches
    set differently, the guardrail would then be deciding on stub-vs-Nautilus
    differences rather than on the suggestion itself. So measure it here, on
    the unmodified definition, with TRIAL_ADAPTER.

    Cached per (engine, definition): the AI loop asks for the same baseline
    once per iteration, and under STUDIO_BACKTEST_OPT=nautilus each miss is a
    full engine run.

    Whether a baseline exists at all is unchanged — it still takes a completed
    run on the strategy. Measuring one unconditionally would switch the
    guardrails on for strategies that have never been backtested, which is a
    different decision from "which engine produced the comparison".
    """
    if not _baseline_metrics(defn.id):
        return None
    key = (type(TRIAL_ADAPTER).__name__,
           hashlib.sha256(defn.model_dump_json().encode()).hexdigest())
    hit = _TRIAL_BASELINE_CACHE.get(key)
    if hit is not None:
        _TRIAL_BASELINE_CACHE.move_to_end(key)
        return hit
    try:
        metrics = TRIAL_ADAPTER.run(compile_strategy(defn))
    except (CompileError, UnsupportedStrategy):
        # This strategy cannot be measured on this engine — guardrails skip the
        # comparison. Anything else (a broken engine, a bad adapter) propagates
        # rather than silently disabling the guardrails.
        return None
    _TRIAL_BASELINE_CACHE[key] = metrics
    _TRIAL_BASELINE_CACHE.move_to_end(key)
    while len(_TRIAL_BASELINE_CACHE) > _TRIAL_BASELINE_CACHE_MAX:
        _TRIAL_BASELINE_CACHE.popitem(last=False)
    return metrics


def _make_suggestion(defn: StrategyDefinition, ask: str,
                     scope: str | None, source: str,
                     min_trades: int = 0) -> tuple[str, str]:
    """suggest -> trial -> guardrails. Returns (suggestion_id, status)."""
    from strategy_studio.registry import INDICATOR_REGISTRY as REG
    # Same engine as the trial — see _trial_baseline.
    baseline = _trial_baseline(defn)
    prompt = build_prompt(defn, baseline, ask, scope,
                          store.rejected_rationales(defn.id),
                          list(REG.keys()))
    sid = uuid.uuid4().hex[:12]
    try:
        sugg = parse_suggestion(LLM, prompt)
    except SuggestionFailure as e:
        store.add_suggestion(sid, defn.id, scope or "entry",
                             json.dumps({"rationale": str(e)}),
                             "failed", note=str(e), source=source)
        return sid, "failed"
    if scope and sugg.block != scope:
        sugg = sugg.model_copy(update={"block": scope})
    try:
        trial_metrics, _trial = evaluate_trial(
            defn, sugg, TRIAL_ADAPTER, baseline, min_trades)
    except GuardrailReject as e:
        store.add_suggestion(sid, defn.id, sugg.block,
                             sugg.model_dump_json(), "rejected",
                             baseline=baseline.to_json() if baseline else None,
                             note=str(e), source=source)
        return sid, "rejected"
    store.add_suggestion(sid, defn.id, sugg.block, sugg.model_dump_json(),
                         "review", trial_metrics=trial_metrics.to_json(),
                         baseline=baseline.to_json() if baseline else None,
                         source=source)
    return sid, "review"


@router.post("/studio/{strategy_id}/ai/suggest")
def route_ai_suggest(request: Request, strategy_id: str,
                     ask: str = Form(""), block: str = Form("")):
    defn = _load_working(strategy_id)
    scope = block or None
    try:
        sid, status = _make_suggestion(defn, ask, scope, "manual")
    except Exception as e:  # noqa: BLE001 — an unusable engine is not a 500
        # _trial_baseline deliberately lets unexpected engine failures through
        # rather than silently disabling the guardrails. That belongs in the
        # response, not in a stack trace: HTMX would swap nothing on a 500 and
        # the user would see the button do nothing at all.
        return PlainTextResponse(
            f"AI suggestion could not run: {type(e).__name__}: {e}",
            status_code=422)
    if status == "failed":
        row = store.get_suggestion(sid)
        return PlainTextResponse(row["note"] or "AI suggestion failed",
                                 status_code=422)
    if status == "rejected":
        row = store.get_suggestion(sid)
        return PlainTextResponse(
            f"AI proposal auto-rejected by guardrails: {row['note']}",
            status_code=422)
    target = store.get_suggestion(sid)["block"]
    return HTMLResponse(_block_html(request, defn, target, oob=True))


@router.post("/studio/{strategy_id}/ai/suggestions/{sid}/accept")
def route_ai_accept(request: Request, strategy_id: str, sid: str):
    defn = _load_working(strategy_id)
    row = store.get_suggestion(sid)
    if not row or row["status"] != "review":
        return PlainTextResponse("suggestion not reviewable", status_code=422)
    sugg = Suggestion.model_validate_json(row["suggestion"])
    try:
        apply_suggestion(defn, sugg)
    except MutationError as e:
        store.set_suggestion_status(sid, "failed", str(e))
        return PlainTextResponse(str(e), status_code=422)
    store.set_suggestion_status(sid, "accepted")
    store.save_draft(defn)
    return HTMLResponse(_block_html(request, defn, row["block"])
                        + _render_side(request, defn, oob=True))


@router.post("/studio/{strategy_id}/ai/suggestions/{sid}/dismiss")
def route_ai_dismiss(request: Request, strategy_id: str, sid: str):
    defn = _load_working(strategy_id)
    row = store.get_suggestion(sid)
    if not row:
        return PlainTextResponse("suggestion not found", status_code=422)
    store.set_suggestion_status(sid, "rejected", "dismissed by user")
    return HTMLResponse(_block_html(request, defn, row["block"]))


def _execute_loop(loop_id: str, strategy_id: str, cfg: dict) -> None:
    try:
        for n in range(1, cfg["max_iterations"] + 1):
            defn, _ = store.working_copy(strategy_id)
            sid, status = _make_suggestion(
                defn, cfg.get("ask", ""), None, "loop",
                min_trades=cfg["min_trades"])
            if status == "review" and cfg["apply_mode"] == "auto":
                row = store.get_suggestion(sid)
                trial = BacktestMetrics.from_json(row["trial_metrics"])
                base = (BacktestMetrics.from_json(row["baseline"])
                        if row["baseline"] else None)
                sugg = Suggestion.model_validate_json(row["suggestion"])
                if improved(defn, trial, base):
                    apply_suggestion(defn, sugg)
                    store.save_draft(defn)
                    store.set_suggestion_status(sid, "accepted",
                                                "auto-accepted (OOS improved)")
                    status = "accepted"
                else:
                    store.set_suggestion_status(sid, "rejected",
                                                "no OOS improvement")
                    status = "rejected"
            store.add_iteration(loop_id, n, sid, status)
            if status == "review":
                store.finish_loop(loop_id, "done",
                                  "paused for review at iteration "
                                  f"{n}/{cfg['max_iterations']}")
                return
        store.finish_loop(loop_id, "done", "max iterations reached")
    except Exception as e:  # noqa: BLE001
        store.finish_loop(loop_id, "failed", str(e))


@router.post("/studio/{strategy_id}/ai/loop/start")
def route_loop_start(request: Request, strategy_id: str,
                     background_tasks: BackgroundTasks,
                     max_iterations: int = Form(6),
                     min_trades: int = Form(100),
                     apply_mode: str = Form("review"),
                     ask: str = Form("")):
    _load_working(strategy_id)
    active = store.latest_loop(strategy_id)
    if active and active["status"] == "running":
        return PlainTextResponse("a loop is already running", status_code=422)
    if apply_mode not in ("review", "auto"):
        return PlainTextResponse("apply_mode must be review|auto",
                                 status_code=422)
    cfg = {"max_iterations": max(1, min(50, max_iterations)),
           "min_trades": max(0, min_trades),
           "apply_mode": apply_mode, "ask": ask}
    loop_id = uuid.uuid4().hex[:12]
    store.create_loop(loop_id, strategy_id, json.dumps(cfg))
    background_tasks.add_task(_execute_loop, loop_id, strategy_id, cfg)
    defn = _load_working(strategy_id)
    return HTMLResponse(_render_side(request, defn))


@router.get("/studio/{strategy_id}/ai/panel")
def route_ai_panel(request: Request, strategy_id: str):
    defn = _load_working(strategy_id)
    return HTMLResponse(_render_side(request, defn))


@router.get("/studio/{strategy_id}/deploy/modal")
def route_deploy_modal(request: Request, strategy_id: str):
    try:
        defn = store.load(strategy_id)   # latest SAVED version only
    except KeyError:
        raise HTTPException(404, f"strategy '{strategy_id}' not found")
    metrics = _baseline_metrics(strategy_id)
    objective_value = None
    if metrics:
        objective_value = {"sharpe": metrics.sharpe,
                           "max_dd": metrics.max_dd_pct}.get(
            defn.walkforward.objective, metrics.dsr)
    return HTMLResponse(_tpl().get_template(
        "studio/_deploy_modal.html").render(_ctx(
            request, defn, metrics=metrics,
            objective_value=objective_value,
            gate_default=DEFAULT_GATE_DSR,
            has_draft=store.load_draft(strategy_id) is not None)))


@router.post("/studio/{strategy_id}/deploy")
def route_deploy(request: Request, strategy_id: str,
                 background_tasks: BackgroundTasks,
                 environment: str = Form(...),
                 instruments: str = Form("active"),
                 capital: float = Form(10000.0),
                 kill_switch: str = Form("3"),
                 gate_enabled: bool = Form(False),
                 gate_min: float = Form(DEFAULT_GATE_DSR),
                 confirm_name: str = Form("")):
    try:
        defn = store.load(strategy_id)   # deploy compiles the SAVED version
    except KeyError:
        raise HTTPException(404, f"strategy '{strategy_id}' not found")
    if environment == "live" and confirm_name.strip() != defn.name:
        return PlainTextResponse(
            "live deploy requires typing the exact strategy name to confirm",
            status_code=422)
    kill = None if kill_switch in ("", "off") else float(kill_switch)
    cfg = DeployConfig(environment=environment, instruments=instruments,
                       capital=capital, kill_switch_daily_pct=kill,
                       gate_enabled=gate_enabled, gate_min_objective=gate_min)
    try:
        artifact = prepare_deployment(defn, _baseline_metrics(strategy_id), cfg)
    except (DeployBlocked, CompileError) as e:
        return PlainTextResponse(str(e), status_code=422)
    deploy_id = uuid.uuid4().hex[:12]
    store.create_deployment(deploy_id, strategy_id, defn.version,
                            environment, artifact)
    # INTEGRATION POINT: replace _stub_runner_pickup with a hand-off to your
    # live/sim runner; it should flip the row to status='running' on start.
    background_tasks.add_task(_stub_runner_pickup, deploy_id)
    return HTMLResponse(
        f'<div class="deploy-ok">Deployment <b>{deploy_id[:6]}</b> created '
        f'({environment}, v{defn.version}) — pending runner pickup.</div>'
        + _render_deployments(request, defn, oob=True))


_DEPLOY_TRANSITIONS = {
    "pause": ({"running"}, "paused"),
    "resume": ({"paused"}, "running"),
    "stop": ({"pending", "running", "paused"}, "stopped"),
}


def _stub_runner_pickup(deploy_id: str) -> None:
    """Simulated runner: picks a pending deployment up and starts it."""
    time.sleep(1.0)
    dep = store.get_deployment(deploy_id)
    if dep and dep["status"] == "pending":
        store.set_deployment_status(deploy_id, "running")


def _render_deployments(request: Request, defn: StrategyDefinition,
                        oob: bool = False) -> str:
    deployments = store.list_deployments(defn.id)
    html = _tpl().get_template("studio/_deployments.html").render(
        _ctx(request, defn, deployments=deployments))
    if oob:
        html = html.replace('id="deployments-panel"',
                            'id="deployments-panel" hx-swap-oob="true"', 1)
    return html


@router.get("/studio/{strategy_id}/deployments/panel")
def route_deployments_panel(request: Request, strategy_id: str):
    defn = _load_working(strategy_id)
    return HTMLResponse(_render_deployments(request, defn))


@router.post("/studio/{strategy_id}/deployments/{deploy_id}/{action}")
def route_deployment_action(request: Request, strategy_id: str,
                            deploy_id: str, action: str):
    defn = _load_working(strategy_id)
    if action not in _DEPLOY_TRANSITIONS:
        return PlainTextResponse(f"unknown action '{action}'", status_code=422)
    dep = store.get_deployment(deploy_id)
    if not dep or dep["strategy_id"] != strategy_id:
        return PlainTextResponse("deployment not found", status_code=422)
    allowed_from, to = _DEPLOY_TRANSITIONS[action]
    if dep["status"] not in allowed_from:
        return PlainTextResponse(
            f"cannot {action} a deployment in status '{dep['status']}'",
            status_code=422)
    store.set_deployment_status(deploy_id, to)
    return HTMLResponse(_render_deployments(request, defn))


@router.post("/studio/{strategy_id}/discard")
def route_discard(strategy_id: str):
    store.delete_draft(strategy_id)
    return Response(headers={"HX-Refresh": "true"})
