"""Reports görünüm durumu kalıcılığı (sort + kolon filtreleri + varyant).

Kullanıcı isteği: sıralama/filtre durumu kaydedilsin, sonraki açılışta aynen
gelsin. Durum reports_layout.json'da saklanır; bu testler yaz/oku
round-trip'ini ve bozuk girdilere dayanıklılığı doğrular.
"""

from __future__ import annotations

import json


def _patch_path(monkeypatch, tmp_path):
    import web.routes.reports as rp

    p = tmp_path / "reports_layout.json"
    monkeypatch.setattr(rp, "REPORTS_LAYOUT", p)
    return rp, p


class TestViewStateRoundtrip:
    def test_full_state_roundtrip(self, tmp_path, monkeypatch):
        rp, _ = _patch_path(monkeypatch, tmp_path)
        rp._save_layout(
            {
                "order": ["ts", "pnl"],
                "hidden": ["commission"],
                "sort": {"key": "sharpe", "asc": False},
                "filters": {"symbol": "btc", "pnl": ">0", "trades": "10..500"},
                "variant": "profitable",
            }
        )
        out = rp._load_layout()
        assert out["order"] == ["ts", "pnl"]
        assert out["hidden"] == ["commission"]
        assert out["sort"] == {"key": "sharpe", "asc": False}
        assert out["filters"] == {"symbol": "btc", "pnl": ">0", "trades": "10..500"}
        assert out["variant"] == "profitable"

    def test_legacy_file_without_new_fields(self, tmp_path, monkeypatch):
        """Eski format (yalnız order/hidden) hâlâ okunur; yeni alanlar yok."""
        rp, p = _patch_path(monkeypatch, tmp_path)
        p.write_text(json.dumps({"order": ["ts"], "hidden": []}))
        out = rp._load_layout()
        assert out["order"] == ["ts"]
        assert "sort" not in out and "filters" not in out and "variant" not in out

    def test_junk_fields_dropped(self, tmp_path, monkeypatch):
        """Yanlış tipli sort/filters/variant sessizce atılır, sayfa kırılmaz."""
        rp, p = _patch_path(monkeypatch, tmp_path)
        p.write_text(
            json.dumps(
                {
                    "order": [],
                    "hidden": [],
                    "sort": "sharpe-desc",  # dict değil
                    "filters": [1, 2, 3],  # dict değil
                    "variant": 42,  # str değil
                }
            )
        )
        out = rp._load_layout()
        assert "sort" not in out and "filters" not in out and "variant" not in out

    def test_empty_filter_values_dropped(self, tmp_path, monkeypatch):
        rp, _ = _patch_path(monkeypatch, tmp_path)
        rp._save_layout(
            {"order": [], "hidden": [], "filters": {"symbol": "  ", "pnl": ">0"}}
        )
        out = rp._load_layout()
        assert out["filters"] == {"pnl": ">0"}

    def test_sort_without_key_dropped(self, tmp_path, monkeypatch):
        rp, _ = _patch_path(monkeypatch, tmp_path)
        rp._save_layout({"order": [], "hidden": [], "sort": {"asc": True}})
        out = rp._load_layout()
        assert "sort" not in out

    def test_page_size_roundtrip_and_validation(self, tmp_path, monkeypatch):
        rp, _ = _patch_path(monkeypatch, tmp_path)
        rp._save_layout({"order": [], "hidden": [], "pageSize": 500})
        assert rp._load_layout()["pageSize"] == 500
        # 0 = Tümü geçerli; negatif / bool / str atılır
        rp._save_layout({"order": [], "hidden": [], "pageSize": 0})
        assert rp._load_layout()["pageSize"] == 0
        rp._save_layout({"order": [], "hidden": [], "pageSize": -5})
        assert "pageSize" not in rp._load_layout()
        rp._save_layout({"order": [], "hidden": [], "pageSize": True})
        assert "pageSize" not in rp._load_layout()
