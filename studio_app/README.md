# Strategy Studio — Phases 1-6 (STUDIO_SPEC.md)

Visual strategy builder for `nautilus_web_app`: versioned strategy schema,
HTMX editing with a draft workflow, backtest + walk-forward optimization
wiring, an AI improvement layer with server-side guardrails, and gated
deployment. Delivered as a standalone FastAPI package; every place that must
touch your existing code is marked `INTEGRATION POINT` in the source.

> **Merged.** This package is no longer standalone — it now runs inside the
> main app. See "Where it lives now" below for the file map and the state of
> each INTEGRATION POINT. Only docs remain in `studio_app/`.

## Run it

```bash
python scripts/seed_studio.py           # creates studio.db + demo strategy
uvicorn server:app --reload
# open http://127.0.0.1:8000/studio/wt-funding-v3
python -m pytest tests/studio -q        # 111 tests (+1 env-flagged LLM smoke)
```

## What each phase added

| Phase | Feature | Key files |
|---|---|---|
| 1 | Schema, versioned store, compiler, read-only render | `schema.py`, `store.py`, `compiler.py`, `registry.py` |
| 2 | Editing: add/edit/delete rules, drafts, Save/Discard | `mutations.py`, `main.py`, templates |
| 3 | Backtest wiring: run → poll → metrics/sparkline/folds | `backtest.py` |
| 4 | Optimization: sweep toggles, ranges, limits, Top-10 + Apply | `optimizer.py` |
| 5 | AI: suggest → ghost → accept/dismiss, loop, guardrails | `ai.py` |
| 6 | Deploy: gate (OOS objective), kill switch, live confirm | `deploy.py` |
| +opt | Regime ELSE substrategy (inline, compilable, sweepable) | `schema.py`, `compiler.py` |
| +opt | Allocation block: ranked universe, top-N, weighting | `schema.py`, `compiler.py` |
| +opt | Deployment lifecycle: stub pickup, pause/resume/stop | `main.py`, `_deployments.html` |
| +opt | Side-panel tab state survives OOB refresh | `studio.js` |

## Where it lives now

| Was | Is |
|---|---|
| `app/studio/` | `strategy_studio/` |
| `app/main.py` (FastAPI app) | `web/routes/strategy_studio.py` (`APIRouter`, mounted in `server.py`) |
| `templates/studio/` | `web/templates/studio/` |
| `static/studio.{css,js}` | `web/static/studio.{css,js}` |
| `tests/studio/` | `tests/studio/` (repo root) |
| `scripts/seed_studio.py` | `scripts/seed_studio.py` (repo root) |
| `studio.db` | repo root (gitignored) |

`/studio/{strategy_id}` is the builder; the bare `/studio` route is the
Composer+Backtest page in `web/routes/studio.py`. They coexist — neither
shadows the other — but the shared name is worth remembering.

The store keeps its own SQLite file rather than joining an existing one: the
app has no other SQLite database.

### INTEGRATION POINT status

- **`registry.py` — wired.** Eight indicators (`rsi`, `ema`, `adx`, `atr`,
  `wavetrend`, `stochrsi`, `nadaraya_watson`, `relative_volume`) call the real
  functions in `indicators.py` through `impl(bars, **schema_params)`; the
  adapters translate schema param names to each function's own. The remaining
  seven have no feature function and stay `impl=None`. Guarded by
  `tests/studio/test_registry_impl.py`.
- **`backtest.py` — wired.** `to_nautilus(CompiledStrategy)` lowers onto a
  composer `ComposedStrategySpec`; `NautilusBacktestAdapter.run()` executes it
  through `run_composed_backtest`. What it cannot express faithfully (regime
  branch, ranked allocation, indicators with no composer block) raises
  `UnsupportedStrategy` listing every reason instead of silently dropping
  rules. **The stub is still the default** — see the two switches below.
  Guarded by `tests/studio/test_nautilus_adapter.py`.
- **`optimizer.py` — still the stub.** Adapting `wfo_optimizer` / 
  `backtest_robustness.run_walk_forward` is untouched; keep the
  `"<rule_id>.<param>"` addressing so Apply keeps working.
- **`ai.py` — still `HttpAnthropicClient`.** Set `ANTHROPIC_API_KEY`, or swap
  for `agent.py`'s client loop.
  `STUDIO_LLM_SMOKE=1 pytest tests/studio/test_ai.py -k smoke` for a live check.
- **`deploy.py` — still the stub runner.** `launch(artifact)` against a live/sim
  TradingNode is not wired.

### Two engine switches, on purpose

| Env var | Selects the engine for | Default |
|---|---|---|
| `STUDIO_BACKTEST=nautilus` | the Run button — one backtest per click | stub |
| `STUDIO_BACKTEST_OPT=nautilus` | optimizer sweeps and AI-loop trials | stub |

They are separate because the fan-out consumers call `adapter.run()` once per
combination (capped at `OPTIMIZER_MAX_RUNS = 20_000`) or per AI suggestion,
and `NautilusBacktestAdapter` costs `1 + walkforward.folds` engine runs per
call — so a 100-combination sweep is ~700 Nautilus runs. Flipping the
single-run switch alone must not trigger that, and it doesn't: the optimizer
and `evaluate_trial` read `TRIAL_ADAPTER`, the Run button reads `ADAPTER`.

Because the two can differ, the AI guardrail baseline is **measured on
`TRIAL_ADAPTER`** (`_trial_baseline`, cached per engine+definition) rather than
read from the last recorded run — otherwise a stub trial would be compared
against Nautilus numbers and the guardrail would be judging the engine gap.
Whether a baseline exists at all is unchanged: it still takes a completed run,
so guardrails stay off until the strategy has actually been backtested. The
**deploy gate keeps reading `latest_run`** (`_baseline_metrics`) on purpose —
it must judge a real run the user triggered. Guarded by
`tests/studio/test_baseline_engine.py`.

### Known gap: the demo fixture cannot run on the real engine

`wt-funding-v3` uses a regime branch, `funding_z`, price-vs-`ema` and
`time_stop` — none of which the composer spec can express, so
`STUDIO_BACKTEST=nautilus` rejects it with all four reasons. Strategies built
from the mapped indicators (`rsi`, `adx`, `macd`, `stochrsi`, `wavetrend`,
`relative_volume`, `atr`) run for real.

## Guarantees worth knowing

- Human edits and AI edits share one path (`mutations.py`) — same validation,
  same bounds, no way for the AI to write something you couldn't.
- Guardrails are server-side: compile failure, trades below the floor, or a
  worse OOS objective auto-reject a suggestion even in auto-apply mode.
- Deploy compiles the **saved version only**, never the draft, and the OOS
  gate is enforced in `deploy.py`, not just greyed-out in the UI.
- Optimize sweeps are capped (`OPTIMIZER_MAX_RUNS`) before they start.
- The regime ELSE branch can now run an inline substrategy (shared risk
  block); its rules use the same endpoints, guardrails, and sweep machinery
  as the main blocks — `sub_entry` / `sub_exit` are just two more blocks.
- Ranked allocation validates `top_n` against active instruments at compile
  time and ships in the deploy artifact.
- Deployment status flow: pending → running (stub runner; INTEGRATION POINT
  in `_stub_runner_pickup`) → paused ⇄ running → stopped (terminal).
