"""GET /tokens/badge — Claude token spend + notional USD, session-scoped.

Renders the small usage line under the sidebar ENGINE card: tokens consumed
and their Anthropic-API-list-price dollar equivalent, "session" = since this
server process started (``web.shared.SERVER_STARTED_AT`` filtering the
persistent ledger — one source of truth, no second counter), plus the
all-time ledger total as a tooltip/secondary line. Costs are NOTIONAL: LLM
calls ride the Claude CLI subscription, so this is a money-scale for usage,
not an invoice (see ``token_ledger._PRICES_PER_MTOK``).

``GET /tokens/table`` returns the full per-model plain-text table (the
``python token_ledger.py`` CLI view) for a quick drill-down.

Wiki References
---------------
See: [[webapp_module_map]] (bu modülün satırı), [[nau_token_tuketim_izleme]]
(ledger'ın neden kalıcı ve tek-kaynak olduğu).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse

import token_ledger
from web.shared import SERVER_STARTED_AT

router = APIRouter()


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_usd(v: float) -> str:
    return f"${v:.2f}" if v >= 0.01 or v == 0 else f"${v:.4f}"


@router.get("/tokens/badge", response_class=HTMLResponse)
def tokens_badge() -> str:
    ses = token_ledger.summary(since=SERVER_STARTED_AT)
    all_time = token_ledger.summary()
    s, a = ses["total"], all_time["total"]
    return (
        '<div class="token-badge" title="Claude token kullanımı — '
        "oturum = sunucu başlangıcından beri; $ = Anthropic API liste "
        'fiyatıyla kavramsal karşılık (abonelik faturası değil)">'
        f'<span class="tb-k">Tokens</span>'
        f'<span class="tb-v mono">{_fmt_tokens(s["total"])}</span>'
        f'<span class="tb-usd mono">{_fmt_usd(s["cost_usd"])}</span>'
        f'<span class="tb-all mono">Σ {_fmt_tokens(a["total"])} · '
        f"{_fmt_usd(a['cost_usd'])}</span>"
        "</div>"
    )


@router.get("/tokens/table", response_class=PlainTextResponse)
def tokens_table() -> str:
    return token_ledger.format_table()
