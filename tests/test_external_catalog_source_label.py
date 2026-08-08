"""`_external_catalog_source_label()` (DeepR 2026-08-08 [DÜŞÜK]).

The /data page's external-catalog panel title was hardcoded "(NAU_ev)"
regardless of which root actually supplied the data — when the configured
NAU_ev desk root was missing (the common case: it's a path on another
machine) but this project's own EQUITY_CATALOG_DIR (ingest_equities.py
output) existed, the panel silently served local data under the NAU_ev
label. The user had no way to tell "NAU_ev" from "my own ingest" apart.

Wiki References
---------------
See: [[nau_deepr_toplu_sertlestirme_2026_08]]
"""

from __future__ import annotations

import pytest

import data


@pytest.fixture()
def isolated_roots(tmp_path, monkeypatch):
    """Two disjoint catalog roots — a stand-in NAU_ev root and a stand-in
    local-ingest root (EQUITY_CATALOG_DIR) — neither populated by default."""
    nau_ev_root = tmp_path / "nau_ev_desk"
    local_root = tmp_path / "equity_catalog"
    monkeypatch.setattr(data, "EXTERNAL_CATALOGS", [nau_ev_root, local_root])
    monkeypatch.setattr(data, "EQUITY_CATALOG_DIR", local_root)
    return nau_ev_root, local_root


def _populate(root):
    (root / "data" / "bar").mkdir(parents=True)


class TestSourceLabel:
    def test_neither_root_present_is_not_found(self, isolated_roots):
        assert data._external_catalog_source_label() == "not found"

    def test_only_nau_ev_root_present(self, isolated_roots):
        nau_ev_root, _local_root = isolated_roots
        _populate(nau_ev_root)

        assert data._external_catalog_source_label() == "NAU_ev"

    def test_only_local_ingest_present_is_labelled_local_not_nau_ev(
        self, isolated_roots
    ):
        """The exact regression: NAU_ev missing, local ingest present — the
        panel must NOT claim this is NAU_ev data."""
        _nau_ev_root, local_root = isolated_roots
        _populate(local_root)

        assert data._external_catalog_source_label() == "local ingest"

    def test_both_roots_present(self, isolated_roots):
        nau_ev_root, local_root = isolated_roots
        _populate(nau_ev_root)
        _populate(local_root)

        assert data._external_catalog_source_label() == "NAU_ev + local ingest"


class TestListCatalogThreadsTheLabel:
    def test_list_catalog_includes_the_computed_label(
        self, isolated_roots, monkeypatch
    ):
        nau_ev_root, _local_root = isolated_roots
        _populate(nau_ev_root)
        monkeypatch.setattr(data, "_bybit_rows", lambda: [])
        monkeypatch.setattr(data, "_index_rows", lambda **k: [])

        catalog = data.list_catalog()

        assert catalog["external_source_label"] == "NAU_ev"
