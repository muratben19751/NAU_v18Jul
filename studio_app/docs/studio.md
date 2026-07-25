# Strategy Studio — architecture notes

## Data flow

```
draft (strategy_drafts) ── Save ──> version N (strategy_versions, append-only)
   ▲            │                        │
   │      compile_strategy()             │ Deploy (saved only)
mutations.py    ▼                        ▼
(human + AI) CompiledStrategy ──> BacktestAdapter / Optimizer / deploy artifact
```

Every mutation endpoint: load working copy (draft ?? latest) → mutate →
validate → save draft → re-render block partial + OOB side panel.

## Endpoint map

| Method/Path | Purpose |
|---|---|
| GET `/studio/{id}` | Full page (draft if present); `?version=N` for history |
| GET `/studio/{id}/history` | Version list |
| POST `…/blocks/{block}/rules` | Add rule (registry defaults) |
| PATCH `…/rules/{rule_id}` | Edit param/target (bounds-checked) |
| DELETE `…/rules/{rule_id}` | Delete rule (entry keeps ≥1) |
| PATCH `…/blocks/{block}` | match / evaluate |
| PATCH `…/risk`, `…/instruments/{sym}` | Risk fields, instrument toggles |
| POST `…/save`, `…/discard` | Promote draft / drop it |
| POST `…/backtest` · GET `…/runs/latest[,/folds]` | Run + 2s poll + folds |
| PATCH `…/opt/toggle`, `…/opt/range` | Sweep membership / min-step-max |
| POST `…/optimize` · GET `…/optimize/panel` | Walk-forward sweep (capped) + poll |
| POST `…/optimize/apply` | Write ranked params into draft |
| POST `…/ai/suggest` | One suggestion → trial → ghost |
| POST `…/ai/suggestions/{sid}/accept·dismiss` | Human decision |
| POST `…/ai/loop/start` · GET `…/ai/panel` | Iterative loop + poll |
| GET `…/deploy/modal` · POST `…/deploy` | Gated deployment |
| PATCH `…/blocks/regime` (`else_mode`) | Flat ⇄ inline substrategy |
| POST `…/blocks/sub_entry·sub_exit/rules` | Substrategy rules (same PATCH/DELETE) |
| PATCH `…/allocation` | Universe mode / sort / top-N / weighting |
| GET `…/deployments/panel` | Deployment list (polls while pending) |
| POST `…/deployments/{id}/pause·resume·stop` | Lifecycle transitions |

## AI contract

LLM must return exactly one JSON `Suggestion`:
`kind` ∈ add_rule | modify_param | remove_rule | modify_risk, `block`,
`diff`, `rationale`, `expected.dsr_delta`. Parsing retries once with the
validation error fed back. Rejected/dismissed rationales are included in the
next prompt so the loop doesn't repeat itself.

Guardrails (server-side, `evaluate_trial`): invalid diff / compile error,
`trades < min_trades`, OOS objective worse than baseline → reject, even in
auto mode. Auto-accept additionally requires strict OOS improvement.

## Walk-forward contract

`WalkForwardOptimizer.run(defn) -> list[OptResult]`, param addressing
`"<rule_id>.<param>"` / `"risk.<field>"` (Apply depends on that spelling).

Two stages per candidate, both on windows the adapter cuts out of its own
sample (`run(compiled, window=Window(start, end, embargo_bars))`):

1. **anchored in-sample screen** — leading `in_sample_months` share; below
   `min_trades` (5) or `-inf` objective ⇒ the candidate never reaches the folds.
2. **purged walk-forward folds** — `folds` consecutive out-of-sample windows of
   `oos_months` each, every one purged by `embargo_bars` at its front. Ranking
   key is `mean − 0.5·std` of the per-fold objective (host `wfo_optimizer`
   convention); `dsr`/`sharpe` folds are trade-count damped (`n/(n+20)`),
   `max_dd` is not (damping a negative number rewards a thin history).
   A candidate needs 60% of its folds valid.

The months are a **ratio**, not calendar lengths: they split whatever sample
the adapter loaded (`lookback_days`, 180 by default) into 1 + `folds` windows.
Raise the lookback to make them literal.

`OptResult.dsr` is **deflated**: PSR of the stitched out-of-sample return series
against `expected_max_sharpe(σ_trials, N)` — the best Sharpe N combinations
produce by luck alone. It is therefore *not* comparable to the single-run
`dsr`, which is undeflated PSR (one trial). Nothing survives ⇒
`NoViableCandidates` with the rejection tally, surfaced as a failed run.

## Deployment contract

`prepare_deployment(defn, latest_metrics, cfg) -> artifact JSON` compiles the
**saved** version, runs the gate, then lowers it with `to_nautilus`. The
artifact is runnable, not a description:

| key | what a runner does with it |
|---|---|
| `artifact_schema` | refuse a version you do not know (v1 was counts-only) |
| `spec` | `ComposedStrategySpec.from_dict` → one `ComposedStrategy` per instrument |
| `instruments` | the spec is instrument-free, so the pairing lives here |
| `capital`, `risk`, `kill_switch_daily_pct` | account size, risk block, kill switch |

Anything `to_nautilus` refuses (regime branch, ranked allocation, an indicator
with no engine block …) is refused **at deploy time** with its reasons, and the
modal disables the button rather than letting you submit into a 422.

`PaperRunner` (`STUDIO_RUNNER=paper`) runs the artifact as a sandbox
`TradingNode`: live Bybit market data, `SandboxExecutionClient` fills, no
credentials anywhere. One node per deployment, each on its own thread and loop.

* `environment='live'` is **refused** — no exchange credentials exist, and the
  gate still reads the undeflated single-run DSR.
* `pending → running` means the node was built and handed to its loop. If it
  dies later the serve thread flips the row to `failed` with the reason.
* pause stops the strategies and keeps the node up; resume uses the component
  RESUME transition (a STOPPED component refuses START) so warm-up survives.
* The node registry is in-process, so `reconcile_orphans` marks `running` rows
  with no node behind them as `failed` at startup.

## Notes & limitations

- Regime ELSE supports an inline substrategy (entry/exit share the main
  risk block). Referencing a *saved* strategy by id (`substrategy_id`)
  remains reserved for later.
- Allocation covers ranked-universe sort/top-N/weighting; Composer-style
  per-position filter chains stay deferred to M-QLAB.
- Stub metrics are deterministic per config hash — good for UI/tests,
  meaningless for trading decisions. A window is part of that hash, so the
  stub's folds differ from each other instead of being identical.
- Deflation needs a spread across trials to measure; a sweep that scores a
  single candidate has none, and its `dsr` falls back to undeflated PSR.
- The single-run fold table is a *different* split — purged k-fold over the
  run's own sample, `folds` + `embargo_bars` only. The IS/OOS months belong to
  the optimizer.
