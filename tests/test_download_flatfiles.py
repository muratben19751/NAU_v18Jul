"""download_flatfiles.py — sahte S3 istemcisiyle ayna sözleşmesi.

Ağa çıkmaz. İddialar davranışsal: yerel yol S3 key'inin aynısı mı, yarım
dosya asıl adıyla kalıyor mu, tam inmiş dosya yeniden iniyor mu, ve
abonelik dışı dataset (403) yeniden denenen bir ağ hatası gibi mi davranıyor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import download_flatfiles as dff


class _FakePaginator:
    def __init__(self, objects: list[tuple[str, int]]) -> None:
        self._objects = objects

    def paginate(self, **kw):
        prefix = kw.get("Prefix", "")
        yield {
            "Contents": [
                {"Key": k, "Size": s} for k, s in self._objects if k.startswith(prefix)
            ]
        }


class FakeS3:
    """download_file yalnız `payload` yazar; `fail_with` verilirse onu fırlatır."""

    def __init__(
        self, objects: list[tuple[str, int]], *, fail_with: Exception | None = None
    ):
        self._objects = objects
        self._fail = fail_with
        self.calls: list[str] = []

    def get_paginator(self, _name: str) -> _FakePaginator:
        return _FakePaginator(self._objects)

    def download_file(self, bucket: str, key: str, path: str) -> None:
        self.calls.append(key)
        if self._fail:
            raise self._fail
        size = dict(self._objects)[key]
        Path(path).write_bytes(b"x" * size)


_OBJECTS = [
    ("us_indices/minute_aggs_v1/2023/02/2023-02-14.csv.gz", 10),
    ("us_indices/minute_aggs_v1/2024/01/2024-01-02.csv.gz", 20),
    ("us_indices/minute_aggs_v1/2026/07/2026-07-31.csv.gz", 30),
]


def test_local_path_mirrors_the_s3_key(tmp_path: Path):
    client = FakeS3(_OBJECTS)
    stats = dff.mirror(client, _OBJECTS, tmp_path, workers=2)

    assert stats["downloaded"] == 3
    for key, size in _OBJECTS:
        target = tmp_path / key
        assert target.exists(), key
        assert target.stat().st_size == size
    # Yarım dosya artığı kalmaz.
    assert not list(tmp_path.rglob("*.part"))


def test_complete_file_is_skipped_not_refetched(tmp_path: Path):
    key, size = _OBJECTS[0]
    (tmp_path / key).parent.mkdir(parents=True)
    (tmp_path / key).write_bytes(b"y" * size)

    client = FakeS3(_OBJECTS)
    stats = dff.mirror(client, _OBJECTS, tmp_path, workers=2)

    assert (stats["skipped"], stats["downloaded"]) == (1, 2)
    assert key not in client.calls  # hiç istenmedi
    assert (tmp_path / key).read_bytes() == b"y" * size  # üzerine yazılmadı


def test_truncated_file_is_refetched(tmp_path: Path):
    """Boyut tutmuyorsa yarım kalmıştır — atlanmaz, yeniden iner."""
    key, size = _OBJECTS[0]
    (tmp_path / key).parent.mkdir(parents=True)
    (tmp_path / key).write_bytes(b"y" * (size - 3))

    client = FakeS3(_OBJECTS)
    stats = dff.mirror(client, _OBJECTS, tmp_path, workers=1)

    assert stats["downloaded"] == 3
    assert (tmp_path / key).read_bytes() == b"x" * size


def test_403_is_a_configuration_error_and_leaves_no_partial(tmp_path: Path):
    err = Exception("forbidden")
    err.response = {"ResponseMetadata": {"HTTPStatusCode": 403}}  # type: ignore[attr-defined]
    client = FakeS3(_OBJECTS, fail_with=err)

    with pytest.raises(dff.FlatFileError, match="403"):
        dff.mirror(client, _OBJECTS, tmp_path, workers=1)
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("*.csv.gz"))


def test_year_filter_selects_by_path_segment(tmp_path: Path):
    client = FakeS3(_OBJECTS)
    got = dff.list_dataset(client, "us_indices/minute_aggs_v1", years=range(2024, 2027))
    assert [k for k, _ in got] == [k for k, _ in _OBJECTS[1:]]


def test_missing_credentials_is_an_actionable_error(monkeypatch):
    monkeypatch.delenv("MASSIVE_S3_KEY", raising=False)
    monkeypatch.delenv("MASSIVE_S3_SECRET", raising=False)
    with pytest.raises(dff.FlatFileError, match="MASSIVE_S3_KEY"):
        dff.make_client()
