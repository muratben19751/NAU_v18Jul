"""403'ün iki anlamı: kapalı abonelik (ölümcül) ve throttle (yeniden denenir).

2026-08-06 ölçümü: 16 işçiyle 35 MB/s'de 5.000. dosyada 403 alındı, aynı key
hemen ardından 26/26 indi. Bu testler o ayrımı kilitliyor — ön-yoklama kapalı
dataset'i ilk saniyede yakalamalı, koşum ortasındaki tek 403 ise işi
öldürmemeli.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import download_flatfiles as dl


def _s3_error(code: int) -> Exception:
    e = Exception(f"HTTP {code}")
    e.response = {"ResponseMetadata": {"HTTPStatusCode": code}}  # type: ignore[attr-defined]
    return e


class _Client:
    """`fail_times` kez 403 atıp sonra dosyayı yazan sahte istemci."""

    def __init__(self, fail_times: int = 0, code: int = 403) -> None:
        self.fail_times = fail_times
        self.code = code
        self.calls = 0
        self.preflight_code: int | None = None

    def download_file(self, bucket: str, key: str, path: str) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _s3_error(self.code)
        Path(path).write_bytes(b"veri")

    def get_object(self, **kw):  # ön-yoklama
        if self.preflight_code:
            raise _s3_error(self.preflight_code)
        return {"Body": b""}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(dl.time, "sleep", lambda _s: None)


def test_transient_403_is_retried_then_succeeds(tmp_path):
    client = _Client(fail_times=2)

    n = dl._fetch_one(client, "flatfiles", "ds/2023/09/2023-09-01.csv.gz", 4, tmp_path)

    assert n == 4
    assert client.calls == 3  # iki 403, üçüncüde indi
    assert (tmp_path / "ds/2023/09/2023-09-01.csv.gz").read_bytes() == b"veri"


def test_persistent_403_is_fatal_and_leaves_no_part(tmp_path):
    client = _Client(fail_times=99)

    with pytest.raises(dl.FlatFileError, match="denemede de indirilemedi"):
        dl._fetch_one(client, "flatfiles", "ds/2023/09/x.csv.gz", 4, tmp_path)

    assert client.calls == dl._AUTH_RETRIES
    assert list((tmp_path / "ds/2023/09").iterdir()) == []


def test_non_auth_error_is_not_retried(tmp_path):
    """500 boto3'ün kendi retry'ına ait; burada tekrar sarmalanmaz."""
    client = _Client(fail_times=99, code=500)

    with pytest.raises(Exception, match="HTTP 500"):
        dl._fetch_one(client, "flatfiles", "ds/2023/09/x.csv.gz", 4, tmp_path)

    assert client.calls == 1


def test_preflight_stops_a_closed_dataset_before_downloading(tmp_path):
    client = _Client()
    client.preflight_code = 403

    with pytest.raises(dl.FlatFileError, match="abonelikte indirilemiyor"):
        dl.mirror(client, [("ds/2014/06/x.csv.gz", 4)], tmp_path, workers=2)

    assert client.calls == 0  # tek dosya bile denenmedi
