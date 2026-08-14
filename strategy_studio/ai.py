"""AI layer (STUDIO_SPEC Phase 5).

INTEGRATION POINT for nautilus_web_app: replace/extend HttpAnthropicClient
with the LLM client from your existing optimization loop (same prompts style,
same plumbing). Everything else — the Suggestion contract, guardrails, and
apply logic — is engine-agnostic and should be reused as-is so human edits
and AI edits go through the same mutation/validation path.

Wiki References
---------------
Bkz: [[strategy_studio]]
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from .backtest import BacktestAdapter, BacktestMetrics
from .compiler import CompileError, compile_strategy
from .mutations import (
    MutationError,
    add_rule,
    delete_rule,
    update_risk,
    update_rule_param,
)
from .schema import StrategyDefinition

# ── contract ─────────────────────────────────────────────────────


class Expected(BaseModel):
    dsr_delta: float | None = None


class Suggestion(BaseModel):
    kind: Literal["add_rule", "modify_param", "remove_rule", "modify_risk"]
    block: Literal["entry", "exit", "risk", "regime"]
    diff: dict = Field(default_factory=dict)
    rationale: str
    expected: Expected | None = None


class SuggestionFailure(Exception):
    pass


# ── LLM clients ──────────────────────────────────────────────────


class LLMClient(Protocol):
    def complete(self, prompt: str) -> str: ...


class HttpAnthropicClient:
    """Minimal Anthropic Messages API client (INTEGRATION POINT — swap for
    the client your existing LLM loop already uses)."""

    # Not wired to agent.py's cross-run _LEARNED_MAX_TOKENS: that mechanism is
    # keyed to the AUTO loop's rapid repeated calls to the SAME (model, purpose)
    # and reaches into agent.py's private module state — this client is an
    # explicit "INTEGRATION POINT" meant to stay decoupled from agent.py's
    # internals (see class docstring), and Studio suggestion calls are
    # occasional/interactive rather than bursty, so per-call detection is
    # enough: no cross-call memory needed.
    _DEFAULT_MODEL = "claude-sonnet-4-6"
    _DEFAULT_MAX_TOKENS = 4096
    _DEFAULT_BASE_URL = "https://api.anthropic.com"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        max_tokens: int | None = None,
        base_url: str | None = None,
    ):
        self.model = model or os.environ.get(
            "NAUTILUS_STUDIO_LLM_MODEL", self._DEFAULT_MODEL
        )
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.max_tokens = max_tokens or int(
            os.environ.get("NAUTILUS_STUDIO_LLM_MAX_TOKENS", self._DEFAULT_MAX_TOKENS)
        )
        # DeepR 2026-08-11 [YÜKSEK]: uç, uygulamanın diğer LLM yolu
        # (llm_dispatch._build_client) ile AYNI değişkenden okunur. Eskiden bu
        # istemci ucu sabit resmi API'ydi, öbürü ise varsayılan olarak yerel bir
        # proxy'ye gidiyordu: aynı ANTHROPIC_API_KEY iki farklı uca gönderiliyor,
        # bir proxy anahtarı üçüncü tarafa gidebiliyordu. Tek kaynak: boşsa resmi
        # uç, doluysa o proxy — iki entegrasyon da aynı yere konuşur.
        self.base_url = (
            base_url
            or os.environ.get("ANTHROPIC_BASE_URL", "").strip()
            or self._DEFAULT_BASE_URL
        ).rstrip("/")

    # Retry only transient failures (429 / 5xx / timeout / connection) — a
    # single one of these used to kill the whole AUTO loop for this client
    # (agent.py's own LLM path already retries the same failure classes;
    # this one didn't, so a rate-limit blip failed the run for no reason
    # related to the actual suggestion).
    _RETRY_WAITS = (1.0, 2.0, 4.0)
    _TRUNCATION_RETRY_SCALE = 2
    _TRUNCATION_RETRY_CAP = 16000

    def complete(self, prompt: str) -> str:
        text, stop_reason = self._complete_at(prompt, self.max_tokens)
        if stop_reason != "max_tokens":
            return text
        bigger = min(
            self.max_tokens * self._TRUNCATION_RETRY_SCALE, self._TRUNCATION_RETRY_CAP
        )
        if bigger <= self.max_tokens:
            return text  # already at the cap — nothing more to try
        logging.warning(
            "HttpAnthropicClient.complete: response truncated at max_tokens=%d — "
            "retrying once with %d",
            self.max_tokens,
            bigger,
        )
        text, _ = self._complete_at(prompt, bigger)
        return text

    def _record_usage(self, usage) -> None:
        """Bu çağrının token'larını ortak deftere yaz.

        DeepR 2026-08-11 [ORTA]: Studio'nun kendi LLM istemcisi tek choke
        point'i (`llm_dispatch`) baypas ediyordu ve yanıttaki ``usage`` bloğunu
        atıyordu. Sonuç muhasebe değil, KARAR sorunu: `/tokens` rozeti ve AUTO'nun
        bütçe tavanı aynı defterden okuyor, yani Studio'da harcanan her token
        "hiç harcanmamış" görünüyordu. Uç noktayı birleştirmek (aynı DeepR turu,
        [YÜKSEK]) yarısıydı; ölçümü birleştirmek diğer yarısı.

        Yolun tamamını `llm_dispatch`'e taşımak değil, defteri paylaşmak seçildi:
        bu sınıf docstring'inde bilinçli bir "INTEGRATION POINT" ve
        `agent.py`'nin private durumundan uzak durması isteniyor. Defter zaten
        bağımsız bir modül; bağlanması gereken tek şey o.

        Kayıt asla çağrıyı düşürmez: muhasebe, sonucun kendisinden daha az
        önemli (aynı duruş `llm_dispatch._ledger_record_usage`'da da var).
        """
        if not usage:
            return
        try:
            import token_ledger

            token_ledger.record(self.model, usage, "studio:suggest")
        except Exception:
            logging.getLogger(__name__).debug(
                "studio token ledger write failed", exc_info=True
            )

    def _complete_at(self, prompt: str, max_tokens: int) -> tuple[str, str | None]:
        """One logical call (with transient-failure retry) → (text, stop_reason)."""
        import httpx  # local import: optional dependency at runtime

        if not self.api_key:
            raise SuggestionFailure("ANTHROPIC_API_KEY is not set")
        last_exc: Exception | None = None
        for attempt, planned_wait in enumerate((*self._RETRY_WAITS, None)):
            try:
                r = httpx.post(
                    f"{self.base_url}/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": max_tokens,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=60,
                )
                r.raise_for_status()
                body = r.json()
                self._record_usage(body.get("usage"))
                text = "".join(
                    b.get("text", "")
                    for b in body.get("content", [])
                    if b.get("type") == "text"
                )
                return text, body.get("stop_reason")
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_exc = e
                transient = True
            except httpx.HTTPStatusError as e:
                last_exc = e
                transient = (
                    e.response.status_code == 429 or e.response.status_code >= 500
                )

            if not transient or planned_wait is None:
                raise self._endpoint_error(last_exc)
            wait = planned_wait
            if isinstance(last_exc, httpx.HTTPStatusError):
                retry_after = last_exc.response.headers.get("retry-after")
                try:
                    if retry_after:
                        wait = float(retry_after)
                except (TypeError, ValueError):
                    pass
            logging.warning(
                "HttpAnthropicClient.complete transient error (%s) — retrying in "
                "%.0fs (%d/%d)",
                type(last_exc).__name__,
                wait,
                attempt + 1,
                len(self._RETRY_WAITS),
            )
            time.sleep(wait)
        raise self._endpoint_error(  # pragma: no cover - loop always exits above
            last_exc
        )

    def _endpoint_error(self, exc: Exception) -> Exception:
        """Ulaşılamayan ÖZEL uç, adıyla anılsın — jenerik bağlantı hatası değil.

        Resmi uçta bir `ConnectError` "internet yok" demektir ve kullanıcının
        yapacağı bir şey yoktur; ANTHROPIC_BASE_URL ile bir proxy verilmişken
        aynı hata neredeyse her zaman "o proxy koşmuyor" demektir ve yapılacak
        iş bellidir. Panelde görünen metin bunu söylesin diye çevriliyor.
        """
        import httpx

        if self.base_url == self._DEFAULT_BASE_URL or not isinstance(
            exc, httpx.ConnectError
        ):
            return exc
        return SuggestionFailure(
            f"LLM proxy yanıt vermiyor: {self.base_url} (ANTHROPIC_BASE_URL). "
            "Proxy'yi başlat ya da bu değişkeni kaldır — kaldırılınca çağrılar "
            f"resmi uca ({self._DEFAULT_BASE_URL}) gider."
        )


class MockLLMClient:
    """Deterministic client for tests: pops canned responses in order."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            raise SuggestionFailure("mock exhausted")
        return self.responses.pop(0)


# ── prompt & parsing ─────────────────────────────────────────────

_PROMPT = """You are improving an algorithmic trading strategy. Respond with
ONLY a JSON object (no markdown fences, no prose) matching exactly:

{{"kind": "add_rule|modify_param|remove_rule|modify_risk",
  "block": "entry|exit|risk|regime",
  "diff": {{...}},
  "rationale": "one short paragraph",
  "expected": {{"dsr_delta": 0.0}}}}

diff shapes by kind:
- add_rule:     {{"indicator": "<registry key>", "as_filter": false}}
- modify_param: {{"owner": "<rule_id or 'risk'>", "param": "<name>", "value": <number>}}
- remove_rule:  {{"rule_id": "<rule_id>"}}
- modify_risk:  {{"name": "<risk field>", "value": <number>}}

Available indicators: {indicators}
{scope}
Current strategy JSON:
{strategy}

Latest backtest metrics (out-of-sample):
{metrics}

Previously rejected suggestions (do not repeat):
{rejected}

User request: {ask}
Propose exactly ONE change that most improves the objective ({objective})."""


def build_prompt(
    defn: StrategyDefinition,
    metrics: BacktestMetrics | None,
    ask: str,
    scope: str | None,
    rejected: list[str],
    indicators: list[str],
) -> str:
    return _PROMPT.format(
        indicators=", ".join(indicators),
        scope=f"Restrict the change to the '{scope}' block.\n" if scope else "",
        strategy=defn.model_dump_json(by_alias=True),
        metrics=metrics.to_json() if metrics else "none yet",
        rejected="\n".join(f"- {r}" for r in rejected) or "none",
        ask=ask or "improve the strategy",
        objective=defn.walkforward.objective,
    )


def parse_suggestion(client: LLMClient, prompt: str) -> Suggestion:
    """Parse with a single retry that feeds the validation error back."""
    raw = client.complete(prompt)
    for attempt in range(2):
        try:
            cleaned = (
                raw.strip()
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )
            return Suggestion.model_validate(json.loads(cleaned))
        except (json.JSONDecodeError, ValidationError) as e:
            if attempt == 1:
                raise SuggestionFailure(f"LLM returned invalid suggestion: {e}") from e
            raw = client.complete(
                prompt + f"\n\nYour previous reply was invalid ({e}). "
                "Reply with ONLY the corrected JSON object."
            )
    raise SuggestionFailure("unreachable")


# ── application & evaluation ─────────────────────────────────────


def apply_suggestion(defn: StrategyDefinition, s: Suggestion) -> None:
    """Mutates defn in place. Raises MutationError on invalid diffs."""
    d = s.diff
    if s.kind == "add_rule":
        rule = add_rule(
            defn, s.block, d.get("indicator", ""), bool(d.get("as_filter", False))
        )
        for name, value in (d.get("params") or {}).items():
            update_rule_param(defn, rule.id, name, str(value))
        if "target" in d and d["target"] is not None:
            update_rule_param(defn, rule.id, "target", str(d["target"]))
    elif s.kind == "modify_param":
        owner = d.get("owner", "")
        if owner == "risk":
            update_risk(defn, d.get("param", ""), str(d.get("value")))
        else:
            update_rule_param(defn, owner, d.get("param", ""), str(d.get("value")))
    elif s.kind == "remove_rule":
        delete_rule(defn, d.get("rule_id", ""))
    elif s.kind == "modify_risk":
        update_risk(defn, d.get("name", ""), str(d.get("value")))


class GuardrailReject(Exception):
    pass


def evaluate_trial(
    defn: StrategyDefinition,
    s: Suggestion,
    adapter: BacktestAdapter,
    baseline: BacktestMetrics | None,
    min_trades: int,
) -> tuple[BacktestMetrics, StrategyDefinition]:
    """Apply to a copy, compile, trial-backtest, enforce hard guardrails.
    Guardrails are non-negotiable and enforced here, server-side:
    compile failure, trades below the floor, or a worse OOS objective than
    baseline are rejected regardless of apply mode."""
    trial = defn.model_copy(deep=True)
    try:
        apply_suggestion(trial, s)
        metrics = adapter.run(compile_strategy(trial))
    except (MutationError, CompileError) as e:
        raise GuardrailReject(f"invalid change: {e}") from e
    if metrics.trades < min_trades:
        raise GuardrailReject(f"trades {metrics.trades} below guardrail {min_trades}")
    if baseline is not None and _objective(trial, metrics) < _objective(
        trial, baseline
    ):
        raise GuardrailReject(
            f"OOS {trial.walkforward.objective} worsened: "
            f"{_objective(trial, baseline):.2f} -> "
            f"{_objective(trial, metrics):.2f}"
        )
    return metrics, trial


def _objective(defn: StrategyDefinition, m: BacktestMetrics) -> float:
    if defn.walkforward.objective == "sharpe":
        return m.sharpe
    if defn.walkforward.objective == "max_dd":
        return m.max_dd_pct
    return m.dsr


def improved(
    defn: StrategyDefinition, trial: BacktestMetrics, baseline: BacktestMetrics | None
) -> bool:
    if baseline is None:
        return True
    return _objective(defn, trial) > _objective(defn, baseline)
