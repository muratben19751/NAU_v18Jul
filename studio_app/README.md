# Strategy Studio — Phases 1-6 (STUDIO_SPEC.md)

Visual strategy builder for `nautilus_web_app`: versioned strategy schema,
HTMX editing with a draft workflow, backtest + walk-forward optimization
wiring, an AI improvement layer with server-side guardrails, and gated
deployment. Delivered as a standalone FastAPI package; every place that must
touch your existing code is marked `INTEGRATION POINT` in the source.

## Run it

```bash
pip install fastapi jinja2 uvicorn pytest httpx python-multipart
python scripts/seed_studio.py           # creates studio.db + demo strategy
uvicorn app.main:app --reload
# open http://127.0.0.1:8000/studio/wt-funding-v3
python -m pytest tests/ -q              # 69 tests (+1 env-flagged LLM smoke)
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

## Merging into nautilus_web_app

1. Copy `app/studio/`, `templates/studio/`, `static/studio.{css,js}`.
2. Convert `app/main.py` routes to an `APIRouter` and `include_router()`.
3. Point `StrategyStore(DB_PATH)` at your app's SQLite (tables are additive).
4. Wire the four INTEGRATION POINTs:
   - `registry.py` — map each indicator's `impl` to your feature functions.
   - `backtest.py` — `NautilusBacktestAdapter.run()` over your runner
     (write `to_nautilus(CompiledStrategy)` next to your strategy classes).
   - `optimizer.py` — adapt your walk-forward optimizer; keep the
     `"<rule_id>.<param>"` addressing so Apply keeps working.
   - `ai.py` — swap `HttpAnthropicClient` for your LLM loop's client.
   - `deploy.py` — `launch(artifact)` against your live/sim TradingNode;
     flip the deployment row to `running` on pickup.
5. Set `ANTHROPIC_API_KEY` if you keep the built-in HTTP client.
   `STUDIO_LLM_SMOKE=1 pytest tests/studio/test_ai.py -k smoke` for a live check.

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
