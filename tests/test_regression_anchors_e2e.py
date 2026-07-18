"""Regression anchors (fixture-heavy) — the highest blast-radius remaining fixes
of the audit wave that require a parquet cache + Nautilus catalog + real engine
run. For pure-unit anchors see test_regression_anchors.py.

Fixes covered:
- #4 (H626)  load_bybit_bars force_refresh cache MERGE (concat+dedup keep='last')
- #5 (M1030) delete_data_range error → SKIP the write (non-disjoint parquet guard)
- #6         composer long→short FLIP path (_cancel_working + tags=['flip'])

Each test drives the REAL shipped function; if the fix is reverted it FAILs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest


# ===========================================================================
# #4 (critical, H626) — force_refresh cache MERGE (NOT truncate)
# The /data refresh button (days=7) was clipping years of 1m history down to 7
# days (permanent parquet data loss). force_refresh=True must READ the existing
# cache and merge the narrow window with concat+dedup(keep='last').
# ===========================================================================
def _refresh_fetch(step_ms, marker):
    """Synthetic _fetch_bybit_page: returns only the NARROW re-fetched window,
    with a distinguishing value (marker) in every column (open-time UTC index)."""

    def _fetch(category, symbol, interval, start_ms, end_ms):
        ts = list(range(start_ms, end_ms + 1, step_ms))
        if not ts:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        idx = pd.to_datetime(ts, unit="ms", utc=True)
        n = len(ts)
        return pd.DataFrame(
            {c: [marker] * n for c in ("open", "high", "low", "close", "volume")},
            index=idx,
        )

    return _fetch


class TestForceRefreshMerge:
    def test_force_refresh_merges_not_truncates(self, tmp_path, monkeypatch):
        import data

        monkeypatch.setattr(data, "BYBIT_CACHE_DIR", tmp_path / "bybit")
        monkeypatch.setattr(data, "NAUTILUS_CATALOG_DIR", tmp_path / "cat")
        data.BYBIT_CACHE_DIR.mkdir(parents=True)
        # Catalog auto-write is orthogonal to cache-merge; don't depend on nautilus_trader.
        monkeypatch.setattr(data, "_auto_write_bybit_catalog", lambda *a, **k: None)

        step_ms = data._BYBIT_MS["1"]
        SEED = 1.0
        REFRESH = 999.0
        monkeypatch.setattr(data, "_fetch_bybit_page", _refresh_fetch(step_ms, REFRESH))

        # WIDE cache seed: 100 one-minute bars, all columns == SEED.
        base = datetime(2024, 1, 1, tzinfo=UTC)
        cache_idx = pd.to_datetime(
            [int((base + timedelta(minutes=i)).timestamp() * 1000) for i in range(100)],
            unit="ms",
            utc=True,
        )
        cache_df = pd.DataFrame(
            {c: [SEED] * 100 for c in ("open", "high", "low", "close", "volume")},
            index=cache_idx,
        )
        cache_path = data._bybit_cache_path("linear", "BTCUSDT", "1")
        cache_df.to_parquet(cache_path)

        # /data refresh button: a NARROW window deep inside the seed range (40..50).
        start = base + timedelta(minutes=40)
        end = base + timedelta(minutes=50)
        data.load_bybit_bars(
            "BTCUSDT",
            interval="1",
            category="linear",
            start=start,
            end=end,
            force_refresh=True,
        )

        merged = pd.read_parquet(cache_path)

        # 1) Out-of-window HISTORY survives: cache still spans the whole wide range.
        assert merged.index[0] == pd.Timestamp(base)
        assert merged.index[-1] == pd.Timestamp(base + timedelta(minutes=99))
        assert len(merged) == 100

        # 2) Out-of-window bars keep the SEED value (refresh didn't touch them).
        assert merged.loc[pd.Timestamp(base), "open"] == SEED
        assert merged.loc[pd.Timestamp(base + timedelta(minutes=99)), "open"] == SEED
        assert merged.loc[pd.Timestamp(base + timedelta(minutes=30)), "open"] == SEED

        # 3) In-window bars reflect the REFETCH value (dedup keep='last').
        assert merged.loc[pd.Timestamp(base + timedelta(minutes=45)), "open"] == REFRESH
        assert (
            merged.loc[pd.Timestamp(base + timedelta(minutes=40)), "close"] == REFRESH
        )


# ===========================================================================
# #5 (critical, M1030) — a delete_data_range error must SKIP the write
# If write_data is CALLED after a non-benign (NOT 'no data'/'not found', e.g.
# PermissionError) delete error, overlapping (non-disjoint) old+new parquet
# ranges remain → the next catalog read hard-crashes. write_to_nautilus_catalog
# RAISEs, _auto_write_bybit_catalog RETURNs silently. Benign error → PROCEED
# with the write.
# ===========================================================================
class _FakeCatalog:
    """Fake catalog with a controllable delete_data_range that records write_data."""

    def __init__(self, delete_error: Exception | None = None):
        self.delete_error = delete_error
        self.write_calls: list[str] = []  # payload type of each write_data
        self.delete_called = False

    def write_data(self, data):
        # data.py calls write_data first with [instrument], THEN with bars.
        self.write_calls.append(type(data[0]).__name__ if data else "empty")

    def delete_data_range(self, **kw):
        self.delete_called = True
        if self.delete_error is not None:
            raise self.delete_error

    @property
    def bar_writes(self) -> int:
        return self.write_calls.count("Bar")


def _seed_bybit_cache(data_mod, tmp_path, monkeypatch):
    """5-bar tiny bybit parquet cache — enough to reach the delete line.
    Assigned via monkeypatch (a direct assignment would leak the module global)."""
    monkeypatch.setattr(data_mod, "BYBIT_CACHE_DIR", tmp_path / "bybit")
    monkeypatch.setattr(data_mod, "NAUTILUS_CATALOG_DIR", tmp_path / "cat")
    data_mod.BYBIT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    base = datetime(2024, 1, 10, tzinfo=UTC)
    idx = pd.to_datetime(
        [int((base + timedelta(minutes=i)).timestamp() * 1000) for i in range(5)],
        unit="ms",
        utc=True,
    )
    df = pd.DataFrame(
        {c: [1.0] * 5 for c in ("open", "high", "low", "close", "volume")},
        index=idx,
    )
    df.to_parquet(data_mod._bybit_cache_path("linear", "BTCUSDT", "1"))
    return df


class TestDeleteRangeFailureSkipsWrite:
    def test_hard_delete_error_raises_and_skips_write(self, tmp_path, monkeypatch):
        import data

        _seed_bybit_cache(data, tmp_path, monkeypatch)
        fake = _FakeCatalog(delete_error=PermissionError("denied"))
        monkeypatch.setattr(data, "get_nautilus_catalog", lambda: fake)

        with pytest.raises(RuntimeError, match="delete_data_range"):
            data.write_to_nautilus_catalog(
                "bybit", symbol="BTCUSDT", category="linear", interval="1"
            )

        assert fake.delete_called, "delete branch not reached — fixture insufficient"
        # M1030 core: after a failed delete, bars must NOT be written.
        assert fake.bar_writes == 0, (
            f"bars written while delete failed ({fake.bar_writes}) — "
            "overlapping parquet ranges remain, catalog read crashes"
        )

    def test_benign_no_data_error_proceeds_to_write(self, tmp_path, monkeypatch):
        import data

        _seed_bybit_cache(data, tmp_path, monkeypatch)
        fake = _FakeCatalog(delete_error=RuntimeError("no data for identifier"))
        monkeypatch.setattr(data, "get_nautilus_catalog", lambda: fake)

        out = data.write_to_nautilus_catalog(
            "bybit", symbol="BTCUSDT", category="linear", interval="1"
        )

        assert fake.delete_called
        assert fake.bar_writes == 1, "a benign delete must not block the write"
        assert out["rows_written"] == 5

    def test_clean_delete_proceeds_to_write(self, tmp_path, monkeypatch):
        import data

        _seed_bybit_cache(data, tmp_path, monkeypatch)
        fake = _FakeCatalog(delete_error=None)
        monkeypatch.setattr(data, "get_nautilus_catalog", lambda: fake)

        out = data.write_to_nautilus_catalog(
            "bybit", symbol="BTCUSDT", category="linear", interval="1"
        )
        assert fake.bar_writes == 1
        assert out["rows_written"] == 5

    def test_auto_write_hard_delete_error_returns_without_write(
        self, tmp_path, monkeypatch
    ):
        # _auto_write_bybit_catalog: same M1030 branch but a silent RETURN, not RAISE.
        import data

        df = _seed_bybit_cache(data, tmp_path, monkeypatch)
        fake = _FakeCatalog(delete_error=PermissionError("denied"))
        monkeypatch.setattr(data, "get_nautilus_catalog", lambda: fake)

        assert (
            data._auto_write_bybit_catalog("BTCUSDT", "1", df, category="linear")
            is None
        )
        assert fake.delete_called
        assert fake.bar_writes == 0, "_auto_write must not write bars while delete failed"


# ===========================================================================
# #6 (critical) — composer long→short FLIP path
# allow_short was never True in any test → the reversal branch (on_bar:
# _cancel_working + close_all_positions(tags=['flip']) then the opposite side)
# was never driven.
# ===========================================================================
class TestComposerFlipPath:
    def test_long_to_short_reversal_is_observable(self):
        from composer import ComposedStrategySpec, SignalBlock
        from tests.test_trade_reasons import _run

        spec = ComposedStrategySpec(
            id="flip1",
            name="Flip E2E",
            description="",
            blocks=[
                # Golden cross → long
                SignalBlock(
                    type="ma_cross",
                    role="entry",
                    params={"fast": 5, "slow": 20, "direction": "up"},
                ),
                # Death cross → short (triggers a flip while position is long)
                SignalBlock(
                    type="ma_cross",
                    role="entry",
                    params={"fast": 5, "slow": 20, "direction": "down"},
                ),
            ],
            trade_size=0.1,
            allow_short=True,  # the sole switch for the reversal path
        )
        r = _run(spec)  # _bars() sine produces multiple golden/death crosses
        assert r.error is None, r.error
        trades = r.trades or []
        assert trades, "no trades — the flip path wasn't driven"

        sides = [t["side"] for t in trades]
        kinds = [t["exit_kind"] for t in trades]

        # (1) The opposite side ACTUALLY opened: at least one net-short (SELL). With
        # allow_short off (all existing tests) this NEVER happens.
        assert any(s == "SELL" for s in sides), f"no short position: {sides}"

        # (2) 'flip' exit-attribution: close_all_positions(tags=['flip']) is only
        # called in the reversal branch → exit_kind=='flip' is a definitive trace
        # of the flip path.
        assert any(k == "flip" for k in kinds), f"no flip attribution: {kinds}"

        # (3) A direction REVERSAL happened: consecutive opposite-side positions
        # (the M17 guard didn't leave the position FLAT — a new entry could fill).
        assert any(a != b for a, b in zip(sides, sides[1:])), (
            f"no side reversal: {sides}"
        )
