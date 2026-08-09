"""Web research (DuckDuckGo, no API key required).

Feeds `agent.propose_composed_strategy` optional real-world context; degrades
to "" on any failure (network, missing `ddgs` package, empty results) so a
caller can apply its own fallback without special-casing this module.

agent.py decomposition (Adım 1, safe-first slice): extracted verbatim from
agent.py, zero internal dependency on anything else in that file.

Wiki References
---------------
See: [[model_secici_ve_gorunurluk]].
"""

from __future__ import annotations


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
