"""Claude Fable 5 strategy-parameter proposer.

Returns a dict:
  {"strategy": "ma_crossover", "params": {"fast": 12, "slow": 34}, "rationale": "..."}

Wiki References
---------------
See: [[nau_auto_kosulari_2026_08_18]] (`_fallback_composed` bir AUTO koşusunu
öldürüyordu: giriş bloğu elle yazılmış bir listeden seçiliyordu, oysa katalog
custom blokların rolünü META'DA ilan ediyor — 408 bloğun 162'si `exit`, yani
her fallback ~%40 ihtimalle ölümcül bir yazı-turaydı),
[[model_secici_ve_gorunurluk]] (model seçici, canlı OpenRouter kataloğu,
ücretsiz filtresi, model adının çözülmesi, `model_unavailable_reason` ile kapıda
ret — listelenebilir ≠ çalıştırılabilir), [[llm_maliyet_kaldiraclari]] (defter
denetimi + maliyet kaldıraçları; `_ClaudeCLIMessages.create`'in `max_tokens`'ı
düşürdüğü ve `_purpose`suz çağrıların kör alan oluşturduğu buradan izlenir —
`set_thread_effort`/`current_effort` ile bağlanan `--effort` de orada: modelden
bağımsız ve onunla ÇARPILAN ikinci kol),
[[kesilme_ve_degrade_gorunurlugu]] (`max_tokens` tavanı modelin üslubuna bağlıdır;
`TruncatedResponse` + tek seferlik büyük-tavan denemesi, ve fallback'lerin
`degraded` işaretiyle sayılıp ekrana taşınması; `_LEARNED_MAX_TOKENS_LOCK` +
`max()` yazımı 2026-08-08 DeepR bulgusu).
Geri kalanı app-specific; wiki kapsamı dışında (LLM parametre önericisi, bir
Nautilus kavramı değil).
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from typing import Any

# Adım 3: TerminalLLMError/LLMCallCancelled/LLMTokenBudgetExceeded,
# _LLM_CONTROL/_OUTPUT_RESERVATION_LOCK/_OBSERVED_OUTPUT_HIGH_WATER,
# set_thread_llm_control through _tag_degraded, _random_ctx/
# set_thread_random_seed/_fallback_rng extracted to llm_client.py, imported
# back further down. Adım 6 (Faz 2): _interruptible_sleep + _sleep (the
# patchable time.sleep alias) extracted to openrouter_backend.py, along
# with the rest of the OpenRouter backend below -- imported back further
# down. Adım 9: the dispatch core -- _client/_client_lock,
# model_unavailable_reason, credit-exhaustion detection, _ledger_record,
# TruncatedResponse + the learned-ceiling truncation retry,
# _create_message/_create_message_once, _find_claude_cli, _build_client,
# _get_client, and _usage_dict (pulled up from its stranded Domain C spot)
# extracted to llm_dispatch.py, re-exported below (_client/_client_lock and
# _ledger_record deliberately NOT re-exported -- see llm_dispatch.py's own
# docstring).
# Adım 2: MODEL/FALLBACK_MODEL/_active_model/_model_lock and (below)
# _MODEL_OVERRIDE through current_model() extracted to llm_client.py,
# re-exported below. Listed in __all__ (further down) so ruff's F401
# unused-import autofix does not silently delete names that are genuinely
# unused WITHIN this file but still part of agent.<name>'s public surface
# for every external caller (already happened twice while writing this
# step — confirmed via a fresh interpreter, not assumed).
from llm_client import (  # noqa: E402
    _LLM_CONTROL,
    _OBSERVED_OUTPUT_HIGH_WATER,
    _OUTPUT_RESERVATION_LOCK,
    EFFORT,
    FALLBACK_MODEL,
    MODEL,
    SELECTABLE_EFFORTS,
    SELECTABLE_MODELS,
    LLMCallCancelled,
    LLMTokenBudgetExceeded,
    TerminalLLMError,
    _admit_llm_request,
    _check_llm_cancelled,
    _ClaudeCLIClient,
    _ClaudeCLIMessages,
    _CLIError,
    _CLIResponse,
    _fallback_rng,
    _is_free,
    _llm_request_token_bound,
    _observe_llm,
    _output_cap_telemetry,
    _poll_until_deadline,
    _raise_if_llm_control_abort,
    _random_ctx,
    _record_cli_side_models,
    _tag_degraded,
    current_effort,
    current_model,
    hybrid_note,
    is_terminal_llm_error,
    model_for_purpose,
    model_id,
    model_label,
    openrouter_catalog,
    openrouter_extra_models,
    openrouter_free_only,
    openrouter_paid_extras,
    purpose_model_map,
    resolve_effort,
    selectable_models,
    set_thread_effort,
    set_thread_llm_control,
    set_thread_model,
    set_thread_random_seed,
)

# Adım 9: the dispatch core (client construction/selection, credit-exhaustion
# fallback, TruncatedResponse + the learned-ceiling retry, and
# _create_message/_create_message_once -- the single choke point every app
# LLM call passes through) extracted to llm_dispatch.py, re-exported below.
# _client/_client_lock and _ledger_record are deliberately NOT re-exported --
# see llm_dispatch.py's own docstring; a test needing them imports
# llm_dispatch directly.
from llm_dispatch import (  # noqa: E402
    _TRUNCATION_RETRY_SCALE,
    MAX_TOKENS_COMPOSED,
    MAX_TOKENS_IDEA,
    TruncatedResponse,
    _build_client,
    _create_message,
    _create_message_once,
    _find_claude_cli,
    _get_client,
    _is_credit_exhausted,
    _usage_dict,
    _was_truncated,
    model_unavailable_reason,
)
from openrouter_backend import (  # noqa: E402
    _OR_RETRY_WAITS,
    _build_openrouter_client,
    _get_openrouter_client,
    _interruptible_sleep,
    _is_rate_limited,
    _openrouter_process_main,
    _openrouter_usage_payload,
    _OpenRouterClient,
    _OpenRouterMessages,
    _OpenRouterProcessError,
    _or_create_with_backoff,
    _ORResponse,
    _retry_after_seconds,
    _run_openrouter_killable,
    _sleep,
)
from strategies import STRATEGY_PARAM_SPEC

__all__ = [
    "EFFORT",
    "FALLBACK_MODEL",
    "LLMCallCancelled",
    "LLMTokenBudgetExceeded",
    "MAX_TOKENS_COMPOSED",
    "MAX_TOKENS_IDEA",
    "MODEL",
    "SELECTABLE_EFFORTS",
    "SELECTABLE_MODELS",
    "TerminalLLMError",
    "TruncatedResponse",
    "_CLIError",
    "_CLIResponse",
    "_ClaudeCLIClient",
    "_ClaudeCLIMessages",
    "_LLM_CONTROL",
    "_OBSERVED_OUTPUT_HIGH_WATER",
    "_OR_RETRY_WAITS",
    "_OUTPUT_RESERVATION_LOCK",
    "_OpenRouterClient",
    "_OpenRouterMessages",
    "_OpenRouterProcessError",
    "_ORResponse",
    "_TRUNCATION_RETRY_SCALE",
    "_admit_llm_request",
    "_build_client",
    "_build_openrouter_client",
    "_check_llm_cancelled",
    "_create_message",
    "_create_message_once",
    "_fallback_rng",
    "_find_claude_cli",
    "_get_client",
    "_get_openrouter_client",
    "_interruptible_sleep",
    "_is_credit_exhausted",
    "_is_free",
    "_is_rate_limited",
    "_llm_request_token_bound",
    "_observe_llm",
    "_openrouter_process_main",
    "_openrouter_usage_payload",
    "_or_create_with_backoff",
    "_output_cap_telemetry",
    "_poll_until_deadline",
    "_raise_if_llm_control_abort",
    "_random_ctx",
    "_record_cli_side_models",
    "_retry_after_seconds",
    "_run_openrouter_killable",
    "_sleep",
    "_tag_degraded",
    "_usage_dict",
    "_was_truncated",
    "current_effort",
    "current_model",
    "hybrid_note",
    "is_terminal_llm_error",
    "model_for_purpose",
    "model_id",
    "model_label",
    "model_unavailable_reason",
    "openrouter_catalog",
    "openrouter_extra_models",
    "openrouter_free_only",
    "openrouter_paid_extras",
    "purpose_model_map",
    "resolve_effort",
    "selectable_models",
    "set_thread_effort",
    "set_thread_llm_control",
    "set_thread_model",
    "set_thread_random_seed",
]

# ── Web research (DuckDuckGo, no API key required) ─────────────────────────
# Adım 1: extracted to web_research.py. _ddg_search is not re-exported here —
# it had zero consumers outside this module and its own new home; only
# web_research_strategies is imported, for propose_composed_strategy's call
# below.
from web_research import web_research_strategies  # noqa: E402

# Adım 4: the Claude Code CLI backend (_CLITextBlock/_CLIUsage/
# _CLIResponse/_CLIError, _poll_until_deadline, _ClaudeCLIMessages,
# _record_cli_side_models, _ClaudeCLIClient) extracted to llm_client.py,
# re-exported above.

# Adım 6 (Faz 2): the OpenRouter response shims (_ORTextBlock/_ORUsage/
# _ORResponse), _OpenRouterProcessError, _openrouter_usage_payload,
# _openrouter_process_main, _stop_provider_process, _run_openrouter_killable,
# _OpenRouterMessages, _OpenRouterClient, _build_openrouter_client,
# _get_openrouter_client extracted to openrouter_backend.py, re-exported
# above. Adım 9: _find_claude_cli -- once documented here as staying behind,
# unlike its OpenRouter neighbors, because it was _build_client's helper --
# moved too: the whole dispatch core (including _build_client, which calls
# both _find_claude_cli and openrouter_backend.py's _build_openrouter_client)
# now lives together in llm_dispatch.py; re-exported above.


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
    except Exception as e:
        logging.warning("propose_strategy: client setup failed: %s", e, exc_info=True)
        fb = _fallback_proposal()
        fb["rationale"] = f"fallback ({type(e).__name__})"
        _tag_degraded(fb, e)
        return fb

    user_msg = f"""Past iterations:
{_summarize_history(history)}

Propose the next strategy + parameters as JSON."""

    try:
        resp = _create_message(
            client,
            max_tokens=MAX_TOKENS_IDEA,
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
        fb["degraded"] = type(e).__name__
        return fb


# Default market context — if no market parameter is given, the Bybit crypto
# expression is preserved byte-for-byte (existing behavior unchanged).
_DEFAULT_MARKET_CONTEXT = (
    "crypto trading strategies on Bybit (BTCUSDT USDT perp, 1-minute bars)"
)

# Kabul ölçütü PROMPT'A YAZILIR — hedefi söylemeden hedef tutturulamaz.
#
# ÖLÇÜLDÜ (beş AUTO koşusu, 173 aday, 2026-08-18): kapının ölçüsü al-tut'a göre
# Calmar oranı; adayların medyanı ×0,21 ve yalnız %11'i ×1,00'ı geçti. Daha
# rahatsız edici olan kırılım: LLM erişilemediğinde devreye giren RASTGELE
# fallback kompozisyonları medyan ×0,40 / p90 ×1,01 ile üç Claude modelinin
# hepsini (×0,14-0,26) geçti — ve onlar `research-only` damgası yüzünden tanım
# gereği yayımlanamıyor. Yani sistem kapıya en yakın adaylarını kendi eliyordu.
#
# En olası açıklama prompt'ta duruyordu: sistem mesajı ne kabul ölçütünü ne de
# reddin en sık sebebini söylüyor, yalnız "makul bir strateji üret" diyor. Model
# hedefi bilmeden hedefe nişan alamaz.
#
# İki şey KASITLI olarak yazılmıyor:
#   * Skor formülü (`(0.7×Calmar + 0.3×Sharpe) × T/(T+20)`) — işlem sayısını
#     ödüllendiren çarpan doğrudan oynanabilir ve ölçüm frekans ile Calmar
#     arasında ilişki BULMADI (bantlar: %12/%4/%8/%15/%20, n=10'a kadar düşüyor).
#   * "Basit daha iyidir" bir YASA gibi — rastgele tabanın üstünlüğü bunu ima
#     ediyor ama örneklem dengesiz (rastgele n=72 iki koşudan, haiku n=11).
#     Bu yüzden sadelik bir HİPOTEZ olarak, deneme yönergesi biçiminde giriyor.
#
# Kapatma kolu var ve bilinçli: `AGENT_OBJECTIVE_IN_PROMPT=0` kontrol kolunu
# geri getirir. Hangi kolun koştuğu koşu kaydına yazılır (`objective_in_prompt`)
# — aksi hâlde iki koşuyu karşılaştıran kişi neyin değiştiğini bilemez.
OBJECTIVE_IN_PROMPT = os.environ.get("AGENT_OBJECTIVE_IN_PROMPT", "1") != "0"

_OBJECTIVE_BLOCK = """
ACCEPTANCE CRITERIA — this is what the candidate is judged on. Design FOR these,
not for a plausible-looking strategy:

1. RISK-ADJUSTED SUPERIORITY OVER BUY-AND-HOLD is the primary bar. The gate
   compares your strategy's Calmar ratio (annualised return / max drawdown)
   against buy-and-hold on the SAME instrument and window, and requires
   strategy_calmar > benchmark_calmar with positive CAGR. Measured reality: the
   median proposal reaches only ~20% of the buy-and-hold Calmar and ~1 in 9
   clears the bar. Beating the raw return is NOT enough — a strategy that earns
   less than buy-and-hold per unit of drawdown is rejected.
2. AT LEAST 20 closed trades in the training window, otherwise the result is
   discarded as statistically unusable regardless of profit.
3. It must also survive out-of-sample checks that you cannot see: the same spec
   is run on 5 peer instruments, on rolling walk-forward windows (over half of
   them must beat buy-and-hold too, not merely be profitable), on a Monte Carlo
   trade-order shuffle (median drawdown must stay under 25%), and finally on a
   sealed tail of the sample that needs at least a handful of entries. A spec
   that only works on one instrument or one period fails here.

Two design consequences follow from the measurements, and they are hypotheses to
try rather than rules to obey:

- DRAWDOWN CONTROL usually moves Calmar more than extra return does. A tight,
  well-reasoned exit (or a bracket stop) is worth more than another entry filter.
- FEWER CONDITIONS have been scoring better than heavily-filtered ones. Prefer
  2-3 blocks with a clear thesis over 4 blocks of stacked confirmation, unless
  you have a specific reason for the extra condition.
"""

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
{objective}

BLOCK CATALOG:
{catalog}

Sensible defaults if you have no strong reason to override: entry_logic="OR" (STRONGLY PREFERRED — AND only with exactly 2 blocks and a deliberate confluence reason), exit_logic="OR", order_type="market", use_bracket=false, allow_short=false, trade_size_mode="fixed".
"""


# Katalog özeti TTL memo'su — MALİYET düzeltmesi (2026-08-02): bu özet
# composed-proposer'ın SYSTEM prompt'una gömülür; AUTO loop her iterasyonda
# yeni agnt_* bloğu kaydettiğinden özet her çağrıda değişiyor ve ~30K'lık
# prompt cache'e her seferinde YENİDEN YAZILIYORDU (263 çağrı × ~30K = gecelik
# yazımların %80'i, cache_read=0). 30 dk'lık dondurma prefix'i bayt-sabit
# yapar → yazımlar okumaya döner (×2 → ×0.1). Yeni blokların özete ~30 dk
# gecikmeyle girmesi davranışsal olarak önemsizdir (fikir yolu zaten taze).
_CATALOG_SUMMARY_TTL_S = 1800.0
_catalog_summary_cache: tuple[float, str] | None = None
_catalog_summary_lock = threading.Lock()


def _catalog_summary(*, force: bool = False) -> str:
    global _catalog_summary_cache
    now = time.monotonic()
    with _catalog_summary_lock:
        if (
            not force
            and _catalog_summary_cache is not None
            and now - _catalog_summary_cache[0] < _CATALOG_SUMMARY_TTL_S
        ):
            return _catalog_summary_cache[1]

    from composer import BLOCK_CATALOG

    out = []
    generated = []
    for k, meta in BLOCK_CATALOG.items():
        # Skip lab-generated temporary blocks — they are EMA/RSI variants
        # that Claude itself produced; showing them biases proposals toward
        # the same family of indicators.
        if k.startswith("lab_entry_") or k.startswith("lab_exit_"):
            continue
        # Agent-generated blocks: ONE compact line each (params come from
        # registered defaults; the validator clamps). Full specs for hundreds
        # of self-produced variants both bloated the prompt (~100 token/blok)
        # and biased proposals toward Claude's own past output.
        if k.startswith("agnt_"):
            generated.append(f"- {k}: {meta['label']}")
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

    generated.sort()  # deterministik sıra — prefix kararlılığının parçası
    if len(generated) > 80:
        extra = len(generated) - 80
        generated = generated[:80] + [
            f"  (+{extra} more generated blocks omitted — defaults apply)"
        ]
    if generated:
        out.append("Generated blocks (params use registered defaults):")
        out.extend(generated)
    text = "\n".join(out)
    with _catalog_summary_lock:
        _catalog_summary_cache = (now, text)
    return text


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
            bi = getattr(r, "bars_info", None) or {}
            # The three fields below were missing and each cost the loop a
            # measurable mistake (audit 2026-08-04, run 1376c812):
            #   tf   — the timeframe is assigned by round-robin AFTER the spec is
            #          written, so a proposal tuned for minutes was scored on
            #          daily bars (one iteration produced a single trade). Without
            #          it in the feedback the model cannot even see the mismatch.
            #   dd   — users ask for "minimum drawdown" and the model was never
            #          shown drawdown; it cannot optimize what it cannot see.
            #   comm — one iteration paid 8,566 USD of commission on 52 USD of
            #          gross profit. Net PnL alone reads as "bad strategy"; with
            #          the commission line it reads as "too many trades".
            tf = bi.get("interval") or bi.get("granularity") or "?"
            lines.append(
                f"  · {r.strategy} tf={tf} pnl={m.get('pnl', 'n/a')} "
                f"sharpe={m.get('sharpe', 'n/a')} "
                f"max_dd={m.get('max_dd', 'n/a')} "
                f"commission={m.get('commission_total', 'n/a')} "
                f"trades={m.get('n_trades', 'n/a')} winrate={m.get('win_rate', 'n/a')} err={r.error}"
            )
        lines.append(
            "  → Read these together: `pnl` is NET of `commission`. If commission "
            "is a large share of the gross result the strategy trades too often "
            "for its timeframe — widen the entry filter or move to a slower tf. "
            "`max_dd` is a fraction and negative (-0.26 = 26% drawdown); a high "
            "pnl with a deep max_dd is not an improvement."
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

    rng = _fallback_rng()

    # Uygun tipler KATALOGUN İLAN ETTİĞİ ROLDEN türetilir, elle yazılmış bir
    # listeden değil.
    #
    # Eski hâli `exit_only = {"atr_stop"}` diye TEK isimlik bir kümeydi ve
    # yorumu doğru sebebi yazıyordu ("rol zorlanır → sıfır entry → ValueError")
    # ama kapsamı yalnız built-in'leri biliyordu. Oysa `BLOCK_CATALOG` çalışma
    # anında custom blokları da içeriyor ve onların meta'sında `role` İLAN
    # EDİLİYOR; `_coerce_block` ilan edilen rolle çelişen bloğu (haklı olarak)
    # sessizce çevirmek yerine DÜŞÜRÜYOR.
    #
    # ÖLÇÜLDÜ (bu kutudaki canlı katalog, 2026-08-18): 408 blok — 71 built-in
    # (rolsüz), 175 `entry`, **162 `exit`**. Yani fallback girişini 407 tip
    # arasından seçerken **%40 ihtimalle** exit-only bir blok seçiyordu; o
    # blok düşünce entry kalmıyor ve `_validate_composed` ValueError atıyordu.
    # Bu bir kenar durum değil, yazı-tura: AUTO koşusu 72029368'i öldüren şey
    # tam olarak buydu (44 fallback, biri round 3'ün ilk önerisinde patladı ve
    # `_propose_initial_strategy` onu yakalamadığı için TÜM oturum düştü).
    #
    # Eksik EXIT ölümcül değil (aşağıdaki onarım bir tane ekliyor); eksik ENTRY
    # ölümcül. Yine de iki taraf da aynı kuraldan türetiliyor — ikisini farklı
    # yerden türetmek bu hatanın ikinci yarısını açık bırakırdı.
    exit_only = {"atr_stop"}

    def _eligible(role: str) -> list[str]:
        opposite = "exit" if role == "entry" else "entry"
        out = [
            t
            for t, meta in BLOCK_CATALOG.items()
            if (meta.get("role") or role) != opposite
            and not (role == "entry" and t in exit_only)
        ]
        # Katalog beklenmedik biçimde boşsa built-in'lere düş: onların rolü
        # ilan edilmemiştir, yani her iki tarafta da geçerlidirler.
        return out or [t for t in BLOCK_CATALOG if not BLOCK_CATALOG[t].get("role")]

    entry_types = _eligible("entry")
    exit_types = _eligible("exit")
    entry_type = rng.choice(entry_types)
    exit_type = rng.choice([t for t in exit_types if t != entry_type] or exit_types)

    def _rand_params(btype: str) -> dict:
        p = {}
        for pname, pspec in BLOCK_CATALOG[btype]["params"].items():
            if pspec["type"] == "int":
                p[pname] = rng.randint(pspec["min"], pspec["max"])
            elif pspec["type"] == "float":
                p[pname] = round(rng.uniform(pspec["min"], pspec["max"]), 1)
            else:
                p[pname] = rng.choice(pspec["options"])
        return p

    def _fix_fast_slow(btype: str, params: dict) -> dict:
        """slow <= fast → swap to valid range for any crossover block."""
        if btype in ("ma_cross", "ema_cross", "macd_cross"):
            if params.get("slow", 0) <= params.get("fast", 0):
                params["fast"], params["slow"] = 10, 40
        return params

    def _compose(e_type: str, x_type: str) -> dict:
        return {
            "name": f"Random {e_type}/{x_type}",
            "description": "Fallback random composition (Claude unavailable).",
            "blocks": [
                {
                    "type": e_type,
                    "role": "entry",
                    "params": _fix_fast_slow(e_type, _rand_params(e_type)),
                },
                {
                    "type": x_type,
                    "role": "exit",
                    "params": _fix_fast_slow(x_type, _rand_params(x_type)),
                },
            ],
            "strategy_options": dict(_STRATEGY_OPTION_DEFAULTS),
        }

    # Run _validate_composed — fix the role of exit-only blocks like atr_stop
    try:
        return _validate_composed(_compose(entry_type, exit_type))
    except ValueError as exc:
        # SON SAVUNMA HATTI GERİ DÜŞEMEZ: bu fonksiyon zaten LLM'in düştüğü
        # yerde çağrılıyor, yani buradan atılan bir istisna koşunun tamamını
        # öldürüyor (ölçüldü: koşu 72029368, round 3). Built-in'ler rol İLAN
        # ETMEZ, dolayısıyla her iki tarafta da geçerlidirler — katalog ne
        # kadar bozulursa bozulsun bu kompozisyon doğrulanır.
        #
        # İkinci basamak SESSİZ DEĞİL: sessiz bir fallback, düzeltmeye
        # çalıştığı arızadan tehlikelidir — sebep log'a yazılır.
        builtins = [t for t in BLOCK_CATALOG if not BLOCK_CATALOG[t].get("role")]
        if not builtins:
            raise
        logging.warning(
            "fallback composition (%s/%s) did not validate (%s) — "
            "retrying with builtin blocks only",
            entry_type,
            exit_type,
            exc,
        )
        b_entry = rng.choice([t for t in builtins if t not in exit_only] or builtins)
        b_exit = rng.choice([t for t in builtins if t != b_entry] or builtins)
        return _validate_composed(_compose(b_entry, b_exit))


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


def _coerce_block(b) -> dict | None:
    """Tek bir blok önerisini katalog sözleşmesine indirger; uymuyorsa ``None``.

    ``_validate_composed`` ile ``_coerce_catalog_blocks``'un ORTAK çekirdeği.
    İkisi de aynı int/float clamp'lerini, aynı options whitelist'ini, aynı
    cross-family (``slow > fast``) düzeltmesini ve aynı ``atr_stop → exit``
    zorlamasını kopya kod olarak taşıyordu; kopyalardan yalnız biri test
    altındaydı (DeepR 2026-08-11 [ORTA]).

    Kopyalar bir yerde GERÇEKTEN ıraksamıştı: ``_coerce_catalog_blocks``
    declared-role denetimini hiç yapmıyordu, yani meta'sında ``role: "exit"``
    yazan bir custom block sohbetle taslak düzenleme yolundan ``role="entry"``
    ile spec'e girebiliyordu. Çalışma anında ``composer.register_custom_from_disk``
    bunu fail-closed edip her barda ``None`` döndürüyor — yani blok sessizce hiç
    sinyal üretmiyordu. Tek kaynağa indirmek bu ıraksamayı kapatır: rol çelişen
    blok artık iki yolda da DÜŞÜRÜLÜR.

    ``None`` dönen durumlar: dict olmayan öğe, katalog dışı tip, entry/exit
    dışı rol, meta'nın ilan ettiği rolle çelişen rol.
    """
    from composer import BLOCK_CATALOG

    if not isinstance(b, dict):
        return None
    btype = b.get("type")
    role = b.get("role")
    if btype not in BLOCK_CATALOG:
        return None
    if role not in ("entry", "exit"):
        return None
    meta = BLOCK_CATALOG[btype]
    declared_role = meta.get("role")
    if declared_role in ("entry", "exit") and role != declared_role:
        # Do not silently coerce a custom block into the opposite role.
        # Dropping it lets the existing missing-entry/missing-exit logic
        # reject or repair the proposal without changing signal semantics.
        return None
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
    return {"type": btype, "role": role, "params": params}


def _validate_composed(data: dict) -> dict:
    """Clamp params to catalog ranges and drop invalid blocks; raise on hopeless."""
    if not isinstance(data, dict) or "blocks" not in data:
        raise ValueError("missing 'blocks'")

    clean_blocks = []
    for b in data["blocks"]:
        coerced = _coerce_block(b)
        if coerced is not None:
            clean_blocks.append(coerced)

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


_TF_ABBREV_TO_SPELLED = {
    "1m": "1-minute",
    "5m": "5-minute",
    "15m": "15-minute",
    "30m": "30-minute",
    "1h": "1-hour",
    "4h": "4-hour",
    "12h": "12-hour",
    "1d": "daily",
}


def _build_tf_human() -> dict[str, str]:
    """Bybit-code / external-DSL → human phrase for LLM prompts.

    Derived from data.py's canonical interval tables (BYBIT_ALL_INTERVALS,
    EXTERNAL_GRAN_BY_BYBIT_CODE) instead of a third hand-maintained copy of
    the same code set — data.py already has two (BYBIT_ALL_INTERVALS and
    web/mission.py's ``_tf_short``); a new interval added there is now picked
    up here automatically instead of silently degrading to the raw code in
    the LLM prompt.
    """
    from data import BYBIT_ALL_INTERVALS, EXTERNAL_GRAN_BY_BYBIT_CODE

    out: dict[str, str] = {}
    for code, abbrev in BYBIT_ALL_INTERVALS:
        spelled = _TF_ABBREV_TO_SPELLED.get(abbrev, abbrev)
        out[code] = spelled
        dsl = EXTERNAL_GRAN_BY_BYBIT_CODE.get(code)
        if dsl:
            out[dsl] = spelled
    # 30m has no external-catalog counterpart (EXTERNAL_GRAN_BY_BYBIT_CODE
    # excludes it) but the DSL spelling is kept for callers that still pass it.
    out.setdefault("30-MINUTE", _TF_ABBREV_TO_SPELLED["30m"])
    return out


_TF_HUMAN = _build_tf_human()


def _timeframe_line(timeframe: str) -> str:
    """Prompt line naming the bar this spec will be scored on.

    The AUTO loop picks the timeframe round-robin and it used to do so AFTER the
    spec came back, so the model wrote indicator periods blind. On daily bars a
    minute-scale filter fires almost never (audit run 1376c812: a daily
    iteration opened exactly ONE trade and was disqualified for having <20 —
    the idea was never tested, only the mismatch was).
    """
    tf = (timeframe or "").strip()
    if not tf:
        return ""
    human = _TF_HUMAN.get(tf, tf)
    return (
        f"\nTARGET TIMEFRAME: this strategy will be backtested on {human} bars. "
        "Size every indicator period, threshold and stop for that bar — a filter "
        "tuned for minutes fires almost never on daily bars, and one tuned for "
        "days fires on every minute bar and pays commission on each."
    )


def propose_composed_strategy(
    history: list[Any],
    catalog: list[Any],
    hint: str = "",
    web_research: bool = False,
    market: str | None = None,
    timeframe: str = "",
) -> tuple[dict, dict | None]:
    """Ask Claude to design a full composed strategy.
    Returns (strategy_dict, usage_dict | None).
    usage_dict has keys: input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens.

    ``market`` — optional market context (e.g. "US equity QQQ.NASDAQ (1-DAY
    bars, USD cash account)"). If None the Bybit BTCUSDT expression is preserved as-is.

    ``timeframe`` — the bar interval this spec will ACTUALLY be scored on. The
    AUTO loop assigns timeframes round-robin, so the caller knows the target
    before it asks for the spec; passing it lets the model size periods and
    thresholds for that bar. Empty = don't mention it (prompt unchanged).
    """
    try:
        client = _get_client()
    except Exception as e:
        logging.warning(
            "propose_composed_strategy: client setup failed: %s", e, exc_info=True
        )
        fb = _fallback_composed()
        fb["description"] = (
            fb.get("description", "") + f" · fallback ({type(e).__name__})"
        ).strip()
        _tag_degraded(fb, e)
        return fb, None

    market_context = (
        f"trading strategies for {market} — a US stock from a historical Nautilus "
        "data catalog (2003→present). The account is a long-only USD CASH account: "
        "prefer allow_short=false, and trade_size is in whole SHARES (integer >= 1)"
        if market
        else _DEFAULT_MARKET_CONTEXT
    )
    system = (
        COMPOSED_SYSTEM_PROMPT.replace("{market_context}", market_context)
        .replace("{catalog}", _catalog_summary())
        .replace("{objective}", _OBJECTIVE_BLOCK if OBJECTIVE_IN_PROMPT else "")
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
    tf_line = _timeframe_line(timeframe)
    user = f"""Context:
{_summarize_composed_history(history, catalog)}{hint_line}{tf_line}{web_section}

Design a new {market_target} composed strategy as specified. Return JSON only."""

    try:
        resp = _create_message(
            client,
            _purpose="composed",
            max_tokens=MAX_TOKENS_COMPOSED,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        usage = _usage_dict(resp)
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
        data = json.loads(_extract_json_object(text))
        return _validate_composed(data), usage
    except Exception as e:
        _raise_if_llm_control_abort(e)
        fb = _fallback_composed()
        fb["description"] = (
            fb.get("description", "") + f" · fallback ({type(e).__name__})"
        ).strip()
        # Makine-okunur bozulma işareti. Açıklamanın kuyruğundaki metni ayrıştırmak
        # kırılgan; koşu bu alanı sayar ve fazı "degraded" kapatır — fallback bir
        # koşuyu başarı gibi göstermesin.
        _tag_degraded(fb, e)
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


def _find_evaluate_def(tree):
    """Find the top-level ``def evaluate(...)`` in a parsed module, if any.

    Shared by the AST role-contract checks below, which each independently
    searched for this same node with different empty-case handling (fallback
    to the whole tree vs. ``None``) — a predicate duplicated with different
    syntax makes it easy for the two checks to diverge over time even though
    they're meant to enforce the same rule.
    """
    import ast

    return next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "evaluate"
        ),
        None,
    )


def _test_execute_generated(
    src: str,
    meta: dict | None = None,
    require_max_lookback: bool = False,
    role_hint: str = "entry",
) -> None:
    """Smoke — ÖLDÜRÜLEBİLİR bir alt süreçte. Hata → ``GeneratedCodeError``.

    DeepR 2026-08-11 [ORTA]: bu fonksiyonun gövdesi (artık
    ``_test_execute_generated_inproc``) web sunucusunun kendi sürecinde
    exec ediyordu ve tek zaman koruması bir ``t.join(timeout=2.0)``'dı.
    O join hiçbir şeyi ÖLDÜRMEZ: süre dolunca kullanıcı "timed out" hatası
    alıyor, daemon thread arka planda çalışmaya devam ediyor ve tekrarlanan
    gönderimlerle sunucu OOM'a taşınabiliyordu. codegate'in kendi yorumu
    bunu belgeliyor bile: `9**9**9` gibi GIL'i bırakmayan tek bir bytecode'da
    join provably geri dönmez. Bir thread preempt edilemez — süreç edilebilir.
    Ayrıntılı gerekçe ve maliyet ölçümü: ``sandbox.run_block_smoke_guarded``.

    Sözleşme değişmedi: aynı imza, aynı istisna türü, aynı mesajlar. Sadece
    kod artık bu sürecin dışında koşuyor ve aşımda gerçekten ölüyor.
    """
    from sandbox import run_block_smoke_guarded

    err = run_block_smoke_guarded(
        src,
        meta,
        require_max_lookback=require_max_lookback,
        role_hint=role_hint,
    )
    if err is None:
        return
    # Çocuk her istisnayı "Tür: mesaj" olarak düzleştiriyor; zaten bizim olan
    # bir GeneratedCodeError'da o öneki geri sök ki mesaj aynen korunsun
    # (çağıranlar ve testler metne göre eşleşiyor).
    prefix = "GeneratedCodeError: "
    raise GeneratedCodeError(err[len(prefix) :] if err.startswith(prefix) else err)


def _test_execute_generated_inproc(
    src: str,
    meta: dict | None = None,
    require_max_lookback: bool = False,
    role_hint: str = "entry",
) -> None:
    """Compile + execute the module in an isolated namespace, then invoke
    evaluate() once with harmless inputs to catch runtime errors (NameError,
    KeyError on missing param, etc.). Raises GeneratedCodeError on failure.

    ÜRETİMDE DOĞRUDAN ÇAĞIRMAYIN — bu, ``sandbox._block_smoke_child``'ın alt
    süreçte koştuğu gövdedir; adı bilerek ``_inproc``. Doğrudan çağırmak
    2026-08-11 öncesindeki "sunucu sürecinde, öldürülemez" davranışa döner.

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
    # A single smoke input cannot exercise every conditional branch.  The live
    # AUTO review found exit blocks whose smoke returned None while a later
    # branch returned "long"/"short".  Validate every literal return in the
    # syntax tree before execution so dead/rare branches cannot evade the role
    # contract.  Dynamic expressions remain covered by the runtime check below.
    import ast

    try:
        tree = ast.parse(src, filename="<custom_block>")
    except SyntaxError as exc:
        raise GeneratedCodeError(f"invalid Python syntax: {exc}") from exc
    allowed_literals = {"long", "short"} if role_hint == "entry" else {"exit"}
    evaluate_def = _find_evaluate_def(tree)
    for node in ast.walk(evaluate_def if evaluate_def is not None else tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        if not isinstance(node.value, ast.Constant):
            raise GeneratedCodeError(
                f"evaluate() role return at line {getattr(node, 'lineno', '?')} "
                "must be a literal signal or None; dynamic returns are not auditable"
            )
        value = node.value.value
        if value is not None and value not in allowed_literals:
            expected = "'long'/'short'/None" if role_hint == "entry" else "'exit'/None"
            raise GeneratedCodeError(
                f"evaluate() violated the {role_hint} role contract at line "
                f"{getattr(node, 'lineno', '?')}: returned {value!r}; expected {expected}"
            )

    import math as _math
    import statistics as _stats

    import indicators as _ind_mod
    from codegate import compile_with_loop_budget, safe_module_proxy

    safe_globals = {
        # Single source of truth shared with composer._load_module_from_path so
        # smoke and runtime resolve the SAME restricted builtins (incl. the
        # RuntimeError the injected loop-budget guard raises).
        "__builtins__": _safe_builtins(),
        # Read-only proxies, not the live modules: this exec runs inside the web
        # server process, so a write through an injected module would poison every
        # later strategy in the process.
        "math": safe_module_proxy(_math, "math"),
        "statistics": safe_module_proxy(_stats, "statistics"),
        "ind": safe_module_proxy(_ind_mod, "ind"),
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
            self.role = role_hint
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

    allowed = {None, "long", "short"} if role_hint == "entry" else {None, "exit"}
    if out not in allowed:
        expected = "None/'long'/'short'" if role_hint == "entry" else "None/'exit'"
        raise GeneratedCodeError(
            f"evaluate() violated the {role_hint} role contract: returned {out!r}; "
            f"expected {expected}"
        )


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


# Adım 9: _usage_dict extracted to llm_dispatch.py (pure function, pulled up
# from this Domain C spot since llm_dispatch.py's own _create_message_once
# needs it too), re-exported above; every call site below is unchanged.


def _custom_block_system_prompt(role_hint: str) -> str:
    """Return a role-locked system prompt for one custom-block call.

    The generic prompt documents both return vocabularies.  In live AUTO runs
    Fable consequently generated entry signals (``long``/``short``) for three
    exit blocks even though the user prompt named the role.  Repeating the
    contract at system priority removes that ambiguity.
    """

    if role_hint == "exit":
        lock = (
            "\n\nROLE LOCK — EXIT ONLY:\n"
            "This call generates an EXIT block. Every firing branch in evaluate() "
            'must return the literal "exit". Non-firing branches return None. '
            'The strings "long" and "short" are forbidden anywhere in a '
            "return statement. Do not infer direction; closing either open side is "
            "the engine's responsibility."
        )
    else:
        lock = (
            "\n\nROLE LOCK — ENTRY ONLY:\n"
            "This call generates an ENTRY block. Every firing branch in evaluate() "
            'must return the literal "long" or "short". Non-firing branches '
            'return None. The string "exit" is forbidden in return statements.'
        )
    # Custom generation was the dominant AUTO token consumer in live runs:
    # one idea plus entry/exit code could spend several 4k+ CLI responses.
    # This is an instruction-level budget for the CLI path (which has no hard
    # output switch) and also keeps API output concise without weakening the
    # role/sandbox contract above.
    compact = (
        "\n\nCOMPACT OUTPUT BUDGET:\n"
        "Think silently. Return only the schema JSON, with no rationale beyond "
        "meta.help. Use at most 3 parameters and no helper unless essential; "
        "keep code at most 60 lines and the entire response under 1,800 tokens."
    )
    return CUSTOM_BLOCK_SYSTEM_PROMPT + lock + compact


def _env_bounded(name: str, default, *, lo=None, hi=None, cast=int):
    """Parse a numeric env var with clamping and a safe fallback on a bad value.

    Shared by the custom-block tunables below, which each independently
    hand-rolled the same try/except-ValueError + clamp shape.
    """
    try:
        v = cast(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return cast(default)
    if lo is not None:
        v = max(cast(lo), v)
    if hi is not None:
        v = min(cast(hi), v)
    return v


def _call_claude_for_block(
    user_prompt: str,
    *,
    role_hint: str = "entry",
    token_limit: int | None = None,
    tokens_spent: int = 0,
) -> tuple[dict, dict]:
    """Returns (parsed_json, usage) — M1583: count custom-block tokens too."""
    client = _get_client()
    # Compact JSON-only custom blocks do not need the generic 4k ceiling.
    # Keeping the limit below the prompt's 1,800-token contract reduces
    # OpenRouter latency and prevents oversized retry spend.
    # 2026-08-17: varsayılan 1.800'dü ve `hi` de 1.800'dü — yani ayar VARDI ama
    # yukarı yolu yoktu; ortam değişkeniyle bile çalışan bir değer verilemiyordu.
    # Defterden ölçüm (1.566 `custom_block` çağrısı, gerçekleşen `output`):
    # medyan 1.196 · p90 2.889 · p95 3.268 · p99 3.785 · maks 4.270 — yani
    # çağrıların yarısına yakını tavanı aşıyordu (canlı koşuda 22/39, %56).
    # Yeni varsayılan maks'ın üstünde; `hi` de tırmanma tavanına (16.000)
    # çekildi ki operatörün eli bağlı kalmasın.
    custom_max_tokens = _env_bounded(
        "AGENT_CUSTOM_BLOCK_MAX_TOKENS", 6_000, lo=512, hi=16_000
    )
    # A custom-block timeout should be shorter than the generic research
    # call deadline; the composer can safely select a builtin fallback.
    custom_timeout = _env_bounded(
        "AGENT_CUSTOM_BLOCK_TIMEOUT", 75.0, lo=15.0, hi=120.0, cast=float
    )
    request = {
        "_purpose": "custom_block",
        "max_tokens": custom_max_tokens,
        "timeout": custom_timeout,
        "system": _custom_block_system_prompt(role_hint),
        "messages": [{"role": "user", "content": user_prompt}],
    }

    # A custom block's budget has to apply to *provider attempts*, not merely
    # to the final response. `_create_message` may retry a truncated 4k answer
    # at 16k, and counting only after it returned made the advertised 25k cap
    # an overshoot detector rather than a spending guard. Temporarily compose a
    # local admission hook with AUTO's run-wide hook; this also keeps STOP and
    # the global 250k ceiling intact.
    if token_limit is None:
        resp = _create_message(client, **request)
        usage = _usage_dict(resp)
    else:
        limit = max(0, int(token_limit))
        spent_before = max(0, int(tokens_spent))
        observed: dict[str, int] = {}
        previous_admit = getattr(_LLM_CONTROL, "admit_check", None)
        previous_observer = getattr(_LLM_CONTROL, "observer", None)

        def _observed_total() -> int:
            return sum(
                int(observed.get(k) or 0)
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            )

        def _local_admit(bound: dict) -> None:
            reserve = max(0, int(bound.get("total_token_bound") or 0))
            projected = spent_before + _observed_total() + reserve
            if projected > limit:
                raise LLMTokenBudgetExceeded(
                    "custom_block token cap cannot admit provider attempt: "
                    f"{spent_before + _observed_total():,}/{limit:,} spent, "
                    f"{reserve:,} reserved"
                )
            if callable(previous_admit):
                previous_admit(bound)

        def _local_observer(event: dict) -> None:
            payload = event.get("usage") or {}
            if isinstance(payload, dict):
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                ):
                    observed[key] = observed.get(key, 0) + int(payload.get(key) or 0)
            if callable(previous_observer):
                previous_observer(event)

        _LLM_CONTROL.admit_check = _local_admit
        _LLM_CONTROL.observer = _local_observer
        try:
            resp = _create_message(client, **request)
        finally:
            _LLM_CONTROL.admit_check = previous_admit
            _LLM_CONTROL.observer = previous_observer
        # Includes a discarded truncated response, if any. The caller's retry
        # counter and telemetry now agree with actual provider attempts.
        usage = observed or _usage_dict(resp)
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    payload = _extract_json_object(text)
    try:
        return json.loads(payload), usage
    except json.JSONDecodeError as e:
        snippet = (payload[:400] if payload else text[:400]).replace("\n", "\\n")
        raise json.JSONDecodeError(
            f"{e.msg} — response snippet: {snippet!r}", e.doc or "", e.pos
        ) from None


def _repair_exit_return_literals(src: str) -> str | None:
    """Safely map literal entry signals to ``exit`` in an exit-only block.

    This is deliberately narrow: only direct literal returns inside
    ``evaluate`` are changed. Dynamic expressions, helper returns, syntax
    errors, and entry blocks still go through the normal retry/fallback path.
    The repaired source is re-run through the full AST and runtime gates by the
    caller before it can be persisted.
    """

    import ast

    try:
        tree = ast.parse(src, filename="<custom_block_repair>")
    except SyntaxError:
        return None
    evaluate = _find_evaluate_def(tree)
    if evaluate is None:
        return None
    changed = False
    for node in ast.walk(evaluate):
        if (
            isinstance(node, ast.Return)
            and isinstance(node.value, ast.Constant)
            and node.value.value in {"long", "short"}
        ):
            node.value = ast.copy_location(ast.Constant(value="exit"), node.value)
            changed = True
    if not changed:
        return None
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


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
    role_contract = (
        'This is an ENTRY block: every firing branch MUST return exactly "long" or "short"; '
        'never return "exit".'
        if role_hint == "entry"
        else 'This is an EXIT block: every firing branch MUST return exactly "exit"; '
        'never return "long" or "short".'
    )
    user_prompt = f"""Design a new signal block.

Label (user's short name for it): {label}
Role hint: {role_hint}
{role_line}
ROLE CONTRACT (applies to EVERY conditional branch): {role_contract}

Description (user's words — infer parameters, thresholds, logic):
\"\"\"{description.strip()}\"\"\"

Return the JSON only."""

    last_error = None
    _acc_usage: dict = {}
    _attempt_limit = _env_bounded("AGENT_CUSTOM_BLOCK_MAX_ATTEMPTS", 2, lo=1, hi=2)
    # 25.000 → 40.000 (2026-08-17). Bu bir KAÇAK-DÖNGÜ freni, üretimin normal
    # maliyet sınırı değil; frenin meşru bir üretimde ateşlemesi onu frenlikten
    # çıkarır. Defterden ölçüm (1.566 `custom_block` çağrısı, çağrı başına
    # input+output+cache toplamı): medyan 6.240 · p95 9.878 · maks 16.121. İki
    # denemelik en kötü hâl bugün bile 32.242 — yani eski değer ZATEN ateşleme
    # menzilindeydi. `max_tokens` 1.800'den 6.000'e çıkınca çıktı payı
    # büyüyeceği için sınır, gerçekçi en kötü hâlin ~2 katına çekildi.
    _token_limit = _env_bounded("AGENT_CUSTOM_BLOCK_TOKEN_LIMIT", 40_000, lo=4_000)

    def _spent_tokens() -> int:
        return sum(
            int(_acc_usage.get(k) or 0)
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        )

    def _retry_allowed() -> bool:
        nonlocal last_error
        spent = _spent_tokens()
        if spent >= _token_limit:
            last_error = (
                f"{last_error}; custom_block token limit reached "
                f"({spent:,}/{_token_limit:,})"
            )
            return False
        return True

    def _retry_with(next_prompt: str) -> bool:
        """Set the next retry prompt; report whether another attempt is allowed.

        Every failure branch below ends with "set the next prompt, then break
        if the retry budget is spent" — this collapses that pair to one call.
        """
        nonlocal user_prompt
        user_prompt = next_prompt
        return _retry_allowed()

    for attempt in range(_attempt_limit):
        try:
            data, _u = _call_claude_for_block(
                user_prompt,
                role_hint=role_hint,
                token_limit=_token_limit,
                tokens_spent=_spent_tokens(),
            )
            for k, v in _u.items():
                _acc_usage[k] = _acc_usage.get(k, 0) + v
        except LLMTokenBudgetExceeded as e:
            # Local cap refusal is intentional and cannot be healed by another
            # paid retry. Return a normal generated-code failure to the caller,
            # which can select the role-safe builtin candidate instead.
            last_error = str(e)
            break
        except Exception as e:
            _raise_if_llm_control_abort(e)
            if is_terminal_llm_error(e):
                raise TerminalLLMError(
                    f"terminal LLM provider failure: {type(e).__name__}: {e}"
                ) from e
            last_error = f"Claude request failed: {type(e).__name__}: {e}"
            # Repeating a hard provider timeout doubles wall-clock delay while
            # producing no usable block.  Bubble a normal generated-code
            # failure so AUTO's existing role-safe builtin fallback is used.
            if isinstance(e, TimeoutError):
                break
            if not _retry_with(
                f"Previous request failed ({type(e).__name__}). {user_prompt}"
            ):
                break
            continue

        if (
            not isinstance(data, dict)
            or "name" not in data
            or "meta" not in data
            or "code" not in data
        ):
            last_error = f"schema mismatch: missing keys in {list(data.keys()) if isinstance(data, dict) else type(data).__name__}"
            if not _retry_with(
                f"Your last output was invalid: {last_error}. Return JSON with keys name/meta/code."
            ):
                break
            continue

        name = str(data["name"]).strip()
        meta = data["meta"]
        code = data["code"]

        # Basic name validation happens later in the store; validate meta shape here.
        if not isinstance(meta, dict) or "label" not in meta or "params" not in meta:
            last_error = "meta must have label and params"
            if not _retry_with(
                f"Your last output was invalid: {last_error}. Fix and return valid JSON."
            ):
                break
            continue

        # Persist the provenance contract with the block. The composer validates
        # this metadata whenever a spec assigns the block to a role.
        meta = dict(meta)
        meta["role"] = role_hint

        try:
            _validate_generated_code(code)
            _test_execute_generated(
                code,
                meta=meta,
                require_max_lookback=True,
                role_hint=role_hint,
            )
        except GeneratedCodeError as e:
            last_error = str(e)
            # Live Fable runs repeatedly produced otherwise-valid exit code
            # whose firing branches returned the entry vocabulary.  For direct
            # literal returns the semantics are unambiguous: an exit condition
            # firing means ``exit``. Repair locally and re-run every safety gate
            # instead of paying for another full generation call.
            if role_hint == "exit" and "role contract" in last_error:
                repaired = _repair_exit_return_literals(code)
                if repaired is not None:
                    try:
                        _validate_generated_code(repaired)
                        _test_execute_generated(
                            repaired,
                            meta=meta,
                            require_max_lookback=True,
                            role_hint=role_hint,
                        )
                    except GeneratedCodeError as repair_error:
                        last_error = f"{last_error}; safe repair failed: {repair_error}"
                    else:
                        return {
                            "name": name,
                            "meta": meta,
                            "code": repaired,
                            "usage": _acc_usage,
                            "repair": "exit_literal_normalized",
                        }
            if not _retry_with(
                f"Your last code was REJECTED with this error:\n\n{last_error}\n\n"
                f"ROLE CONTRACT (still mandatory): {role_contract}\n"
                "Fix the code and return the same JSON schema. Remember: no imports, "
                "no leading underscore names, no try/with/lambda/global/nonlocal, only whitelisted "
                "attributes (.params/.role/.value/.upper/.lower/.middle/.initialized/.get/.keys/"
                ".values/.items) and only whitelisted builtins."
            ):
                break
            continue

        return {"name": name, "meta": meta, "code": code, "usage": _acc_usage}

    raise GeneratedCodeError(
        f"Claude could not produce valid code after {_attempt_limit} attempts. "
        f"Last error: {last_error}"
    )


# ── "Blok kodunu AI ile düzenle" — çok-turlu sohbet ─────────────────────────
# propose_custom_block (yukarı) tek-atış ÜRETİR; bu ise MEVCUT bir custom block'un
# kodunu kullanıcıyla SOHBET ederek düzenler. Sohbet danışman tonu _CHAT_SYSTEM_PROMPT
# (chat_refine) deseninden; kod/güvenlik kuralları CUSTOM_BLOCK_SYSTEM_PROMPT'tan gelir.
# Protokol: asistan serbest TR sohbet metni verir; kodu DEĞİŞTİRDİĞİNDE yanıtın EN SONUNA
# tek satır [NET_KOD]: işareti + tek bir JSON {"meta":..., "code":...} bloğu ekler.
_BLOCK_EDIT_SYSTEM_PROMPT = """Sen bir alım-satım (trading) sinyal BLOĞU (Python kodu) düzenleme asistanısın.
Kullanıcı MEVCUT bir bloğun kodunu seninle SOHBET ederek düzenliyor.
TÜM sohbet yanıtların TÜRKÇE, kısa ve net olsun.

Davranış:
- Kullanıcının isteğine DOĞRUDAN cevap ver; ne değiştirdiğini bir-iki cümleyle açıkla.
- Bloğun ADINI (name) ASLA değiştirme — sadece meta (label/params/help) ve code üzerinde çalış.
- Kullanıcı yalnızca SORU sorduysa (değişiklik istemediyse) kodu OLDUĞU GİBİ bırak; [NET_KOD] EKLEME.
- Mevcut kodu koru; yalnızca istenen değişikliği uygula, gereksiz yere baştan yazma.

Kod kuralları (ihlal edilirse kod reddedilir):
- `evaluate(state, block, closes, indicators, portfolio)` tanımlı olmalı; "long"/"short"/"exit"/None döner.
- `max_lookback(params)` modül seviyesinde tanımlı olmalı (gerekli bar sayısı).
- import YOK; `math`, `statistics`, `ind` (NAU-parite indikatör kütüphanesi) hazır — import gerekmez.
  Indikatör matematiğini elle yazma; `ind.calc_rsi/calc_atr/calc_adx/...` kullan.
- try/except, with, async, lambda, yield, global/nonlocal, delete YOK.
- `_` ile başlayan ad/attribute (dunder) YOK.
- İzinli built-in'ler: abs, min, max, sum, len, round, sorted, range, int, float, bool, str,
  list, tuple, dict, set, any, all, enumerate, zip, reversed, isinstance.
- Sinyal frekansı: blok çok seyrek ateşlerse işlem az olur ve elenir; eşikleri gevşek tut.

Kodu DEĞİŞTİRDİĞİNDE yanıtının EN SONUNA — sohbet metninden sonra — tam olarak şu biçimi ekle:
[NET_KOD]:
{"meta": {"label": "...", "params": {...}, "help": "..."}, "code": "def evaluate(...):\\n    ...\\n"}
Bu JSON tek satır veya çok satır olabilir ama GEÇERLİ JSON olmalı (code alanı \\n kaçışlı string).
Bu blok dışındaki metin kullanıcıyla serbest sohbetindir. Kodu değiştirmediysen bu bloğu HİÇ ekleme.
"""


def chat_edit_block(
    name: str,
    existing_meta: dict,
    existing_code: str,
    conversation_messages: list[dict],
) -> dict:
    """Mevcut bir custom block'un kodunu çok-turlu sohbetle düzenle.

    ``conversation_messages``: Anthropic messages[] turn'leri. İlk user turn'üne
    çağıran taraf mevcut kod+meta'yı gömer; bu fonksiyon listeyi olduğu gibi iletir.
    Döndürür: {"text", "meta", "code", "changed", "error", "usage"}.
      - ``changed`` True ise ``meta``/``code`` yeni (doğrulanmış) değerlerdir.
      - ``changed`` False ise kod DEĞİŞMEDİ (kullanıcı soru sormuş) — çağıran eski
        kod/meta'yı korur; ``error`` doluysa kod önerildi ama doğrulama başarısız.
    Herhangi bir LLM/parse hatasında nazik TR metin + ``changed=False`` döner.
    """
    client = _get_client()
    try:
        import re

        resp = _create_message(
            client,
            max_tokens=4000,
            system=_BLOCK_EDIT_SYSTEM_PROMPT,
            messages=conversation_messages,
        )
        usage = _usage_dict(resp)
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()

        # [NET_KOD]: işaretini ara; sonrasındaki JSON'u ayıkla (chat_refine deseni).
        m = re.search(r"\[NET_KOD\]\s*:", text)
        if not m:
            # Kod değişmedi — kullanıcı soru sormuş olabilir.
            if not text:
                text = (
                    "Anladım. Blokta ne değiştirmemi istediğini biraz daha açar mısın?"
                )
            return {
                "text": text,
                "meta": existing_meta,
                "code": existing_code,
                "changed": False,
                "error": "",
                "usage": usage,
            }

        chat_text = text[: m.start()].rstrip()
        payload = _extract_json_object(text[m.end() :])
        try:
            data = json.loads(payload)
            new_meta = data["meta"]
            new_code = data["code"]
            if (
                not isinstance(new_meta, dict)
                or "label" not in new_meta
                or "params" not in new_meta
            ):
                raise ValueError("meta must have label and params")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            return {
                "text": chat_text
                or "Kodu güncellemeye çalıştım ama çıktı bozuktu. Tekrar dener misin?",
                "meta": existing_meta,
                "code": existing_code,
                "changed": False,
                "error": f"model output parse error: {e}",
                "usage": usage,
            }

        # Güvenlik + smoke: propose_custom_block:1554-1566 ile birebir defense-in-depth.
        try:
            _validate_generated_code(new_code)
            edit_role = str(
                existing_meta.get("role") or new_meta.get("role") or "entry"
            )
            new_meta = dict(new_meta)
            new_meta["role"] = edit_role
            _test_execute_generated(
                new_code,
                meta=new_meta,
                require_max_lookback=True,
                role_hint=edit_role,
            )
        except GeneratedCodeError as e:
            # Kod reddedildi — eski geçerli kodu KORU, hatayı kullanıcıya göster
            # (otomatik iç-tur yok; kullanıcı sohbetle düzelttirir).
            return {
                "text": chat_text,
                "meta": existing_meta,
                "code": existing_code,
                "changed": False,
                "error": str(e),
                "usage": usage,
            }

        if not chat_text:
            chat_text = "Kodu güncelledim. Önizlemeyi kontrol edip kaydedebilirsin."
        return {
            "text": chat_text,
            "meta": new_meta,
            "code": new_code,
            "changed": True,
            "error": "",
            "usage": usage,
        }
    except Exception as _e:
        import logging as _logging

        _logging.getLogger("agent.chat").warning("chat_edit_block failed: %r", _e)
        return {
            "text": ("Şu an AI danışmana ulaşılamadı. Biraz sonra tekrar dener misin?"),
            "meta": existing_meta,
            "code": existing_code,
            "changed": False,
            "error": "",
            "usage": {},
        }


# ── "Blok listesini AI ile düzenle" — çok-turlu sohbet ──────────────────────
# Strateji taslağındaki (draft) SignalBlock listesini kullanıcıyla sohbet ederek
# düzenler. propose_composed_strategy'nin katalog-gömme (_catalog_summary) ve
# param-clamp (_coerce_catalog_blocks) mantığını yeniden kullanır; halüsinasyon
# blok tipi üretmeyi ENGELLER (yalnızca BLOCK_CATALOG'daki tipler geçer).
_BLOCKS_EDIT_SYSTEM_PROMPT = """Sen bir alım-satım (trading) stratejisi TASLAĞINI düzenleyen bir asistansın.
Kullanıcı, stratejinin BLOK LİSTESİNİ (giriş/çıkış sinyalleri) seninle SOHBET ederek düzenliyor.
TÜM sohbet yanıtların TÜRKÇE, kısa ve net olsun.

Kullanabileceğin blok tipleri YALNIZCA aşağıdaki katalogdur — BAŞKA tip UYDURMA:
{catalog}

Kurallar:
- Kullanıcının isteğine DOĞRUDAN cevap ver; ne değiştirdiğini bir-iki cümleyle açıkla.
- Her blok: type (yukarıdaki katalogdan), role ("entry" veya "exit"), params (kataloğun
  o tip için tanımladığı parametreler; verilmeyenler varsayılana düşer).
- Yalnızca SORU sorulduysa listeyi OLDUĞU GİBİ bırak; [NET_BLOKLAR] EKLEME.
- Mevcut listeyi koru; sadece istenen değişikliği uygula (blok ekle/çıkar/parametre değiştir).

Listeyi DEĞİŞTİRDİĞİNDE yanıtının EN SONUNA — sohbet metninden sonra — tam olarak şu biçimi ekle:
[NET_BLOKLAR]:
{"blocks": [{"type": "rsi_threshold", "role": "entry", "params": {"period": 14, "threshold": 30}}, ...]}
Bu GEÇERLİ bir JSON olmalı. Bu blok dışındaki metin kullanıcıyla serbest sohbetindir.
Listeyi değiştirmediysen bu bloğu HİÇ ekleme.
"""


def _coerce_catalog_blocks(raw_blocks: list) -> list[dict]:
    """Katalog-dışı tipleri düşür, paramları katalog aralığına clamp'le.

    Blok başına indirgeme ``_coerce_block`` ile TEK kaynaktan gelir (eskiden
    kopya koddu, bkz. o fonksiyonun docstring'i). Buradaki tek fark çevresi:
    entry/exit ZORUNLULUĞU ve fallback-exit EKLEMEZ — draft düzenlemede liste
    yarı-tamamlanmış olabilir. Dönen her öğe {type, role, params}.
    """
    out: list[dict] = []
    for b in raw_blocks or []:
        coerced = _coerce_block(b)
        if coerced is not None:
            out.append(coerced)
    return out


def chat_edit_blocks(
    existing_blocks: list[dict],
    conversation_messages: list[dict],
) -> dict:
    """Strateji taslağının blok listesini çok-turlu sohbetle düzenle.

    ``existing_blocks``: [{type, role, params}, ...]. Döndürür:
      {"text", "blocks", "changed", "error", "usage"}.
      - ``changed`` True ise ``blocks`` yeni (katalog-doğrulanmış) listedir.
      - ``changed`` False ise liste DEĞİŞMEDİ; ``error`` doluysa öneri geçersizdi.
    LLM/parse hatasında nazik TR metin + ``changed=False`` döner.
    """
    client = _get_client()
    try:
        import re

        system = _BLOCKS_EDIT_SYSTEM_PROMPT.replace("{catalog}", _catalog_summary())
        resp = _create_message(
            client,
            max_tokens=1500,
            system=system,
            messages=conversation_messages,
        )
        usage = _usage_dict(resp)
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()

        m = re.search(r"\[NET_BLOKLAR\]\s*:", text)
        if not m:
            if not text:
                text = "Anladım. Blok listesinde ne değiştirmemi istediğini biraz açar mısın?"
            return {
                "text": text,
                "blocks": existing_blocks,
                "changed": False,
                "error": "",
                "usage": usage,
            }

        chat_text = text[: m.start()].rstrip()
        payload = _extract_json_object(text[m.end() :])
        try:
            data = json.loads(payload)
            raw_blocks = data["blocks"]
            if not isinstance(raw_blocks, list):
                raise ValueError("blocks must be a list")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            return {
                "text": chat_text
                or "Listeyi güncellemeye çalıştım ama çıktı bozuktu. Tekrar dener misin?",
                "blocks": existing_blocks,
                "changed": False,
                "error": f"model output parse error: {e}",
                "usage": usage,
            }

        clean = _coerce_catalog_blocks(raw_blocks)
        if not clean:
            # Model yalnızca geçersiz/katalog-dışı tipler önerdi — eski listeyi koru.
            return {
                "text": chat_text,
                "blocks": existing_blocks,
                "changed": False,
                "error": "önerilen bloklar geçersiz (katalog-dışı tip veya boş liste)",
                "usage": usage,
            }

        if not chat_text:
            chat_text = (
                "Blok listesini güncelledim. Önizlemeyi kontrol edip onaylayabilirsin."
            )
        return {
            "text": chat_text,
            "blocks": clean,
            "changed": True,
            "error": "",
            "usage": usage,
        }
    except Exception as _e:
        import logging as _logging

        _logging.getLogger("agent.chat").warning("chat_edit_blocks failed: %r", _e)
        return {
            "text": ("Şu an AI danışmana ulaşılamadı. Biraz sonra tekrar dener misin?"),
            "blocks": existing_blocks,
            "changed": False,
            "error": "",
            "usage": {},
        }


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
A linear regression channel is also a real indicator here: least-squares line fitted over the
last N closes, plus/minus k x the standard deviation of the residuals (its slope is a trend
filter). Do NOT substitute SMA +/- std (that is Bollinger) for it.
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
    "Linear Regression Channel": ["regression", "regresyon", r"\blinreg\b"],
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
    timeframe: str = "",
) -> dict:
    """Ask Claude for a novel strategy idea (labels + descriptions only, no code).

    Returns dict with keys: name, description, entry_label, entry_desc,
    exit_label, exit_desc. Falls back to a hardcoded idea on any failure.

    ``market`` — optional market context; if None the crypto phrasing is kept.
    ``timeframe`` — the bar this idea will be scored on (see _timeframe_line);
    the custom-block path needs it as much as the builtin one.
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

    market_note += _timeframe_line(timeframe)

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
            _purpose="idea",
            max_tokens=MAX_TOKENS_IDEA,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
        idea = json.loads(_extract_json_object(text))
        idea["usage"] = _usage_dict(resp)  # M1583: count idea tokens too
        return idea
    except Exception as e:
        _raise_if_llm_control_abort(e)
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
        # Bozulma işareti fikirle birlikte taşınır: bu strateji modelin önerisi
        # DEĞİL, kaynak kodda sabit duran bir dizge. İşaretsiz bırakılırsa makul
        # bir yedek (ör. "RSI Oversold Reversal") sıralamada gerçek önerilerle
        # yarışır ve "kazanan" ilan edilir.
        fb = dict(_FALLBACK_IDEAS[idx])
        _tag_degraded(fb, e)
        return fb


if __name__ == "__main__":
    print(json.dumps(propose_strategy([]), indent=2))
    print("---")
    print(json.dumps(propose_composed_strategy([], []), indent=2))
