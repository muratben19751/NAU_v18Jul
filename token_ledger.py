"""Persistent per-model LLM token ledger.

Wiki References
---------------
See: [[webapp_module_map]] (bu modülün rolü + `_create_message` ledger-hook'u),
[[crash_only_design]] (best-effort append; ledger I/O bir LLM çağrısını asla
bozamaz — restart'ta veri kaybolmasın diye diske yazılır),
[[llm_maliyet_kaldiraclari]] (bu defterden çıkarılan maliyet denetimi: kalem
kırılımı, model kıyası, çağrı-sınıfı başına cache oranı), [[nau_performans_denetimi]]
(`/tokens/badge`'in 60sn'de bir tetiklediği maliyet: 2026-08-08'de tam-dosya
yeniden OKUMA, 2026-08-11'de tam-liste yeniden KATLAMA — bugün `_folded`
artımlı akümülatörü ikisini de kaldırdı).

Every LLM call in the app funnels through ``agent._create_message`` (and the four
narrative/summary helpers that were routed through it). Each successful call
appends ONE JSONL line here with the **actual** model the API answered with —
so the Fable→Opus credit fallback is attributed to the model that really ran,
not the one the app defaults to. This is the durable source the in-memory
``_AGENT_PROGRESS`` counters never were (they were AUTO-loop-scoped, single-model,
and lost on restart — see the second-brain note ``nau_token_tuketim_izleme``).

Design contract:
- ``record`` is best-effort and MUST NOT raise into an LLM call path — a ledger
  I/O hiccup can never break strategy generation. All failures are swallowed.
- One JSONL line per call: ts, model, purpose, and the four token counts.
- ``summary`` folds the log into a per-model table; malformed/partial lines
  (a line torn by a concurrent append) are skipped, not fatal.

Location: ``~/.cache/nautilus_web_app/token_usage.jsonl`` (same cache root as the
agent session logs).
"""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import UTC, datetime
from pathlib import Path

from app_constants import DATA_DIR

LEDGER_PATH = DATA_DIR / "token_usage.jsonl"  # kök: app_constants.DATA_DIR

_LOCK = threading.Lock()

# The four usage fields, in the order the app already uses everywhere.
_FIELDS = ("input", "output", "cache_read", "cache_write")

# ── Pricing (USD per 1M tokens, Anthropic API list prices) ───────────────────
# Source: claude-api skill model table (cached 2026-06-24). Cache multipliers
# are Anthropic's published ones: reads 0.1× input; writes 1.25× input for the
# 5-minute TTL and 2× input for the 1-hour TTL. The Claude CLI writes with the
# 1-HOUR TTL (probe 2026-08-01: our 2× math reproduced the CLI's own
# ``total_cost_usd`` to the cent; 1.25× under-billed by 28%), so unlabeled
# cache_write lines — every line this app's CLI backend ever wrote before the
# 5m/1h split was recorded — price at the 1h rate. Prefix-matched so dated
# variants (claude-opus-4-8-20xx…) price the same as their alias; an unknown
# model prices as None and shows "?" rather than inventing a number.
# NOTE: calls via the Claude CLI ride the subscription — this is the NOTIONAL
# API-equivalent cost, shown so usage has a money scale, not an invoice.
_PRICES_PER_MTOK: tuple[tuple[str, float, float], ...] = (
    # (model-id prefix, input $/MTok, output $/MTok)
    ("claude-fable-5", 10.0, 50.0),
    ("claude-mythos-5", 10.0, 50.0),
    ("claude-opus", 5.0, 25.0),  # Opus 5 / 4.8 / 4.7 / 4.6 all $5/$25
    ("claude-sonnet", 3.0, 15.0),
    ("claude-haiku", 1.0, 5.0),
)
_CACHE_READ_MULT = 0.1
_CACHE_WRITE_5M_MULT = 1.25
_CACHE_WRITE_1H_MULT = 2.0


def cost_usd(counts: dict, model: str) -> float | None:
    """Notional API cost of one counter dict for ``model``; None if unpriced."""
    for prefix, in_p, out_p in _PRICES_PER_MTOK:
        if (model or "").startswith(prefix):
            w5 = counts.get("cache_write_5m", 0)
            w1 = counts.get("cache_write_1h", 0)
            # Unlabeled remainder = written before the TTL split existed; all
            # of it came through the Claude CLI → 1h TTL (see pricing note).
            unlabeled = max(counts.get("cache_write", 0) - w5 - w1, 0)
            return (
                counts.get("input", 0) * in_p
                + counts.get("cache_read", 0) * in_p * _CACHE_READ_MULT
                + w5 * in_p * _CACHE_WRITE_5M_MULT
                + (w1 + unlabeled) * in_p * _CACHE_WRITE_1H_MULT
                + counts.get("output", 0) * out_p
            ) / 1_000_000
    return None


# Anthropic/CLI usage attribute (or dict-key) names → our short field names.
_USAGE_KEYS = {
    "input": ("input_tokens",),
    "output": ("output_tokens",),
    "cache_read": ("cache_read_input_tokens",),
    "cache_write": ("cache_creation_input_tokens",),
}


def _extract_usage(usage) -> dict[str, int]:
    """Normalize an Anthropic ``resp.usage`` object OR a plain dict → the four
    int counters (+ the cache-write TTL split when the response carries one).
    Missing/None fields become 0."""
    out = {f: 0 for f in _FIELDS}
    if usage is None:
        return out
    is_dict = isinstance(usage, dict)
    for field, keys in _USAGE_KEYS.items():
        for k in keys:
            val = usage.get(k) if is_dict else getattr(usage, k, None)
            if val:
                out[field] = int(val)
                break
    # Cache-write TTL split (``usage.cache_creation``): 5m and 1h writes price
    # differently (1.25× vs 2× input), so keep the split when it's available.
    cc = (
        usage.get("cache_creation")
        if is_dict
        else getattr(usage, "cache_creation", None)
    )
    if cc is not None:
        cc_dict = isinstance(cc, dict)
        for field, key in (
            ("cache_write_5m", "ephemeral_5m_input_tokens"),
            ("cache_write_1h", "ephemeral_1h_input_tokens"),
        ):
            val = cc.get(key) if cc_dict else getattr(cc, key, None)
            if val:
                out[field] = int(val)
    return out


def _extract_provider_cost(usage) -> float | None:
    """Read an exact provider-reported USD cost when the response has one."""

    if usage is None:
        return None
    raw = (
        usage.get("cost_usd")
        if isinstance(usage, dict)
        else getattr(usage, "cost_usd", None)
    )
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def record(model: str, usage, purpose: str = "") -> None:
    """Append one usage line. Best-effort — never raises into the caller.

    ``model`` should be the model the API actually answered with
    (``getattr(resp, "model", called_model)``), so fallback is attributed
    correctly. ``purpose`` is a free-text tag for the call site (e.g.
    "strategy", "custom_block", "narrative"); "" is fine.
    """
    try:
        counts = _extract_usage(usage)
        if not any(counts.values()):
            return  # nothing to record (e.g. an empty/failed response)
        line = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "model": model or "unknown",
            "purpose": purpose or "",
            # TTL-split keys only when present — legacy line shape otherwise.
            **{k: v for k, v in counts.items() if k in _FIELDS or v},
        }
        provider_cost = _extract_provider_cost(usage)
        if provider_cost is not None:
            # OpenRouter and Claude CLI can return the billed request cost. It
            # must survive a server restart; the old ledger kept only tokens and
            # silently turned an invoice fact back into a local price estimate.
            line["provider_cost_usd"] = provider_cost
        payload = json.dumps(line, ensure_ascii=False)
        with _LOCK:
            LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LEDGER_PATH, "a", encoding="utf-8") as f:
                f.write(payload + "\n")
    except Exception:
        # A ledger write must never break an LLM call — but it must not vanish
        # without a trace either.
        logging.getLogger(__name__).warning("token_ledger.record failed", exc_info=True)


# Perf (DeepR 2026-08-08 [ORTA] → 2026-08-11 [ORTA]): incremental FOLD cache.
#
# İlk tur dosya okumasını artımlı hale getirdi ama PARSE EDİLMİŞ KAYIT
# LİSTESİNİ tutuyordu; `summary()` her çağrıda o listenin TAMAMINI yeniden
# katlıyordu. `/tokens/badge` (base.html, `every 60s` + her sayfa yükünde)
# summary'yi İKİ kez çağırıyor (`since=SERVER_STARTED_AT` + tüm-zaman), yani
# yoklama başına iki tam O(n) geçiş. Liste de hiç kırpılmıyordu: ledger her
# LLM çağrısında bir satır büyüyor, AUTO oturumları binlerce çağrı üretiyor.
#
# Artık kayıt listesi HİÇ tutulmuyor. Durum (path, since) çiftine göre
# tutuluyor ve yalnız MODEL BAŞINA toplamları taşıyor (~5 sözlük), yani RAM
# kayıt sayısından bağımsız. Her çağrı sadece kendi offset'inden sonra EKLENEN
# baytları okuyup akümülatöre katlıyor → yoklama maliyeti O(yeni bayt).
#
# GEÇERSİZLEŞME SÖZLEŞMESİ:
#   • Bayatlama yok: her çağrı `stat()` ile dosya boyuna bakar, büyümüşse
#     yeni baytları okur. Ledger yalnızca `record()`'un append'iyle büyür.
#   • Dosya KÜÇÜLDÜYSE (rotasyon/silme/truncate) o anahtarın durumu sıfırlanır
#     ve dosya baştan katlanır — sessizce eksik saymaktansa yeniden say.
#   • Son onaylı '\n'e kadar tüketilir; yazılmakta olan yarım satır bir
#     SONRAKİ çağrıya bırakılır (bkz. record()'un torn-line notu).
#   • Anahtar sayısı `_PARSE_CACHE_MAX_KEYS` ile sınırlı. Gerçekte iki anahtar
#     var (None + SERVER_STARTED_AT); sınıra çarpmak beklenmedik bir durumdur,
#     bu yüzden tahliye SESSİZ DEĞİL, warning ile loglanır. Tahliye edilen
#     anahtar bir sonraki çağrıda dosyayı baştan katlar (doğru, sadece yavaş).
_PARSE_CACHE: dict[tuple[str, str | None], dict] = {}
_PARSE_CACHE_LOCK = threading.Lock()
_PARSE_CACHE_MAX_KEYS = 8


def _new_counters() -> dict:
    """Bir modelin (ya da genel toplamın) sıfırlanmış sayaç sözlüğü."""
    return {
        "calls": 0,
        **{f: 0 for f in _FIELDS},
        "total": 0,
        "provider_cost_usd": 0.0,
        "provider_cost_calls": 0,
        "_estimated_missing_cost_usd": 0.0,
        "_unpriced_missing_calls": 0,
    }


def _new_fold() -> dict:
    return {
        "offset": 0,
        "models": {},
        "grand": _new_counters(),
        "first_ts": None,
        "last_ts": None,
    }


def _fold_record(state: dict, rec: dict, since: str | None) -> None:
    """Tek bir ledger satırını akümülatöre ekle (eski summary döngüsünün gövdesi)."""
    if since and (rec.get("ts") or "") < since:
        return
    model = rec.get("model") or "unknown"
    m = state["models"].setdefault(model, _new_counters())
    grand = state["grand"]
    m["calls"] += 1
    grand["calls"] += 1
    for fld in _FIELDS:
        v = int(rec.get(fld) or 0)
        m[fld] += v
        m["total"] += v
        grand[fld] += v
        grand["total"] += v
    # TTL split rides along for pricing only — it is a breakdown of
    # cache_write, so it must NOT be added into "total" again.
    for fld in ("cache_write_5m", "cache_write_1h"):
        v = int(rec.get(fld) or 0)
        if v:
            m[fld] = m.get(fld, 0) + v
    reported = rec.get("provider_cost_usd")
    try:
        reported = float(reported) if reported is not None else None
    except (TypeError, ValueError):
        reported = None
    if reported is not None and math.isfinite(reported) and reported >= 0:
        m["provider_cost_usd"] += reported
        m["provider_cost_calls"] += 1
        grand["provider_cost_usd"] += reported
        grand["provider_cost_calls"] += 1
    else:
        line_counts = {
            fld: int(rec.get(fld) or 0)
            for fld in (*_FIELDS, "cache_write_5m", "cache_write_1h")
        }
        estimate = cost_usd(line_counts, model)
        if estimate is None:
            m["_unpriced_missing_calls"] += 1
            grand["_unpriced_missing_calls"] += 1
        else:
            m["_estimated_missing_cost_usd"] += estimate
            grand["_estimated_missing_cost_usd"] += estimate
    ts = rec.get("ts")
    if ts:
        if state["first_ts"] is None or ts < state["first_ts"]:
            state["first_ts"] = ts
        if state["last_ts"] is None or ts > state["last_ts"]:
            state["last_ts"] = ts


def _folded(p: Path, since: str | None) -> dict:
    """(p, since) için artımlı katlama durumu — yalnız yeni baytları okur."""
    key = (str(p), since)
    with _PARSE_CACHE_LOCK:
        state = _PARSE_CACHE.get(key)
        if state is None:
            if len(_PARSE_CACHE) >= _PARSE_CACHE_MAX_KEYS:
                oldest = next(iter(_PARSE_CACHE))
                _PARSE_CACHE.pop(oldest, None)
                logging.getLogger(__name__).warning(
                    "token_ledger fold cache full (%d keys) — evicted %r; the next "
                    "summary() for it re-folds the whole ledger",
                    _PARSE_CACHE_MAX_KEYS,
                    oldest,
                )
            state = _new_fold()
            _PARSE_CACHE[key] = state
        try:
            size = p.stat().st_size
        except OSError:
            return state
        if size < state["offset"]:
            state = _new_fold()
            _PARSE_CACHE[key] = state
        if size > state["offset"]:
            with open(p, "rb") as f:
                f.seek(state["offset"])
                chunk = f.read()
            last_nl = chunk.rfind(b"\n")
            if last_nl != -1:
                usable = chunk[: last_nl + 1]
                state["offset"] += len(usable)
                for ln in usable.decode("utf-8", errors="ignore").splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rec = json.loads(ln)
                    except Exception:
                        continue  # malformed line — permanently skipped, as before
                    _fold_record(state, rec, since)
        return state


def summary(path: Path | None = None, *, since: str | None = None) -> dict:
    """Fold the ledger into a per-model breakdown.

    ``since`` (ISO-8601 UTC string) keeps only records with ``ts >= since`` —
    string comparison is safe because every line is written with the same
    ``isoformat(timespec="seconds")`` shape. This is how "this session"
    (= since server start) views are produced without a second ledger.

    Returns::

        {
          "models": {
            "claude-fable-5": {"calls": 12, "input": …, "output": …,
                               "cache_read": …, "cache_write": …, "total": …,
                               "cost_usd": 1.23 | None},
            "claude-opus-4-8": {…},
          },
          "total": {"calls": …, "input": …, …, "total": …, "cost_usd": …},
          "first_ts": "…", "last_ts": "…",
        }

    ``cost_usd`` is the NOTIONAL Anthropic-API list cost (see _PRICES_PER_MTOK
    — CLI/subscription calls aren't billed per token; this gives usage a money
    scale). ``None`` when the model isn't in the price table; the total prices
    only priced models. Missing/torn JSONL lines are skipped. Returns empty
    structures if the ledger does not exist yet.
    """
    p = path or LEDGER_PATH
    models: dict[str, dict] = {}
    grand = _new_counters()
    first_ts = last_ts = None
    if not p.exists():
        return {
            "models": models,
            "total": {**grand, "cost_usd": 0.0, "cost_source": "price_table"},
            "first_ts": None,
            "last_ts": None,
        }
    try:
        # Katlama akümülatörü ÖNBELLEKTE yaşıyor; aşağıdaki bitirme adımı
        # (fiyatlama + özel anahtarların silinmesi) sözlükleri mutasyona
        # uğrattığı için kopya üzerinde çalışılır — yoksa bir sonraki çağrı
        # bozulmuş bir akümülatör bulurdu.
        state = _folded(p, since)
        models = {name: dict(counters) for name, counters in state["models"].items()}
        grand = dict(state["grand"])
        first_ts, last_ts = state["first_ts"], state["last_ts"]
    except Exception:
        logging.getLogger(__name__).warning(
            "token_ledger.summary read failed", exc_info=True
        )
    total_cost = 0.0
    for model, m in models.items():
        # Keep the all-token list-price estimate for comparison, but prefer an
        # exact reported cost for each line that supplied one. Mixed ledgers
        # combine exact costs with estimates only for the unreported lines —
        # but if ANY call couldn't be priced (no provider cost, no price-table
        # entry), the total is incomplete and must read as unknown (None)
        # rather than silently understating it as the sum of what we do know.
        m["estimated_cost_usd"] = cost_usd(m, model)
        c = (
            None
            if m["_unpriced_missing_calls"]
            else m["provider_cost_usd"] + m["_estimated_missing_cost_usd"]
        )
        m["cost_usd"] = c
        m["cost_source"] = _cost_source(m["provider_cost_calls"], m["calls"])
        del m["_estimated_missing_cost_usd"]
        del m["_unpriced_missing_calls"]
        if c is not None:
            total_cost += c
    grand["estimated_cost_usd"] = sum(
        float(m["estimated_cost_usd"] or 0.0) for m in models.values()
    )
    grand["cost_usd"] = round(total_cost, 4)
    grand["cost_source"] = _cost_source(grand["provider_cost_calls"], grand["calls"])
    del grand["_estimated_missing_cost_usd"]
    del grand["_unpriced_missing_calls"]
    return {"models": models, "total": grand, "first_ts": first_ts, "last_ts": last_ts}


def _cost_source(provider_cost_calls: int, calls: int) -> str:
    if provider_cost_calls == calls:
        return "provider_reported"
    if provider_cost_calls:
        return "mixed"
    return "price_table"


def format_table(path: Path | None = None) -> str:
    """A plain-text per-model table for CLI / text/plain responses."""
    s = summary(path)
    models = s["models"]
    if not models:
        return "No token usage recorded yet (token_usage.jsonl is empty or absent)."

    headers = ["MODEL", "CALLS", "INPUT", "OUTPUT", "CACHE_RD", "CACHE_WR", "TOTAL"]
    rows = []
    # Highest total first.
    for model, m in sorted(models.items(), key=lambda kv: kv[1]["total"], reverse=True):
        rows.append(
            [
                model,
                f"{m['calls']:,}",
                f"{m['input']:,}",
                f"{m['output']:,}",
                f"{m['cache_read']:,}",
                f"{m['cache_write']:,}",
                f"{m['total']:,}",
            ]
        )
    g = s["total"]
    total_row = [
        "TOTAL",
        f"{g['calls']:,}",
        f"{g['input']:,}",
        f"{g['output']:,}",
        f"{g['cache_read']:,}",
        f"{g['cache_write']:,}",
        f"{g['total']:,}",
    ]

    widths = [len(h) for h in headers]
    for r in rows + [total_row]:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))

    def _fmt(cells: list[str]) -> str:
        # Left-align the model name, right-align the numbers.
        out = [cells[0].ljust(widths[0])]
        out += [cells[i].rjust(widths[i]) for i in range(1, len(cells))]
        return "  ".join(out)

    sep = "-" * (sum(widths) + 2 * (len(headers) - 1))
    lines = [_fmt(headers), sep]
    lines += [_fmt(r) for r in rows]
    lines += [sep, _fmt(total_row)]
    span = ""
    if s["first_ts"] and s["last_ts"]:
        span = f"\n\nRange: {s['first_ts']} → {s['last_ts']}"
    return "\n".join(lines) + span


if __name__ == "__main__":
    # `python token_ledger.py` → print the per-model token table.
    print(format_table())
