"""Strategy Studio — FastAPI app (Phases 1-2).

INTEGRATION POINT for nautilus_web_app: move these routes onto an APIRouter
and include_router() on your existing app; merge template/static setup.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.studio.ai import (
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
from app.studio.backtest import BacktestMetrics, StubBacktestAdapter
from app.studio.compiler import CompileError, compile_strategy
from app.studio.deploy import (
    DEFAULT_GATE_DSR,
    DeployBlocked,
    DeployConfig,
    prepare_deployment,
)
from app.studio.mutations import (
    MutationError,
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
from app.studio.optimizer import (
    OPTIMIZER_MAX_RUNS,
    OptResult,
    StubWalkForwardOptimizer,
    apply_params,
)
from app.studio.registry import INDICATOR_REGISTRY, library_by_category
from app.studio.schema import StrategyDefinition
from app.studio.store import StrategyStore

BASE = Path(__file__).resolve().parents[1]

app = FastAPI(title="Strategy Studio")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

store = StrategyStore()

# INTEGRATION POINT: swap for NautilusBacktestAdapter (see backtest.py)
ADAPTER = StubBacktestAdapter()
# INTEGRATION POINT: swap for your real walk-forward optimizer (optimizer.py)
OPTIMIZER = StubWalkForwardOptimizer(adapter=ADAPTER)
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
    return templates.get_template("studio/_side_panel.html").render(
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
    html = templates.get_template(tpl).render(ctx)
    if oob:
        html = html.replace(f'id="block-{block}"',
                            f'id="block-{block}" hx-swap-oob="true"', 1)
    return html


def _ghost_ctx(defn: StrategyDefinition, block: str) -> list[dict]:
    out = []
    for row in store.pending_suggestions(defn.id, block):
        g = {"id": row["id"], "source": row["source"]}
        g["s"] = Suggestion.model_validate_json(row["suggestion"])
        g["trial"] = (BacktestMetrics.from_json(row["trial_metrics"])
                      if row["trial_metrics"] else None)
        g["base"] = (BacktestMetrics.from_json(row["baseline"])
                     if row["baseline"] else None)
        out.append(g)
    return out


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

@app.get("/studio/{strategy_id}", response_class=HTMLResponse)
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
    return templates.TemplateResponse(request, "studio/page.html", _side_ctx(
        request, defn, library=library_by_category(), is_draft=is_draft,
        initial_run=run, initial_metrics=metrics, initial_spark=spark,
        ghosts_by_block={b: _ghost_ctx(defn, b)
                         for b in ("entry", "exit", "risk", "regime")}))


@app.get("/studio/{strategy_id}/history")
def studio_history(strategy_id: str):
    return store.history(strategy_id)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ── mutations (Phase 2) ──────────────────────────────────────────

@app.post("/studio/{strategy_id}/blocks/{block}/rules")
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


@app.patch("/studio/{strategy_id}/rules/{rule_id}")
def route_edit_rule(request: Request, strategy_id: str, rule_id: str,
                    param: str = Form(...), value: str = Form(...)):
    defn = _load_working(strategy_id)
    try:
        update_rule_param(defn, rule_id, param, value)
        block, *_ = find_rule(defn, rule_id)
    except MutationError as e:
        return PlainTextResponse(str(e), status_code=422)
    store.save_draft(defn)
    return _render_block(request, defn, block)


@app.delete("/studio/{strategy_id}/rules/{rule_id}")
def route_delete_rule(request: Request, strategy_id: str, rule_id: str):
    defn = _load_working(strategy_id)
    try:
        block = delete_rule(defn, rule_id)
    except MutationError as e:
        return PlainTextResponse(str(e), status_code=422)
    store.save_draft(defn)
    return _render_block(request, defn, block)


@app.patch("/studio/{strategy_id}/blocks/{block}")
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


@app.patch("/studio/{strategy_id}/allocation")
def route_allocation(request: Request, strategy_id: str,
                     name: str = Form(...), value: str = Form(...)):
    defn = _load_working(strategy_id)
    try:
        update_allocation(defn, name, value)
    except MutationError as e:
        return PlainTextResponse(str(e), status_code=422)
    store.save_draft(defn)
    return _render_block(request, defn, "allocation", oob_side=False)


@app.patch("/studio/{strategy_id}/risk")
def route_risk(request: Request, strategy_id: str,
               name: str = Form(...), value: str = Form(...)):
    defn = _load_working(strategy_id)
    try:
        update_risk(defn, name, value)
    except MutationError as e:
        return PlainTextResponse(str(e), status_code=422)
    store.save_draft(defn)
    return _render_block(request, defn, "risk")


@app.patch("/studio/{strategy_id}/instruments/{symbol}")
def route_instrument(request: Request, strategy_id: str, symbol: str):
    defn = _load_working(strategy_id)
    try:
        toggle_instrument(defn, symbol)
    except MutationError as e:
        return PlainTextResponse(str(e), status_code=422)
    store.save_draft(defn)
    html = templates.get_template("studio/_instruments.html").render(
        _ctx(request, defn))
    return HTMLResponse(html)


@app.post("/studio/{strategy_id}/save")
def route_save(request: Request, strategy_id: str):
    try:
        store.promote_draft(strategy_id)
    except KeyError:
        return PlainTextResponse("nothing to save", status_code=422)
    defn = store.load(strategy_id)
    html = templates.get_template("studio/_strategy_name.html").render(
        _ctx(request, defn, is_draft=False))
    return HTMLResponse(html)


def _render_footer(request: Request, defn: StrategyDefinition,
                   run: dict | None) -> HTMLResponse:
    metrics = spark = None
    if run and run["status"] == "done" and run["metrics"]:
        metrics = BacktestMetrics.from_json(run["metrics"])
        spark = _spark_path(metrics.equity_curve)
    return HTMLResponse(templates.get_template(
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


@app.post("/studio/{strategy_id}/backtest")
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


@app.get("/studio/{strategy_id}/runs/latest/folds")
def route_run_folds(request: Request, strategy_id: str):
    run = store.latest_run(strategy_id)
    if not run or run["status"] != "done" or not run["metrics"]:
        return HTMLResponse(
            '<p class="opt-note">No completed run yet.</p>')
    metrics = BacktestMetrics.from_json(run["metrics"])
    return HTMLResponse(templates.get_template(
        "studio/_results_pane.html").render(
            _ctx(request, _load_working(strategy_id),
                 metrics=metrics, run=run)))


@app.get("/studio/{strategy_id}/runs/latest")
def route_latest_run(request: Request, strategy_id: str):
    defn = _load_working(strategy_id)
    return _render_footer(request, defn, store.latest_run(strategy_id))


@app.patch("/studio/{strategy_id}/opt/toggle")
def route_opt_toggle(request: Request, strategy_id: str,
                     owner: str = Form(...), param: str = Form(...)):
    defn = _load_working(strategy_id)
    try:
        toggle_optimize(defn, owner, param)
        block = _block_of_owner(defn, owner)
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
    blk = templates.get_template(tpl).render(ctx)
    blk = blk.replace(f'id="block-{block}"',
                      f'id="block-{block}" hx-swap-oob="true"', 1)
    return HTMLResponse(html + blk)


@app.patch("/studio/{strategy_id}/opt/range")
def route_opt_range(request: Request, strategy_id: str,
                    owner: str = Form(...), param: str = Form(...),
                    min_v: str = Form(..., alias="min"),
                    step_v: str = Form(..., alias="step"),
                    max_v: str = Form(..., alias="max")):
    defn = _load_working(strategy_id)
    try:
        set_optimize_range(defn, owner, param, min_v, step_v, max_v)
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


@app.post("/studio/{strategy_id}/optimize")
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
    try:
        compile_strategy(defn)
    except CompileError as e:
        return PlainTextResponse(str(e), status_code=422)
    run_id = uuid.uuid4().hex[:12]
    store.create_opt(run_id, strategy_id, defn.version, is_draft)
    background_tasks.add_task(_execute_opt, run_id, defn)
    return HTMLResponse(_render_side(request, defn))


@app.get("/studio/{strategy_id}/optimize/panel")
def route_opt_panel(request: Request, strategy_id: str):
    defn = _load_working(strategy_id)
    return HTMLResponse(_render_side(request, defn))


@app.post("/studio/{strategy_id}/optimize/apply")
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
    run = store.latest_run(strategy_id)
    if run and run["status"] == "done" and run["metrics"]:
        return BacktestMetrics.from_json(run["metrics"])
    return None


def _make_suggestion(defn: StrategyDefinition, ask: str,
                     scope: str | None, source: str,
                     min_trades: int = 0) -> tuple[str, str]:
    """suggest -> trial -> guardrails. Returns (suggestion_id, status)."""
    from app.studio.registry import INDICATOR_REGISTRY as REG
    baseline = _baseline_metrics(defn.id)
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
            defn, sugg, ADAPTER, baseline, min_trades)
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


@app.post("/studio/{strategy_id}/ai/suggest")
def route_ai_suggest(request: Request, strategy_id: str,
                     ask: str = Form(""), block: str = Form("")):
    defn = _load_working(strategy_id)
    scope = block or None
    sid, status = _make_suggestion(defn, ask, scope, "manual")
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


@app.post("/studio/{strategy_id}/ai/suggestions/{sid}/accept")
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


@app.post("/studio/{strategy_id}/ai/suggestions/{sid}/dismiss")
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


@app.post("/studio/{strategy_id}/ai/loop/start")
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


@app.get("/studio/{strategy_id}/ai/panel")
def route_ai_panel(request: Request, strategy_id: str):
    defn = _load_working(strategy_id)
    return HTMLResponse(_render_side(request, defn))


@app.get("/studio/{strategy_id}/deploy/modal")
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
    return HTMLResponse(templates.get_template(
        "studio/_deploy_modal.html").render(_ctx(
            request, defn, metrics=metrics,
            objective_value=objective_value,
            gate_default=DEFAULT_GATE_DSR,
            has_draft=store.load_draft(strategy_id) is not None)))


@app.post("/studio/{strategy_id}/deploy")
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
    html = templates.get_template("studio/_deployments.html").render(
        _ctx(request, defn, deployments=deployments))
    if oob:
        html = html.replace('id="deployments-panel"',
                            'id="deployments-panel" hx-swap-oob="true"', 1)
    return html


@app.get("/studio/{strategy_id}/deployments/panel")
def route_deployments_panel(request: Request, strategy_id: str):
    defn = _load_working(strategy_id)
    return HTMLResponse(_render_deployments(request, defn))


@app.post("/studio/{strategy_id}/deployments/{deploy_id}/{action}")
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


@app.post("/studio/{strategy_id}/discard")
def route_discard(strategy_id: str):
    store.delete_draft(strategy_id)
    return Response(headers={"HX-Refresh": "true"})
