"""Persistent per-model LLM token ledger.

Wiki References
---------------
See: [[webapp_module_map]] (bu modülün rolü + `_create_message` ledger-hook'u),
[[crash_only_design]] (best-effort append; ledger I/O bir LLM çağrısını asla
bozamaz — restart'ta veri kaybolmasın diye diske yazılır).

Every LLM call in the app funnels through ``agent._create_message`` (and the four
narrative/summary helpers that were routed through it). Each successful call
appends ONE JSONL line here with the **actual** model the API answered with —
so the Fable→Opus credit fallback is attributed to the model that really ran,
not the one the app defaults to. This is the durable source the in-memory
``_AGENT_PROGRESS`` counters never were (they were AUTO-loop-scoped, single-model,
and lost on restart — see the second-brain note ``nau_token_tuketim_izleme``).

Design contract:
- ``record`` is best-effort and MUST NOT raise into an LLM call path — a ledger
  I/O hiccup can never break strategy generation. All failures are swallowed.
- One JSONL line per call: ts, model, purpose, and the four token counts.
- ``summary`` folds the log into a per-model table; malformed/partial lines
  (a line torn by a concurrent append) are skipped, not fatal.

Location: ``~/.cache/nautilus_web_app/token_usage.jsonl`` (same cache root as the
agent session logs).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

LEDGER_PATH = Path.home() / ".cache" / "nautilus_web_app" / "token_usage.jsonl"

_LOCK = threading.Lock()

# The four usage fields, in the order the app already uses everywhere.
_FIELDS = ("input", "output", "cache_read", "cache_write")

# Anthropic/CLI usage attribute (or dict-key) names → our short field names.
_USAGE_KEYS = {
    "input": ("input_tokens",),
    "output": ("output_tokens",),
    "cache_read": ("cache_read_input_tokens",),
    "cache_write": ("cache_creation_input_tokens",),
}


def _extract_usage(usage) -> dict[str, int]:
    """Normalize an Anthropic ``resp.usage`` object OR a plain dict → the four
    int counters. Missing/None fields become 0."""
    out = {f: 0 for f in _FIELDS}
    if usage is None:
        return out
    is_dict = isinstance(usage, dict)
    for field, keys in _USAGE_KEYS.items():
        for k in keys:
            val = usage.get(k) if is_dict else getattr(usage, k, None)
            if val:
                out[field] = int(val)
                break
    return out


def record(model: str, usage, purpose: str = "") -> None:
    """Append one usage line. Best-effort — never raises into the caller.

    ``model`` should be the model the API actually answered with
    (``getattr(resp, "model", called_model)``), so fallback is attributed
    correctly. ``purpose`` is a free-text tag for the call site (e.g.
    "strategy", "custom_block", "narrative"); "" is fine.
    """
    try:
        counts = _extract_usage(usage)
        if not any(counts.values()):
            return  # nothing to record (e.g. an empty/failed response)
        line = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "model": model or "unknown",
            "purpose": purpose or "",
            **counts,
        }
        payload = json.dumps(line, ensure_ascii=False)
        with _LOCK:
            LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LEDGER_PATH, "a", encoding="utf-8") as f:
                f.write(payload + "\n")
    except Exception:
        # A ledger write must never break an LLM call.
        pass


def summary(path: Path | None = None) -> dict:
    """Fold the ledger into a per-model breakdown.

    Returns::

        {
          "models": {
            "claude-fable-5": {"calls": 12, "input": …, "output": …,
                               "cache_read": …, "cache_write": …, "total": …},
            "claude-opus-4-8": {…},
          },
          "total": {"calls": …, "input": …, …, "total": …},
          "first_ts": "…", "last_ts": "…",
        }

    Missing/torn JSONL lines are skipped. Returns empty structures if the ledger
    does not exist yet.
    """
    p = path or LEDGER_PATH
    models: dict[str, dict] = {}
    grand = {"calls": 0, **{f: 0 for f in _FIELDS}, "total": 0}
    first_ts = last_ts = None
    if not p.exists():
        return {"models": models, "total": grand, "first_ts": None, "last_ts": None}
    try:
        with open(p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue  # torn line from a concurrent append — skip
                model = rec.get("model") or "unknown"
                m = models.setdefault(
                    model, {"calls": 0, **{fld: 0 for fld in _FIELDS}, "total": 0}
                )
                m["calls"] += 1
                grand["calls"] += 1
                for fld in _FIELDS:
                    v = int(rec.get(fld) or 0)
                    m[fld] += v
                    m["total"] += v
                    grand[fld] += v
                    grand["total"] += v
                ts = rec.get("ts")
                if ts:
                    if first_ts is None or ts < first_ts:
                        first_ts = ts
                    if last_ts is None or ts > last_ts:
                        last_ts = ts
    except Exception:
        pass
    return {"models": models, "total": grand, "first_ts": first_ts, "last_ts": last_ts}


def format_table(path: Path | None = None) -> str:
    """A plain-text per-model table for CLI / text/plain responses."""
    s = summary(path)
    models = s["models"]
    if not models:
        return "No token usage recorded yet (token_usage.jsonl is empty or absent)."

    headers = ["MODEL", "CALLS", "INPUT", "OUTPUT", "CACHE_RD", "CACHE_WR", "TOTAL"]
    rows = []
    # Highest total first.
    for model, m in sorted(models.items(), key=lambda kv: kv[1]["total"], reverse=True):
        rows.append(
            [
                model,
                f"{m['calls']:,}",
                f"{m['input']:,}",
                f"{m['output']:,}",
                f"{m['cache_read']:,}",
                f"{m['cache_write']:,}",
                f"{m['total']:,}",
            ]
        )
    g = s["total"]
    total_row = [
        "TOTAL",
        f"{g['calls']:,}",
        f"{g['input']:,}",
        f"{g['output']:,}",
        f"{g['cache_read']:,}",
        f"{g['cache_write']:,}",
        f"{g['total']:,}",
    ]

    widths = [len(h) for h in headers]
    for r in rows + [total_row]:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))

    def _fmt(cells: list[str]) -> str:
        # Left-align the model name, right-align the numbers.
        out = [cells[0].ljust(widths[0])]
        out += [cells[i].rjust(widths[i]) for i in range(1, len(cells))]
        return "  ".join(out)

    sep = "-" * (sum(widths) + 2 * (len(headers) - 1))
    lines = [_fmt(headers), sep]
    lines += [_fmt(r) for r in rows]
    lines += [sep, _fmt(total_row)]
    span = ""
    if s["first_ts"] and s["last_ts"]:
        span = f"\n\nRange: {s['first_ts']} → {s['last_ts']}"
    return "\n".join(lines) + span


if __name__ == "__main__":
    # `python token_ledger.py` → print the per-model token table.
    print(format_table())
