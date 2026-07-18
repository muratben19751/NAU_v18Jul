"""Regresyon çapaları — derin denetim dalgasının (fix(denetim*)/fix(wfo*))
düzelttiği yüksek-blast-radius davranışlarını KİLİTLER.

Bu testler olmadan, düzeltmelerin birini geri getiren bir değişiklik yeşil
geçerdi (test-suite analizi, 2026-07: para/seçim/güvenlik dallarında sıfır
kapsam). Her sınıf tek bir düzeltmeyi GERÇEK shipping edilen fonksiyon üzerinden
sürer (elle kopyalanmış mantık değil).
"""

from __future__ import annotations

import ast
import json
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# #1 (critical) — codegate AST whitelist red-matrisi
# LLM/kullanıcı kodunun in-process çalıştığı TÜM güvenlik sınırı. Her kaçış
# denemesi GeneratedCodeError vermeli; _ALLOWED_ATTRS genişletmesi / alt-çizgi
# guard'ının düşmesi `().__class__.__bases__[0].__subclasses__()` RCE'sini açar.
# ---------------------------------------------------------------------------
def _ev(body: str) -> str:
    """closes/... imzalı geçerli bir evaluate gövdesine tek satır sar."""
    return "def evaluate(state, block, closes, indicators, portfolio):\n    " + body


class TestCodegateRejectionMatrix:
    @pytest.mark.parametrize(
        "src, needle",
        [
            (_ev("return ().__class__"), "disallowed attribute access"),
            (_ev("return open('x')"), "disallowed function: open"),
            (_ev("return getattr(block, 'x')"), "disallowed function: getattr"),
            (_ev("return eval('1')"), "disallowed function: eval"),
            (_ev("return exec('x')"), "disallowed function: exec"),
            (_ev("return __import__('os')"), "disallowed function: __import__"),
            # M#1: subscript/dict/call-chain callees bypassed the direct-Name
            # gate (`[exec][0](...)` fell into `else: pass`). Now denied.
            (_ev("return [exec][0]('x')"), "non-name callee"),
            (_ev("return [open][0]('f')"), "non-name callee"),
            (_ev("return [getattr][0](block, 'x')"), "non-name callee"),
            # M#1: a dangerous builtin passed as an arg to an allowed callee
            # (`sorted(..., key=eval)`) or aliased (`f = eval; f(...)`) is now
            # rejected at the reference site, not just at a direct call.
            (_ev("return sorted(closes, key=eval)"), "reference to disallowed builtin"),
            (
                "def evaluate(state, block, closes, indicators, portfolio):\n"
                "    f = eval\n"
                "    return f('1')",
                "reference to disallowed builtin",
            ),
            (_ev("return closes.system"), "attribute not in whitelist"),
            (_ev("return __x"), "leading underscore"),
            (
                "@deco\ndef evaluate(state, block, closes, indicators, portfolio):\n    return True",
                "decorators are not allowed",
            ),
            (
                "def evaluate(a, b):\n    return True",
                "evaluate signature must be",
            ),
            (
                "import os\ndef evaluate(state, block, closes, indicators, portfolio):\n    return True",
                "top-level statement not allowed",
            ),
            (
                "def evaluate(state, block, closes, indicators, portfolio):\n    import os\n    return True",
                "disallowed node",
            ),
            ("def helper():\n    return 1", "missing `evaluate`"),
        ],
    )
    def test_each_escape_is_rejected(self, src, needle):
        from codegate import GeneratedCodeError, validate_generated_code

        with pytest.raises(GeneratedCodeError) as exc:
            validate_generated_code(src)
        assert needle in str(exc.value), f"beklenen ret sebebi yok: {exc.value!r}"

    def test_the_classic_rce_chain_is_blocked(self):
        from codegate import GeneratedCodeError, validate_generated_code

        # `().__class__.__bases__[0].__subclasses__()` — sandbox-kaçış klasiği.
        with pytest.raises(GeneratedCodeError):
            validate_generated_code(
                _ev("return ().__class__.__bases__[0].__subclasses__()")
            )

    def test_legit_block_with_subscript_indexing_and_builtins_passes(self):
        # Tightening the callee/reference checks must NOT reject normal blocks:
        # subscript INDEXING (closes[-1]) is a Load, not a Call callee, and the
        # builtins used here are all whitelisted.
        from codegate import validate_generated_code

        src = _ev(
            "return closes[-1] > (sum(closes) / len(closes)) "
            "if len(closes) > 0 else None"
        )
        validate_generated_code(src)  # must not raise

    def test_valid_block_passes(self):
        # Pozitif kontrol: whitelist içi kod (builtin + ind.* + container ops)
        # AST döndürmeli — red-matrisinin fazla-geniş olmadığının kanıtı.
        from codegate import validate_generated_code

        src = (
            "def evaluate(state, block, closes, indicators, portfolio):\n"
            "    n = len(closes)\n"
            "    fast = ind.sma(closes, 3)\n"
            "    return closes[-1] > sum(closes) / n and fast > 0\n"
        )
        assert isinstance(validate_generated_code(src), ast.Module)


# ---------------------------------------------------------------------------
# #3 (security) — data._bybit_cache_path path-traversal
# /lab, /backtest, /robustness pass RAW form symbol/category/interval to the
# cache-path builder (unlike /data, which whitelists). Windows `\` was not
# sanitized, so "..\..\evil" created dirs/lockfiles/parquet OUTSIDE the cache
# root. A crafted value must now stay inside BYBIT_CACHE_DIR or raise.
# ---------------------------------------------------------------------------
class TestBybitCachePathTraversal:
    def test_legit_paths_unchanged(self):
        from data import BYBIT_CACHE_DIR, _bybit_cache_path

        p = _bybit_cache_path("linear", "BTCUSDT", "1")
        assert p == BYBIT_CACHE_DIR / "linear_BTCUSDT_1m.parquet"
        assert (
            _bybit_cache_path("spot", "ETHUSDT", "D").name == "spot_ETHUSDT_1d.parquet"
        )
        # historical "/"->"_" behaviour preserved
        assert (
            _bybit_cache_path("linear", "BTC/USDT", "1").name
            == "linear_BTC_USDT_1m.parquet"
        )

    @pytest.mark.parametrize(
        "bad_symbol",
        ["..\\..\\..\\evil", "../../../../etc/passwd", "a/../../b", "....//x"],
    )
    def test_traversal_symbol_stays_inside_cache_dir(self, bad_symbol):
        from data import BYBIT_CACHE_DIR, _bybit_cache_path

        p = _bybit_cache_path("linear", bad_symbol, "1")
        assert p.parent == BYBIT_CACHE_DIR  # no directory component escapes
        assert ".." not in p.parts

    @pytest.mark.parametrize(
        "category, symbol, interval",
        [
            ("../evil", "BTCUSDT", "1"),  # category not in the closed set
            ("linear", "BTCUSDT", "../x"),  # interval not in the closed set
            ("linear", "....", "1"),  # symbol collapses to all-underscore
            ("linear", "///", "1"),
        ],
    )
    def test_invalid_components_raise(self, category, symbol, interval):
        from data import _bybit_cache_path

        with pytest.raises(ValueError):
            _bybit_cache_path(category, symbol, interval)


# ---------------------------------------------------------------------------
# #2 (critical) — composer._compute_qty: 4 sizing modu (3'ü hiçbir e2e'de
# çağrılmıyor; her golden spec 'fixed'). Direkt para matematiği → notional → PnL.
# GERÇEK metodu sahte-self ile çağırır (unbound function).
# ---------------------------------------------------------------------------
def _qty_self(
    mode,
    *,
    trade_size=0.1,
    usdt=1000.0,
    pct=50.0,
    atr_risk=1.0,
    equity=10_000.0,
    atr=None,
):
    spec = SimpleNamespace(
        trade_size_mode=mode,
        trade_size=trade_size,
        trade_size_usdt=usdt,
        trade_size_percent=pct,
        trade_size_atr_risk=atr_risk,
    )
    return SimpleNamespace(spec=spec, _atr=atr, _current_equity=lambda: equity)


class TestComputeQty:
    def _qty(self, self_obj, price):
        from composer import ComposedStrategy

        return ComposedStrategy._compute_qty(self_obj, price)

    def test_fixed_returns_trade_size(self):
        assert self._qty(_qty_self("fixed", trade_size=0.1), 50_000.0) == 0.1

    def test_fixed_usdt_divides_notional_by_price(self):
        # 1000 USDT / 50000 = 0.02
        assert self._qty(
            _qty_self("fixed_usdt", usdt=1000.0), 50_000.0
        ) == pytest.approx(0.02)

    def test_percent_equity(self):
        # equity 10000 * 50% / 50000 = 0.1
        assert self._qty(
            _qty_self("percent_equity", equity=10_000.0, pct=50.0), 50_000.0
        ) == pytest.approx(0.1)

    def test_atr_target_uses_risk_over_atr(self):
        # risk_usd = 10000 * 1% = 100 ; qty = 100 / atr.value(200) = 0.5
        atr = SimpleNamespace(initialized=True, value=200.0)
        assert self._qty(
            _qty_self("atr_target", equity=10_000.0, atr_risk=1.0, atr=atr), 50_000.0
        ) == pytest.approx(0.5)

    def test_atr_target_falls_back_when_atr_not_ready(self):
        assert (
            self._qty(_qty_self("atr_target", trade_size=0.1, atr=None), 50_000.0)
            == 0.1
        )

    def test_nonpositive_price_falls_back_to_trade_size(self):
        assert self._qty(_qty_self("fixed_usdt", trade_size=0.1), 0.0) == 0.1


# ---------------------------------------------------------------------------
# #3 (critical) — wfo objective_value / _calmar (M236/M240): calmar GA seçim
# kriteri. DD_FLOOR(%1) + CALMAR_CAP(±10) + in-scale NaN-fallback. Ham pnl/|dd|'ye
# dönüş (O(1e4)) her seçilen parametre setini sessizce bozar.
# ---------------------------------------------------------------------------
def _res(**metrics):
    return SimpleNamespace(error=None, metrics=metrics)


def _conf(n):
    from wfo_optimizer import WFO_TRADE_CONF_K

    return n / (n + WFO_TRADE_CONF_K)


class TestObjectiveCalmar:
    def test_calmar_cap_clamps_micro_dd(self):
        from wfo_optimizer import _CALMAR_CAP, objective_value

        # pnl_pct 1.0 / max(|-0.001|, 0.01)=0.01 → 100 → CAP 10 (5000 DEĞİL)
        v = objective_value(_res(n_trades=100, pnl_pct=1.0, max_dd=-0.001), "calmar")
        assert v == pytest.approx(_CALMAR_CAP * _conf(100))
        assert v < 100  # sınırsız oran O(1e3-1e4) olurdu

    def test_dd_floor_engages(self):
        from wfo_optimizer import objective_value

        # pnl_pct 0.05 / max(|-0.0001|, DD_FLOOR=0.01)=0.01 → 5 (CAP altında)
        # floor olmadan 0.05/0.0001 = 500 olurdu.
        v = objective_value(_res(n_trades=100, pnl_pct=0.05, max_dd=-0.0001), "calmar")
        assert v == pytest.approx(5.0 * _conf(100))

    def test_pnl_pct_fallback_uses_starting_cash(self):
        from app_constants import STARTING_CASH
        from wfo_optimizer import objective_value

        # pnl_pct yok → pnl / STARTING_CASH = 5000/10000 = 0.5 ; /max(0.5,0.01)=1.0
        v = objective_value(_res(n_trades=100, pnl=5000.0, max_dd=-0.5), "calmar")
        assert STARTING_CASH == 10_000.0
        assert v == pytest.approx(1.0 * _conf(100))

    def test_nan_fallback_stays_capped_not_unbounded(self):
        from wfo_optimizer import _CALMAR_CAP, objective_value

        # M240: sharpe NaN & sortino NaN → CAPLİ calmar (±10), sınırsız pnl/|dd| DEĞİL.
        # pnl_pct 1.0 / 0.01 = 100 → CAP 10. (unbounded 1.0/0.0001 = 10000 olurdu.)
        v = objective_value(
            _res(
                n_trades=100,
                sharpe=float("nan"),
                sortino=float("nan"),
                pnl_pct=1.0,
                max_dd=-0.0001,
            ),
            "sharpe",
        )
        assert v == pytest.approx(_CALMAR_CAP * _conf(100))

    def test_calmar_none_when_max_dd_missing(self):
        from wfo_optimizer import objective_value

        v = objective_value(_res(n_trades=100, pnl_pct=1.0, max_dd=None), "calmar")
        assert v == float("-inf")

    def test_undertraded_is_neg_inf(self):
        from wfo_optimizer import objective_value

        assert objective_value(
            _res(n_trades=3, pnl_pct=1.0, max_dd=-0.1), "calmar"
        ) == float("-inf")


# ---------------------------------------------------------------------------
# #9 (critical) — _wfo_window_bounds M62 non-positive guard (sonsuz döngü/DoS)
# + M28 embargo boşluğu (test_start = train_end + WF_EMBARGO_DAYS).
# ---------------------------------------------------------------------------
class TestWfoWindowBounds:
    def _bounds(self, train, test, step):
        import pandas as pd

        from backtest_robustness import _wfo_window_bounds

        start = pd.Timestamp("2023-01-01", tz="UTC")
        end = pd.Timestamp("2024-06-01", tz="UTC")
        return _wfo_window_bounds(start, end, train, test, step)

    def test_nonpositive_months_return_empty(self):
        # M62: step/train/test <= 0 → [] (yoksa cursor ilerlemez → sonsuz döngü,
        # sınırsız bounds listesi, sandbox child global timeout'a dek CPU yakar).
        assert self._bounds(6, 1, 0) == []
        assert self._bounds(0, 1, 1) == []
        assert self._bounds(6, 0, 1) == []

    def test_positive_produces_windows_with_embargo_gap(self):
        from datetime import timedelta

        from backtest_robustness import WF_EMBARGO_DAYS

        bounds = self._bounds(6, 1, 1)
        assert bounds, "pozitif parametreler pencere üretmeli"
        for _n, _tr_s, train_end, test_start, _te in bounds:
            # M28: test train_end'den WF_EMBARGO_DAYS gün SONRA başlar (leakage guard)
            assert test_start == train_end + timedelta(days=WF_EMBARGO_DAYS)


# ---------------------------------------------------------------------------
# #7a (critical) — state.AppState.append best-selection: "şimdiye dek en iyi"nin
# tek doğruluk kaynağı. pnl kıyas, eksik-pnl→-inf, error-asla-kazanmaz.
# ---------------------------------------------------------------------------
class TestAppStateAppend:
    def _res(self, rid, pnl=None, error=None):
        from datetime import UTC, datetime

        from state import IterationResult

        metrics = {} if pnl is None else {"pnl": pnl}
        return IterationResult(
            id=rid,
            strategy="s",
            params={},
            metrics=metrics,
            equity_curve=[],
            rationale="",
            error=error,
            timestamp=datetime.now(UTC),
        )

    def test_best_tracks_highest_pnl_and_does_not_downgrade(self):
        from state import AppState

        st = AppState()
        st.append(self._res(1, pnl=10.0))
        assert st.best.id == 1
        st.append(self._res(2, pnl=50.0))
        assert st.best.id == 2
        st.append(self._res(3, pnl=20.0))  # düşük → değişmez
        assert st.best.id == 2

    def test_errored_iteration_never_wins(self):
        from state import AppState

        st = AppState()
        st.append(self._res(1, pnl=10.0))
        st.append(self._res(2, pnl=999.0, error="boom"))  # hata → best OLAMAZ
        assert st.best.id == 1

    def test_missing_pnl_is_neg_inf(self):
        from state import AppState

        st = AppState()
        st.append(self._res(1))  # pnl yok → -inf, -inf > -inf değil → best None
        assert st.best is None
        st.append(self._res(2, pnl=5.0))  # gerçek pnl kazanır
        assert st.best.id == 2


# ---------------------------------------------------------------------------
# #8 (critical) — agent.propose_custom_block retry döngüsü + _acc_usage token
# defteri: usage HER denemede toplanmalı; 2 başarısız deneme → GeneratedCodeError.
# GERÇEK propose_custom_block'u sahte client ile sürer (mock messages.create).
# ---------------------------------------------------------------------------
class TestProposeCustomBlockRetry:
    def _resp(self, text, i, o):
        block = SimpleNamespace(type="text", text=text)
        usage = SimpleNamespace(
            input_tokens=i,
            output_tokens=o,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        return SimpleNamespace(content=[block], usage=usage)

    def _client(self, responses):
        seq = iter(responses)
        return SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: next(seq)))

    _VALID_CODE = (
        "def max_lookback(params):\n"
        "    return 5\n\n"
        "def evaluate(state, block, closes, indicators, portfolio):\n"
        "    if closes[-1] > closes[-2]:\n"
        "        return 'long'\n"
        "    return None\n"
    )

    def test_usage_sums_across_attempts_then_succeeds(self, monkeypatch):
        import json

        import agent

        bad = json.dumps({"name": "x"})  # geçerli JSON ama şema eksik → retry
        good = json.dumps(
            {
                "name": "blk",
                "meta": {"label": "t", "params": {}},
                "code": self._VALID_CODE,
            }
        )
        client = self._client([self._resp(bad, 10, 5), self._resp(good, 20, 7)])
        monkeypatch.setattr("agent._get_client", lambda: client)

        out = agent.propose_custom_block("blk", "desc", "entry")
        assert out["name"] == "blk"
        # M1583: iki denemenin token'ları da toplanmalı (10+20, 5+7)
        assert out["usage"]["input_tokens"] == 30
        assert out["usage"]["output_tokens"] == 12

    def test_raises_after_two_failed_attempts(self, monkeypatch):
        import json

        import agent
        from codegate import GeneratedCodeError

        bad = json.dumps({"name": "x"})
        client = self._client([self._resp(bad, 1, 1), self._resp(bad, 1, 1)])
        monkeypatch.setattr("agent._get_client", lambda: client)

        with pytest.raises(GeneratedCodeError, match="after 2 attempts"):
            agent.propose_custom_block("blk", "desc", "entry")


# ---------------------------------------------------------------------------
# #7b (critical) — loop M29: /loop/start çalışıyorken ikinci POST no-op olmalı
# (senkron running=True-under-lock guard). Regresyon flag'i thread gövdesine
# geri taşırsa çift loop thread'i başlar (paylaşılan state / çift token).
# ---------------------------------------------------------------------------
class TestLoopDoubleStart:
    def test_second_start_while_running_is_noop(self, monkeypatch):
        import time

        from fastapi.testclient import TestClient

        import web.routes.loop as loop_mod
        from server import app
        from state import get_state

        calls = []

        def fake_run_loop(state, bars, mode, **kw):
            calls.append(1)
            time.sleep(0.3)  # meşgul kal — running'i SIFIRLAMA (route set etti)

        monkeypatch.setattr(loop_mod, "run_loop", fake_run_loop)
        monkeypatch.setattr("server.get_bars", lambda: None)

        st = get_state()
        with st.lock:
            st.running = False
            st.stop_requested = False
            st.iterations = []
        try:
            client = TestClient(app)
            r1 = client.post("/loop/start", data={"mode": "agent"})
            r2 = client.post("/loop/start", data={"mode": "agent"})
            assert r1.status_code == 200 and r2.status_code == 200
            time.sleep(0.45)  # tek fake thread'in koşmasına izin ver
            assert len(calls) == 1, f"çift-başlatma: {len(calls)} thread"
        finally:
            with st.lock:
                st.stop_requested = True
                st.running = False


# ---------------------------------------------------------------------------
# #10 (high) — custom_block_store delete dependency (M621): bir strateji bloğu
# kullanıyorken silmek 409 dönmeli (force=1 hariç); yoksa blok silinince
# bağımlı stratejiler bir sonraki load_catalog'da sessizce KALICI siliniyordu.
# ---------------------------------------------------------------------------
class TestDeleteCustomDependency:
    def test_delete_blocked_when_a_strategy_references_it(self, monkeypatch):
        from fastapi.testclient import TestClient

        import composer
        from composer import ComposedStrategySpec, SignalBlock
        from server import app

        spec = ComposedStrategySpec(
            id="s1",
            name="Dep Strat",
            description="",
            blocks=[SignalBlock(type="mycustom", role="entry", params={})],
        )
        monkeypatch.setattr(composer, "load_catalog", lambda: [spec])

        client = TestClient(app)
        r = client.delete("/strategy/blocks/custom/mycustom")
        assert r.status_code == 409
        assert "Dep Strat" in r.text

    def test_force_deletes_despite_dependents(self, monkeypatch):
        from fastapi.testclient import TestClient

        import composer
        import custom_block_store as cbs
        import web.routes.strategy as strat_mod
        from server import app

        monkeypatch.setattr(composer, "load_catalog", lambda: [])
        deleted = []
        monkeypatch.setattr(cbs, "delete_custom", lambda name: deleted.append(name))
        monkeypatch.setattr(strat_mod, "unregister_custom_block", lambda name: None)

        client = TestClient(app)
        r = client.delete("/strategy/blocks/custom/whatever?force=1")
        assert r.status_code == 200
        assert deleted == ["whatever"]

    def test_builtin_block_cannot_be_deleted(self):
        from fastapi.testclient import TestClient

        from server import app

        client = TestClient(app)
        r = client.delete("/strategy/blocks/custom/ma_cross")  # builtin
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# LLM model fallback: Fable kredisi bitince Opus'a düş (kullanıcı isteği).
# İki backend iki ayrı sinyal verir:
#   - API:        403 + error.type == "billing_error" (SDK .type alanı
#                 billing_error'ı permission_error'dan ayırır — ikisi de 403).
#   - claude-cli: tipli exception YOK; harcama limiti geçici rate-limit ile aynı
#                 429 kodundan gelir → ayrım yalnız "monthly spend limit" metni.
# ÇIPLAK 429 KASITLI olarak fallback tetiklemez (geçici; retry edilir) — bir
# regresyon o ayrımı kaldırırsa bu testler kırılır.
# ---------------------------------------------------------------------------
class _LLMErr(Exception):
    def __init__(self, msg, type_=None):
        super().__init__(msg)
        self.type = type_


class _FakeLLMClient:
    """İlk çağrıda hata fırlatır, sonrakinde başarılı döner; modelleri kaydeder."""

    def __init__(self, exc):
        self._exc = exc
        self._failed = False
        self.models: list[str] = []
        self.messages = self

    def create(self, model=None, **kw):
        self.models.append(model)
        if not self._failed:
            self._failed = True
            raise self._exc
        return f"OK({model})"


class TestLLMCreditFallback:
    def _fresh(self):
        import importlib

        import agent

        return importlib.reload(agent)  # _active_model'i sıfırla

    def test_billing_error_falls_back_to_opus(self):
        agent = self._fresh()
        c = _FakeLLMClient(_LLMErr("credit balance is too low", "billing_error"))
        out = agent._create_message(c, max_tokens=10, messages=[])
        assert out == f"OK({agent.FALLBACK_MODEL})"
        assert c.models == [agent.MODEL, agent.FALLBACK_MODEL]
        # Kalıcı kilit: sonraki çağrılar doğrudan fallback modelini kullanır.
        assert agent.current_model() == agent.FALLBACK_MODEL

    def test_credit_message_without_type_falls_back(self):
        # claude-cli backend'i tipli exception üretmez → mesaj yüzeyi de tanınmalı.
        agent = self._fresh()
        c = _FakeLLMClient(_LLMErr("400 credit balance is too low to access the API"))
        assert agent._create_message(c, max_tokens=10, messages=[]) == (
            f"OK({agent.FALLBACK_MODEL})"
        )

    def test_rate_limit_does_not_fall_back(self):
        # 429 geçicidir; modeli kalıcı değiştirmek yanlış olurdu.
        agent = self._fresh()
        c = _FakeLLMClient(_LLMErr("rate limited", "rate_limit_error"))
        with pytest.raises(_LLMErr):
            agent._create_message(c, max_tokens=10, messages=[])
        assert c.models == [agent.MODEL]  # tek deneme, fallback YOK
        assert agent.current_model() == agent.MODEL

    def test_permission_error_does_not_fall_back(self):
        agent = self._fresh()
        c = _FakeLLMClient(_LLMErr("not allowed", "permission_error"))
        with pytest.raises(_LLMErr):
            agent._create_message(c, max_tokens=10, messages=[])
        assert agent.current_model() == agent.MODEL

    def test_model_ids_are_the_documented_ones(self):
        agent = self._fresh()
        assert agent.MODEL == "claude-fable-5"
        assert agent.FALLBACK_MODEL == "claude-opus-4-8"

    # -- claude-cli backend: kredi bitişi 429 + metin olarak gelir --------------
    # Aşağıdaki gövde server.err.log'dan ALINMIŞTIR. Fallback bu şekli tanımadığı
    # için sahada hiç tetiklenmedi: ajan 15 adım boyunca LLM yerine builtin random
    # strateji üretti (hepsi Skor=-inf). Üstteki testler kaçırdı çünkü hepsi
    # "credit balance is too low" gibi ZATEN eşleşen sentetik metin kullanıyordu.
    _CLI_SPEND_LIMIT_ENVELOPE = {
        "type": "result",
        "subtype": "success",  # (evet: hata gövdesinde bile "success")
        "is_error": True,
        "api_error_status": 429,
        "result": (
            "You've hit your monthly spend limit. Run /usage-credits to manage "
            "your limit and keep using Fable 5 or switch models to continue this chat."
        ),
    }

    def test_cli_monthly_spend_limit_falls_back(self):
        agent = self._fresh()
        env = self._CLI_SPEND_LIMIT_ENVELOPE
        exc = agent._CLIError(
            "claude CLI exited 1: ...",
            status=env["api_error_status"],
            message=env["result"],
        )
        c = _FakeLLMClient(exc)
        assert agent._create_message(c, max_tokens=10, messages=[]) == (
            f"OK({agent.FALLBACK_MODEL})"
        )
        assert c.models == [agent.MODEL, agent.FALLBACK_MODEL]
        assert agent.current_model() == agent.FALLBACK_MODEL

    def test_cli_transient_429_does_not_fall_back(self):
        # Aynı 429 kodu, kalıcı OLMAYAN gövde → model değişmemeli.
        agent = self._fresh()
        exc = agent._CLIError(
            "claude CLI exited 1: ...",
            status=429,
            message="Rate limit exceeded. Please try again later.",
        )
        c = _FakeLLMClient(exc)
        with pytest.raises(agent._CLIError):
            agent._create_message(c, max_tokens=10, messages=[])
        assert c.models == [agent.MODEL]  # tek deneme, fallback YOK
        assert agent.current_model() == agent.MODEL

    def test_cli_error_preserves_message_when_raw_text_is_truncated(self):
        # Ham stdout 500 karakterde kırpılıyor; sinyal `message` alanında durmalı,
        # yoksa uzun gövdelerde kalıcı/geçici ayrımı kaybolur.
        agent = self._fresh()
        # padding ÖNDE: kırpma `result`a ulaşsın diye onu 500. karakterin ötesine iter.
        env = {"padding": "x" * 4000, **self._CLI_SPEND_LIMIT_ENVELOPE}
        exc = agent._CLIError(
            f"claude CLI exited 1: {json.dumps(env)[:500]}",
            status=429,
            message=env["result"],
        )
        assert "monthly spend limit" not in str(exc)  # kırpma sinyali yedi
        assert agent._is_credit_exhausted(exc)  # ama tipli alan kurtarıyor


# ---------------------------------------------------------------------------
# Windows konsol penceresi: alt-süreçler (claude CLI, bash/gunzip/awk) her
# çağrıda terminal açıp kapatıyordu. CREATE_NO_WINDOW konsolu hiç yaratmaz
# (startupinfo/windowsHide bu makinede WT tarafından yok sayılıyor).
# ---------------------------------------------------------------------------
class TestNoConsoleWindow:
    def test_flag_matches_platform(self):
        import os
        import subprocess

        from app_constants import NO_WINDOW_FLAGS

        if os.name == "nt":
            assert NO_WINDOW_FLAGS == subprocess.CREATE_NO_WINDOW
        else:
            # POSIX: subprocess sıfır-olmayan creationflags'i REDDEDER.
            assert NO_WINDOW_FLAGS == 0

    def test_every_subprocess_call_hides_console(self):
        """agent.py/data.py'deki her subprocess.run/Popen creationflags geçmeli.

        Kaynak-seviyesi guard: bayrağı unutan YENİ bir çağrı eklenirse kırılır
        (kullanıcıya terminal penceresi olarak geri döner, sessiz regresyon).
        """
        import ast as _ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        offenders = []
        for name in ("agent.py", "data.py"):
            tree = _ast.parse((root / name).read_text(encoding="utf-8"))
            for node in _ast.walk(tree):
                if not isinstance(node, _ast.Call):
                    continue
                fn = node.func
                if not (
                    isinstance(fn, _ast.Attribute)
                    and isinstance(fn.value, _ast.Name)
                    and fn.value.id == "subprocess"
                    and fn.attr in ("run", "Popen", "call", "check_output")
                ):
                    continue
                if not any(k.arg == "creationflags" for k in node.keywords):
                    offenders.append(f"{name}:{node.lineno} subprocess.{fn.attr}")
        assert not offenders, (
            "creationflags=NO_WINDOW_FLAGS eksik (Windows'ta konsol penceresi "
            f"açar): {offenders}"
        )
