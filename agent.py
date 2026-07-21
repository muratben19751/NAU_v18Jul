"""Claude Fable 5 strategy-parameter proposer.

Returns a dict:
  {"strategy": "ma_crossover", "params": {"fast": 12, "slow": 34}, "rationale": "..."}

Wiki References
---------------
_(app-specific — outside wiki scope)_

App-specific; outside wiki scope (LLM parameter proposer, not a Nautilus concept).
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from anthropic import Anthropic

from app_constants import NO_WINDOW_FLAGS
from strategies import STRATEGY_PARAM_SPEC

MODEL = os.environ.get("NAUTILUS_LLM_MODEL", "claude-fable-5")
# Model to automatically fall back to if Fable credit/quota runs out. Opus 4.8
# offers the same 1M context and API surface (adaptive thinking, no sampling
# params), at a lower per-token price → call bodies run unchanged.
FALLBACK_MODEL = os.environ.get("NAUTILUS_LLM_FALLBACK_MODEL", "claude-opus-4-8")

# Once credit is exhausted, lock to FALLBACK_MODEL for the process lifetime (so we
# don't get a 403 on every call). None = not yet fallen back, MODEL is in use.
_active_model: str | None = None
_model_lock = threading.Lock()

_client: Anthropic | _ClaudeCLIClient | None = None
_client_lock = threading.Lock()


def current_model() -> str:
    """The model currently in use (FALLBACK_MODEL if fallback has kicked in)."""
    return _active_model or MODEL


_CREDIT_EXHAUSTED_SIGNALS = (
    "credit balance is too low",  # API: 400
    "billing_error",
    "insufficient credit",
    "monthly spend limit",  # claude-cli: 429 but PERMANENT
)


def _is_credit_exhausted(exc: Exception) -> bool:
    """Is this credit/quota exhaustion? (NOT a rate-limit or transient error)

    The two backends emit two distinct signals:

    - API: HTTP 403 + ``error.type == "billing_error"``. The SDK's `.type` field
      separates billing_error from permission_error (both are 403), so a typed
      field is used instead of string matching.
    - claude-cli (subscription): does not produce a typed exception and reports
      the spend limit with the SAME 429 code as a transient rate-limit → the
      distinction can only be made from the message text ("monthly spend limit",
      which advises switching models as the remedy).

    That's why a bare 429 is DELIBERATELY excluded: it is transient, retried with
    backoff — permanently switching the model would be wrong. Only the permanent
    expressions above match.
    """
    if getattr(exc, "type", None) == "billing_error":
        return True
    msg = f"{getattr(exc, 'message', '')} {exc}".lower()
    return any(s in msg for s in _CREDIT_EXHAUSTED_SIGNALS)


def _create_message(client, **kwargs):
    """messages.create + automatic Fable→Opus fallback on credit exhaustion.

    The model kwarg is added HERE; callers do not pass a model. On a credit
    error the active model is permanently switched to FALLBACK_MODEL and the
    request is retried once (the request body is passed as-is — both models
    share the same API surface).
    """
    global _active_model
    model = current_model()
    try:
        return client.messages.create(model=model, **kwargs)
    except Exception as e:
        if model == FALLBACK_MODEL or not _is_credit_exhausted(e):
            raise
        with _model_lock:
            _active_model = FALLBACK_MODEL
        logging.warning(
            "%s credit exhausted (%s) — permanently falling back to %s",
            model,
            type(e).__name__,
            FALLBACK_MODEL,
        )
        return client.messages.create(model=FALLBACK_MODEL, **kwargs)


# ── Web research (DuckDuckGo, no API key required) ─────────────────────────


def _ddg_search(query: str, max_results: int = 5) -> list[dict]:
    """Web search via the ddgs library. Returns [] on error."""
    try:
        from ddgs import DDGS

        results = DDGS().text(query, max_results=max_results)
        return [
            {"title": r.get("title", ""), "snippet": r.get("body", "")}
            for r in (results or [])
        ]
    except Exception:
        return []


def web_research_strategies(
    hint: str = "", n: int = 5, market: str | None = None
) -> str:
    """Searches the web for successful strategies; returns the found ideas as text.
    Returns "" if the result is empty (caller applies a fallback).

    If ``market`` is given (e.g. "US equity QQQ.NASDAQ (1-DAY bars, ...)") the
    queries target that instrument instead of crypto; if None the existing crypto
    queries are kept.
    """
    queries = []
    if market:
        # "US equity QQQ.NASDAQ (1-DAY bars, ...)" → short form for the search
        market_q = market.split("(")[0].strip()
        if hint.strip():
            queries.append(f"{hint.strip()} {market_q} trading strategy backtest")
        queries += [
            f"{market_q} profitable trading strategy backtest",
            "best US stock swing trading strategy indicators backtest results",
        ]
    else:
        if hint.strip():
            queries.append(
                f"{hint.strip()} crypto trading strategy backtest profitable"
            )
        queries += [
            "best crypto intraday trading strategy 2024 backtest results",
            "BTCUSDT profitable trading strategy indicators confluence",
        ]

    all_snippets: list[str] = []
    for q in queries[:2]:  # 2 queries are enough, for speed
        for r in _ddg_search(q, max_results=4):
            title = r.get("title", "").strip()
            snip = r.get("snippet", "").strip()
            if title or snip:
                all_snippets.append(f"- {title}: {snip}")

    if not all_snippets:
        return ""
    return (
        "WEB RESEARCH — Successful strategy ideas (draw inspiration from these hints, "
        "but implement them with the block types in the BLOCK CATALOG):\n"
        + "\n".join(all_snippets[:8])
    )


# ── Claude Code CLI backend (subscription / OAuth — no ANTHROPIC_API_KEY needed) ─
#
# `claude -p` uses Claude Code's existing session (Pro/Max subscription) in
# headless mode. It mimics the minimal messages.create surface the app uses:
# a single user message + optional system prompt → a text-block response.


class _CLITextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _CLIUsage:
    def __init__(self, usage: dict) -> None:
        self.input_tokens = int(usage.get("input_tokens", 0) or 0)
        self.output_tokens = int(usage.get("output_tokens", 0) or 0)
        self.cache_read_input_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
        self.cache_creation_input_tokens = int(
            usage.get("cache_creation_input_tokens", 0) or 0
        )


class _CLIResponse:
    def __init__(self, text: str, usage: dict) -> None:
        self.content = [_CLITextBlock(text)]
        self.usage = _CLIUsage(usage)


class _CLIError(RuntimeError):
    """claude CLI error; preserves typed fields if a JSON body is present.

    ``message`` (the envelope's ``result``) is kept separate from the raw text:
    the expression that separates a permanent spend limit from a transient
    rate-limit lives there, and it can be lost when the raw text is truncated →
    see _is_credit_exhausted.
    """

    def __init__(self, text: str, status: int | None = None, message: str = "") -> None:
        super().__init__(text)
        self.status = status
        self.message = message


class _ClaudeCLIMessages:
    def __init__(self, cli_path: str) -> None:
        self._cli = cli_path

    def create(
        self,
        *,
        model: str,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 0,  # no equivalent in the CLI; prompts already request short answers
        **_ignored: Any,
    ) -> _CLIResponse:
        # Single-turn calls (every existing proposer) join user content verbatim
        # — unchanged behaviour. Multi-turn chat prefixes each turn with a
        # speaker label so the model sees its own prior replies (the CLI runs
        # with --no-session-persistence, so conversation state lives server-side
        # and the full history is re-sent as the prompt each call).
        prompt = "\n\n".join(
            (
                m["content"]
                if len(messages) == 1
                else f"{'Kullanıcı' if m['role'] == 'user' else 'Asistan'}: {m['content']}"
            )
            for m in messages
            if isinstance(m.get("content"), str)
            and m.get("role") in ("user", "assistant")
        )
        cmd = [
            self._cli,
            "-p",
            "--output-format",
            "json",
            "--model",
            model,
            "--tools",
            "",  # all tools off: a pure LLM call
            "--no-session-persistence",
            "--strict-mcp-config",
        ]

        # The system prompt is passed via a file: on Windows the command line
        # over the .cmd shim is limited to ~8K chars; prompts containing the
        # catalog can exceed that.
        sys_file: str | None = None
        try:
            if system:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".txt", delete=False, encoding="utf-8"
                ) as f:
                    f.write(system)
                    sys_file = f.name
                cmd += ["--system-prompt-file", sys_file]

            # Clear the API key/base URL from the env so the subscription (OAuth)
            # is used; make cwd a neutral directory so the project CLAUDE.md/settings
            # are not loaded.
            env = os.environ.copy()
            for var in (
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_BASE_URL",
            ):
                env.pop(var, None)

            timeout = float(os.environ.get("NAUTILUS_CLI_TIMEOUT", "300"))
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=tempfile.gettempdir(),
                timeout=timeout,
                # Windows: don't open/close a console window on every LLM call.
                creationflags=NO_WINDOW_FLAGS,
            )
        finally:
            if sys_file:
                try:
                    os.unlink(sys_file)
                except OSError:
                    pass

        # The error body also comes back as JSON (even when exit≠0) and the real
        # cause is in its ``result``/``api_error_status`` fields — truncating the
        # raw stdout and embedding it into a string lost this signal.
        envelope: dict[str, Any] = {}
        try:
            envelope = json.loads(proc.stdout) or {}
        except (json.JSONDecodeError, TypeError):
            pass

        if proc.returncode != 0 or envelope.get("is_error"):
            message = str(envelope.get("result") or "")
            status = envelope.get("api_error_status")
            detail = message or (proc.stderr or proc.stdout or "").strip()
            raise _CLIError(
                f"claude CLI exited {proc.returncode}: {detail[:500]}",
                status=status if isinstance(status, int) else None,
                message=message,
            )
        if envelope.get("subtype") != "success":
            # If envelope is empty (stdout not JSON) show the raw output, not "None".
            detail = str(envelope.get("result") or (proc.stdout or "").strip())
            raise _CLIError(
                f"claude CLI error ({envelope.get('subtype')}): {detail[:500]}"
            )
        return _CLIResponse(envelope.get("result") or "", envelope.get("usage") or {})


class _ClaudeCLIClient:
    def __init__(self, cli_path: str) -> None:
        self.messages = _ClaudeCLIMessages(cli_path)


def _find_claude_cli() -> str | None:
    override = os.environ.get("NAUTILUS_CLAUDE_CLI", "").strip()
    if override:
        return override if Path(override).exists() else None
    return shutil.which("claude")


def _build_client() -> Anthropic | _ClaudeCLIClient:
    """Backend selection (NAUTILUS_LLM_BACKEND env var):

    - "api":        anthropic SDK — ANTHROPIC_API_KEY / ~/.nautilus_proxy_key required
    - "claude-cli": Claude Code CLI (`claude -p`) — subscription (OAuth), no key needed
    - "auto" (default): API if a key exists, otherwise the claude CLI
    """
    backend = os.environ.get("NAUTILUS_LLM_BACKEND", "auto").strip().lower()

    # Hyperspace AI proxy takes priority; falls back to direct Anthropic.
    # The proxy key must be set via ANTHROPIC_API_KEY env var or
    # ~/.nautilus_proxy_key file — never hardcoded.
    proxy_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not proxy_key:
        key_file = Path.home() / ".nautilus_proxy_key"
        if key_file.exists():
            proxy_key = key_file.read_text().strip()

    if backend in ("api", "auto") and proxy_key:
        proxy_url = os.environ.get("ANTHROPIC_BASE_URL", "http://localhost:6655")
        logging.info("LLM backend: anthropic SDK (%s)", proxy_url)
        return Anthropic(base_url=proxy_url, api_key=proxy_key)
    if backend == "api":
        raise RuntimeError(
            "NAUTILUS_LLM_BACKEND=api but ANTHROPIC_API_KEY is not set. "
            "Set it as an environment variable or write it to ~/.nautilus_proxy_key"
        )

    cli = _find_claude_cli()
    if cli:
        logging.info("LLM backend: claude CLI / subscription (%s)", cli)
        return _ClaudeCLIClient(cli)
    if backend == "claude-cli":
        raise RuntimeError(
            "NAUTILUS_LLM_BACKEND=claude-cli but the `claude` CLI was not found on PATH. "
            "Install Claude Code and sign in (subscription), or set NAUTILUS_CLAUDE_CLI."
        )
    raise RuntimeError(
        "No LLM access: ANTHROPIC_API_KEY is not set and the `claude` CLI was not found. "
        "Either set an API key (env var or ~/.nautilus_proxy_key) or install Claude Code "
        "and sign in with your subscription."
    )


def _get_client() -> Anthropic | _ClaudeCLIClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = _build_client()
    return _client


SYSTEM_PROMPT = f"""You are a quantitative trading research agent.

You propose numeric hyperparameters for one of the following pre-implemented strategies. You never write code. You only choose a strategy name and a JSON object of parameters.

Available strategies and parameter specs:
{json.dumps(STRATEGY_PARAM_SPEC, indent=2)}

Constraints:
- For ma_crossover: slow > fast.
- Values must lie within the given ranges.
- Try different parameters and strategies over time — do not repeat past proposals verbatim.
- Use the history of past iterations (win rates, PnL, drawdown) to guide your next proposal.

Return ONLY a JSON object with keys "strategy", "params", "rationale". Nothing else. No markdown, no code fences."""


def _summarize_history(history: list[Any]) -> str:
    if not history:
        return "No prior iterations."
    lines = []
    for r in history[-10:]:
        m = r.metrics if r.error is None else {}
        lines.append(
            f"- id={r.id} strat={r.strategy} params={r.params} "
            f"pnl={m.get('pnl', 'n/a')} sharpe={m.get('sharpe', 'n/a')} "
            f"trades={m.get('n_trades', 'n/a')} err={r.error}"
        )
    return "\n".join(lines)


def _fallback_proposal() -> dict:
    strat = random.choice(list(STRATEGY_PARAM_SPEC.keys()))
    if strat == "ma_crossover":
        fast = random.randint(5, 20)
        slow = random.randint(fast + 5, min(200, fast + 60))
        return {
            "strategy": strat,
            "params": {"fast": fast, "slow": slow},
            "rationale": "fallback random (agent unavailable)",
        }
    else:
        return {
            "strategy": "rsi_mean_reversion",
            "params": {
                "rsi_period": random.randint(7, 21),
                "oversold": round(random.uniform(20.0, 35.0), 1),
                "overbought": round(random.uniform(65.0, 80.0), 1),
            },
            "rationale": "fallback random (agent unavailable)",
        }


def propose_strategy(history: list[Any]) -> dict:
    try:
        client = _get_client()
    except Exception:
        return _fallback_proposal()

    user_msg = f"""Past iterations:
{_summarize_history(history)}

Propose the next strategy + parameters as JSON."""

    try:
        resp = _create_message(
            client,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        ).strip()

        data = json.loads(_extract_json_object(text))
        if "strategy" not in data or "params" not in data:
            raise ValueError("missing keys")
        if data["strategy"] not in STRATEGY_PARAM_SPEC:
            raise ValueError(f"unknown strategy: {data['strategy']}")
        data.setdefault("rationale", "")
        return data
    except Exception as e:
        logging.warning("propose_strategy error: %s", e, exc_info=True)
        fb = _fallback_proposal()
        fb["rationale"] = f"fallback ({type(e).__name__})"
        return fb


# Default market context — if no market parameter is given, the Bybit crypto
# expression is preserved byte-for-byte (existing behavior unchanged).
_DEFAULT_MARKET_CONTEXT = (
    "crypto trading strategies on Bybit (BTCUSDT USDT perp, 1-minute bars)"
)

COMPOSED_SYSTEM_PROMPT = """You are a quantitative trading research agent designing {market_context}.

You must return a JSON object describing a complete "composed strategy" for the Nautilus backtest engine:

{
  "name": "short human-readable name (2-4 words, English or Turkish)",
  "description": "one-sentence description of the trading thesis",
  "blocks": [
    {"type": "<block_type>", "role": "entry" | "exit", "params": { ... }},
    ...
  ],
  "strategy_options": {
    "entry_logic": "OR" | "AND",
    "exit_logic": "OR" | "AND",
    "order_type": "market" | "limit",
    "limit_offset_bps": <float 0..50>,
    "use_bracket": true | false,
    "sl_type": "percent" | "atr",
    "sl_value": <float>,
    "tp_type": "percent" | "atr" | "off",
    "tp_value": <float>,
    "atr_period": <int 5..100>,
    "allow_short": true | false,
    "trade_size_mode": "fixed" | "fixed_usdt" | "percent_equity" | "atr_target",
    "trade_size_percent": <float 0.5..25>,
    "trade_size_atr_risk": <float 0.1..5>
  }
}

RULES:
- The list must contain AT LEAST one entry block and AT LEAST one exit block.
- 2 to 4 blocks total is usually best.
- entry_logic="OR" fires on any entry-block hit; "AND" requires ALL entry blocks to fire the same bar (strict, fewer trades).
- ⚠️ CRITICAL: With 2 or more entry blocks, ALWAYS use entry_logic="OR". Using "AND" across 3 blocks fires on less than 0.5% of bars and almost always produces ZERO trades — the strategy is useless. Only use "AND" with exactly 2 blocks when you have a specific confluence reason. Default is "OR".
- exit_logic works the same.
- order_type="limit" adds a small offset via limit_offset_bps (0-50 bps typical).
- use_bracket=true attaches an atomic SL (+ optional TP) with the entry (submitted as a Nautilus OrderList). sl_type/tp_type "percent" uses % of entry price; "atr" multiplies ATR by sl_value/tp_value.
- allow_short=true lets entry blocks with direction=down (or cross=above / sign=negative / side=upper) open SHORT positions via SELL. The backend switches to MARGIN account when this is enabled.
- trade_size_mode="fixed" uses the blocks-level trade_size (BTC). "percent_equity" sizes as %equity/price. "atr_target" targets a fixed % risk per trade using ATR distance.
- Use ONLY the block types and parameter names defined in the catalog below.
- Every param must be within its declared range. Enum params must use one of the given options.
- For ma_cross / ema_cross / macd_cross: slow > fast strictly.
- atr_stop is EXIT-only.
- Prefer diverse ideas: do NOT copy an existing catalog strategy verbatim; look at history and try something with a plausibly different behavior (different lookbacks, different combinations, mix indicator + non-indicator blocks, try shorts + bracket occasionally).
- IMPORTANT: If recent history shows mostly EMA/RSI-based strategies, deliberately choose a DIFFERENT indicator family this time: Bollinger Bands, Price Breakout, Momentum, MACD, or ATR-based approaches. Rotate through all available block types across iterations.
- Return ONLY the JSON. No markdown, no code fences, no explanation.

BLOCK CATALOG:
{catalog}

Sensible defaults if you have no strong reason to override: entry_logic="OR" (STRONGLY PREFERRED — AND only with exactly 2 blocks and a deliberate confluence reason), exit_logic="OR", order_type="market", use_bracket=false, allow_short=false, trade_size_mode="fixed".
"""


def _catalog_summary() -> str:
    from composer import BLOCK_CATALOG

    out = []
    for k, meta in BLOCK_CATALOG.items():
        # Skip lab-generated temporary blocks — they are EMA/RSI variants
        # that Claude itself produced; showing them biases proposals toward
        # the same family of indicators.
        if k.startswith("lab_entry_") or k.startswith("lab_exit_"):
            continue
        params = []
        for pname, pspec in meta["params"].items():
            if pspec["type"] == "enum":
                params.append(
                    f"{pname}: enum {pspec['options']} (default {pspec['default']})"
                )
            else:
                params.append(
                    f"{pname}: {pspec['type']} [{pspec['min']}..{pspec['max']}] (default {pspec['default']})"
                )
        out.append(f"- {k} ({meta['label']}): {'; '.join(params)}")
    return "\n".join(out)


def _summarize_composed_history(history: list[Any], catalog: list[Any]) -> str:
    lines = []
    if catalog:
        lines.append("EXISTING SAVED STRATEGIES (avoid duplicating these):")
        for s in catalog[-10:]:
            block_desc = ", ".join(
                f"{b.type}/{b.role}({','.join(f'{k}={v}' for k, v in b.params.items())})"
                for b in s.blocks
            )
            lines.append(f"  · {s.name}: [{block_desc}]")
    if history:
        lines.append("\nRECENT BACKTEST RESULTS (learn from these):")
        for r in history[-8:]:
            m = (r.metrics or {}) if r.error is None else {}
            lines.append(
                f"  · {r.strategy} pnl={m.get('pnl', 'n/a')} sharpe={m.get('sharpe', 'n/a')} "
                f"trades={m.get('n_trades', 'n/a')} winrate={m.get('win_rate', 'n/a')} err={r.error}"
            )
        # Show which block types have already been tried so Claude avoids repeating them
        tried_blocks: set[str] = set()
        for r in history:
            # r.strategy = "composed:Name [block1+block2]" or "composed:Name"
            import re as _re

            m = _re.search(r"\[([^\]]+)\]", r.strategy)
            if m:
                for bt in m.group(1).split("+"):
                    tried_blocks.add(bt.strip())
        if tried_blocks:
            from composer import BLOCK_CATALOG as _BC

            all_blocks = list(_BC.keys())
            untried = [b for b in all_blocks if b not in tried_blocks]
        else:
            untried = []
        lines.append(
            f"\nBLOCK TYPES USED IN THIS SESSION (use DIFFERENT combinations):\n"
            f"  Already used: {', '.join(sorted(tried_blocks)) or 'none yet'}\n"
            f"  Not yet tried: {', '.join(sorted(untried)) or 'all tried'}\n"
            "  → Prefer block types from 'Not yet tried'. Vary the indicator family: "
            "if recent runs used EMA/RSI, try Bollinger, price_breakout, momentum, "
            "macd_cross, or atr combinations instead."
        )
    if not lines:
        return "No prior context — first strategy proposal."
    return "\n".join(lines)


def _fallback_composed() -> dict:
    from composer import BLOCK_CATALOG

    # Exclude exit-only blocks (e.g. atr_stop) from entry selection to avoid
    # _validate_composed forcing role="exit" → zero entry blocks → ValueError.
    exit_only = {"atr_stop"}
    all_types = list(BLOCK_CATALOG.keys())
    entry_types = [t for t in all_types if t not in exit_only]
    entry_type = random.choice(entry_types)
    exit_type = random.choice([t for t in all_types if t != entry_type] or all_types)

    def _rand_params(btype: str) -> dict:
        p = {}
        for pname, pspec in BLOCK_CATALOG[btype]["params"].items():
            if pspec["type"] == "int":
                p[pname] = random.randint(pspec["min"], pspec["max"])
            elif pspec["type"] == "float":
                p[pname] = round(random.uniform(pspec["min"], pspec["max"]), 1)
            else:
                p[pname] = random.choice(pspec["options"])
        return p

    def _fix_fast_slow(btype: str, params: dict) -> dict:
        """slow <= fast → swap to valid range for any crossover block."""
        if btype in ("ma_cross", "ema_cross", "macd_cross"):
            if params.get("slow", 0) <= params.get("fast", 0):
                params["fast"], params["slow"] = 10, 40
        return params

    e_params = _fix_fast_slow(entry_type, _rand_params(entry_type))
    x_params = _fix_fast_slow(exit_type, _rand_params(exit_type))

    result = {
        "name": f"Random {entry_type}/{exit_type}",
        "description": "Fallback random composition (Claude unavailable).",
        "blocks": [
            {"type": entry_type, "role": "entry", "params": e_params},
            {"type": exit_type, "role": "exit", "params": x_params},
        ],
        "strategy_options": dict(_STRATEGY_OPTION_DEFAULTS),
    }
    # Run _validate_composed — fix the role of exit-only blocks like atr_stop
    return _validate_composed(result)


_STRATEGY_OPTION_DEFAULTS: dict = {
    "entry_logic": "OR",
    "exit_logic": "OR",
    "order_type": "market",
    "limit_offset_bps": 0.0,
    "use_bracket": False,
    "sl_type": "percent",
    "sl_value": 2.0,
    "tp_type": "off",
    "tp_value": 4.0,
    "atr_period": 14,
    "allow_short": False,
    "trade_size_mode": "fixed",
    "trade_size_percent": 5.0,
    "trade_size_atr_risk": 1.0,
    "trade_size_usdt": 1000.0,
}


def _clamp(v, lo, hi, default):
    try:
        f = float(v)
        if f != f:  # NaN
            return default
        return max(lo, min(hi, f))
    except (TypeError, ValueError):
        return default


def _validate_strategy_options(raw: dict) -> dict:
    """Clamp / default strategy_options into safe values."""
    if not isinstance(raw, dict):
        raw = {}
    opts = dict(_STRATEGY_OPTION_DEFAULTS)

    def pick_enum(key, options):
        v = raw.get(key, opts[key])
        return v if v in options else opts[key]

    opts["entry_logic"] = pick_enum("entry_logic", ["OR", "AND"])
    opts["exit_logic"] = pick_enum("exit_logic", ["OR", "AND"])
    opts["order_type"] = pick_enum("order_type", ["market", "limit"])
    opts["limit_offset_bps"] = _clamp(raw.get("limit_offset_bps", 0.0), 0.0, 100.0, 0.0)
    opts["use_bracket"] = bool(raw.get("use_bracket", False))
    opts["sl_type"] = pick_enum("sl_type", ["percent", "atr"])
    opts["sl_value"] = _clamp(raw.get("sl_value", 2.0), 0.1, 50.0, 2.0)
    opts["tp_type"] = pick_enum("tp_type", ["percent", "atr", "off"])
    opts["tp_value"] = _clamp(raw.get("tp_value", 4.0), 0.1, 100.0, 4.0)
    opts["atr_period"] = int(_clamp(raw.get("atr_period", 14), 5, 100, 14))
    opts["allow_short"] = bool(raw.get("allow_short", False))
    opts["trade_size_mode"] = pick_enum(
        "trade_size_mode", ["fixed", "fixed_usdt", "percent_equity", "atr_target"]
    )
    opts["trade_size_percent"] = _clamp(
        raw.get("trade_size_percent", 5.0), 0.1, 50.0, 5.0
    )
    opts["trade_size_atr_risk"] = _clamp(
        raw.get("trade_size_atr_risk", 1.0), 0.05, 20.0, 1.0
    )
    opts["trade_size_usdt"] = _clamp(
        raw.get("trade_size_usdt", 1000.0), 1.0, 10_000_000.0, 1000.0
    )
    return opts


def _validate_composed(data: dict) -> dict:
    """Clamp params to catalog ranges and drop invalid blocks; raise on hopeless."""
    from composer import BLOCK_CATALOG

    if not isinstance(data, dict) or "blocks" not in data:
        raise ValueError("missing 'blocks'")

    clean_blocks = []
    for b in data["blocks"]:
        btype = b.get("type")
        role = b.get("role")
        if btype not in BLOCK_CATALOG:
            continue
        if role not in ("entry", "exit"):
            continue
        meta = BLOCK_CATALOG[btype]
        params = {}
        for pname, pspec in meta["params"].items():
            raw = (b.get("params") or {}).get(pname, pspec["default"])
            try:
                if pspec["type"] == "int":
                    v = int(raw)
                    v = max(pspec["min"], min(pspec["max"], v))
                elif pspec["type"] == "float":
                    v = float(raw)
                    v = max(pspec["min"], min(pspec["max"], v))
                else:
                    v = raw if raw in pspec["options"] else pspec["default"]
            except (TypeError, ValueError):
                v = pspec["default"]
            params[pname] = v
        # Enforce cross fast<slow for cross-family blocks.
        if btype in ("ma_cross", "ema_cross", "macd_cross") and params.get(
            "slow", 0
        ) <= params.get("fast", 0):
            params["fast"], params["slow"] = 10, max(params.get("slow", 30), 30)
        # atr_stop is exit-only.
        if btype == "atr_stop" and role != "exit":
            role = "exit"
        clean_blocks.append({"type": btype, "role": role, "params": params})

    if not clean_blocks or not any(b["role"] == "entry" for b in clean_blocks):
        raise ValueError("proposal missing entry block after cleanup")

    # Add one of several exit options if no exit block exists (not always atr_stop)
    if not any(b["role"] == "exit" for b in clean_blocks):
        from composer import BLOCK_CATALOG

        # Choose a suitable exit based on the entry block
        entry_types = {b["type"] for b in clean_blocks if b["role"] == "entry"}
        # Preference order: block giving the opposite signal to entry → atr_stop last resort
        _exit_candidates = [
            "momentum",
            "rsi_threshold",
            "bollinger_break",
            "macd_cross",
            "atr_stop",
        ]
        # Exclude types already used
        _exit_candidates = [t for t in _exit_candidates if t not in entry_types]
        fallback_exit_type = _exit_candidates[0] if _exit_candidates else "atr_stop"
        exit_meta = BLOCK_CATALOG.get(fallback_exit_type, {}).get("params", {})
        clean_blocks.append(
            {
                "type": fallback_exit_type,
                "role": "exit",
                "params": {k: v["default"] for k, v in exit_meta.items()},
            }
        )

    opts = _validate_strategy_options(data.get("strategy_options") or {})

    return {
        "name": str(data.get("name") or "Claude Suggestion")[:60].strip(),
        "description": str(data.get("description") or "")[:300].strip(),
        "blocks": clean_blocks,
        "strategy_options": opts,
    }


def propose_composed_strategy(
    history: list[Any],
    catalog: list[Any],
    hint: str = "",
    web_research: bool = False,
    market: str | None = None,
) -> tuple[dict, dict | None]:
    """Ask Claude to design a full composed strategy.
    Returns (strategy_dict, usage_dict | None).
    usage_dict has keys: input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens.

    ``market`` — optional market context (e.g. "US equity QQQ.NASDAQ (1-DAY
    bars, USD cash account)"). If None the Bybit BTCUSDT expression is preserved as-is.
    """
    try:
        client = _get_client()
    except Exception:
        return _fallback_composed(), None

    market_context = (
        f"trading strategies for {market} — a US stock from a historical Nautilus "
        "data catalog (2003→present). The account is a long-only USD CASH account: "
        "prefer allow_short=false, and trade_size is in whole SHARES (integer >= 1)"
        if market
        else _DEFAULT_MARKET_CONTEXT
    )
    system = COMPOSED_SYSTEM_PROMPT.replace("{market_context}", market_context).replace(
        "{catalog}", _catalog_summary()
    )
    hint_line = (
        f"\nUser hint (incorporate this into the strategy concept): {hint.strip()}"
        if hint.strip()
        else ""
    )

    web_section = ""
    if web_research:
        web_text = web_research_strategies(hint, market=market)
        if web_text:
            web_section = f"\n\n{web_text}"

    market_target = market or "BTCUSDT Bybit"
    user = f"""Context:
{_summarize_composed_history(history, catalog)}{hint_line}{web_section}

Design a new {market_target} composed strategy as specified. Return JSON only."""

    try:
        resp = _create_message(
            client,
            max_tokens=900,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        usage = {
            "input_tokens": getattr(resp.usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(resp.usage, "output_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0)
            or 0,
            "cache_creation_input_tokens": getattr(
                resp.usage, "cache_creation_input_tokens", 0
            )
            or 0,
        }
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
        data = json.loads(_extract_json_object(text))
        return _validate_composed(data), usage
    except Exception as e:
        fb = _fallback_composed()
        fb["description"] = (
            fb.get("description", "") + f" · fallback ({type(e).__name__})"
        ).strip()
        return fb, None


# ============================================================================


# Nodes explicitly ALLOWED. Anything else is rejected.
# AST security gate. The validator + whitelists live in codegate.py so the
# on-disk load path (composer) can re-validate stored blocks WITHOUT importing
# this heavy (anthropic) module. Imported here for the generation path.
from codegate import (  # noqa: E402
    _ALLOWED_BUILTINS,
    GeneratedCodeError,
)
from codegate import safe_builtins as _safe_builtins  # noqa: E402
from codegate import (
    validate_generated_code as _validate_generated_code,
)


def _test_execute_generated(
    src: str, meta: dict | None = None, require_max_lookback: bool = False
) -> None:
    """Compile + execute the module in an isolated namespace, then invoke
    evaluate() once with harmless inputs to catch runtime errors (NameError,
    KeyError on missing param, etc.). Raises GeneratedCodeError on failure.

    `meta` — if provided, `block.params` is pre-populated with declared defaults
    so the smoke call matches real runtime shape.

    `require_max_lookback` — M16: for NEWLY generated blocks the max_lookback
    export is required (if not declared, the window was clipped to 50+5 and
    long-period indicators were silently computed wrong). False for old disk
    blocks (backward compatibility).

    Environment parity (H8): the injection set here (math/statistics/ind) and
    the loop-budgeted compilation are IDENTICAL to composer._load_module_from_path —
    a block that passes smoke finds the same environment at runtime.
    """
    import math as _math
    import statistics as _stats

    import indicators as _ind_mod
    from codegate import compile_with_loop_budget

    safe_globals = {
        # Single source of truth shared with composer._load_module_from_path so
        # smoke and runtime resolve the SAME restricted builtins (incl. the
        # RuntimeError the injected loop-budget guard raises).
        "__builtins__": _safe_builtins(),
        "math": _math,
        "statistics": _stats,
        "ind": _ind_mod,
    }
    ns: dict = {}
    try:
        # M25: loop-budgeted compilation — `while True: pass` now raises a
        # RuntimeError on budget overrun instead of hitting the 2s thread
        # timeout and LEAKING a daemon thread (this closes the L4 core-leak).
        exec(compile_with_loop_budget(src, "<custom_block>"), safe_globals, ns)
    except Exception as e:
        raise GeneratedCodeError(f"module init failed: {type(e).__name__}: {e}") from e

    if require_max_lookback and not callable(ns.get("max_lookback")):
        raise GeneratedCodeError(
            "max_lookback(params) function is required (M16): it must return the "
            "number of bars the block needs, otherwise the window is clipped to 55 bars"
        )

    # Make helpers defined in the module visible when evaluate() runs.
    # Python exec with separate globals/locals means name lookups inside
    # function bodies go through globals (safe_globals), not locals (ns).
    # Merge all callable names from ns into safe_globals so helpers resolve.
    # Guard: do NOT overwrite whitelisted builtins (e.g. str, int, list) —
    # a helper named "str" would shadow the builtin for the smoke call.
    _protected = set(_ALLOWED_BUILTINS) | {"math", "statistics", "ind", "__builtins__"}
    for k, v in ns.items():
        if callable(v) and not k.startswith("_") and k not in _protected:
            safe_globals[k] = v
    # M1084: the budget preamble (__budget/__budget_tick) lands in ns inside
    # exec's separate globals/locals; the injected functions look these up in
    # GLOBALS — move them so they are reachable in smoke too (the loader uses a
    # single namespace, no problem there).
    for _bk in ("__budget", "__budget_tick"):
        if _bk in ns:
            safe_globals[_bk] = ns[_bk]

    ev = ns.get("evaluate")
    if not callable(ev):
        raise GeneratedCodeError("evaluate is not callable after exec")

    # Build defaults from meta.params, matching how the composer populates
    # block.params from BLOCK_CATALOG specs at add-block time.
    defaults: dict = {}
    if isinstance(meta, dict):
        for pname, pspec in (meta.get("params") or {}).items():
            if isinstance(pspec, dict) and "default" in pspec:
                defaults[pname] = pspec["default"]

    class _Block:
        def __init__(self, params):
            self.params = params
            self.role = "entry"
            self.type = "custom"

    class _Portfolio:
        def is_net_long(self, _):
            return False

        def is_net_short(self, _):
            return False

        def is_flat(self, _):
            return True

    # Give the block a decently long price series so most lookbacks don't
    # underrun and skip execution entirely.
    closes = [100.0 + i * 0.1 for i in range(300)]
    # Volume + high/low series are also provided via indicators just like at
    # runtime — blocks reading OHLC (ADX/ATR/Stochastic/Donchian) should really
    # run in smoke-exec (not fall into the None-guard). The high > close > low
    # ordering is preserved so True-Range etc. logic sees sensible values.
    indicators = {
        "volumes": [1000.0 + (i % 7) * 150.0 for i in range(300)],
        "highs": [100.0 + i * 0.1 + 0.5 for i in range(300)],
        "lows": [100.0 + i * 0.1 - 0.5 for i in range(300)],
    }

    # Run evaluate() in a daemon thread with a 2s timeout to guard against
    # infinite loops (e.g. `while True: pass`) that pass the AST whitelist.
    result_holder: list = []
    error_holder: list = []

    def _run():
        try:
            result_holder.append(
                ev({}, _Block(dict(defaults)), closes, indicators, _Portfolio())
            )
        except Exception as exc:
            error_holder.append(exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=2.0)
    if t.is_alive():
        raise GeneratedCodeError(
            "evaluate() timed out after 2s (possible infinite loop)"
        )
    if error_holder:
        raise GeneratedCodeError(
            f"evaluate() raised on smoke input: {type(error_holder[0]).__name__}: {error_holder[0]}"
        ) from error_holder[0]
    out = result_holder[0] if result_holder else None

    if out not in (None, "long", "short", "exit"):
        raise GeneratedCodeError(f"evaluate() returned invalid value: {out!r}")


CUSTOM_BLOCK_SYSTEM_PROMPT = """You are a Python code generator for a trading strategy composer.

The user describes a signal condition in natural language. You output a JSON
object describing a new "signal block" that plugs into an existing framework.

STRICT OUTPUT SCHEMA (return this and NOTHING else — no markdown, no fences):
{
  "name": "lowercase_snake_case (2-40 chars, letters/digits/underscore, starts with a letter)",
  "meta": {
    "label": "Human-readable Turkish or English label (max 40 chars)",
    "params": {
      "<param_name>": {"type": "int"|"float"|"enum", ...spec...},
      ...
    },
    "help": "One or two sentences explaining what the block does (Turkish OK)."
  },
  "code": "def evaluate(state, block, closes, indicators, portfolio):\\n    ...\\n"
}

param spec rules (mirror the built-ins):
- {"type": "int",   "min": <int>,   "max": <int>,   "default": <int>}
- {"type": "float", "min": <float>, "max": <float>, "default": <float>}
- {"type": "enum",  "options": ["a","b"], "default": "a"}

`code` rules:
- MUST define `evaluate(state, block, closes, indicators, portfolio)` — this is the main function.
- MAY also define helper functions at module level (e.g. `def ema_series(closes, period):`) BEFORE `evaluate`. Helper functions may call each other and be called from `evaluate`.
- `evaluate`:
- Return "long" or "short" for an entry-fire, "exit" for an exit-fire, or None.
- `closes` is a list of float bar closes (oldest first, newest last).
- `block.params` is a dict of the current param values (already coerced to declared types).
- `state` is a mutable dict — persistent across bars, scoped to this block. Use it for prev-value tracking.
- `indicators` is a dict with three aligned bar series (each a list[float], oldest-first, same length as `closes`): `indicators.get("volumes")`, `indicators.get("highs")`, `indicators.get("lows")`. No other keys — do NOT rely on anything else in it.
- `portfolio` exposes `is_net_long(...)`, `is_net_short(...)`, `is_flat(...)`. Do not import anything.

DATA AVAILABLE — FULL OHLCV:
- `closes` (bar closes) + `indicators.get("highs")` (bar highs) + `indicators.get("lows")` (bar lows) + `indicators.get("volumes")` (bar volumes). All four are aligned float lists, oldest-first. (Open is not exposed; use `closes[i-1]` as the prior close where an OHLC formula needs "previous close".) Always guard length: `hi = indicators.get("highs") or []; if len(hi) < n: return None`.
- Because highs/lows ARE available, indicators that need OHLC CAN be computed for real: **ATR**, **ADX / DMI**, **Stochastic**, **Donchian / Keltner channels**, **WaveTrend**, **SuperTrend** (ATR bands). Volume logic (volume spike, OBV, volume-weighted momentum) is also supported. Builtin blocks already cover RSI, EMA/MACD cross, Bollinger, ATR-stop, volume_spike, ADX (adx_threshold), StochRSI (stoch_rsi_cross), WaveTrend (wave_trend_cross), Donchian (donchian_channel) — write a custom block only for something NOT in that set.
- ⭐ INDICATOR LIBRARY `ind` (M27/M33 — USE IT, do NOT hand-roll the math): a vetted NAU-parity library is pre-available as `ind` (no import needed). Prefer these over reimplementing formulas — hand-rolled indicator math drifts and breaks parity:
  * `ind.calc_rsi(closes, period)` → float; `ind.calc_rsi_series(closes, period)` → list
  * `ind.sma(values, period)` / `ind.ema(values, period)` → list (aligned tails)
  * `ind.calc_atr(highs, lows, closes, period)` → float | None
  * `ind.calc_adx(highs, lows, closes, period)` → {"adx", "plusDI", "minusDI"} | None
  * `ind.calc_stoch_rsi(closes, rsi_period, stoch_period)` → {"k", "d"}
  * `ind.calc_wave_trend(highs, lows, closes, channel_len, avg_len)` → {"wt1", "wt2"} | None
  * `ind.calc_volume_change(volumes, lookback)` → float
  Example: `adx = ind.calc_adx(indicators.get("highs") or [], indicators.get("lows") or [], closes, 14); if adx is None or adx.get("adx", 0) < 20: return None`. Only hand-roll math for exotic indicators NOT in this list.
- MULTI-indicator confluence IS allowed. If the user asks for "RSI AND ADX AND ATR" (or any multi-indicator combo), you MAY AND all of them together — implement the full confluence the user requested, don't collapse it. The ONLY cost is signal frequency (see below): each extra AND cuts firing, so you MUST compensate with very loose thresholds so the combined block still fires enough. When the user did NOT ask for a confluence, keep it simple (one condition).

⚠️ SIGNAL FREQUENCY — the real constraint (not a condition-count cap):
A block that almost never fires produces too few trades and gets filtered out downstream (runs with fewer than ~20 trades are discarded). So there is no hard limit on the NUMBER of AND conditions — but every AND you add sharply cuts how often the block fires, and you must offset that with LOOSE thresholds. A GOOD entry block fires ~100-500 times on 50,000 bars (0.2-1%).

RULES FOR ADEQUATE SIGNAL FREQUENCY:
- AND as many conditions as the user's confluence requires, but LOOSEN each threshold hard so the combination still fires (e.g. a 4-way confluence needs each gate wide open). Err toward too-loose, never too-strict — a noisy block still beats a 0-trade one.
- PREFER simple threshold or crossover per condition: "if rsi < 40, fire" or "if ema5 crosses ema20, fire"
- AVOID multi-stage state machines like: "was_below AND now_above AND momentum > 0" — this fires too rarely
- For crossover detection: ONE state variable is enough. Store prev value, compare to current.
- Default parameter values MUST produce frequent signals. Use LOOSE thresholds:
  * RSI: oversold threshold default=40 (not 25), overbought default=60 (not 75)
  * Std deviations for Bollinger-style: default=1.5 (not 2.0 or higher)
  * Lookback periods: default=10-14 (not 20+)
  * Momentum bars: default=3 (not 5+)
- Provide wide param ranges so the backtest engine can optimize: e.g. RSI period min=5, max=30

SIGNAL FREQUENCY SELF-CHECK (mental simulation):
Before writing the code, ask: "On 1000 consecutive bars of BTC price data, how many times does this fire?"
- If answer < 5: your thresholds are too strict. Loosen them (do NOT drop conditions the user asked for).
- If answer > 200: might be too noisy, but better than 0.
- Target: 10-100 fires per 1000 bars.

AVOID (proven to produce 0 trades unless thresholds are loosened hard):
- Bollinger band crossing with std_dev>=1.5 as the entry trigger: fires <0.1% of bars, almost always 0 trades. If you use Bollinger, loosen std_dev to <=1.5 and don't rely on a clean band-cross.
- Multiple TIGHT AND-gates stacked (e.g. "was_below AND now_above AND momentum>0" with strict values): a many-gate state machine with tight thresholds fires ~never. Multi-AND is fine — TIGHT multi-AND is the trap. Loosen every gate.
- VWAP approximation using rolling std bands with tight multipliers (<=1.0): fires too rarely.

PREFERRED CONCEPTS (proven to produce trades):
- RSI threshold crossover (single condition: rsi < 35 → long)
- EMA or SMA crossover (fast crosses slow from below)
- Donchian channel breakout (close > max of last N bars)
- MACD histogram sign change (prev < 0, current >= 0)
- Rate-of-change threshold (ROC > X%)
- Hull MA crossover (use ~0.5*period weighted MA trick)


- No `import` statements. `math`, `statistics` and `ind` (indicator library, see above) are pre-available (no import needed).
- MUST also define `max_lookback(params)` at module level returning the number of bars the block needs (e.g. `def max_lookback(params): return int(params.get("period", 14)) * 2 + 10`). Without it the price window is silently clipped to 55 bars and long-period indicators miscompute (M16).
- No try/except, no with, no async, no lambda, no yield, no global/nonlocal, no delete.
- No dunder access (anything starting with `_`) — not on attributes, not on names.
- Only these built-ins may be called: abs, min, max, sum, len, round, sorted, range, int, float, bool, str, list, tuple, dict, set, any, all, enumerate, zip, reversed, isinstance.
- Helper functions defined in the same `code` string may call each other — that is allowed.
- Only these attributes may be accessed: .params, .role, .type, .get, .keys, .values, .items, .value, .upper, .lower, .middle, .initialized, .is_net_long, .is_net_short, .is_flat, math/statistics module functions.

STYLE:
- Keep each function short (helper 3-15 lines, evaluate 5-20 lines).
- Guard against short `closes` lists (return None when `len(closes) < required`).
- Use `state.get('prev', ...)` / `state['prev'] = ...` pattern for cross-detection.
- Prefer clear code over cleverness — the framework runs one bar at a time.

Return ONLY the JSON object. No prose, no explanation, no code fences."""


def _summarize_role_hint(role_hint: str) -> str:
    if role_hint == "entry":
        return "This block is meant for ENTRY: evaluate should return 'long' or 'short' (or None). Do not return 'exit'."
    if role_hint == "exit":
        return "This block is meant for EXIT: evaluate should return 'exit' (or None). Do not return 'long'/'short'."
    return "This block may be used as either entry or exit; check `block.role` and act accordingly."


def _extract_json_object(text: str) -> str:
    """Return the first balanced {...} block found in text. Handles preambles,
    trailing commentary, and code fences. Respects string literals."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
    start = s.find("{")
    if start < 0:
        return s  # let json.loads produce the error
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return s[start:]  # unbalanced — let json.loads raise


def _usage_dict(resp) -> dict:
    """resp.usage → normalize token dict (M1583: counted on every LLM call)."""
    u = getattr(resp, "usage", None)
    return {
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0)
        or 0,
    }


def _call_claude_for_block(user_prompt: str) -> tuple[dict, dict]:
    """Returns (parsed_json, usage) — M1583: count custom-block tokens too."""
    client = _get_client()
    resp = _create_message(
        client,
        max_tokens=4000,
        system=CUSTOM_BLOCK_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    usage = _usage_dict(resp)
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    payload = _extract_json_object(text)
    try:
        return json.loads(payload), usage
    except json.JSONDecodeError as e:
        snippet = (payload[:400] if payload else text[:400]).replace("\n", "\\n")
        raise json.JSONDecodeError(
            f"{e.msg} — response snippet: {snippet!r}", e.doc or "", e.pos
        ) from None


_BREAKDOWN_SYSTEM_PROMPT = """You split a trading-strategy description into DISTINCT
signal conditions, one per role, so each becomes a separate editable block that a
strategy engine combines with block-level OR/AND.

Return JSON ONLY (no markdown, no fences):
{
  "label": "short strategy name (<=40 chars, Turkish or English)",
  "entry_logic": "OR" | "AND",
  "exit_logic":  "OR" | "AND",
  "conditions": [
    {"role": "entry", "label": "<=40 char human label",
     "desc": "ONE self-contained signal condition, in the user's language, "
             "detailed enough to become a standalone block (name the indicator, "
             "period, threshold, and when it fires)"},
    ...
  ]
}

RULES:
- Split the description into its SEPARATE conditions — one condition per array item.
  "RSI<30 AND volume>2x average, exit on ATR stop or RSI>70" → two entry items
  (RSI-oversold, volume-spike) + two exit items (ATR-stop, RSI-overbought).
- Each `desc` must stand ALONE: a code generator will read it with no other context.
- AT LEAST one entry and AT LEAST one exit condition. If the user gave no explicit
  exit, add a single sensible exit (e.g. ATR trailing stop).
- entry_logic="OR" fires the entry when ANY entry condition hits; "AND" requires ALL
  the same bar. Default OR. Use AND only for a genuine confluence the user asked for,
  and NEVER AND across 3+ conditions (fires on <0.5% of bars → zero trades).
- exit_logic works the same; exits are almost always OR.
- Keep it to 2-4 conditions total unless the user clearly described more.
Return the JSON only."""


def propose_condition_breakdown(description: str) -> dict:
    """Split a natural-language description into SEPARATE signal conditions (each
    becomes its own block).

    Returns: {label, entry_logic, exit_logic, conditions:[{role,label,desc}], usage}.
    Conditions include at least 1 entry + 1 exit; otherwise ValueError (the caller
    falls back to the single-block path). LLM/parse errors are also raised.
    """
    client = _get_client()
    resp = _create_message(
        client,
        max_tokens=1500,
        system=_BREAKDOWN_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": description.strip()}],
    )
    usage = _usage_dict(resp)
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    data = json.loads(_extract_json_object(text))

    conds = data.get("conditions")
    if not isinstance(conds, list) or not conds:
        raise ValueError("breakdown: no/empty 'conditions' list")
    clean: list[dict] = []
    for c in conds:
        if not isinstance(c, dict):
            continue
        role = c.get("role")
        desc = str(c.get("desc") or "").strip()
        if role in ("entry", "exit") and desc:
            clean.append(
                {
                    "role": role,
                    "label": str(c.get("label") or role).strip()[:40] or role,
                    "desc": desc,
                }
            )
    n_entry = sum(1 for c in clean if c["role"] == "entry")
    n_exit = sum(1 for c in clean if c["role"] == "exit")
    if n_entry < 1 or n_exit < 1:
        raise ValueError(
            f"breakdown: at least 1 entry + 1 exit required (entry={n_entry}, exit={n_exit})"
        )

    def _logic(v):
        return "AND" if str(v).upper() == "AND" else "OR"

    return {
        "label": str(data.get("label") or "").strip()[:40] or "Described strategy",
        "entry_logic": _logic(data.get("entry_logic")),
        "exit_logic": _logic(data.get("exit_logic")),
        "conditions": clean,
        "usage": usage,
    }


_REFINE_SYSTEM_PROMPT = """Sen bir alım-satım (trading) stratejisi editörüsün.
Kullanıcı, doğal dilde kabaca bir strateji tarifi yazdı. TÜM çıktıların TÜRKÇE olmalı.

Görevlerin:
1. Tarifi tek, net, kesin ve test edilebilir bir kural setine dönüştür — dilbilgisini
   düzelt, eksik çıkış (exit) koşulu varsa ekle, belirsizliği gider, kısa tut (en fazla
   2-4 cümle). Kullanıcının anlattığı çekirdek mantığı DEĞİŞTİRME; yalnızca netleştir.
2. Eğer backtest metrikleri verilmişse, zayıf noktalara (düşük Sharpe, yüksek DD,
   az işlem, düşük win rate) odaklan ve tarifi bu sorunları giderecek şekilde güçlendir.
3. Stratejiyi güçlendirebilecek 2-4 SOMUT ilave ÖNERİ ver (ör. bir trend filtresi,
   hacim teyidi, uygun stop/take-profit, parametre aralığı). Her öneri kısa ve
   uygulanabilir olsun.
4. Olası TUZAKLARA/RİSKLERE dair uyarılar ver (ör. çok sıkı AND koşulu → az işlem,
   Bollinger tam kesişimi → 0 işlem, aşırı optimizasyon riski, düşük likidite).
   Uyarı yoksa boş liste döndür.

Yalnızca JSON döndür:
{"refined": "<iyileştirilmiş tarif>",
 "notes": "<tek kısa cümle: neyi değiştirdin>",
 "suggestions": [{"kind": "oneri|uyari", "text": "<kısa madde>"}]}
Girdi zaten net görünse bile her zaman en az 2 somut öneri ver; notes alanına kısa bir iyileştirme gerekçesi yaz.
"""


def _format_metrics_block(
    raw_description: str,
    backtest_metrics: dict | None = None,
    robustness: dict | None = None,
) -> str:
    """Compose the user-prompt text: strategy description + optional backtest &
    robustness metrics. Shared by propose_refined_description (single-shot) and
    chat_refine (multi-turn) so both feed the model the same context format.

    If backtest_metrics is empty/None the raw description is returned as-is.
    """
    user_content = raw_description.strip()
    if not backtest_metrics:
        return user_content
    parts = ["Strateji tarifi:\n" + user_content, "\nSon backtest sonuçları:"]
    if backtest_metrics.get("spec_name"):
        parts.append(f"  Strateji: {backtest_metrics['spec_name']}")
    if backtest_metrics.get("best_tf"):
        parts.append(f"  En iyi TF: {backtest_metrics['best_tf']}")
    for key, label in [
        ("pnl_pct", "PnL %"),
        ("sharpe", "Sharpe"),
        ("max_dd", "Max DD %"),
        ("n_trades", "İşlem sayısı"),
        ("win_rate", "Kazanç %"),
    ]:
        val = backtest_metrics.get(key)
        if val not in (None, ""):
            parts.append(f"  {label}: {val}")
    # Robustness özeti — overfitting-farkındalıklı öneri için.
    if robustness and any(v not in (None, "") for v in robustness.values()):
        parts.append("\nRobustness analizi:")
        for key, label in [
            (
                "overfitting_score",
                "Overfitting skoru (≥0.7 sağlam · <0.4 aşırı-uyum)",
            ),
            ("verdict", "Verdict"),
            ("wfo_efficiency", "WFO verimliliği (OOS/in-sample)"),
            ("oos_sharpe", "OOS Sharpe"),
            ("stability", "Parametre kararlılığı"),
        ]:
            val = robustness.get(key)
            if val not in (None, ""):
                parts.append(f"  {label}: {val}")
        parts.append(
            "\nBu sonuçlara göre stratejiyi iyileştir. Overfitting skoru düşük "
            "(<0.4) ya da OOS Sharpe zayıfsa: parametreleri sadeleştir, aşırı "
            "optimizasyondan kaçın, daha genel kurallar öner. Robustsa (≥0.7): "
            "güçlendirme/pozisyon ölçekleme önerebilirsin."
        )
    else:
        parts.append("\nBu sonuçlara göre stratejiyi iyileştir.")
    return "\n".join(parts)


def propose_refined_description(
    raw_description: str,
    backtest_metrics: dict | None = None,
    robustness: dict | None = None,
) -> dict:
    """Rewrite the user's raw strategy description into a cleaner, more precise version.

    If backtest_metrics is provided (pnl_pct, sharpe, max_dd, n_trades, win_rate,
    spec_name, best_tf), the AI uses them to suggest targeted improvements.
    If robustness is provided (overfitting_score, verdict, wfo_efficiency,
    oos_sharpe, stability), the AI weighs overfitting risk: low score / poor OOS
    → simplify & de-tune; robust → allowed to strengthen.
    Returns: {refined: str, notes: str, suggestions: list[{kind, text}]}.
    Falls back to original text (empty suggestions) on any error.
    """
    client = _get_client()
    try:
        user_content = _format_metrics_block(
            raw_description, backtest_metrics, robustness
        )
        resp = _create_message(
            client,
            max_tokens=700,
            system=_REFINE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        data = json.loads(_extract_json_object(text))
        refined = str(data.get("refined") or "").strip()
        notes = str(data.get("notes") or "").strip()
        # Normalize suggestions: keep only {kind in oneri|uyari, non-empty text}.
        suggestions: list[dict] = []
        for s in data.get("suggestions") or []:
            if not isinstance(s, dict):
                continue
            stext = str(s.get("text") or "").strip()
            if not stext:
                continue
            kind = str(s.get("kind") or "oneri").strip().lower()
            if kind not in ("oneri", "uyari"):
                kind = "oneri"
            suggestions.append({"kind": kind, "text": stext})
        if not refined:
            raise ValueError("empty refined")
        return {"refined": refined, "notes": notes, "suggestions": suggestions}
    except Exception as _e:
        import logging as _logging

        _logging.getLogger("agent.refine").warning(
            "propose_refined_description failed: %r", _e
        )
        return {"refined": raw_description.strip(), "notes": "", "suggestions": []}


_CHAT_SYSTEM_PROMPT = """Sen bir alım-satım (trading) stratejisi danışmanısın.
Kullanıcı seninle SOHBET ederek bir strateji tarifini birlikte netleştiriyor.
TÜM yanıtların TÜRKÇE, kısa ve net olsun (gereksiz uzatma).

Davranış:
- Kullanıcının sorusuna/isteğine DOĞRUDAN cevap ver; önceki mesajlara atıfta bulunabilirsin.
- Stratejinin çekirdek mantığını DEĞİŞTİRME; yalnızca netleştir, güçlendir, tuzakları
  (aşırı-uyum, çok sıkı AND koşulu → az işlem, eksik çıkış koşulu, düşük likidite) uyar.
- Backtest/robustness metrikleri verildiyse zayıf noktalara odaklan (düşük Sharpe,
  yüksek DD, az işlem). Overfitting skoru düşükse sadeleştirmeyi öner.
- Somut ve uygulanabilir ol; istendiğinde parametre aralığı/filtre öner.

Her yanıtının EN SONUNA, o ana dek üzerinde uzlaşılan güncel ve net strateji tarifini
TEK SATIRLIK şu işaretli biçimde ekle (2-4 cümlelik özet, test edilebilir kurallar):
[NET_TARİF]: <buraya güncel net tarif>
Bu satır dışındaki metin kullanıcıyla serbest sohbetindir.
"""


def chat_refine(conversation_messages: list[dict], context: dict | None = None) -> dict:
    """Multi-turn strateji-iyileştirme sohbeti.

    conversation_messages: Anthropic messages[] — {"role": "user"|"assistant",
      "content": str} turn'leri. Metrik bağlamı (varsa) çağıran taraf, ilk user
      turn'üne ``_format_metrics_block`` ile gömer; bu fonksiyon listeyi olduğu
      gibi modele iletir. ``context`` şu an yalnızca ileriye dönük imza uyumu
      için tutulur (gömme çağıran tarafta yapılır).
    Döndürür: {"text": "<asistan sohbet yanıtı>", "refined": "<güncel net tarif>|''"}.
    Herhangi bir hatada nazik Türkçe özür + boş ``refined`` döner.
    """
    client = _get_client()
    try:
        import re

        resp = _create_message(
            client,
            max_tokens=1000,
            system=_CHAT_SYSTEM_PROMPT,
            messages=conversation_messages,
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
        # Sondaki [NET_TARİF]: satırını ayıkla; kalanı sohbet metni olarak bırak.
        refined = ""
        m = re.search(r"\[NET_TAR[İI]F\]\s*:\s*(.+)", text)
        if m:
            refined = m.group(1).strip()
            text = text[: m.start()].rstrip()
        if not text:
            text = "Anladım. Devam edebilmem için biraz daha detay verir misin?"
        return {"text": text, "refined": refined}
    except Exception as _e:
        import logging as _logging

        _logging.getLogger("agent.chat").warning("chat_refine failed: %r", _e)
        return {
            "text": (
                "Şu an AI danışmana ulaşılamadı. Biraz sonra tekrar dener misin? "
                "İstersen tarifini elle düzenleyip 'Blokları Oluştur'a devam edebilirsin."
            ),
            "refined": "",
        }


def propose_custom_block(
    label: str, description: str, role_hint: str = "entry"
) -> dict:
    """Ask Claude to design a new custom signal block from a natural-language description.

    Returns {name, meta, code} on success. Raises GeneratedCodeError with a
    friendly message on repeated validation failure.
    """
    role_line = _summarize_role_hint(role_hint)
    user_prompt = f"""Design a new signal block.

Label (user's short name for it): {label}
Role hint: {role_hint}
{role_line}

Description (user's words — infer parameters, thresholds, logic):
\"\"\"{description.strip()}\"\"\"

Return the JSON only."""

    last_error = None
    _acc_usage: dict = {}
    for attempt in range(2):
        try:
            data, _u = _call_claude_for_block(user_prompt)
            for k, v in _u.items():
                _acc_usage[k] = _acc_usage.get(k, 0) + v
        except Exception as e:
            last_error = f"Claude request failed: {type(e).__name__}: {e}"
            user_prompt = f"Previous request failed ({type(e).__name__}). {user_prompt}"
            continue

        if (
            not isinstance(data, dict)
            or "name" not in data
            or "meta" not in data
            or "code" not in data
        ):
            last_error = f"schema mismatch: missing keys in {list(data.keys()) if isinstance(data, dict) else type(data).__name__}"
            user_prompt = f"Your last output was invalid: {last_error}. Return JSON with keys name/meta/code."
            continue

        name = str(data["name"]).strip()
        meta = data["meta"]
        code = data["code"]

        # Basic name validation happens later in the store; validate meta shape here.
        if not isinstance(meta, dict) or "label" not in meta or "params" not in meta:
            last_error = "meta must have label and params"
            user_prompt = f"Your last output was invalid: {last_error}. Fix and return valid JSON."
            continue

        try:
            _validate_generated_code(code)
            _test_execute_generated(code, meta=meta, require_max_lookback=True)
        except GeneratedCodeError as e:
            last_error = str(e)
            user_prompt = (
                f"Your last code was REJECTED with this error:\n\n{last_error}\n\n"
                "Fix the code and return the same JSON schema. Remember: no imports, "
                "no leading underscore names, no try/with/lambda/global/nonlocal, only whitelisted "
                "attributes (.params/.role/.value/.upper/.lower/.middle/.initialized/.get/.keys/"
                ".values/.items) and only whitelisted builtins."
            )
            continue

        return {"name": name, "meta": meta, "code": code, "usage": _acc_usage}

    raise GeneratedCodeError(
        f"Claude could not produce valid code after 2 attempts. Last error: {last_error}"
    )


_AGENT_IDEA_PROMPT = """\
You are a {market_tr} research agent.{market_note}

{exploration_directive}

⚠️ FORBIDDEN: Bollinger Band crossing — always produces 0 trades. NEVER select this.
⚠️ FORBIDDEN: VWAP deviation + momentum AND combination — produces 0 trades.

Past strategies and their RESULTS (0 trades = failed, change it!):
{history}

Concepts previously generated as custom blocks (DO NOT REGENERATE):
{used_concepts}

User hint:
{hint}

Rule: Usable data = FULL OHLCV. closes (close) + indicators["highs"] (high)
+ indicators["lows"] (low) + indicators["volumes"] (volume) — all four are float lists,
aligned with closes, oldest to newest. Since high/low are AVAILABLE, real OHLC indicators
can be computed: ATR, ADX/DMI, Stochastic, Donchian/Keltner channel, WaveTrend, SuperTrend.
Volume-based ideas (volume spike, OBV, volume confirmation) are also valid. If the user hint
contains one of these indicators, use its REAL formula (do not fall back to a crude proxy).
If the user asked for multi-indicator confluence (e.g. "RSI AND ADX AND ATR"), produce an idea
that uses them ALL TOGETHER — do not trim. There is no upper limit on AND condition count; the
only cost is signal frequency, so compensate with loose thresholds (low-trade runs are already
filtered out later). If the user did not ask for confluence, a single simple condition is enough.

Return in this JSON format (write nothing else):
{{
  "name": "short strategy name (2-4 words)",
  "description": "1-sentence trading thesis",
  "entry_label": "short name for the entry block",
  "entry_desc": "describe how to compute the entry signal (over closes/highs/lows/volumes series — ATR/ADX/Stochastic requiring high/low can also be used)",
  "exit_label": "short name for the exit block",
  "exit_desc": "describe the exit signal"
}}
"""


# Indicators recognized in hints: canonical name → search patterns. Short acronyms
# (rsi/adx/atr…) match on word boundaries, long distinctive names (bollinger/stochastic…)
# match as substrings — so that e.g. "ma" inside "smart" is not a false positive.
_HINT_INDICATORS: dict[str, list[str]] = {
    "RSI": [r"\brsi\b"],
    "ADX/DMI": [r"\badx\b", r"\bdmi\b", r"\bdx\b"],
    "ATR": [r"\batr\b"],
    "MACD": [r"\bmacd\b"],
    "Stochastic": ["stochastic", "stokastik", r"\bstoch\b"],
    "Bollinger": ["bollinger", r"\bbband"],
    "EMA": [r"\bema\b"],
    "SMA/MA": [r"\bsma\b", r"\bwma\b", "hareketli ortalama", r"\bmoving average\b"],
    "WaveTrend": ["wavetrend", "wave trend", r"\bwt\b"],
    "Donchian": ["donchian"],
    "Keltner": ["keltner"],
    "CCI": [r"\bcci\b"],
    "Williams %R": ["williams", "%r"],
    "OBV": [r"\bobv\b"],
    "SuperTrend": ["supertrend", "super trend"],
    "Momentum/ROC": ["momentum", r"\broc\b", "rate of change"],
    "Ichimoku": ["ichimoku"],
    "Volume": ["hacim", r"\bvolume\b"],
}


def _hint_indicators(hint: str) -> list[str]:
    """Canonical names of recognized indicators mentioned in the hint (ordered, deduplicated)."""
    import re

    low = (hint or "").lower()
    found = []
    for canon, patterns in _HINT_INDICATORS.items():
        if any(re.search(p, low) for p in patterns):
            found.append(canon)
    return found


def _exploration_directive(hint: str) -> str:
    """If the hint contains explicit indicators, returns a 'stay in this set +
    scan variations' directive, otherwise a 'pick a different indicator family'
    directive.

    Goal: when the user gives 'RSI+ADX+ATR', the agent should scan the
    combination/parameter space of this set instead of drifting to a different
    indicator family every round.
    """
    inds = _hint_indicators(hint)
    if inds:
        names = ", ".join(inds)
        return (
            f"Task: The user requested these indicators: {names}. These are the "
            "CORE of the strategy — use them in every idea (alone, in pairs, or all "
            "together with AND; each round try a DIFFERENT combination/subset + "
            "DIFFERENT parameters/thresholds/logic, so you systematically scan the "
            "space of this set). ALSO be CREATIVE by adding to each idea a "
            "COMPLEMENTARY indicator (an extra filter, confirmation, or a better "
            "exit) that you think COULD INCREASE PROFIT — do not abandon the "
            "requested set, but do not stay limited to them alone either; build on "
            "top. Do NOT REPEAT the combinations tried in the history below; produce "
            "a NEW variation each round. (STAY IN THIS SET.)"
        )
    return (
        "Task: Looking at the past results below, produce a NEW and COMPLETELY "
        "DIFFERENT strategy idea. pick a DIFFERENT indicator family from the "
        "existing history (e.g. Donchian channel, Hull MA, Williams %R, Keltner "
        "channel, DEMA/TEMA, rate-of-change threshold, CCI, WaveTrend, MACD "
        "histogram sign change)."
    )


def _propose_agent_strategy_idea(
    hint: str,
    history: list,
    used_concepts: list | None = None,
    market: str | None = None,
) -> dict:
    """Ask Claude for a novel strategy idea (labels + descriptions only, no code).

    Returns dict with keys: name, description, entry_label, entry_desc,
    exit_label, exit_desc. Falls back to a hardcoded idea on any failure.

    ``market`` — optional market context; if None the crypto phrasing is kept.
    """
    history_summary = ""
    if history:
        tried_with_outcomes = []
        for r in history[-8:]:
            n_trades = (r.metrics or {}).get("n_trades", 0) if not r.error else 0
            if n_trades == 0:
                outcome = "❌ 0 TRADES — NEVER RAN"
            else:
                sh = (r.metrics or {}).get("sharpe", 0) or 0
                outcome = f"✓ {n_trades} trade, sharpe={sh:.1f}"
            name = r.strategy.split(":")[-1].strip()
            tried_with_outcomes.append(f"  {name}: {outcome}")
        history_summary = "Previously tried and their results:\n" + "\n".join(
            tried_with_outcomes
        )

        # Highlight zero-trade failures explicitly
        zero_names = [
            r.strategy.split(":")[-1].strip()
            for r in history
            if not r.error and (r.metrics or {}).get("n_trades", 0) == 0
        ]
        if zero_names:
            history_summary += (
                "\n\n⛔ CONCEPTS THAT PRODUCED ZERO TRADES — ALWAYS SKIP THESE: "
                + ", ".join(zero_names[-8:])
            )

    concepts_str = "None (first round)"
    if used_concepts:
        concepts_str = ", ".join(used_concepts[-12:])  # last 12 concepts

    if market:
        market_tr = "US equity trading"
        market_note = (
            f"\nInstrument: {market}. Not crypto — produce ideas suited to equity "
            "dynamics and the bar interval (on daily bars use swing logic instead "
            "of 'intraday')."
        )
    else:
        market_tr = "crypto trading"
        market_note = ""

    prompt = _AGENT_IDEA_PROMPT.format(
        market_tr=market_tr,
        market_note=market_note,
        exploration_directive=_exploration_directive(hint),
        history=history_summary or "No history yet.",
        used_concepts=concepts_str,
        hint=hint.strip() or "None (fully autonomous)",
    )

    try:
        client = _get_client()
        resp = _create_message(
            client,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
        idea = json.loads(_extract_json_object(text))
        idea["usage"] = _usage_dict(resp)  # M1583: count idea tokens too
        return idea
    except Exception as e:
        logging.warning("_propose_agent_strategy_idea failed: %s", e, exc_info=True)
        # Fallback: pick from a variety of concepts (avoid always returning Bollinger)
        _FALLBACK_IDEAS = [
            {
                "name": "RSI Oversold Reversal",
                "description": "Enter when RSI turns up from below 30, exit above 70.",
                "entry_label": "RSI Oversold Entry",
                "entry_desc": "RSI 14 period; if the previous bar was below 30 and the current bar crosses above 30, long signal.",
                "exit_label": "RSI Overbought Exit",
                "exit_desc": "Produce an exit signal when RSI rises above 70.",
            },
            {
                "name": "MACD Zero Line Cross",
                "description": "Momentum begins when the MACD histogram crosses zero upward.",
                "entry_label": "MACD Zero Cross Entry",
                "entry_desc": "When the MACD histogram (12-26 EMA difference) crosses from below zero to above, long signal.",
                "exit_label": "MACD Negative Exit",
                "exit_desc": "Produce an exit signal when the MACD histogram turns negative.",
            },
            {
                "name": "EMA Ribbon Breakout",
                "description": "Trend starts when the short EMA crosses above the long EMA.",
                "entry_label": "EMA Ribbon Entry",
                "entry_desc": "If the 5-period EMA is below the 20-period EMA and crosses above it, long signal.",
                "exit_label": "EMA Ribbon Exit",
                "exit_desc": "Produce an exit signal when the 5-period EMA falls below the 20-period EMA.",
            },
            {
                "name": "Stochastic Reversal",
                "description": "Enter as Stochastic turns up from the oversold zone.",
                "entry_label": "Stoch Reversal Entry",
                "entry_desc": "If Stochastic K (14,3) turns up from below 20, long signal. K = (close-min14)/(max14-min14)*100.",
                "exit_label": "Stoch Overbought Exit",
                "exit_desc": "Produce an exit signal when Stochastic K rises above 80.",
            },
            {
                "name": "Donchian Channel Breakout",
                "description": "Breakout entry when price breaks the N-period high.",
                "entry_label": "Donchian Breakout Entry",
                "entry_desc": "When close breaks the maximum of the last 20 bars (Donchian upper channel), long signal.",
                "exit_label": "Donchian Lower Exit",
                "exit_desc": "Produce an exit signal when close falls below the minimum of the last 10 bars.",
            },
        ]
        # pick the least-used fallback according to used_concepts
        idx = 0
        if used_concepts:
            used_str = " ".join(used_concepts).lower()
            # compute a usage score for each fallback
            scores = []
            for idea in _FALLBACK_IDEAS:
                score = sum(
                    1
                    for kw in [idea["entry_label"].lower(), idea["name"].lower()]
                    if any(w in used_str for w in kw.split()[:2])
                )
                scores.append(score)
            idx = scores.index(min(scores))
        return _FALLBACK_IDEAS[idx]


if __name__ == "__main__":
    print(json.dumps(propose_strategy([]), indent=2))
    print("---")
    print(json.dumps(propose_composed_strategy([], []), indent=2))
