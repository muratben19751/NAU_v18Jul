"""Log-inceleme planının P2/P3 düzeltme testleri.

P2: robustness_log kimlik alanları + reports bileşik-anahtar join.
P3: JSONL log rotation (20MB eşik).
"""

from __future__ import annotations

import json


class TestRobustnessLogIdentity:
    def test_writer_persists_identity_fields(self, tmp_path, monkeypatch):
        import web.routes.robustness as rb

        log = tmp_path / "robustness_log.jsonl"
        monkeypatch.setattr(rb, "ROBUSTNESS_LOG", log)
        rb._log_robustness(
            "spec1",
            "Test Spec",
            {"wfo_windows": [], "wfo_summary": {}, "mc": {}, "split": {}},
            symbol="ETHUSDT",
            category="linear",
            interval="60",
        )
        rec = json.loads(log.read_text().strip())
        assert rec["symbol"] == "ETHUSDT"
        assert rec["category"] == "linear"
        assert rec["interval"] == "60"

    def test_writer_falls_back_to_result_fields(self, tmp_path, monkeypatch):
        import web.routes.robustness as rb

        log = tmp_path / "robustness_log.jsonl"
        monkeypatch.setattr(rb, "ROBUSTNESS_LOG", log)
        rb._log_robustness(
            "spec1",
            "Test Spec",
            {"symbol": "SOLUSDT", "category": "spot", "interval": "240"},
        )
        rec = json.loads(log.read_text().strip())
        assert rec["symbol"] == "SOLUSDT" and rec["interval"] == "240"

    def test_reader_composite_key_no_overwrite(self, tmp_path, monkeypatch):
        """Aynı spec_id iki farklı sembol/TF → iki ayrı indeks girişi."""
        import web.routes.reports as rp

        log = tmp_path / "robustness_log.jsonl"
        recs = [
            {
                "spec_id": "s1",
                "spec_name": "N",
                "symbol": "BTCUSDT",
                "interval": "60",
                "in_out_split": {"overfitting_label": "BTC"},
            },
            {
                "spec_id": "s1",
                "spec_name": "N",
                "symbol": "ETHUSDT",
                "interval": "D",
                "in_out_split": {"overfitting_label": "ETH"},
            },
        ]
        log.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
        monkeypatch.setattr(rp, "ROBUSTNESS_LOG", log)
        idx = rp._load_robustness_index()
        btc = idx[("s1", "BTCUSDT", "60")]
        eth = idx[("s1", "ETHUSDT", "D")]
        assert btc["in_out_split"]["overfitting_label"] == "BTC"
        assert eth["in_out_split"]["overfitting_label"] == "ETH"
        # Legacy tekli anahtar hâlâ var (son-yazan-kazanır fallback)
        assert idx["s1"]["symbol"] == "ETHUSDT"

    def test_reader_legacy_records_still_join(self, tmp_path, monkeypatch):
        """Kimlik alansız eski kayıt tekli anahtarla erişilebilir kalır."""
        import web.routes.reports as rp

        log = tmp_path / "robustness_log.jsonl"
        log.write_text(json.dumps({"spec_id": "old1", "spec_name": "Eski"}) + "\n")
        monkeypatch.setattr(rp, "ROBUSTNESS_LOG", log)
        idx = rp._load_robustness_index()
        assert idx["old1"]["spec_name"] == "Eski"
        assert ("old1", "", "") not in idx  # boş kimlikle bileşik anahtar üretilmez


class TestLogRotation:
    def test_rotates_when_over_threshold(self, tmp_path):
        from web.routes.backtest import _rotate_if_large

        log = tmp_path / "x.jsonl"
        log.write_text("a" * 1000)
        _rotate_if_large(log, max_bytes=500)
        assert not log.exists()
        archive = tmp_path / "x.jsonl.1"
        assert archive.exists() and archive.stat().st_size == 1000

    def test_no_rotation_under_threshold(self, tmp_path):
        from web.routes.backtest import _rotate_if_large

        log = tmp_path / "x.jsonl"
        log.write_text("a" * 100)
        _rotate_if_large(log, max_bytes=500)
        assert log.exists() and not (tmp_path / "x.jsonl.1").exists()

    def test_second_rotation_replaces_archive(self, tmp_path):
        from web.routes.backtest import _rotate_if_large

        log = tmp_path / "x.jsonl"
        archive = tmp_path / "x.jsonl.1"
        archive.write_text("eski")
        log.write_text("b" * 1000)
        _rotate_if_large(log, max_bytes=500)
        assert archive.read_text() == "b" * 1000

    def test_writer_applies_rotation(self, tmp_path, monkeypatch):
        """_log_backtest eşik aşımında dosyayı devirip temiz başlar."""
        import web.routes.backtest as bt

        log = tmp_path / "backtest_log.jsonl"
        log.write_text("x" * 2000)
        monkeypatch.setattr(bt, "BACKTEST_LOG", log)
        monkeypatch.setattr(bt, "_LOG_ROTATE_BYTES", 1000)

        class _Spec:
            id = "s"
            name = "n"
            blocks = []
            entry_logic = exit_logic = "OR"
            order_type = "market"
            trade_size = 0.1
            trade_size_mode = "fixed"
            use_bracket = False
            sl_type = "percent"
            sl_value = 2.0
            tp_type = "off"
            tp_value = 4.0
            allow_short = False
            emulate = False

        class _Res:
            rationale = ""
            error = None
            metrics = {}
            equity_curve = []

        bt._log_backtest(_Spec(), _Res(), "Bybit", {})
        assert (tmp_path / "backtest_log.jsonl.1").exists()
        lines = log.read_text().strip().splitlines()
        assert len(lines) == 1  # yeni dosya tek taze kayıtla başladı
