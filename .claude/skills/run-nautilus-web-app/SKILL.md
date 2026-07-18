---
name: run-nautilus-web-app
description: Build, launch, and drive the Nautilus web app (FastAPI + HTMX backtesting lab). Use when asked to run, start, launch, serve, smoke-test, or drive a backtest through this app, or to confirm a change works in the running app rather than only in tests.
---

# Run the Nautilus web app

A single-process **FastAPI + HTMX** app for composing and backtesting Bybit
strategies on `nautilus_trader`. The user surface is HTTP: a backtest is a
`POST /backtest/run` that spawns a worker thread, then HTMX polls
`GET /backtest/progress/{run_id}` until the result renders.

The agent handle is **[driver.py](.claude/skills/run-nautilus-web-app/driver.py)**
— it launches `uvicorn server:app`, drives a real backtest over HTTP, prints the
metrics, and stops the server. All paths below are relative to the repo root;
run everything from there. Windows + PowerShell host; the Bash tool is Git Bash.

## Prerequisites

Python **3.12** (`requires-python = ">=3.12,<3.13"`) and the deps in
`pyproject.toml` (hard pin: `nautilus_trader==1.230.0`). On this dev machine they
are already installed — verify with:

```bash
python -c "import nautilus_trader, fastapi, uvicorn, anthropic, pandas; print('deps present', nautilus_trader.__version__)"
# -> deps present 1.230.0
```

Fresh machine: `pip install -e ".[dev]"` (or `uv sync --extra dev`). First launch
fetches BTCUSDT/linear/1m from Bybit's public API at startup, so it needs network
once; after that the parquet cache under `~/.cache/nautilus_web_app/` serves it.

## Run — agent path (drive a real backtest)

```bash
python .claude/skills/run-nautilus-web-app/driver.py --port 8199
```

Exit 0 + a `[driver] PASS: ...` line = the app launched and returned real
metrics. Verified output this session:

```
[startup] nautilus_trader 1.230.0
[catalog] wrote 10,109 bars → BTCUSDT.BYBIT_LINEAR-1-MINUTE-LAST-EXTERNAL
[driver] using existing builtin spec d695cc169f37 (Random price_breakout/bollinger_break)
[driver] server up; driving a backtest ...
[driver] backtest run_id=4e7b85d3
[driver] PASS: BacktestEngine ... Bu strateji 202 işlem açtı ve +2.09 USDT kazandı. Win rate %49.0 ... 10,108 bar | +2.09 USDT
[driver] server stopped
```

The driver picks any builtin-block spec from the catalog (seeds a minimal
`ma_cross+atr_stop` one only if the catalog is empty), so it doesn't depend on
your saved strategies. It stops uvicorn via `proc.terminate()` on exit.

## Run — human path

```bash
PYTHONUTF8=1 python -m uvicorn server:app --port 8111 --log-level warning
```

Then open `http://127.0.0.1:8111/` — nav has Dashboard, Data, Strategy Composer,
Backtest, Strategy Lab, Reports, Agent, Session Logları. Ctrl-C to stop. Useless
headless; use the driver instead.

## Direct invocation (PRs touching internals)

Most backtest/robustness/data changes don't need the server — call the functions
on cached bars. Verified pattern:

```bash
python - <<'PY'
import pandas as pd, data
from composer import ComposedStrategySpec, SignalBlock
from sandbox import run_backtest_guarded          # builtin -> in-process; custom -> killable subprocess
df = pd.read_parquet(data._bybit_cache_path("linear", "BTCUSDT", "1"))
spec = ComposedStrategySpec(id="x", name="x", description="", blocks=[
    SignalBlock(type="ma_cross", role="entry", params={"fast":10,"slow":30,"direction":"up"})], trade_size=0.1)
r = run_backtest_guarded(spec, df, {"symbol":"BTCUSDT","category":"linear","interval":"1"})
print("error:", r.error, "n_trades:", (r.metrics or {}).get("n_trades"))
PY
```

Rebuild the Nautilus catalog after any timestamp/category change (migration):

```bash
PYTHONUTF8=1 python data.py rebuild
# -> Rebuilt 54 series (0 skipped) → .../nautilus_catalog
```

## Test

```bash
python -m pytest tests -q       # 72 tests
python -m ruff check .           # lint (config in pyproject [tool.ruff])
python -m ruff format --check .
```

## Gotchas

- **Force UTF-8 or Windows prints crash.** Progress logs contain `→ · …` and
  Turkish text; a plain Windows console (cp125x) raises `UnicodeEncodeError`.
  `server.py` and the `data.py` CLI reconfigure stdout, and the driver sets
  `PYTHONUTF8=1` for the uvicorn subprocess. Run standalone scripts with
  `PYTHONUTF8=1`.
- **First run needs network** for the startup Bybit fetch; offline it warns and
  serves whatever the cache holds. A backtest with no cached bars for the chosen
  symbol/category/interval returns an error panel, not a crash.
- **Custom-block backtests fork a subprocess.** Builtin-only specs run
  in-process (fast); specs with a custom block run in a killable `spawn` child
  (`sandbox.py`) with a wall-clock timeout. The child re-imports `composer`, so
  the custom block must be persisted to disk (the save flow does this).
- **Catalog keys are per-category + close-time.** Bars are stored under
  `BYBIT_SPOT`/`BYBIT_LINEAR`/`BYBIT_INVERSE` at bar-close time. Old open-time,
  category-blind `*.BYBIT-*` keys are stale — `python data.py rebuild` clears
  and regenerates them.
- **Startup asserts the nautilus version** (`constants.NAUTILUS_REQUIRED`); a
  mismatched wheel fails fast with a clear message instead of odd runtime errors.

## Troubleshooting

- `ModuleNotFoundError: No module named 'composer'` — you ran a script from
  outside the repo root. Run from the root; the driver already inserts the repo
  root on `sys.path` and sets the uvicorn `cwd`, so it works from anywhere.
- `uvicorn: No module named uvicorn` / import errors on launch — deps not
  installed; see Prerequisites.
- Driver prints `FAIL: backtest did not complete within Ns` — user-run
  backtests use the FULL cached range (1m BTCUSDT cache is 1M+ bars → run
  takes minutes, incl. commissions since 2026-07). Default wait is 420s;
  raise with `--backtest-timeout 900` if the cache keeps growing.
- Driver prints `FAIL: server did not become ready` — a slow first Bybit fetch or
  an occupied port. Retry with a different `--port`, or check the uvicorn output
  it inherits to stdout.
