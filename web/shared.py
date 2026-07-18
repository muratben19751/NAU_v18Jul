"""Shared web-layer infrastructure — progress stores + logging/chart helpers.

Leaf module: routes import FROM here, and this imports nothing from
``web.routes`` (keeps the dependency arrow one-directional). Extracted to end
two smells the route modules had grown:

- the same "progress dict + lock + capped done-first eviction" was hand-copied
  into five route modules (``ProgressStore`` replaces it), and
- ``_log_backtest`` / ``_log_robustness`` / ``_chart_url`` / ``_rotate_if_large``
  were cross-imported between route modules (content coupling). They live here
  now as public functions; the route modules keep thin ``_name`` re-export
  aliases for internal use and backward compat with tests.
"""

from __future__ import annotations

import json
import math
import threading
from datetime import UTC, datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Progress store — one per long-running feature (backtest/gen/sweep/lab/robust/
# agent). Holds the run→state dict, its lock, and the capacity policy so the
# identical eviction block isn't copy-pasted per module. Callers keep using the
# underlying dict/lock directly (via .raw()/.lock aliases) for reads/updates;
# only the create+evict step goes through the store.
# ---------------------------------------------------------------------------
class ProgressStore:
    """Bounded run-id → state-dict registry with a done-first eviction policy.

    ``max_entries`` caps memory from abandoned runs. Two create policies match
    the two behaviours the routes had: ``create_evicting`` drops the oldest
    entry (done ones first) to make room — accepting that a still-running run
    could lose its slot under load; ``create_or_refuse`` drops only *done*
    entries and refuses (returns ``False``) if every slot is an active run.
    """

    def __init__(self, max_entries: int) -> None:
        self._d: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.max_entries = max_entries

    @property
    def lock(self) -> threading.Lock:
        """The store's lock — alias so existing ``with _X_LOCK:`` sites work."""
        return self._lock

    def raw(self) -> dict[str, dict]:
        """The underlying dict — alias so existing direct access is unchanged.

        Callers must hold ``self.lock`` while touching it, exactly as before.
        """
        return self._d

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            return self._d.get(run_id)

    def _evict_oldest_locked(self, on_evict) -> None:
        # done-first, else the oldest run (data-loss last resort). Caller holds
        # the lock; the store is non-empty (len >= max_entries >= 1).
        oldest = next(
            (k for k, v in self._d.items() if v.get("done")),
            next(iter(self._d)),
        )
        self._d.pop(oldest, None)
        if on_evict is not None:
            on_evict(oldest)

    def create_evicting(self, run_id: str, initial: dict, on_evict=None) -> None:
        """Insert ``initial`` under ``run_id``, evicting to stay within cap."""
        with self._lock:
            while len(self._d) >= self.max_entries:
                self._evict_oldest_locked(on_evict)
            self._d[run_id] = initial

    def create_or_refuse(self, run_id: str, initial: dict, on_evict=None) -> bool:
        """Insert ``initial`` unless every slot holds an active (not done) run.

        Returns ``True`` on insert, ``False`` when refused (all running).
        ``on_evict(evicted_id)`` runs under the lock for each dropped done run.
        """
        with self._lock:
            while len(self._d) >= self.max_entries:
                evict_id = next((k for k, v in self._d.items() if v.get("done")), None)
                if evict_id is None:
                    return False
                self._d.pop(evict_id, None)
                if on_evict is not None:
                    on_evict(evict_id)
            self._d[run_id] = initial
            return True


# ---------------------------------------------------------------------------
# Append-only JSONL logs (single source of truth for the paths, which used to
# be duplicated across the writer modules and reports.py).
# ---------------------------------------------------------------------------
_CACHE_DIR = Path.home() / ".cache" / "nautilus_web_app"
BACKTEST_LOG = _CACHE_DIR / "backtest_log.jsonl"
ROBUSTNESS_LOG = _CACHE_DIR / "robustness_log.jsonl"

# Append-only JSONL logs were growing without bound (backtest_log ~10MB,
# robustness ~5MB). On threshold exceed, roll over to a single-generation
# archive: the active file starts clean, and readers (tail-read / full read)
# see only the active file.
LOG_ROTATE_BYTES = 20 * 1024 * 1024

_BACKTEST_LOG_LOCK = threading.Lock()
_ROBUSTNESS_LOG_LOCK = threading.Lock()


def rotate_if_large(path: Path, max_bytes: int | None = None) -> None:
    """Roll the file over to `<name>.jsonl.1` if it exceeds the threshold (overwriting the existing archive).

    The threshold is resolved at call time (so it can be monkeypatched in tests)."""
    limit = max_bytes if max_bytes is not None else LOG_ROTATE_BYTES
    try:
        if path.exists() and path.stat().st_size >= limit:
            archive = path.with_name(path.name + ".1")
            if archive.exists():
                archive.unlink()
            path.rename(archive)
    except OSError:
        pass  # a rotation failure must not block log writing


def sanitize_floats(obj):
    """Replace NaN/Inf floats with None so json.dumps produces valid JSON."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_floats(v) for v in obj]
    return obj


def chart_url(bi: dict, spec_id: str = "") -> str:
    """Build chart URL scoped to the backtest's actual time window + interval.

    Interval → chart TF mapping picks a resolution that keeps bar count sane
    while covering the full backtest range so trade markers land on-screen.
    spec_id → chart, draws the strategy's actual indicators.
    """
    sym = bi.get("symbol")
    if not sym:
        return ""  # Index path (no symbol) — chart not supported
    cat = bi.get("category", "linear")
    interval = bi.get("interval", "60")
    sid = f"&spec_id={spec_id}" if spec_id else ""
    # Convert the backtest time range to a timestamp
    start_ts = end_ts = None
    try:
        from pandas import Timestamp

        if bi.get("start"):
            start_ts = int(Timestamp(bi["start"]).timestamp())
        if bi.get("end"):
            end_ts = int(Timestamp(bi["end"]).timestamp())
    except Exception:
        pass
    if start_ts and end_ts:
        # For a long range pick a larger TF (keep bar count under ~2000)
        span_days = (end_ts - start_ts) / 86400
        tf = interval
        if span_days > 400:
            tf = "D"
        elif span_days > 60:
            tf = "240"
        elif span_days > 14:
            tf = "60"
        elif span_days > 3:
            tf = "15"
        return f"/chart/data?symbol={sym}&category={cat}&interval={tf}&start_ts={start_ts}&end_ts={end_ts}{sid}"
    return f"/chart/data?symbol={sym}&category={cat}&interval={interval}&bars=2000{sid}"


def log_backtest(
    spec,
    result,
    instrument_kind: str,
    bars_info: dict,
    elapsed_sec: float | None = None,
) -> None:
    BACKTEST_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "elapsed_sec": round(elapsed_sec, 3) if elapsed_sec is not None else None,
        "spec": {
            "id": spec.id,
            "name": spec.name,
            "blocks": [
                {"type": b.type, "role": b.role, "params": b.params}
                for b in spec.blocks
            ],
            "entry_logic": spec.entry_logic,
            "exit_logic": spec.exit_logic,
            "order_type": spec.order_type,
            "trade_size": float(spec.trade_size),
            "trade_size_mode": spec.trade_size_mode,
            "use_bracket": spec.use_bracket,
            "sl_type": spec.sl_type,
            "sl_value": spec.sl_value,
            "tp_type": spec.tp_type,
            "tp_value": spec.tp_value,
            "allow_short": spec.allow_short,
            "emulate": spec.emulate,
            # Remaining spec fields for deterministic re-run (reports/detail)
            # — getattr: don't break duck-typed fake specs in tests.
            "limit_offset_bps": getattr(spec, "limit_offset_bps", 0.0),
            "atr_period": getattr(spec, "atr_period", 14),
            "trade_size_percent": getattr(spec, "trade_size_percent", 5.0),
            "trade_size_atr_risk": getattr(spec, "trade_size_atr_risk", 1.0),
            "trade_size_usdt": getattr(spec, "trade_size_usdt", 1000.0),
            "trend_filter": getattr(spec, "trend_filter", False),
            "trend_interval": getattr(spec, "trend_interval", "60"),
            "trend_ema_period": getattr(spec, "trend_ema_period", 50),
            "delay_fill": getattr(spec, "delay_fill", True),
        },
        "instrument": instrument_kind,
        "bars": bars_info,
        "rationale": result.rationale,
        "error": result.error,
        "metrics": sanitize_floats(result.metrics),
        "n_equity_points": len(result.equity_curve),
    }
    with _BACKTEST_LOG_LOCK:
        rotate_if_large(BACKTEST_LOG)
        with open(BACKTEST_LOG, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")


def log_robustness(
    spec_id: str,
    spec_name: str,
    result: dict,
    *,
    symbol: str | None = None,
    category: str | None = None,
    interval: str | None = None,
    venue: str | None = None,
) -> None:
    """Write the scalar robustness summary to disk (excluding equity curves).

    Identity fields (symbol/category/interval/venue) are added to the record; if
    not provided they are read from ``result``. Without them, different
    symbol/TF runs of the same spec were overwriting each other in a
    last-writer-wins fashion in the report.
    """
    try:
        ROBUSTNESS_LOG.parent.mkdir(parents=True, exist_ok=True)

        # Walk-forward: without equity curve
        wf_clean = []
        for w in result.get("wfo_windows") or []:
            wf_clean.append(
                {
                    "window": w.get("window"),
                    "train_start": w.get("train_start"),
                    "train_end": w.get("train_end"),
                    "test_start": w.get("test_start"),
                    "test_end": w.get("test_end"),
                    "chosen_params": w.get("chosen_params") or {},
                    "train_objective": w.get("train_objective"),
                    "objective_metric": w.get("objective_metric"),
                    "train_metrics": w.get("train_metrics") or {},
                    "test_metrics": w.get("test_metrics") or {},
                    "test_metrics_naive": w.get("test_metrics_naive") or {},
                    "train_n_trades": w.get("train_n_trades"),
                    "test_n_trades": w.get("test_n_trades"),
                }
            )

        # Monte Carlo: excluding large lists
        mc_raw = result.get("mc") or {}
        mc_clean = {
            k: mc_raw[k]
            for k in (
                "n_sims",
                "n_trades",
                "starting_cash",
                "original_final",
                "p5_final",
                "p25_final",
                "median_final",
                "p75_final",
                "p95_final",
                "max_dd_p50",
                "max_dd_p95",
                "win_rate_mean",
                "win_rate_std",
                "method",
            )
            if k in mc_raw
        }
        # Preserve error field so log accurately reflects MC failures.
        if "error" in mc_raw:
            mc_clean["error"] = mc_raw["error"]

        # In/Out-of-Sample: without equity curve
        sp_raw = result.get("split") or {}
        sp_clean = {
            k: sp_raw[k]
            for k in (
                "split_pct",
                "split_date",
                "in_sample_n_bars",
                "oos_n_bars",
                "overfitting_score",
                "overfitting_label",
                "in_sample_error",
                "oos_error",
            )
            if k in sp_raw
        }
        sp_clean["in_sample_metrics"] = sp_raw.get("in_sample_metrics") or {}
        sp_clean["oos_metrics"] = sp_raw.get("oos_metrics") or {}

        record = sanitize_floats(
            {
                "ts": datetime.now(UTC).isoformat(),
                "spec_id": spec_id,
                "spec_name": spec_name,
                "symbol": symbol or result.get("symbol"),
                "category": category or result.get("category"),
                "interval": interval or result.get("interval"),
                "venue": venue or result.get("venue"),
                "walk_forward": wf_clean,
                "wfo_summary": result.get("wfo_summary") or {},
                "monte_carlo": mc_clean,
                "in_out_split": sp_clean,
            }
        )
        with _ROBUSTNESS_LOG_LOCK:
            rotate_if_large(ROBUSTNESS_LOG)
            with open(ROBUSTNESS_LOG, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass
