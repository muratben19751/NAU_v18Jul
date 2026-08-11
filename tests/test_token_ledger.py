"""Persistent per-model token ledger — record/aggregate/report.

Verifies: the four usage fields are extracted from both an Anthropic-style
usage object and a plain dict; per-model totals aggregate correctly (fallback
attributed to the model the API actually answered with); torn/blank JSONL lines
are skipped; and record() never raises into the caller.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import token_ledger


def _usage_obj(i=0, o=0, cr=0, cw=0):
    return SimpleNamespace(
        input_tokens=i,
        output_tokens=o,
        cache_read_input_tokens=cr,
        cache_creation_input_tokens=cw,
    )


def test_extract_from_object_and_dict():
    a = token_ledger._extract_usage(_usage_obj(10, 20, 5, 3))
    assert a == {"input": 10, "output": 20, "cache_read": 5, "cache_write": 3}
    b = token_ledger._extract_usage(
        {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 4,
        }
    )
    assert b == {"input": 1, "output": 2, "cache_read": 0, "cache_write": 4}
    assert token_ledger._extract_usage(None) == {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
    }


def test_record_and_summary_per_model(tmp_path, monkeypatch):
    ledger = tmp_path / "token_usage.jsonl"
    monkeypatch.setattr(token_ledger, "LEDGER_PATH", ledger)

    token_ledger.record("claude-fable-5", _usage_obj(100, 40, 10, 5), "strategy")
    token_ledger.record("claude-fable-5", _usage_obj(50, 20, 0, 0), "narrative")
    # Fallback call attributed to Opus (the model that actually answered).
    token_ledger.record("claude-opus-4-8", _usage_obj(200, 80, 30, 0), "custom_block")

    s = token_ledger.summary(ledger)
    fable = s["models"]["claude-fable-5"]
    opus = s["models"]["claude-opus-4-8"]
    assert fable["calls"] == 2
    assert fable["input"] == 150
    assert fable["output"] == 60
    assert fable["total"] == 150 + 60 + 10 + 5
    assert opus["calls"] == 1
    assert opus["total"] == 200 + 80 + 30
    assert s["total"]["calls"] == 3
    assert s["total"]["total"] == fable["total"] + opus["total"]


def test_zero_usage_not_recorded(tmp_path, monkeypatch):
    ledger = tmp_path / "token_usage.jsonl"
    monkeypatch.setattr(token_ledger, "LEDGER_PATH", ledger)
    token_ledger.record("claude-fable-5", _usage_obj(0, 0, 0, 0), "empty")
    # Nothing written → summary is empty.
    assert token_ledger.summary(ledger)["models"] == {}


def test_torn_lines_skipped(tmp_path):
    ledger = tmp_path / "token_usage.jsonl"
    good = json.dumps(
        {
            "ts": "2026-07-24T00:00:00+00:00",
            "model": "claude-fable-5",
            "input": 10,
            "output": 5,
            "cache_read": 0,
            "cache_write": 0,
        }
    )
    ledger.write_text(good + "\n\n{not valid json\n" + good + "\n", encoding="utf-8")
    s = token_ledger.summary(ledger)
    assert s["models"]["claude-fable-5"]["calls"] == 2  # blank + torn skipped


# ---------------------------------------------------------------------------
# _parsed_records() incremental cache (DeepR 2026-08-08 [ORTA])
# ---------------------------------------------------------------------------


def test_summary_is_not_rereading_unchanged_bytes(tmp_path, monkeypatch):
    """A second summary() call on an unchanged ledger must not touch disk —
    the cache should serve the previously-parsed records directly."""
    ledger = tmp_path / "token_usage.jsonl"
    token_ledger._PARSE_CACHE.clear()
    line = json.dumps(
        {
            "ts": "2026-07-24T00:00:00+00:00",
            "model": "claude-fable-5",
            "input": 10,
            "output": 5,
            "cache_read": 0,
            "cache_write": 0,
        }
    )
    ledger.write_text(line + "\n", encoding="utf-8")

    token_ledger.summary(ledger)  # primes the cache

    import builtins

    calls = {"n": 0}
    real_open = builtins.open

    def _counting_open(*a, **k):
        calls["n"] += 1
        return real_open(*a, **k)

    monkeypatch.setattr(token_ledger, "open", _counting_open, raising=False)

    s1 = token_ledger.summary(ledger)
    s2 = token_ledger.summary(ledger)  # all-time called twice, like /tokens/badge

    assert calls["n"] == 0, "unchanged ledger was re-opened for reading"
    assert s1["total"]["calls"] == s2["total"]["calls"] == 1


def test_summary_picks_up_newly_appended_records_only(tmp_path):
    ledger = tmp_path / "token_usage.jsonl"
    token_ledger._PARSE_CACHE.clear()

    def _line(model, n):
        return json.dumps(
            {
                "ts": f"2026-07-24T00:00:0{n}+00:00",
                "model": model,
                "input": n,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
            }
        )

    ledger.write_text(_line("claude-fable-5", 1) + "\n", encoding="utf-8")
    s1 = token_ledger.summary(ledger)
    assert s1["total"]["calls"] == 1

    with open(ledger, "a", encoding="utf-8") as f:
        f.write(_line("claude-fable-5", 2) + "\n")
    s2 = token_ledger.summary(ledger)

    assert s2["total"]["calls"] == 2
    assert s2["total"]["input"] == 3


def test_summary_defers_an_unterminated_trailing_line_to_the_next_call(tmp_path):
    """A concurrent writer's in-flight append (no trailing '\\n' yet) must not
    be consumed and permanently skipped once the offset moves past it."""
    ledger = tmp_path / "token_usage.jsonl"
    token_ledger._PARSE_CACHE.clear()
    good = json.dumps(
        {
            "ts": "2026-07-24T00:00:00+00:00",
            "model": "claude-fable-5",
            "input": 10,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
        }
    )
    partial = json.dumps(
        {
            "ts": "2026-07-24T00:00:01+00:00",
            "model": "claude-fable-5",
            "input": 20,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
        }
    )
    # No trailing newline on the second line — simulates a writer mid-flush.
    ledger.write_text(good + "\n" + partial, encoding="utf-8")

    s1 = token_ledger.summary(ledger)
    assert s1["total"]["calls"] == 1, "an unterminated line must not be parsed yet"

    with open(ledger, "a", encoding="utf-8") as f:
        f.write("\n")  # the writer finishes the line
    s2 = token_ledger.summary(ledger)

    assert s2["total"]["calls"] == 2, "the completed line must be picked up next call"
    assert s2["total"]["input"] == 30


def test_record_never_raises(monkeypatch):
    # Point the ledger at an un-writable path shape; record must swallow the error.
    monkeypatch.setattr(token_ledger, "LEDGER_PATH", Path("\0illegal") / "x.jsonl")
    token_ledger.record("claude-fable-5", _usage_obj(1, 1, 0, 0), "boom")  # no raise


def test_summary_absent_ledger(tmp_path):
    s = token_ledger.summary(tmp_path / "does_not_exist.jsonl")
    assert s["models"] == {}
    assert s["total"]["calls"] == 0


def test_format_table_smoke(tmp_path, monkeypatch):
    ledger = tmp_path / "token_usage.jsonl"
    monkeypatch.setattr(token_ledger, "LEDGER_PATH", ledger)
    token_ledger.record("claude-fable-5", _usage_obj(1000, 200, 0, 0), "strategy")
    table = token_ledger.format_table(ledger)
    assert "claude-fable-5" in table
    assert "TOTAL" in table
    # Empty ledger → friendly message, not a crash.
    assert "No token usage" in token_ledger.format_table(tmp_path / "nope.jsonl")


# ── cost + session view (the sidebar token badge) ───────────────────────────


def test_cost_usd_prices_fable_with_cache_multipliers():
    # 1M of everything, UNLABELED cache_write → priced at the 1h TTL rate (2×,
    # this ledger's history is all Claude-CLI): 10 + 10*0.1 + 10*2 + 50 = $81
    counts = {
        "input": 1_000_000,
        "output": 1_000_000,
        "cache_read": 1_000_000,
        "cache_write": 1_000_000,
    }
    assert token_ledger.cost_usd(counts, "claude-fable-5") == pytest.approx(81.0)
    # Same write labeled as 5m TTL → 1.25×: 10 + 1 + 12.5 + 50 = $73.50
    assert token_ledger.cost_usd(
        {**counts, "cache_write_5m": 1_000_000}, "claude-fable-5"
    ) == pytest.approx(73.5)
    # Mixed split: 400k@5m (×1.25) + 600k@1h (×2) → 10 + 1 + 5 + 12 + 50 = $78
    assert token_ledger.cost_usd(
        {**counts, "cache_write_5m": 400_000, "cache_write_1h": 600_000},
        "claude-fable-5",
    ) == pytest.approx(78.0)
    # Prefix match: dated Opus variants price like their alias family.
    assert token_ledger.cost_usd(
        {"input": 1_000_000, "output": 0, "cache_read": 0, "cache_write": 0},
        "claude-opus-4-8-20260101",
    ) == pytest.approx(5.0)
    # Unknown model → None, never a made-up number.
    assert token_ledger.cost_usd(counts, "mystery-llm") is None


def test_cost_usd_matches_cli_self_reported_cost():
    # Live-probe fixture (2026-08-01): `claude -p --model claude-fable-5` result
    # envelope reported total fable cost $0.240859; our math must reproduce it
    # to the cent from the same usage payload (1h-TTL cache writes).
    usage = {
        "input_tokens": 4,
        "output_tokens": 97,
        "cache_read_input_tokens": 57_049,
        "cache_creation_input_tokens": 8_946,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 0,
            "ephemeral_1h_input_tokens": 8_946,
        },
    }
    counts = token_ledger._extract_usage(usage)
    assert counts["cache_write"] == 8_946 and counts["cache_write_1h"] == 8_946
    assert token_ledger.cost_usd(counts, "claude-fable-5") == pytest.approx(0.240859)


def test_record_persists_ttl_split_and_summary_prices_it(tmp_path, monkeypatch):
    ledger = tmp_path / "token_usage.jsonl"
    monkeypatch.setattr(token_ledger, "LEDGER_PATH", ledger)
    token_ledger.record(
        "claude-fable-5",
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 1_000_000,
            "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000},
        },
        "x",
    )
    rec = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert rec["cache_write_1h"] == 1_000_000
    assert "cache_write_5m" not in rec  # zero split keys stay off the line
    s = token_ledger.summary(ledger)
    m = s["models"]["claude-fable-5"]
    assert m["total"] == 1_000_000  # split must NOT double into totals
    assert m["cost_usd"] == pytest.approx(20.0)  # 1M 1h-writes × $10 × 2


def test_provider_reported_cost_is_durable_and_preferred_in_summary(
    tmp_path, monkeypatch
):
    ledger = tmp_path / "token_usage.jsonl"
    monkeypatch.setattr(token_ledger, "LEDGER_PATH", ledger)
    token_ledger.record(
        "claude-fable-5",
        {
            "input_tokens": 100,
            "output_tokens": 10,
            "cost_usd": 0.123456,
        },
        "strategy",
    )
    record = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert record["provider_cost_usd"] == pytest.approx(0.123456)
    summary = token_ledger.summary(ledger)
    model = summary["models"]["claude-fable-5"]
    assert model["provider_cost_usd"] == pytest.approx(0.123456)
    assert model["cost_usd"] == pytest.approx(0.123456)
    assert model["cost_source"] == "provider_reported"
    assert summary["total"]["cost_source"] == "provider_reported"


def test_mixed_provider_and_unpriced_calls_yields_unknown_cost(tmp_path, monkeypatch):
    # One provider-reported call plus one call under an unpriced model name —
    # the total must read as unknown (None), not silently just the $0.05 we
    # do know about (regression: used to short-circuit to a false total the
    # moment ANY call had a provider cost).
    ledger = tmp_path / "token_usage.jsonl"
    monkeypatch.setattr(token_ledger, "LEDGER_PATH", ledger)
    token_ledger.record(
        "mystery-llm", {"input_tokens": 100, "output_tokens": 10, "cost_usd": 0.05}, "a"
    )
    token_ledger.record("mystery-llm", _usage_obj(1000, 1000, 0, 0), "b")
    s = token_ledger.summary(ledger)
    m = s["models"]["mystery-llm"]
    assert m["calls"] == 2
    assert m["cost_usd"] is None
    assert m["cost_source"] == "mixed"
    assert s["total"]["cost_usd"] == 0.0  # excluded from the priced total


def test_summary_since_filters_and_prices(tmp_path, monkeypatch):
    ledger = tmp_path / "token_usage.jsonl"
    monkeypatch.setattr(token_ledger, "LEDGER_PATH", ledger)
    old = json.dumps(
        {
            "ts": "2026-07-01T00:00:00+00:00",
            "model": "claude-fable-5",
            "purpose": "",
            "input": 1000,
            "output": 1000,
            "cache_read": 0,
            "cache_write": 0,
        }
    )
    new = old.replace("2026-07-01", "2026-08-01")
    ledger.write_text(old + "\n" + new + "\n", encoding="utf-8")

    full = token_ledger.summary(ledger)
    ses = token_ledger.summary(ledger, since="2026-07-15T00:00:00+00:00")
    assert full["total"]["calls"] == 2 and ses["total"]["calls"] == 1
    # 1000 in + 1000 out on Fable = 0.00001*10 … = $0.06 per line
    assert full["total"]["cost_usd"] == pytest.approx(0.12)
    assert ses["total"]["cost_usd"] == pytest.approx(0.06)
    assert full["models"]["claude-fable-5"]["cost_usd"] == pytest.approx(0.12)


def test_cli_side_models_recorded_once(tmp_path, monkeypatch):
    # The CLI's internal Haiku helper call must land in the ledger; the main
    # model's modelUsage entry must NOT (it's recorded via resp.usage already).
    import agent

    ledger = tmp_path / "token_usage.jsonl"
    monkeypatch.setattr(token_ledger, "LEDGER_PATH", ledger)
    envelope = {
        "modelUsage": {
            "claude-fable-5": {
                "inputTokens": 4,
                "outputTokens": 97,
                "cacheReadInputTokens": 57_049,
                "cacheCreationInputTokens": 8_946,
                "canonicalModel": "claude-fable-5",
            },
            "claude-haiku-4-5-20251001": {
                "inputTokens": 521,
                "outputTokens": 12,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "canonicalModel": "claude-haiku-4-5",
            },
        }
    }
    agent._record_cli_side_models(envelope, "claude-fable-5")
    s = token_ledger.summary(ledger)
    assert list(s["models"]) == ["claude-haiku-4-5"]
    m = s["models"]["claude-haiku-4-5"]
    assert m["input"] == 521 and m["output"] == 12
    # 521×$1 + 12×$5 per MTok = $0.000581 — matches the CLI's own costUSD.
    assert m["cost_usd"] == pytest.approx(0.000581)


def test_tokens_badge_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(token_ledger, "LEDGER_PATH", tmp_path / "token_usage.jsonl")
    token_ledger.record("claude-fable-5", _usage_obj(500_000, 100_000, 0, 0), "x")

    from server import app

    r = TestClient(app).get("/tokens/badge")
    assert r.status_code == 200
    assert "token-badge" in r.text and "$" in r.text
    # Session view can be empty (records predate server start) but the
    # all-time Σ line must reflect the ledger.
    assert "Σ" in r.text
    assert TestClient(app).get("/tokens/table").status_code == 200


# ---------------------------------------------------------------------------
# Artımlı FOLD önbelleği (DeepR 2026-08-11 [ORTA])
#
# Eski hâl: _parsed_records TÜM kayıtları bir listede tutuyordu ve summary()
# her çağrıda o listenin tamamını baştan katlıyordu. /tokens/badge yoklama
# başına İKİ summary() çağırıyor (oturum + tüm-zaman), yani 60 sn'de bir iki
# tam O(n) geçiş; kayıt listesi de hiç kırpılmadığı için RAM kayıt sayısıyla
# doğrusal büyüyordu. Artık kayıt listesi tutulmuyor: (path, since) başına
# yalnız model toplamları saklanıyor ve her çağrı sadece EKLENEN baytları
# katlıyor.
# ---------------------------------------------------------------------------


def _ledger_line(model="claude-fable-5", i=1, ts="2026-08-11T00:00:00+00:00"):
    return json.dumps(
        {
            "ts": ts,
            "model": model,
            "purpose": "t",
            "input": i,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
        }
    )


def _write_lines(path, n, i=1):
    path.write_text(
        "".join(_ledger_line(i=i) + "\n" for _ in range(n)), encoding="utf-8"
    )


class TestFoldCache:
    @pytest.fixture(autouse=True)
    def _clear(self):
        token_ledger._PARSE_CACHE.clear()
        yield
        token_ledger._PARSE_CACHE.clear()

    def test_hit_no_parsed_records_are_retained_in_ram(self, tmp_path):
        """ISABET: durum yalnız model toplamlarını tutar — kayıt listesi yok.

        Bulgunun RAM yarısı: ledger her LLM çağrısında bir satır büyüyor ve
        eski önbellek her satırın parse edilmiş dict'ini süresiz tutuyordu.
        """
        ledger = tmp_path / "token_usage.jsonl"
        _write_lines(ledger, 500)

        assert token_ledger.summary(ledger)["total"]["calls"] == 500

        (state,) = token_ledger._PARSE_CACHE.values()
        assert set(state["models"]) == {"claude-fable-5"}
        assert not any(isinstance(v, list) for v in state.values())

    def test_hit_second_poll_neither_reads_nor_refolds(self, tmp_path, monkeypatch):
        """ISABET: dosya değişmediyse ne okuma ne yeniden katlama yapılır."""
        ledger = tmp_path / "token_usage.jsonl"
        _write_lines(ledger, 1, i=7)
        token_ledger.summary(ledger)  # önbelleği doldur

        folds = {"n": 0}
        real_fold = token_ledger._fold_record

        def _counting_fold(state, rec, since):
            folds["n"] += 1
            return real_fold(state, rec, since)

        opens = {"n": 0}
        real_open = open

        def _counting_open(*a, **k):
            opens["n"] += 1
            return real_open(*a, **k)

        monkeypatch.setattr(token_ledger, "_fold_record", _counting_fold)
        monkeypatch.setattr(token_ledger, "open", _counting_open, raising=False)

        s1 = token_ledger.summary(ledger)
        s2 = token_ledger.summary(ledger)

        assert opens["n"] == 0, "değişmemiş ledger yeniden okundu"
        assert folds["n"] == 0, "değişmemiş ledger yeniden katlandı (O(n) tekrar)"
        assert s1["total"]["input"] == s2["total"]["input"] == 7

    def test_miss_only_the_appended_record_is_folded(self, tmp_path, monkeypatch):
        """ISKA: append sonrası SADECE yeni satır katlanır, dosya baştan değil."""
        ledger = tmp_path / "token_usage.jsonl"
        _write_lines(ledger, 50)
        token_ledger.summary(ledger)

        folded: list[dict] = []
        real_fold = token_ledger._fold_record

        def _recording_fold(state, rec, since):
            folded.append(rec)
            return real_fold(state, rec, since)

        monkeypatch.setattr(token_ledger, "_fold_record", _recording_fold)
        with open(ledger, "a", encoding="utf-8") as f:
            f.write(_ledger_line(i=9) + "\n")
        s = token_ledger.summary(ledger)

        assert len(folded) == 1, "tüm dosya yeniden katlandı"
        assert s["total"]["calls"] == 51
        assert s["total"]["input"] == 50 + 9

    def test_invalidation_a_shrunk_ledger_is_refolded_from_zero(self, tmp_path):
        """GEÇERSİZLEŞME: rotasyon/truncate → durum sıfırlanır, baştan sayılır.

        Offset tabanlı artımlı okuma dosyanın yalnızca BÜYÜDÜĞÜNÜ varsayar;
        küçülme fark edilmezse eski toplamlar yeni dosyanınkilerle toplanıp
        kalıcı bir çift-sayıma dönüşür.
        """
        ledger = tmp_path / "token_usage.jsonl"
        _write_lines(ledger, 20)
        assert token_ledger.summary(ledger)["total"]["calls"] == 20

        _write_lines(ledger, 1, i=3)  # rotasyon/truncate
        s = token_ledger.summary(ledger)

        assert s["total"]["calls"] == 1, "küçülen ledger eskisinin üstüne toplanmış"
        assert s["total"]["input"] == 3

    def test_since_and_all_time_keys_stay_independent(self, tmp_path):
        """/tokens/badge'in iki çağrısı iki ayrı anahtar; ikisi de doğru kalmalı."""
        ledger = tmp_path / "token_usage.jsonl"
        ledger.write_text(
            _ledger_line(i=1, ts="2026-07-01T00:00:00+00:00")
            + "\n"
            + _ledger_line(i=2, ts="2026-08-01T00:00:00+00:00")
            + "\n",
            encoding="utf-8",
        )
        since = "2026-07-15T00:00:00+00:00"

        for _ in range(2):  # rozet iki kez yoklandı
            ses = token_ledger.summary(ledger, since=since)
            allt = token_ledger.summary(ledger)
            assert ses["total"]["calls"] == 1 and ses["total"]["input"] == 2
            assert allt["total"]["calls"] == 2 and allt["total"]["input"] == 3

        with open(ledger, "a", encoding="utf-8") as f:
            f.write(_ledger_line(i=4, ts="2026-08-02T00:00:00+00:00") + "\n")

        assert token_ledger.summary(ledger, since=since)["total"]["input"] == 6
        assert token_ledger.summary(ledger)["total"]["input"] == 7

    def test_summary_never_mutates_the_cached_accumulator(self, tmp_path):
        """Bitirme adımı (fiyatlama + özel anahtar silme) akümülatörü bozmamalı."""
        ledger = tmp_path / "token_usage.jsonl"
        _write_lines(ledger, 1, i=1000)

        first = token_ledger.summary(ledger)
        second = token_ledger.summary(ledger)

        assert first["total"]["calls"] == second["total"]["calls"] == 1
        assert first["models"]["claude-fable-5"]["cost_usd"] == pytest.approx(
            second["models"]["claude-fable-5"]["cost_usd"]
        )
        (state,) = token_ledger._PARSE_CACHE.values()
        assert "_unpriced_missing_calls" in state["grand"], (
            "akümülatörün özel anahtarları summary() tarafından silinmiş"
        )

    def test_key_cap_evicts_the_oldest_and_says_so_in_the_log(self, tmp_path, caplog):
        """SINIR: anahtar tavanına çarpınca tahliye SESSİZ olmamalı."""
        ledger = tmp_path / "token_usage.jsonl"
        _write_lines(ledger, 1)

        with caplog.at_level(logging.WARNING, logger="token_ledger"):
            for n in range(token_ledger._PARSE_CACHE_MAX_KEYS + 1):
                token_ledger.summary(
                    ledger, since=f"2020-01-{n + 1:02d}T00:00:00+00:00"
                )

        assert len(token_ledger._PARSE_CACHE) == token_ledger._PARSE_CACHE_MAX_KEYS
        assert any("fold cache full" in r.message for r in caplog.records)
        # Tahliye doğruluğu bozmaz — düşen anahtar bir sonraki çağrıda baştan katlanır.
        again = token_ledger.summary(ledger, since="2020-01-01T00:00:00+00:00")
        assert again["total"]["calls"] == 1
