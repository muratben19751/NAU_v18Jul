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
| POST `…/optimize` · GET `…/optimize/panel` | Grid run (capped) + poll |
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

## Notes & limitations

- Regime ELSE supports an inline substrategy (entry/exit share the main
  risk block). Referencing a *saved* strategy by id (`substrategy_id`)
  remains reserved for later.
- Allocation covers ranked-universe sort/top-N/weighting; Composer-style
  per-position filter chains stay deferred to M-QLAB.
- Stub metrics are deterministic per config hash — good for UI/tests,
  meaningless for trading decisions.
