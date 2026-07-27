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
python -m pytest tests/studio -q        # 140 tests (+1 env-flagged LLM smoke)
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

## Canvas view

`/studio/{strategy_id}/canvas` is the same strategy as a node graph — a second
visualiser, not a second editor. The form view stays exactly as it was; the two
link to each other from their headers and both read the same working copy.

Screen: **palette | canvas | inspector**, with the metrics strip in the header.

```
GET /studio/{id}/canvas                     page
GET /studio/{id}/canvas/graph               {nodes, edges, meta}
GET /studio/{id}/canvas/inspector/{node}    the selected node's block partial
```

All three are GETs, and they are the only routes the canvas added. Everything
that writes — dropping an indicator, editing a chip, deleting a rule, save,
discard, run — calls an endpoint that already existed. That is why server rules
need no restating: delete the last entry rule from the canvas and you get the
same 422 the form view gets, in the same error banner.

**No node-editor library.** `StrategyDefinition` is a constrained tree
(`regime? → entry/exit RuleGroup → Rule → risk → allocation`), not a free
graph. Edges follow from the schema, so the user never draws them, and a
library that let them would mostly buy validation work. `strategy_studio/graph.py`
derives `{nodes, edges, meta}` as a pure function — no store access, no request
— and `web/static/canvas.js` turns layer indices into pixels with plain SVG.

**Nothing about the view is stored.** No position, no zoom, no edge list; the
schema gained no canvas field. Layout is recomputed on every render, which is
also why the layer assignment lives in `graph.py` where tests can pin it.

| Node | Comes from | Inspector shows |
|---|---|---|
| `instrument` | active `defn.instruments` | `_instruments.html` |
| `regime` | `defn.regime` (its conditions hang off it) | `_regime_block.html` |
| `rule` / `filter` | `iter_rules()` — filters get a dashed edge | its containing block |
| `group` | entry / exit / substrategy `RuleGroup` | `_rule_group.html` |
| `risk` | `defn.risk` | `_risk_block.html` |
| `allocation` | `defn.allocation` | `_allocation_block.html` |
| `ghost` | pending AI suggestion (passed in, not read) | the block, with accept/dismiss |

A rule is inspected through its containing block rather than through
`_rule.html` alone: a lone rule's controls target `closest .block`, which
outside a block has nothing to swap. The page loads `studio.js` for the same
reason — reusing a partial without its behaviour gives you chips that look
editable and are not.

Controls: wheel or `+`/`-` to zoom, drag or arrow keys to pan, `0` or ⤢ to fit,
minimap click to jump, `Esc` to clear the selection. Keyboard shortcuts stand
down while an inspector field has focus.

Deliberately out of scope: drawing your own edges (the schema tree does not
allow it), persisting node positions, and removing the form view — the two
views live side by side indefinitely. Design notes: `CANVAS_DESIGN.md`.

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

### What `to_nautilus` refuses (the full list)

Anything it cannot express faithfully is refused with every reason at once,
never silently dropped: regime branch · ranked allocation · an indicator with
no composer block · an operator with no engine equivalent (`ADX < x`) · a rule
pinned to a **timeframe** other than the instrument's (one bar feed per run) ·
`risk.max_concurrent > 1` (the engine holds one position) ·
`risk.time_stop_bars` (no time-based exit) · `entry match='any'` combined with
filters. Pinned by `tests/studio/test_review_fixes.py`.

### Two engine switches, on purpose

| Env var | Selects the engine for | Default |
|---|---|---|
| `STUDIO_BACKTEST=nautilus` | the Run button — one backtest per click | stub |
| `STUDIO_BACKTEST_OPT=nautilus` | optimizer sweeps and AI-loop trials | stub |

They are separate because the fan-out consumers call `adapter.run()` once per
combination (capped at `OPTIMIZER_MAX_RUNS = 20_000`) or per AI suggestion,
and `NautilusBacktestAdapter` costs `1 + walkforward.folds` engine runs per
call — the optimizer samples up to `STUB_MAX_EVALS` (400) combinations, so a
sweep is up to ~1 600 Nautilus runs. Flipping the single-run switch alone must
not trigger that, and it doesn't: the optimizer and `evaluate_trial` read
`TRIAL_ADAPTER`, the Run button reads `ADAPTER`. With the real engine selected
for sweeps, `POST /optimize` also refuses upfront above
`STUDIO_OPT_MAX_ENGINE_RUNS` (default 200) instead of grinding in the
background.

Because the two can differ, the AI guardrail baseline is **measured on
`TRIAL_ADAPTER`** (`_trial_baseline`, cached per engine+definition) rather than
read from the last recorded run — otherwise a stub trial would be compared
against Nautilus numbers and the guardrail would be judging the engine gap.
Whether a baseline exists at all is unchanged: it still takes a completed run,
so guardrails stay off until the strategy has actually been backtested. The
**deploy gate keeps reading `latest_run`** (`_baseline_metrics`) on purpose —
it must judge a real run the user triggered. Guarded by
`tests/studio/test_baseline_engine.py`.

### Two seeded fixtures, because the mockup one is stub-only

`scripts/seed_studio.py` seeds both:

| Strategy | Runs on | Why |
|---|---|---|
| `wt-funding-v3` | stub only | Matches the design mockup — regime branch, `funding_z`, price-vs-`ema`, `time_stop`. `to_nautilus` rejects it, naming every reason. |
| `rsi-adx-btc` | stub **and** real engine | `rsi_threshold` + `adx_threshold` entry, `atr_stop` exit, no regime/allocation, one Bybit instrument (BTCUSDT 1h). |

So the fastest path to real Nautilus metrics is
`STUDIO_BACKTEST=nautilus` + `/studio/rsi-adx-btc`. Keeping that strategy
engine-runnable is enforced by `tests/studio/test_seed_fixtures.py` — adding a
rule that has no composer block will fail there rather than at run time.

More generally, strategies built from the mapped indicators (`rsi`, `adx`,
`macd`, `stochrsi`, `wavetrend`, `relative_volume`, `atr`) run for real; the
rest are stub-only until their INTEGRATION POINT is wired.

Real run (BTCUSDT 1h, 180 days, 4319 bars, `rsi-adx-btc`): 23 trades,
net +1.54%, Sharpe 0.51, Deflated SR 0.71, Max DD -2.99%, win rate 47.8%,
profit factor 1.24 — 1 full run + 3 fold runs in ~2s.

### Fixed: `equity_curve_mtm` used to collapse while a position was open

`Portfolio.equity()` returns **`dict[Currency, Money]`**, not a `Money`.
`ComposedStrategy._current_equity` called `float()` on it, the `except`
swallowed the `TypeError`, and every run fell through to a balance scan. On a
CASH account — the default whenever `allow_short` is off — that balance is the
*unspent cash*: buying converts USDT into BTC, so equity read 10084 → 5762 for
as long as a position was held.

| | before | after |
|---|---|---|
| MTM minimum | 588.84 | 9 898.82 |
| bars under 5000 | 51 | 0 |
| `max_dd` | −94.27% | −2.99% |
| `sharpe` (bar-frequency) | 18.64 | 6.02 |

It fed the engine's `max_dd` and `sharpe`, so the `/backtest` page was affected
too — this was never studio-specific. Fixed in `composer.py`; the studio now
uses the bar-level curve again.

**Sharpe stays per-trade** on purpose. Bar-frequency Sharpe counts every flat
bar as a zero return, so a strategy that sits out of the market most of the
time gets a deflated denominator — the same run reads 6.02 bar-frequency
against 0.51 per-trade. The studio *ranks* strategies (optimizer objective,
deploy gate) and that bias would systematically favour rarely-trading ones.
One line in `run()` switches it to mirror `/backtest`.

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
