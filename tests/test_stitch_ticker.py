"""stitch_ticker.check_seams — dikişler sahiplik dizisinden çıkarılır.

Kataloğa dokunmaz: `load_daily_closes` sahte serilerle değiştirilir. Asıl
korunan davranış, bir parçanın aralığı diğerini KAPSADIĞINDA iki ucun da
ölçülmesi — QQQ (2003→2026, ortası delik) + QQQQ (2004→2011) deseni.
"""

from __future__ import annotations

import pandas as pd
import pytest

import stitch_ticker as st


def _series(pairs: list[tuple[str, float]]) -> pd.Series:
    return pd.Series(
        [v for _, v in pairs],
        index=pd.to_datetime([d for d, _ in pairs], utc=True),
    )


# QQQ deseni: 2 gün başta, sonra 6,5 yıl atlayıp devam. Aradaki günler QQQQ'da.
_QQQ = _series(
    [
        ("2004-11-29", 39.0),
        ("2004-11-30", 39.14),
        ("2011-03-23", 55.71),
        ("2011-03-24", 56.71),
    ]
)
_QQQQ = _series([("2004-12-01", 39.94), ("2004-12-02", 40.07), ("2011-03-22", 55.41)])


@pytest.fixture
def catalog(monkeypatch):
    data = {"QQQ": _QQQ, "QQQQ": _QQQQ}
    monkeypatch.setattr(
        st, "load_daily_closes", lambda sym, venue=st.DEFAULT_VENUE: data[sym]
    )
    return data


def test_enveloping_part_still_gets_both_seams_measured(catalog):
    seams = st.check_seams(["QQQ", "QQQQ"], "NASDAQ", max_ratio=100)

    assert [(s["from"], s["to"], s["at"]) for s in seams] == [
        ("QQQ", "QQQQ", "2004-12-01"),
        ("QQQQ", "QQQ", "2011-03-23"),
    ]
    assert round(seams[0]["move"] * 100, 2) == 2.04  # 39.14 -> 39.94
    assert round(seams[1]["move"] * 100, 2) == 0.54  # 55.41 -> 55.71


def test_ownership_follows_write_order_first_part_wins(catalog):
    rows = st.ownership(["QQQ", "QQQQ"], "NASDAQ")

    assert [str(ts.date()) for ts, _, _ in rows] == [
        "2004-11-29",
        "2004-11-30",
        "2004-12-01",
        "2004-12-02",
        "2011-03-22",
        "2011-03-23",
        "2011-03-24",
    ]
    assert [p for _, _, p in rows] == [
        "QQQ",
        "QQQ",
        "QQQQ",
        "QQQQ",
        "QQQQ",
        "QQQ",
        "QQQ",
    ]


def test_a_jumping_seam_is_refused(monkeypatch):
    """Parçalardan biri farklı ayarlanmışsa sessiz yanlış seri değil, hata."""
    unadjusted = _series([("2004-12-01", 79.88), ("2011-03-22", 110.82)])  # 2× seviye
    data = {"QQQ": _QQQ, "QQQQ": unadjusted}
    monkeypatch.setattr(
        st, "load_daily_closes", lambda sym, venue=st.DEFAULT_VENUE: data[sym]
    )

    with pytest.raises(st.StitchError, match="dikiş fazla sıçruyor"):
        st.check_seams(["QQQ", "QQQQ"], "NASDAQ", max_ratio=6.0)


def test_shadowed_days_are_reported_not_silently_dropped(monkeypatch, capsys):
    overlapping = _series(
        [("2004-11-30", 39.10), ("2004-12-01", 39.94)]
    )  # 11-30 QQQ'da da var
    data = {"QQQ": _QQQ, "QQQQ": overlapping}
    monkeypatch.setattr(
        st, "load_daily_closes", lambda sym, venue=st.DEFAULT_VENUE: data[sym]
    )

    st.check_seams(["QQQ", "QQQQ"], "NASDAQ", max_ratio=100)

    out = capsys.readouterr().out
    assert "QQQQ: 1 gün başka parçada da var" in out
