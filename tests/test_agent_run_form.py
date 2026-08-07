"""POST /agent/run form plumbing: TF multi-select (tfs) + date window.

The worker is faked (captured kwargs, no LLM/backtest), so what is under test
is exactly the route's parsing: chip selections become the intervals list,
bogus codes are dropped, dates are validated before a worker ever starts.
"""

from __future__ import annotations

import contextlib
import threading

import pytest
from fastapi.testclient import TestClient


def _client_and_capture(monkeypatch):
    import web.routes.agent_backtest as ab
    from server import app

    got: dict = {}
    done = threading.Event()
    # These tests exercise form-to-worker mapping, not the process-wide AUTO
    # admission limit. Keep the asynchronous fake worker from making a rapid
    # sequence of independent form cases race the production semaphore.
    monkeypatch.setattr(ab, "_AUTO_RUN_SLOTS", threading.BoundedSemaphore(50))

    def fake_worker(**kw):
        got.update(kw)
        done.set()

    monkeypatch.setattr(ab, "_agent_worker", fake_worker)
    return TestClient(app), got, done


def test_tfs_multiselect_becomes_intervals(monkeypatch):
    client, got, done = _client_and_capture(monkeypatch)
    r = client.post(
        "/agent/run",
        data={
            "tfs": ["15", "60"],
            "range_start": "2024-01-01",
            "range_end": "2024-06-30",
            "n_iterations": 2,
        },
    )
    assert r.status_code == 200
    assert done.wait(5)
    assert got["intervals"] == ["15", "60"]
    assert got["range_start"] == "2024-01-01"
    assert got["range_end"] == "2024-06-30"


def test_bogus_tf_codes_fall_back_to_single_interval(monkeypatch):
    client, got, done = _client_and_capture(monkeypatch)
    r = client.post(
        "/agent/run", data={"tfs": ["banana"], "interval": "240", "n_iterations": 2}
    )
    assert r.status_code == 200
    assert done.wait(5)
    assert got["intervals"] == ["240"]


def test_dotted_symbol_promotes_to_external_with_mapped_tfs(monkeypatch):
    import data

    monkeypatch.setenv("AGENT_ALLOW_UNADJUSTED", "1")
    monkeypatch.setenv("AGENT_RESEARCH_MODE", "1")
    # This test verifies request mapping, not catalog-quality validation. The
    # real local QQQ fixture intentionally contains the large historical gap
    # that AUTO now rejects.
    monkeypatch.setattr(data, "external_data_gap_report", lambda frame, **kw: None)
    monkeypatch.setattr(data, "load_external_bars", lambda *a, **k: object())
    monkeypatch.setattr(data, "_external_bar_dir", lambda *a, **k: (object(), object()))
    monkeypatch.setattr(data, "_external_adjusted_flag", lambda *a, **k: False)
    client, got, done = _client_and_capture(monkeypatch)
    r = client.post(
        "/agent/run",
        data={"symbol": "QQQ.NASDAQ", "tfs": ["60", "D"], "n_iterations": 2},
    )
    assert r.status_code == 200
    assert done.wait(5)
    assert got["source"] == "external"
    assert got["instrument_id"] == "QQQ.NASDAQ"
    assert got["intervals"] == ["1-HOUR", "1-DAY"]  # bybit codes → granularities
    assert got["research_only"] is True


def test_known_qqq_gap_uses_audited_recent_continuous_tail(monkeypatch):
    """Only the exact reviewed QQQ history gap may select the recent suffix."""
    import pandas as pd

    import data

    old = pd.DatetimeIndex([pd.Timestamp("2004-11-30 13:00", tz="UTC")])
    recent = pd.date_range("2011-03-23 13:00", periods=250, freq="B", tz="UTC")
    frame = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
        index=old.append(recent),
    )
    monkeypatch.setattr(data, "load_external_bars", lambda *a, **k: frame)
    monkeypatch.setattr(data, "_external_bar_dir", lambda *a: (object(), object()))
    monkeypatch.setattr(data, "_external_adjusted_flag", lambda *a: True)
    client, got, done = _client_and_capture(monkeypatch)
    r = client.post(
        "/agent/run",
        data={
            "symbol": "QQQ.NASDAQ",
            "tfs": ["60"],
            "range_end": "2026-08-05",  # UI default end date is not an opt-out.
            "n_iterations": 2,
        },
    )
    assert r.status_code == 200
    assert done.wait(5)
    assert got["range_start"] == "2011-03-23"
    assert got["data_truncation"]["policy"] == "known_continuous_tail_v1"


def test_model_pick_reaches_worker_and_thread_pin(monkeypatch):
    import agent

    client, got, done = _client_and_capture(monkeypatch)
    r = client.post("/agent/run", data={"model": "claude-haiku-4-5", "n_iterations": 2})
    assert r.status_code == 200
    assert done.wait(5)
    assert got["model"] == "claude-haiku-4-5"
    # Thread pin: known model pins THIS thread only; unknown clears.
    agent.set_thread_model("claude-haiku-4-5")
    assert agent.current_model() == "claude-haiku-4-5"
    agent.set_thread_model("uydurma-model")
    assert agent.current_model() == agent.MODEL
    agent.set_thread_model(None)


def test_effort_pick_reaches_worker_and_thread_pin(monkeypatch):
    import agent

    client, got, done = _client_and_capture(monkeypatch)
    r = client.post("/agent/run", data={"effort": "low", "n_iterations": 2})
    assert r.status_code == 200
    assert done.wait(5)
    assert got["effort"] == "low"
    # Pin: bilinen seviye pinlenir (büyük/küçük harf duyarsız), bilinmeyen temizler.
    agent.set_thread_effort("MAX")
    assert agent.current_effort() == "max"
    agent.set_thread_effort("uydurma")
    assert agent.current_effort() == ""
    agent.set_thread_effort(None)
    assert agent.current_effort() == ""


def test_resolve_effort_is_type_safe_and_normalizing():
    """Setter koşunun İLK adımında worker thread'inde çağrılır — orada patlayamaz.

    Brutal test bunu yakalamıştı: `(effort or "").strip()` bir int'te
    AttributeError fırlatıyordu, yani form dışından gelen bir çağrı koşuyu daha
    başlamadan öldürürdü.
    """
    from agent import resolve_effort

    for junk in (42, 3.7, True, None, b"low", ["low"], {"a": 1}, object()):
        assert resolve_effort(junk) == ""
    for junk in ("uydurma", "low max", "--effort=max", "low; rm -rf /", "düşük", ""):
        assert resolve_effort(junk) == ""
    assert resolve_effort("  MAX  ") == "max"
    assert resolve_effort("LOW") == "low"


def test_brief_records_applied_effort_not_requested(monkeypatch):
    """Kokpit İSTENEN değil UYGULANAN effort'u göstermeli.

    Ayrıştıklarında arayüz koşmayan bir ayarı koşuyor gibi gösterir: form
    "uydurma" gönderdiğinde pin boşalır, brief yine "uydurma" derse ekran
    yalan söyler (aynı sınıf: `label` ↔ `meta.label` uyuşmazlığı).
    """
    import web.routes.agent_backtest as ab

    client, _got, done = _client_and_capture(monkeypatch)
    for raw, applied in (
        ("low", "low"),
        ("MAX", "max"),
        ("uydurma", ""),
        ("low; rm -rf /", ""),
        ("  high  ", "high"),
    ):
        done.clear()
        assert (
            client.post(
                "/agent/run", data={"effort": raw, "n_iterations": 1}
            ).status_code
            == 200
        )
        assert done.wait(5)
        run_id = list(ab._AGENT_PROGRESS)[-1]
        assert (ab._AGENT_PROGRESS[run_id]["brief"] or {}).get("effort") == applied


def test_effort_reaches_cli_flag_and_openrouter_body(monkeypatch):
    """Seçilen effort iki backend'e de GERÇEKTEN geçiyor; seçilmediğinde hiç geçmiyor.

    Kaldıracın kendisi burada: `--effort` bayrağı uzun süre kodda yoktu ve
    "seçilebiliyor ama uygulanmıyor" bir parametre, çalışmayan bir parametreden
    daha tehlikeli (ölçüm ona güvenerek yapılır).
    """
    import subprocess

    import agent

    seen: dict = {}

    class _Proc:
        returncode = 0
        stdout = '{"result":"ok","is_error":false}'
        stderr = ""

    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: (seen.update(cmd=list(cmd)), _Proc())[1]
    )

    def cli_cmd(effort):
        agent.set_thread_effort(effort)
        with contextlib.suppress(Exception):  # yanıt gövdesi bu testin konusu değil
            agent._ClaudeCLIMessages("claude").create(
                model="claude-sonnet-5", messages=[{"role": "user", "content": "x"}]
            )
        return seen["cmd"]

    cmd = cli_cmd("low")
    assert cmd[cmd.index("--effort") + 1] == "low"
    assert "--effort" not in cli_cmd(None)

    class _Completions:
        def create(self, **req):
            seen["req"] = req
            msg = type("M", (), {"content": "ok"})()
            choice = type("C", (), {"message": msg, "finish_reason": "stop"})()
            usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()
            return type("R", (), {"choices": [choice], "usage": usage})()

    client_or = type(
        "Cl", (), {"chat": type("Ch", (), {"completions": _Completions()})()}
    )()
    om = agent._OpenRouterMessages(client_or)

    def or_body(effort):
        agent.set_thread_effort(effort)
        om.create(
            model="moonshotai/kimi-k3", messages=[{"role": "user", "content": "x"}]
        )
        return seen["req"].get("extra_body")

    assert or_body("low") == {"reasoning": {"effort": "low"}}
    # xhigh/max Claude CLI'a özgü — OpenRouter sözlüğünde en yakın üst seviye.
    assert or_body("xhigh") == {"reasoning": {"effort": "high"}}
    assert or_body(None) is None
    agent.set_thread_effort(None)


def test_sealed_holdout_stats_counts_only_in_window_entries():
    import web.routes.agent_backtest as ab

    start = 1_000_000
    trades = [
        {"entry_time": start - 50, "pnl": 500.0},  # lead-in (warmup) — sayılmaz
        {"entry_time": start + 10, "pnl": 100.0},
        {"entry_time": start + 20, "pnl": -50.0},
        {"entry_time": start + 30, "pnl": 150.0},
    ]
    n, pnl_fr, sharpe = ab._sealed_holdout_stats(trades, start)
    assert n == 3
    assert pnl_fr == (100.0 - 50.0 + 150.0) / 10_000.0
    assert sharpe is not None and sharpe > 0
    # 0 işlem → ölçüm yok: n=0, sharpe None (uyarı yolunu tetikler).
    n0, pnl0, sh0 = ab._sealed_holdout_stats(
        [{"entry_time": start - 1, "pnl": 9.9}], start
    )
    assert (n0, pnl0, sh0) == (0, 0.0, None)
    assert ab._sealed_holdout_stats(None, start) == (0, 0.0, None)


def test_llm_cost_usd_is_notional_not_none(monkeypatch):
    import agent
    import web.routes.agent_backtest as ab

    agent.set_thread_model(None)
    model, cost = ab._llm_cost_usd(1_000_000, 0, 0, 0)
    assert model == agent.MODEL
    # Fable list price $10/MTok input — notional cost artık CLI'da da dolu.
    assert cost == pytest.approx(10.0)
    # 1h-TTL cache yazımı ×2 kuralı ledger'la aynı kaynaktan gelir.
    _, wcost = ab._llm_cost_usd(0, 0, 0, 1_000_000)
    assert wcost == pytest.approx(20.0)


def test_openrouter_thread_pin_and_routing(monkeypatch):
    import agent

    # "or:" pinleri geçerli; bilinmeyen düz ad temizlenir.
    agent.set_thread_model("or:deepseek/deepseek-chat")
    assert agent.current_model() == "or:deepseek/deepseek-chat"

    captured = {}

    class _FakeMessages:
        def create(self, *, model, **kw):
            captured["model"] = model

            class _R:
                content = []
                usage = None

            return _R()

    class _FakeOR:
        messages = _FakeMessages()

    monkeypatch.setattr(agent, "_get_openrouter_client", lambda: _FakeOR())
    # client argümanı OR yolunda kullanılmaz — sahte nesne yeterli.
    agent._create_message(object(), max_tokens=5, messages=[])
    assert captured["model"] == "deepseek/deepseek-chat"  # or: öneki soyuldu
    agent.set_thread_model(None)


def test_openrouter_pin_survives_the_claude_credit_fallback(monkeypatch):
    """The credit fallback is a billing fact, but only inside Claude's billing.

    `_active_model` is set when CLAUDE runs out of credit. It used to be checked
    first, so a run pinned to OpenRouter silently went back to Claude — breaking
    the feature at the exact moment it is useful ("Claude is out, switch to OR")
    and walking the call straight back into the same wall.
    """
    import agent

    monkeypatch.setattr(agent, "_active_model", agent.FALLBACK_MODEL)

    # A plain preference still loses to the billing fact…
    agent.set_thread_model("claude-haiku-4-5")
    assert agent.current_model() == agent.FALLBACK_MODEL
    # …but a different provider is a different account.
    agent.set_thread_model("or:deepseek/deepseek-chat")
    assert agent.current_model() == "or:deepseek/deepseek-chat"

    agent.set_thread_model(None)
    assert agent.current_model() == agent.FALLBACK_MODEL


def test_selectable_models_openrouter_entries(monkeypatch):
    import agent

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    vals = [v for v, _ in agent.selectable_models()]
    assert "" in vals and not any(v.startswith("or:") for v in vals)

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("NAUTILUS_OPENROUTER_MODELS", "foo/bar, baz/qux")
    vals = [v for v, _ in agent.selectable_models()]
    assert "or:foo/bar" in vals and "or:baz/qux" in vals


def test_selectable_models_free_only(monkeypatch):
    """Picker varsayılanda yalnız ücretsiz OpenRouter uçlarını listeler."""
    import agent

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("NAUTILUS_OPENROUTER_MODELS", raising=False)
    monkeypatch.setattr(
        agent,
        "openrouter_catalog",
        lambda force=False: [
            ("paid/one", "Paid One", False),
            ("free/one:free", "Free One", True),
        ],
    )

    monkeypatch.delenv("NAUTILUS_OPENROUTER_FREE_ONLY", raising=False)
    vals = [v for v, _ in agent.selectable_models()]
    assert "or:free/one:free" in vals
    assert "or:paid/one" not in vals

    # Kapatınca tüm katalog döner.
    monkeypatch.setenv("NAUTILUS_OPENROUTER_FREE_ONLY", "0")
    vals = [v for v, _ in agent.selectable_models()]
    assert "or:free/one:free" in vals and "or:paid/one" in vals


def test_selectable_models_paid_extra_alongside_free(monkeypatch):
    """Ücretsizler + elle izin verilen paralı uç; paralı olan etiketinde söyler."""
    import agent

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("NAUTILUS_OPENROUTER_MODELS", raising=False)
    monkeypatch.delenv("NAUTILUS_OPENROUTER_FREE_ONLY", raising=False)
    monkeypatch.setattr(
        agent,
        "openrouter_catalog",
        lambda force=False: [
            ("free/one:free", "Free One", True),
            ("paid/wanted", "Paid Wanted", False),
            ("paid/other", "Paid Other", False),
        ],
    )
    monkeypatch.setenv("NAUTILUS_OPENROUTER_EXTRA_MODELS", "paid/wanted")

    rows = dict(agent.selectable_models())
    assert "or:free/one:free" in rows
    assert "or:paid/wanted" in rows, "elle eklenen uç listede olmalı"
    assert "or:paid/other" not in rows, "istenmeyen paralı uç sızmamalı"
    # Düz <select>'lerde optgroup yok; ayrım işareti etikettedir.
    assert rows["or:paid/wanted"].endswith(" · paralı")
    assert not rows["or:free/one:free"].endswith(" · paralı")
    # Arayüz bunu ayrı bir gruba koyabilsin diye ayrıca değer olarak da bildirilir.
    assert agent.openrouter_paid_extras() == ["or:paid/wanted"]

    # Katalogda olmayan bir id sessizce düşürülmez; paralı varsayılır.
    monkeypatch.setenv("NAUTILUS_OPENROUTER_EXTRA_MODELS", "ghost/model")
    rows = dict(agent.selectable_models())
    assert rows["or:ghost/model"] == "OR · ghost/model · paralı"

    # Ücretsiz bir uç EXTRA'da tekrarlanırsa iki kez listelenmez.
    monkeypatch.setenv("NAUTILUS_OPENROUTER_EXTRA_MODELS", "free/one:free")
    vals = [v for v, _ in agent.selectable_models()]
    assert vals.count("or:free/one:free") == 1
    assert agent.openrouter_paid_extras() == []

    # Tüm-katalog modunda ayrı grup anlamsız — zaten hepsi listede.
    monkeypatch.setenv("NAUTILUS_OPENROUTER_FREE_ONLY", "0")
    monkeypatch.setenv("NAUTILUS_OPENROUTER_EXTRA_MODELS", "paid/wanted")
    assert agent.openrouter_paid_extras() == []
    assert [v for v, _ in agent.selectable_models()].count("or:paid/wanted") == 1


def test_openrouter_free_flag_and_fallback(monkeypatch):
    """Ücretsizlik ham fiyattan okunur; katalog boşsa yedek de paralı olamaz."""
    import agent

    assert agent._is_free({"prompt": "0", "completion": "0"})
    assert not agent._is_free({"prompt": "0", "completion": "0.0000002"})
    # Eksik/bozuk fiyat ücretsiz sayılmaz — şüphe faturaya değil listeye yazılır.
    assert not agent._is_free({"prompt": "0"})
    assert not agent._is_free({})
    assert not agent._is_free({"prompt": "free", "completion": "0"})

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("NAUTILUS_OPENROUTER_MODELS", raising=False)
    monkeypatch.delenv("NAUTILUS_OPENROUTER_FREE_ONLY", raising=False)
    # Bu test "free-only yedeği paralı olamaz" diyor; elle izin verilen paralı
    # uçlar (geliştirici makinesinde ortamda tanımlı olabilir) iddiayı ilgisiz
    # yere düşürür. Testin okuduğu HER ortam anahtarı testte sabitlenmeli.
    monkeypatch.delenv("NAUTILUS_OPENROUTER_EXTRA_MODELS", raising=False)
    monkeypatch.setattr(agent, "openrouter_catalog", lambda force=False: [])
    vals = [v for v, _ in agent.selectable_models() if v.startswith("or:")]
    assert vals, "katalog çekilemeyince statik yedek listelenmeli"
    assert all(v.endswith(":free") or v == "or:openrouter/free" for v in vals)


def test_unavailable_model_refuses_the_run_instead_of_degrading(monkeypatch):
    """Seçilen model koşamıyorsa AUTO turu HİÇ başlamaz.

    Bu bir gerileme testi: `openai` paketi kurulu olmadığı için OpenRouter
    istemcisi kurulamıyordu, her LLM çağrısı düşüyordu ve koşu "Random …
    (Claude unavailable)" kompozisyonlarıyla normal bir tur gibi ilerliyordu —
    kullanıcının seçtiği model hiç kullanılmadan.
    """
    import agent

    client, got, done = _client_and_capture(monkeypatch)

    def _boom():
        raise RuntimeError("OpenRouter backend requires the `openai` package.")

    monkeypatch.setattr(agent, "_get_openrouter_client", _boom)
    r = client.post(
        "/agent/run", data={"model": "or:moonshotai/kimi-k3", "n_iterations": 2}
    )
    assert r.status_code == 400
    assert "openai" in r.text  # sebep gövdede yazar, sessizce yutulmaz
    assert not done.wait(0.5), "koşu başlatılmamalıydı"

    # İstemci kurulabiliyorsa aynı seçim normal akar (ağa çıkılmaz).
    monkeypatch.setattr(agent, "_get_openrouter_client", lambda: object())
    r = client.post(
        "/agent/run", data={"model": "or:moonshotai/kimi-k3", "n_iterations": 2}
    )
    assert r.status_code == 200
    assert done.wait(5)
    assert got["model"] == "or:moonshotai/kimi-k3"


def test_model_unavailable_reason_only_gates_openrouter(monkeypatch):
    import agent

    def _boom():
        raise RuntimeError("nope")

    monkeypatch.setattr(agent, "_get_openrouter_client", _boom)
    # Claude yolu uygulamanın varsayılanı — burada yoklanmaz.
    assert agent.model_unavailable_reason("") == ""
    assert agent.model_unavailable_reason(None) == ""
    assert agent.model_unavailable_reason("claude-haiku-4-5") == ""
    assert agent.model_unavailable_reason("or:x/y") == "nope"


class _FakeRateLimit(Exception):
    status_code = 429

    def __init__(self, retry_after: str | None = None) -> None:
        super().__init__("Rate limit exceeded: free-models-per-min.")
        if retry_after is not None:
            self.response = type("R", (), {"headers": {"retry-after": retry_after}})()


def _or_client_failing(n_429: int, retry_after: str | None = None):
    """İlk ``n_429`` çağrıda 429 atan, sonra başarılı dönen sahte OR istemcisi."""
    state = {"calls": 0}

    class _Messages:
        def create(self, *, model, **kw):
            state["calls"] += 1
            if state["calls"] <= n_429:
                raise _FakeRateLimit(retry_after)
            return type("Resp", (), {"model": model, "usage": None, "content": []})()

    return type("C", (), {"messages": _Messages()})(), state


def test_openrouter_429_backs_off_and_succeeds(monkeypatch):
    """429 kalıcı arıza değil zamanlama arızası — yeniden denenmeli.

    Denemeden çağıranın fallback'ine düşmek koşuyu rastgele kompozisyona
    çevirip bunu başarı gibi gösteriyordu.
    """
    import agent

    slept: list[float] = []
    monkeypatch.setattr(agent, "_sleep", slept.append)
    client, state = _or_client_failing(2)
    monkeypatch.setattr(agent, "_get_openrouter_client", lambda: client)

    resp = agent._or_create_with_backoff("deepseek/deepseek-v4-flash-0731")
    assert resp.model == "deepseek/deepseek-v4-flash-0731"
    assert state["calls"] == 3
    # Plan bir DAKİKALIK pencereyi kapsamalı; kısa beklemeler dakikalık kotaya
    # karşı işe yaramaz (openai SDK'sının varsayılanı tam olarak bu yüzden yetmez).
    assert slept == [5.0, 15.0]
    assert sum(agent._OR_RETRY_WAITS) >= 60


def test_openrouter_429_honours_retry_after_and_gives_up_within_budget(monkeypatch):
    import agent

    slept: list[float] = []
    monkeypatch.setattr(agent, "_sleep", slept.append)

    # Sunucu söylüyorsa tahminimiz onu ezmez.
    client, _ = _or_client_failing(1, retry_after="3")
    monkeypatch.setattr(agent, "_get_openrouter_client", lambda: client)
    agent._or_create_with_backoff("x/y")
    assert slept == [3.0]

    # Bütçe: sonsuza kadar beklenmez, hata çağırana düşer.
    slept.clear()
    monkeypatch.setenv("NAUTILUS_OPENROUTER_429_MAX_WAIT", "10")
    client, state = _or_client_failing(99)
    monkeypatch.setattr(agent, "_get_openrouter_client", lambda: client)
    with pytest.raises(_FakeRateLimit):
        agent._or_create_with_backoff("x/y")
    assert sum(slept) <= 10


def test_openrouter_non_429_is_not_retried(monkeypatch):
    """Yalnız 429 yeniden denenir; başka hata beklemeden çağırana düşer."""
    import agent

    slept: list[float] = []
    monkeypatch.setattr(agent, "_sleep", slept.append)

    class _Messages:
        def create(self, *, model, **kw):
            raise ValueError("bozuk istek")

    monkeypatch.setattr(
        agent,
        "_get_openrouter_client",
        lambda: type("C", (), {"messages": _Messages()})(),
    )
    with pytest.raises(ValueError):
        agent._or_create_with_backoff("x/y")
    assert slept == []
    assert not agent._is_rate_limited(ValueError("x"))
    assert agent._is_rate_limited(_FakeRateLimit())


def test_catalog_summary_compact_generated_and_ttl_memo(monkeypatch):
    import agent
    import composer

    fake = {
        "ema_cross": {
            "label": "EMA Cross",
            "params": {"fast": {"type": "int", "min": 2, "max": 50, "default": 9}},
        },
        "agnt_e_zzz_1": {"label": "VWAP Custom", "params": {}},
        "lab_entry_x": {"label": "Lab Temp", "params": {}},
    }
    monkeypatch.setattr(composer, "BLOCK_CATALOG", fake)
    monkeypatch.setattr(agent, "_catalog_summary_cache", None)

    s = agent._catalog_summary(force=True)
    assert "ema_cross" in s and "[2..50]" in s  # builtin tam detay
    assert "- agnt_e_zzz_1: VWAP Custom" in s  # üretilmiş blok tek satır
    assert "lab_entry_x" not in s  # lab blokları hâlâ gizli

    # TTL memo: katalog değişse bile TTL içinde AYNI metin döner (bayt-sabit
    # prefix sözleşmesi); force=True taze üretir.
    fake["agnt_e_zzz_2"] = {"label": "Yeni Blok", "params": {}}
    assert agent._catalog_summary() == s
    assert "agnt_e_zzz_2" in agent._catalog_summary(force=True)


def test_bad_dates_rejected_before_worker(monkeypatch):
    client, got, done = _client_and_capture(monkeypatch)
    assert client.post("/agent/run", data={"range_start": "kotu"}).status_code == 400
    assert (
        client.post(
            "/agent/run",
            data={"range_start": "2025-02-02", "range_end": "2025-01-01"},
        ).status_code
        == 400
    )
    assert not done.is_set()  # no worker was started


# ── Kesilme (max_tokens) — modeli suçlamak yerine tavanı büyüt ─────────────


class _FakeResp:
    """_create_message'ın okuduğu asgari yüzey: içerik + stop_reason."""

    def __init__(self, stop_reason: str | None, text: str = "{}") -> None:
        self.content = []
        self.stop_reason = stop_reason
        self.text = text


def _fake_client(stop_reasons: list[str | None], seen: list[int]):
    class _Msgs:
        def create(self, **kw):
            seen.append(int(kw.get("max_tokens") or 0))
            i = min(len(seen) - 1, len(stop_reasons) - 1)
            return _FakeResp(stop_reasons[i])

    class _C:
        messages = _Msgs()

    return _C()


def test_truncated_response_retries_once_with_a_bigger_cap(monkeypatch):
    """Tavana dayanan yanıt fallback'e düşmez; tavan büyütülüp yeniden denenir.

    Gerileme testi: composed çağrısı 900 tavanında kesiliyor, JSON yarım kalıyor
    ve hata JSONDecodeError olarak görünüp MODELİ suçluyordu; koşu beş tur
    rastgele kompozisyon üretip fazları ✓ kapatıyordu.
    """
    import agent

    seen: list[int] = []
    monkeypatch.setattr(agent, "_ledger_record", lambda *a, **k: None)
    monkeypatch.setattr(agent, "current_model", lambda: "claude-fable-5")
    client = _fake_client(["max_tokens", "end_turn"], seen)

    resp = agent._create_message(client, "composed", max_tokens=900, messages=[])

    assert seen == [900, 900 * agent._TRUNCATION_RETRY_SCALE]
    assert not agent._was_truncated(resp)


def test_truncation_surviving_the_retry_raises_its_own_type(monkeypatch):
    """İkinci deneme de sığmazsa hata KENDİ adıyla gelir.

    JSONDecodeError "model geçerli JSON üretemedi" der ve teşhisi model
    değiştirmeye saptırır; TruncatedResponse cevabı kesenin biz olduğumuzu
    söyler. Çağıranın fallback'i yine devreye girer, ama sebebi okunur kalır.
    """
    import agent

    seen: list[int] = []
    monkeypatch.setattr(agent, "_ledger_record", lambda *a, **k: None)
    monkeypatch.setattr(agent, "current_model", lambda: "claude-fable-5")
    client = _fake_client(["max_tokens"], seen)

    with pytest.raises(agent.TruncatedResponse):
        agent._create_message(client, "composed", max_tokens=900, messages=[])
    assert len(seen) == 2  # bir kez yeniden denendi, sonsuza kadar değil


def test_openai_finish_reason_length_maps_to_max_tokens():
    """OpenAI-uyumlu uçlar "length" der; tek alan okunabilsin diye çevrilir."""
    import agent

    assert agent._was_truncated(agent._ORResponse("x", stop_reason="max_tokens"))
    assert not agent._was_truncated(agent._ORResponse("x", stop_reason="stop"))
    # CLI yolunda max_tokens karşılığı yok — kesilme de olamaz.
    assert not agent._was_truncated(agent._CLIResponse("x", {}))


def test_json_call_caps_leave_room_for_a_reasoning_model():
    """Tavanlar ölçülmüş gerçeğe bağlanır, sayıya değil.

    kimi-k3 ölçümü: composed 900 tavanında 900 çıktı (kesildi), custom_block
    4000 tavanında 1.787 (sığdı). Yani sığmayan tarafın eşiği ~1.8K mertebesi;
    tavanlar bunun üstünde kalmalı. Sabiti biri küçültürse gerekçe testte durur.
    """
    import agent

    assert agent.MAX_TOKENS_COMPOSED >= 2000, "composed JSON'u 900'e sığmıyordu"
    assert agent.MAX_TOKENS_IDEA >= 1200, "idea JSON'u 400'e sığmıyordu"


# ── Fallback görünür olmalı: sayaç + degraded fazı ────────────────────────


def test_fallback_proposal_carries_a_machine_readable_degraded_marker(monkeypatch):
    """Bozulma işareti açıklama metninde değil ayrı bir alanda taşınır.

    Açıklamanın kuyruğundaki " · fallback (X)" metnini ayrıştırmak kırılgan;
    koşu bu alanı sayar ve fazı ⚠ kapatır.
    """
    import agent

    monkeypatch.setattr(agent, "_get_client", lambda: object())

    def _boom(*a, **k):
        raise agent.TruncatedResponse("kesildi")

    monkeypatch.setattr(agent, "_create_message", _boom)

    proposal, usage = agent.propose_composed_strategy([], [])
    assert proposal["degraded"] == "TruncatedResponse"
    assert usage is None

    idea = agent._propose_agent_strategy_idea("", [])
    assert idea["degraded"] == "TruncatedResponse"


def test_degraded_run_counts_fallbacks_and_marks_the_phase():
    """5/5 fallback bir koşu değil arızadır — ✓ ile kapanmamalı.

    Faz `status` yine "done" kalır (ilerleme hesabı faz akışını sayar), bozulma
    ayrı bir bayrakla taşınır; kokpit rozeti sayacı okur.
    """
    import web.routes.agent_backtest as ab

    run_id = "tst_degraded"
    ab._AGENT_PROGRESS[run_id] = {
        "phases": [
            {"n": i, "label": lbl, "status": "pending", "detail": "", "ts": ""}
            for i, lbl in enumerate(ab._PHASES)
        ],
        "steps": [],
        "fallback_count": 0,
        "fallback_reasons": [],
    }
    try:
        ab._mark_degraded(run_id, "TruncatedResponse", "strategy proposal")
        ab._mark_degraded(run_id, "TruncatedResponse", "strategy idea")
        ab._done_phase(run_id, 1, "⚠ degraded", degraded=True)

        s = ab._AGENT_PROGRESS[run_id]
        assert s["fallback_count"] == 2
        assert s["fallback_reasons"] == ["TruncatedResponse"]  # sebep tekrarlanmaz
        assert s["phases"][1]["status"] == "done"  # ilerleme akışı bozulmaz
        assert s["phases"][1]["degraded"] is True  # ama başarı gibi görünmez
    finally:
        ab._AGENT_PROGRESS.pop(run_id, None)


def test_mission_view_exposes_the_degraded_badge():
    """Sayaç ekrana ulaşmazsa sayılmış olması bir şey değiştirmez."""
    from web.mission import mission_view

    mv = mission_view(
        {
            "phases": [],
            "steps": [],
            "fallback_count": 3,
            "fallback_reasons": ["TruncatedResponse", "JSONDecodeError"],
        },
        run_id="x",
    )
    assert mv["degraded_count"] == 3
    assert "TruncatedResponse" in mv["degraded_reasons"]
