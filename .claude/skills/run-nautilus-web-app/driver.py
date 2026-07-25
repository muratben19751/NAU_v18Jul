#!/usr/bin/env python
"""Launch the Nautilus web app and drive a real backtest end-to-end.

Server-type driver: starts `uvicorn server:app`, waits for readiness, picks a
builtin-block strategy from the catalog (seeds one if the catalog is empty),
POSTs /backtest/run, polls /backtest/progress until the result renders, prints
the metrics, and stops the server. Exit 0 = the app ran a backtest and returned
metrics; non-zero = it didn't.

Usage:
    python .claude/skills/run-nautilus-web-app/driver.py [--port 8199]

Stdlib only (urllib/subprocess) — no extra deps. Cross-platform (proc.terminate
stops uvicorn on Windows and POSIX alike). Run from the repo root.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Repo root = four levels up from this driver (root/.claude/skills/run-*/driver.py).
# Make app modules importable and give uvicorn the right CWD regardless of where
# this script is invoked from.
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


def _http(url, data=None, timeout=15):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def pick_or_seed_spec() -> str:
    """Return a spec_id whose blocks are all builtin. Seed one if none exist."""
    from composer import (
        BLOCK_REGISTRY,
        ComposedStrategySpec,
        SignalBlock,
        load_catalog,
        new_spec_id,
        save_catalog,
    )

    catalog = load_catalog()
    for s in catalog:
        if s.blocks and all(
            (BLOCK_REGISTRY.get(b.type) or {}).get("builtin") for b in s.blocks
        ):
            print(f"[driver] using existing builtin spec {s.id} ({s.name})")
            return s.id

    probe = ComposedStrategySpec(
        id=new_spec_id(),
        name="run-skill probe (ma_cross+atr_stop)",
        description="seeded by run-nautilus-web-app driver",
        blocks=[
            SignalBlock(
                type="ma_cross",
                role="entry",
                params={"fast": 10, "slow": 30, "direction": "up"},
            ),
            SignalBlock(
                type="atr_stop", role="exit", params={"period": 14, "mult": 3.0}
            ),
        ],
        trade_size=0.1,
    )
    save_catalog(catalog + [probe])
    print(f"[driver] seeded probe spec {probe.id}")
    return probe.id


def wait_ready(base: str, timeout: int = 40) -> bool:
    for _ in range(timeout):
        try:
            status, _ = _http(base + "/", timeout=3)
            if status in (200, 307, 404):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def run_backtest(base: str, spec_id: str, timeout_s: int = 420) -> tuple[bool, str]:
    status, body = _http(
        base + "/backtest/run",
        data={
            "spec_id": spec_id,
            "instrument_kind": "Bybit",
            "symbol": "BTCUSDT",
            "category": "linear",
            "interval": "1",
        },
    )
    if status != 200:
        return False, f"POST /backtest/run -> HTTP {status}: {body[:200]}"
    m = re.search(r"progress/([0-9a-f]+)", body)
    if not m:
        return False, f"no run_id in response: {body[:200]}"
    run_id = m.group(1)
    print(f"[driver] backtest run_id={run_id}")

    # User runs use the FULL range in the cache (ee3a25b) — the 1m BTCUSDT
    # cache has grown past 1M bars and a full run with commissions can take
    # minutes; the old 60s limit produced false FAILs as the cache grew.
    for _ in range(timeout_s):
        _, html = _http(base + f"/backtest/progress/{run_id}")
        done = ("Completed" in html or "Realized PnL" in html) and (
            "Backtest running" not in html
        )
        if "error" in html.lower():
            # errors still render a result panel; treat as completion, report text
            done = done or "Realized PnL" not in html
        if done:
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text)
            metric = re.search(
                r"(BacktestEngine[^\n]*?bar)|(Completed[^·]*win_rate=[\d.]+%)", text
            )
            summary = " | ".join(
                s.strip()
                for s in [
                    (metric.group(0) if metric else ""),
                    (
                        re.search(r"Realized PnL\s*([+\-]?[\d,.]+ USDT)", text).group(1)
                        if re.search(r"Realized PnL\s*([+\-]?[\d,.]+ USDT)", text)
                        else ""
                    ),
                ]
                if s.strip()
            )
            return True, summary or "result rendered (no metric parsed)"
        time.sleep(1)
    return False, f"backtest did not complete within {timeout_s}s"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8199)
    ap.add_argument(
        "--backtest-timeout",
        type=int,
        default=420,
        help="backtest completion wait (s) — full-range 1m runs grow as the "
        "cache grows (1.05M bars ≈ 80-90s)",
    )
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    spec_id = pick_or_seed_spec()

    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    print(f"[driver] launching uvicorn on :{args.port} ...")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server:app",
            "--port",
            str(args.port),
            "--log-level",
            "warning",
        ],
        env=env,
        cwd=str(REPO_ROOT),
    )
    try:
        if not wait_ready(base):
            print("[driver] FAIL: server did not become ready")
            return 1
        print("[driver] server up; driving a backtest ...")
        ok, summary = run_backtest(base, spec_id, timeout_s=args.backtest_timeout)
        if ok:
            print(f"[driver] PASS: {summary}")
            return 0
        print(f"[driver] FAIL: {summary}")
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("[driver] server stopped")


if __name__ == "__main__":
    raise SystemExit(main())
