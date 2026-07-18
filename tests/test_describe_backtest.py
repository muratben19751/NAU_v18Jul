"""/backtest/describe — açıklamadan custom Python blok üret → /backtest/run'a zincirle.

Claude'a ve diske DOKUNMAZ: propose_custom_block / save_custom /
register_custom_from_disk / append_to_catalog mock'lanır. Doğrulanan kablolama:
entry+exit üretimi, blok kaydı, spec derleme, ve zincirin enstrüman
parametrelerini + yeni spec_id'yi taşıması.
"""

from __future__ import annotations

import json
import re
import time

import pytest

_VALID_BLOCK = {
    "name": "x",
    "meta": {
        "label": "T",
        "params": {"period": {"type": "int", "min": 2, "max": 50, "default": 14}},
    },
    "code": (
        "def max_lookback(params):\n"
        "    return 5\n\n"
        "def evaluate(state, block, closes, indicators, portfolio):\n"
        "    return None\n"
    ),
}


@pytest.fixture
def wired(monkeypatch):
    """LLM + disk + katalog yan etkilerini kes; çağrıları kaydet."""
    import agent
    import composer
    import custom_block_store

    calls: list[str] = []
    saved: list[str] = []
    appended: list = []

    def _propose(label, desc, role_hint="entry"):
        calls.append(role_hint)
        return dict(_VALID_BLOCK)

    def _register(name):
        # Gerçek register gibi davran: spec.validate() blok tipini tanısın.
        composer.BLOCK_REGISTRY[name] = {
            "meta": _VALID_BLOCK["meta"],
            "eval": lambda *a, **k: None,
            "max_lookback": lambda p: 5,
            "builtin": False,
        }

    # Varsayılan: breakdown başarısız → worker tek entry+exit yoluna düşer
    # (mevcut testlerin beklediği davranış). Çoklu-blok testi bunu ezer.
    def _no_breakdown(desc):
        raise ValueError("no breakdown in this test")

    monkeypatch.setattr(agent, "propose_condition_breakdown", _no_breakdown)
    monkeypatch.setattr(agent, "propose_custom_block", _propose)
    monkeypatch.setattr(
        custom_block_store, "save_custom", lambda n, m, c, prompt="": saved.append(n)
    )
    monkeypatch.setattr(composer, "register_custom_from_disk", _register)
    monkeypatch.setattr(composer, "append_to_catalog", appended.append)
    return {"calls": calls, "saved": saved, "appended": appended}


def _client():
    from fastapi.testclient import TestClient

    from server import app

    return TestClient(app)


def _poll(c, gen_id, tries=60):
    for _ in range(tries):
        p = c.get(f"/backtest/describe/progress/{gen_id}")
        if "empty-state" in p.text or 'hx-post="/backtest/run"' in p.text:
            return p
        time.sleep(0.1)
    raise AssertionError("üretim bitmedi")


def _poll_chain(c, gen_id, tries=60):
    """Zincir /run VEYA /sweep olabilir (tek/çoklu TF) — ikisini de bekle."""
    for _ in range(tries):
        p = c.get(f"/backtest/describe/progress/{gen_id}")
        if (
            "empty-state" in p.text
            or 'hx-post="/backtest/run"' in p.text
            or 'hx-post="/backtest/sweep"' in p.text
        ):
            return p
        time.sleep(0.1)
    raise AssertionError("üretim bitmedi")


class TestDescribeBacktest:
    def test_short_description_rejected_without_calling_claude(self, wired):
        r = _client().post("/backtest/describe", data={"description": "kısa"})
        assert r.status_code == 400
        assert not wired["calls"], "kısa tarifte LLM çağrılmamalı"

    def test_generates_entry_and_exit_then_chains_to_run(self, wired):
        c = _client()
        r = c.post(
            "/backtest/describe",
            data={
                "description": "RSI 30 altindayken al, ATR 3x stop ile cik",
                "instrument_kind": "Bybit",
                "symbol": "ETHUSDT",
                "category": "linear",
                "interval": "15",
            },
        )
        assert r.status_code == 200
        gen_id = re.search(r"describe/progress/([0-9a-f]+)", r.text).group(1)
        page = _poll(c, gen_id)

        assert wired["calls"] == ["entry", "exit"]
        assert len(wired["saved"]) == 2  # iki blok da diske yazılır
        spec = wired["appended"][0]  # katalog spec'i (tekrar koşulabilir)
        assert [b.role for b in spec.blocks] == ["entry", "exit"]

        # Zincir: YENİ spec + AYNI enstrüman ayarlarıyla /backtest/run
        vals = json.loads(re.search(r"hx-vals='([^']+)'", page.text).group(1))
        assert vals["spec_id"] == spec.id
        assert vals["symbol"] == "ETHUSDT"
        assert vals["interval"] == "15"
        assert vals["instrument_kind"] == "Bybit"

    def test_generation_failure_does_not_chain_a_backtest(self, wired, monkeypatch):
        import agent
        from codegate import GeneratedCodeError

        def _boom(label, desc, role_hint="entry"):
            raise GeneratedCodeError("blok üretilemedi")

        monkeypatch.setattr(agent, "propose_custom_block", _boom)
        c = _client()
        r = c.post(
            "/backtest/describe",
            data={"description": "anlamsiz bir tarif ama yeterince uzun"},
        )
        gen_id = re.search(r"describe/progress/([0-9a-f]+)", r.text).group(1)
        page = _poll(c, gen_id)
        # Hata → backtest ZİNCİRLENMEZ (yanlış strateji sessizce koşmasın).
        assert 'hx-post="/backtest/run"' not in page.text
        assert "üretilemedi" in page.text or "GeneratedCodeError" in page.text


class TestDescribeMultiBlock:
    """propose_condition_breakdown → koşul başına AYRI blok + blok-seviyesi OR/AND."""

    def _breakdown(self, entry_logic="OR", exit_logic="OR"):
        def _bd(desc):
            return {
                "label": "Confluence",
                "entry_logic": entry_logic,
                "exit_logic": exit_logic,
                "conditions": [
                    {"role": "entry", "label": "RSI oversold", "desc": "RSI<30 up"},
                    {"role": "entry", "label": "Volume spike", "desc": "vol>2x avg"},
                    {"role": "exit", "label": "ATR stop", "desc": "3x ATR"},
                ],
                "usage": {},
            }

        return _bd

    def test_breakdown_makes_one_block_per_condition_and_keeps_logic(
        self, wired, monkeypatch
    ):
        import agent

        monkeypatch.setattr(
            agent, "propose_condition_breakdown", self._breakdown(entry_logic="AND")
        )
        c = _client()
        r = c.post(
            "/backtest/describe",
            data={
                "description": "RSI 30 altinda VE hacim 2x iken al; ATR stop cik",
                "symbol": "ETHUSDT",
                "interval": "15",
            },
        )
        gen_id = re.search(r"describe/progress/([0-9a-f]+)", r.text).group(1)
        page = _poll(c, gen_id)

        spec = wired["appended"][0]
        roles = [b.role for b in spec.blocks]
        assert roles.count("entry") == 2 and roles.count("exit") == 1
        # Blok-seviyesi mantık breakdown'dan korunur.
        assert spec.entry_logic == "AND"
        assert spec.exit_logic == "OR"
        # 3 AYRI blok diske yazıldı (tek bloğa gömülmedi).
        assert len(wired["saved"]) == 3
        # Zincir yeni spec'i çalıştırır.
        vals = json.loads(re.search(r"hx-vals='([^']+)'", page.text).group(1))
        assert vals["spec_id"] == spec.id

    def test_block_names_unique_across_roles(self, wired, monkeypatch):
        # "entry"[0]=="exit"[0]=="e" tuzağı: entry#1 ve exit#1 aynı adı ALMAMALI.
        import agent

        monkeypatch.setattr(agent, "propose_condition_breakdown", self._breakdown())
        c = _client()
        r = c.post(
            "/backtest/describe",
            data={"description": "iki entry bir exit tarifi yeterince uzun"},
        )
        gen_id = re.search(r"describe/progress/([0-9a-f]+)", r.text).group(1)
        _poll(c, gen_id)

        names = wired["saved"]
        assert len(names) == len(set(names)), f"blok adları çakıştı: {names}"
        spec = wired["appended"][0]
        types = [b.type for b in spec.blocks]
        assert len(types) == len(set(types)), f"spec'te tip çakışması: {types}"

    def test_breakdown_failure_falls_back_to_single_pair(self, wired, monkeypatch):
        import agent

        def _boom(desc):
            raise ValueError("breakdown yok")

        monkeypatch.setattr(agent, "propose_condition_breakdown", _boom)
        c = _client()
        r = c.post(
            "/backtest/describe",
            data={"description": "tek koşullu tarif ama yeterince uzun"},
        )
        gen_id = re.search(r"describe/progress/([0-9a-f]+)", r.text).group(1)
        _poll(c, gen_id)
        spec = wired["appended"][0]
        roles = [b.role for b in spec.blocks]
        assert roles == ["entry", "exit"]  # eski 1+1 davranışı korunur


class TestDescribeMultiTF:
    """Birleşik panel: tarif + çoklu-TF checkbox → 2+ TF sweep, 1 TF tam run."""

    def test_multi_tf_chains_to_sweep_with_csv(self, wired):
        c = _client()
        r = c.post(
            "/backtest/describe",
            data={
                "description": "RSI 30 altindayken al, ATR 3x stop ile cik",
                "symbol": "ETHUSDT",
                "category": "linear",
                "intervals": ["15", "60", "240"],
            },
        )
        gen_id = re.search(r"describe/progress/([0-9a-f]+)", r.text).group(1)
        page = _poll_chain(c, gen_id)
        # 2+ TF → sweep karşılaştırma tablosu, /run DEĞİL.
        assert 'hx-post="/backtest/sweep"' in page.text
        assert 'hx-post="/backtest/run"' not in page.text
        vals = json.loads(re.search(r"hx-vals='([^']+)'", page.text).group(1))
        assert vals["intervals"] == ["15", "60", "240"]
        # csv yedeği: hx-vals dizi kodlaması tutmasa da /sweep bunu okur.
        assert vals["intervals_csv"] == "15,60,240"
        assert vals["symbol"] == "ETHUSDT"

    def test_single_tf_chains_to_run(self, wired):
        c = _client()
        r = c.post(
            "/backtest/describe",
            data={
                "description": "RSI 30 altindayken al, ATR 3x stop ile cik",
                "symbol": "BTCUSDT",
                "intervals": ["240"],
            },
        )
        gen_id = re.search(r"describe/progress/([0-9a-f]+)", r.text).group(1)
        page = _poll_chain(c, gen_id)
        # Tek TF → tam sonuç (/run), sweep DEĞİL.
        assert 'hx-post="/backtest/run"' in page.text
        assert 'hx-post="/backtest/sweep"' not in page.text
        vals = json.loads(re.search(r"hx-vals='([^']+)'", page.text).group(1))
        assert vals["interval"] == "240"

    def test_invalid_tf_codes_dropped(self, wired):
        # Geçersiz kod ("99") elenir; kalan 2 geçerli TF → sweep.
        c = _client()
        r = c.post(
            "/backtest/describe",
            data={
                "description": "RSI 30 altindayken al, ATR 3x stop ile cik",
                "intervals": ["99", "15", "60"],
            },
        )
        gen_id = re.search(r"describe/progress/([0-9a-f]+)", r.text).group(1)
        page = _poll_chain(c, gen_id)
        vals = json.loads(re.search(r"hx-vals='([^']+)'", page.text).group(1))
        assert vals["intervals"] == ["15", "60"]
        assert 'hx-post="/backtest/sweep"' in page.text
