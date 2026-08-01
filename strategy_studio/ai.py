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
import os
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

    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    def complete(self, prompt: str) -> str:
        import httpx  # local import: optional dependency at runtime

        if not self.api_key:
            raise SuggestionFailure("ANTHROPIC_API_KEY is not set")
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            json={
                "model": self.model,
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        return "".join(
            b.get("text", "")
            for b in r.json().get("content", [])
            if b.get("type") == "text"
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
