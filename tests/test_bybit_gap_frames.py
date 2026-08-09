"""data._bybit_gap_frames (DeepR 2026-08-09 [ORTA]).

Zero test coverage before this -- existing tests only cover expanding the
cache at the start/end; the mid-cache-hole path (M2: targeted-fill a gap
INSIDE the cached range, skip a gap already marked "known empty" so every
call doesn't re-hammer the API for the same dead range) was never
exercised at all.

Wiki References
---------------
See: [[nau_deepr_ucuncu_tur_2026_08_09]]
"""

from __future__ import annotations

import json

import pandas as pd

import data

_STEP_MS = 60_000  # 1-minute bars


def _bars(
    minutes: list[int], *, base=pd.Timestamp("2024-01-01", tz="UTC")
) -> pd.DataFrame:
    """One bar per listed minute offset -- a gap is just a skipped offset.

    tz explicit even when `minutes` is empty: an empty
    ``pd.DatetimeIndex([])`` with no elements to infer a dtype from is
    naive, and _bybit_gap_frames compares it against tz-aware Timestamps
    (`f.index >= lo`) -- a naive-vs-aware comparison raises TypeError.
    """
    idx = pd.DatetimeIndex(
        [base + pd.Timedelta(minutes=m) for m in minutes], tz=base.tz
    )
    return pd.DataFrame({"close": [float(m) for m in minutes]}, index=idx)


def _ms(minute: int, *, base=pd.Timestamp("2024-01-01", tz="UTC")) -> int:
    return int((base + pd.Timedelta(minutes=minute)).value // 1_000_000)


def test_no_gap_returns_empty_without_calling_fetch(tmp_path):
    cached = _bars(list(range(10)))  # 0..9, fully contiguous
    calls = []

    frames = data._bybit_gap_frames(
        cached,
        tmp_path / "x.parquet",
        _STEP_MS,
        lambda a, b: calls.append((a, b)) or [],
    )

    assert frames == []
    assert calls == []


def test_a_single_mid_cache_hole_is_filled(tmp_path):
    cached = _bars([0, 1, 2, 3, 4, 6, 7, 8, 9])  # minute 5 missing
    calls = []

    def _fetch(start_ms, end_ms):
        calls.append((start_ms, end_ms))
        return [_bars([5])]

    frames = data._bybit_gap_frames(cached, tmp_path / "x.parquet", _STEP_MS, _fetch)

    assert len(calls) == 1
    assert calls[0] == (_ms(5), _ms(5) + 1)  # fetch_segment gets [gs, ge+1)
    assert len(frames) == 1
    assert frames[0].index[0] == cached.index[0] + pd.Timedelta(minutes=5)


def test_a_hole_that_fetch_returns_nothing_for_is_marked_known_empty(tmp_path):
    cached = _bars([0, 1, 2, 3, 4, 6, 7, 8, 9])
    cache_path = tmp_path / "x.parquet"
    gaps_path = tmp_path / "x_gaps.json"

    frames = data._bybit_gap_frames(
        cached,
        cache_path,
        _STEP_MS,
        lambda a, b: [_bars([])],  # empty result
    )

    assert frames == []
    assert gaps_path.exists()
    known = json.loads(gaps_path.read_text())
    assert known == [[_ms(5), _ms(5)]]


def test_a_known_empty_hole_is_not_refetched(tmp_path):
    cached = _bars([0, 1, 2, 3, 4, 6, 7, 8, 9])
    cache_path = tmp_path / "x.parquet"
    gaps_path = tmp_path / "x_gaps.json"
    gaps_path.write_text(json.dumps([[_ms(5), _ms(5)]]))
    calls = []

    frames = data._bybit_gap_frames(
        cached, cache_path, _STEP_MS, lambda a, b: calls.append((a, b)) or []
    )

    assert frames == []
    assert calls == [], "a gap already marked known-empty must not hit the API again"


def test_a_hole_covered_by_a_wider_known_empty_range_is_skipped(tmp_path):
    """known ranges are [start, end] bounds, not exact matches -- a smaller
    gap fully inside an already-recorded empty range must also be skipped."""
    cached = _bars([0, 1, 2, 3, 4, 6, 7, 8, 9])
    cache_path = tmp_path / "x.parquet"
    gaps_path = tmp_path / "x_gaps.json"
    gaps_path.write_text(json.dumps([[_ms(4), _ms(6)]]))  # wider than the gap
    calls = []

    data._bybit_gap_frames(
        cached, cache_path, _STEP_MS, lambda a, b: calls.append((a, b)) or []
    )

    assert calls == []


def test_two_independent_holes_one_fills_one_stays_empty(tmp_path):
    # minutes 3 and 7 missing -> two separate one-bar gaps.
    cached = _bars([0, 1, 2, 4, 5, 6, 8, 9])
    cache_path = tmp_path / "x.parquet"
    gaps_path = tmp_path / "x_gaps.json"

    def _fetch(start_ms, end_ms):
        if start_ms == _ms(3):
            return [_bars([3])]
        return [_bars([])]  # the minute-7 gap returns nothing

    frames = data._bybit_gap_frames(cached, cache_path, _STEP_MS, _fetch)

    assert len(frames) == 1
    assert frames[0].index[0] == cached.index[0] + pd.Timedelta(minutes=3)
    known = json.loads(gaps_path.read_text())
    assert known == [[_ms(7), _ms(7)]]


def test_a_corrupt_gaps_sidecar_is_treated_as_no_known_gaps(tmp_path):
    """_atomic_write_json's own failure-cleanup is covered elsewhere; this
    is about the READ side -- a malformed sidecar must not crash the
    caller, just re-probe every gap as if none were known yet."""
    cached = _bars([0, 1, 2, 3, 4, 6, 7, 8, 9])
    cache_path = tmp_path / "x.parquet"
    gaps_path = tmp_path / "x_gaps.json"
    gaps_path.write_text("{not valid json")
    calls = []

    frames = data._bybit_gap_frames(
        cached, cache_path, _STEP_MS, lambda a, b: calls.append((a, b)) or [_bars([5])]
    )

    assert len(calls) == 1
    assert len(frames) == 1
