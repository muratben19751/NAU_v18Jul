"""download_flatfiles.list_dataset — yıl ve gün süzgeçleri.

Ağa/S3'e çıkmaz: sahte bir paginator ile yalnız süzgeç mantığı sınanır. Gün
süzgeci "son 24 ay" gibi yuvarlak pencerelerin takvim yılına yuvarlanmaması
için var; iddialar da bunu koruyor.
"""

from __future__ import annotations

import pytest

import download_flatfiles as dl

_KEYS = [
    "us_stocks_sip/trades_v1/2024/07/2024-07-31.csv.gz",
    "us_stocks_sip/trades_v1/2024/08/2024-08-05.csv.gz",
    "us_stocks_sip/trades_v1/2025/01/2025-01-02.csv.gz",
    "us_stocks_sip/trades_v1/2026/08/2026-08-04.csv.gz",
    "us_stocks_sip/trades_v1/2026/08/BOZUK.csv.gz",  # gün okunamaz
]


class _FakeClient:
    def get_paginator(self, _op: str):
        class _P:
            def paginate(self, **_kw):
                return [{"Contents": [{"Key": k, "Size": 10} for k in _KEYS]}]

        return _P()


@pytest.fixture
def client() -> _FakeClient:
    return _FakeClient()


def _keys(objs) -> list[str]:
    return [k for k, _ in objs]


def test_no_filter_returns_everything_sorted(client):
    assert _keys(dl.list_dataset(client, "us_stocks_sip/trades_v1")) == sorted(_KEYS)


def test_day_window_does_not_round_to_calendar_years(client):
    """2024-08-06'dan itibaren istenince 2024-07-31 gelmemeli."""
    got = _keys(
        dl.list_dataset(
            client, "us_stocks_sip/trades_v1", start="2024-08-01", end="2026-08-05"
        )
    )
    assert got == [
        "us_stocks_sip/trades_v1/2024/08/2024-08-05.csv.gz",
        "us_stocks_sip/trades_v1/2025/01/2025-01-02.csv.gz",
        "us_stocks_sip/trades_v1/2026/08/2026-08-04.csv.gz",
    ]  # BOZUK.csv.gz elendi: günü okunamayan key pencereye alınmaz


def test_year_filter_still_works_and_composes_with_days(client):
    years = dl._parse_years("2024-2025")
    assert _keys(dl.list_dataset(client, "x", years=years)) == [
        "us_stocks_sip/trades_v1/2024/07/2024-07-31.csv.gz",
        "us_stocks_sip/trades_v1/2024/08/2024-08-05.csv.gz",
        "us_stocks_sip/trades_v1/2025/01/2025-01-02.csv.gz",
    ]
    both = _keys(dl.list_dataset(client, "x", years=years, start="2025-01-01"))
    assert both == ["us_stocks_sip/trades_v1/2025/01/2025-01-02.csv.gz"]


def test_key_day_reads_only_well_formed_dates():
    assert dl._key_day("a/b/2026-08-04.csv.gz") == "2026-08-04"
    assert dl._key_day("a/b/BOZUK.csv.gz") == ""
    assert dl._key_day("a/b/20260804.csv.gz") == ""
