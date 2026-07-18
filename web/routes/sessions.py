"""Agent Session Logs — tüm otonom session loglarını görüntüle.

Endpoints:
    GET /sessions          Session listesi
    GET /sessions/{run_id} Tek session detayı
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

router = APIRouter(prefix="/sessions")

# Canonical path — shared with agent_backtest.py (import avoids duplication)
try:
    from web.routes.agent_backtest import SESSION_LOG_DIR
except ImportError:
    SESSION_LOG_DIR = Path.home() / ".cache" / "nautilus_web_app" / "agent_sessions"

# ── Helpers ───────────────────────────────────────────────────────────────────


# H7: yapısal olaylar HER ZAMAN tutulur — cap yalnız 'step'lere uygulanır.
# Eski satır-sayısı cap'i continuous oturumlarda sonraki turların
# backtest_result/winner/token_snapshot olaylarını (binlerce step'ten SONRA
# gelirler) sessizce düşürüyordu: sayfa 10 turluk oturumu 2 tur gösterirdi.
_STRUCTURAL_EVENTS = frozenset(
    {
        "session_start",
        "session_end",
        "winner",
        "backtest_result",
        "robustness_result",
        "token_snapshot",
        "timeline",
        "phase_change",
        "strategy_proposed",
        "custom_block_generated",
        "holdout_result",
        "custom_block_error",
    }
)


def _read_events(run_id: str, max_lines: int | None = None) -> tuple[list[dict], bool]:
    """JSONL olaylarını oku → (events, truncated).

    H7: ``max_lines`` yalnız step-benzeri olayları sınırlar (deque ile en
    YENİ step'ler tutulur); yapısal olaylar dosya sonuna dek toplanır.
    M13: yalnız ``'event'`` anahtarlı dict satırlar kabul edilir — şemasız
    ama geçerli-JSON tek satır bütün sayfayı 500'e düşürüyordu.
    """
    path = SESSION_LOG_DIR / f"{run_id}.jsonl"
    if not path.exists():
        return [], False
    structural: list[tuple[int, dict]] = []
    steps: deque[tuple[int, dict]] = deque(maxlen=max_lines or None)
    n_steps_seen = 0
    try:
        with path.open() as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict) or "event" not in obj:
                    continue  # M13: parse guard
                if obj["event"] in _STRUCTURAL_EVENTS:
                    structural.append((i, obj))
                else:
                    steps.append((i, obj))
                    n_steps_seen += 1
    except Exception:
        pass
    truncated = bool(max_lines) and n_steps_seen > len(steps)
    merged = sorted([*structural, *steps], key=lambda t: t[0])
    return [e for _, e in merged], truncated


def _build_timeline_spans(events: list[dict]) -> list[dict]:
    """JSONL olaylarından zaman çizelgesi span'larını yeniden kur (replay).

    Birincil kaynak: ``timeline`` olayları (op=begin/end — epoch'lar payload'da).
    EOF'ta hâlâ açık span'lar (çökme/kill) son olayın ISO ts'iyle ``warn``
    kapatılır. Eski oturumlar için fallback: ``phase_change`` çiftlerinden
    kaba faz span'ları sentezlenir.
    """
    spans: list[dict] = []
    open_by_key: dict[str, dict] = {}
    for e in events:
        if e.get("event") != "timeline":
            continue
        if e.get("op") == "begin":
            sp = {
                "key": e.get("key", "?"),
                "lane": e.get("lane", "data"),
                "label": e.get("label", e.get("key", "?")),
                "t0": float(e.get("t0") or 0),
                "t1": None,
                "status": "running",
                "sub": bool(e.get("sub")),
                "round": int(e.get("round") or 1),
                "meta": dict(e.get("meta") or {}),
            }
            spans.append(sp)
            open_by_key[sp["key"]] = sp
        elif e.get("op") == "end":
            sp = open_by_key.pop(e.get("key", ""), None)
            if sp is not None:
                sp["t1"] = float(e.get("t1") or sp["t0"])
                sp["status"] = e.get("status", "ok")
                sp["meta"].update(e.get("meta") or {})

    if spans:
        if open_by_key:
            # Oturum yarıda kesilmiş — açıkları son olayın zamanıyla kapat.
            last_t = max(
                (sp["t1"] for sp in spans if sp["t1"] is not None),
                default=None,
            )
            if last_t is None:
                try:
                    last_t = datetime.fromisoformat(
                        events[-1].get("ts", "")
                    ).timestamp()
                except (ValueError, TypeError):
                    last_t = max(sp["t0"] for sp in open_by_key.values()) + 1.0
            for sp in open_by_key.values():
                sp["t1"] = max(last_t, sp["t0"])
                sp["status"] = "warn"
        return spans

    # ── Fallback: timeline olayları yok (eski oturum) → phase_change'ten ──
    _lane = {
        0: "data",
        1: "llm",
        2: "backtest",
        3: "backtest",
        4: "robustness",
        5: "data",
    }
    open_phase: dict[int, dict] = {}
    rnd = 1
    for e in events:
        ev = e.get("event")
        if ev == "step" and "Sürekli mod: tur" in str(e.get("msg", "")):
            rnd += 1
            continue
        if ev != "phase_change":
            continue
        try:
            t = datetime.fromisoformat(e.get("ts", "")).timestamp()
        except (ValueError, TypeError):
            continue
        idx = int(e.get("phase_idx") or 0)
        if e.get("status") == "running":
            sp = {
                "key": f"phase-{idx}-r{rnd}-{len(spans)}",
                "lane": _lane.get(idx, "data"),
                "label": e.get("phase_label", f"Faz {idx}"),
                "t0": t,
                "t1": None,
                "status": "running",
                "sub": False,
                "round": rnd,
                "meta": {},
            }
            spans.append(sp)
            open_phase[idx] = sp
        elif e.get("status") == "done" and idx in open_phase:
            sp = open_phase.pop(idx)
            sp["t1"] = t
            sp["status"] = "ok"
    last_t = max((sp["t1"] for sp in spans if sp["t1"] is not None), default=None)
    for sp in open_phase.values():
        sp["t1"] = last_t if last_t is not None else sp["t0"] + 1.0
        sp["status"] = "warn"
    return spans


# M24: (mtime_ns, size) anahtarlı özet cache'i — /sessions her ziyarette TÜM
# korpusu (yüzlerce MB olabilen jsonl'ler) yeniden parse ediyordu. Kapalı
# oturumların dosyası değişmez → özetleri bir kez hesaplanır; yalnız aktif
# (büyüyen) dosya yeniden okunur. Invalidation anahtarın kendisinde.
_SUMMARY_CACHE: dict[str, tuple[tuple, dict]] = {}


def _session_summary(run_id: str) -> dict:
    """Session listesi için hızlı özet — sadece anahtar event'leri oku."""
    path = SESSION_LOG_DIR / f"{run_id}.jsonl"
    try:
        st = path.stat()
        cache_key = (st.st_mtime_ns, st.st_size)
    except OSError:
        cache_key = None
    if cache_key is not None:
        cached = _SUMMARY_CACHE.get(run_id)
        if cached and cached[0] == cache_key:
            return cached[1]
    size_mb = round(path.stat().st_size / 1_048_576, 1) if path.exists() else 0

    start_ev = end_ev = tok_ev = winner_ev = None
    n_backtest = n_phase = n_step = n_rob = n_custom = 0
    last_ts = ""

    try:
        fh = path.open()
    except OSError:
        return {
            "run_id": run_id,
            "ts_start": "—",
            "ts_end": "—",
            "elapsed": "",
            "size_mb": size_mb,
            "symbol": "—",
            "intervals": [],
            "n_iterations": "—",
            "continuous": False,
            "hint": "",
            "outcome": "unreadable",
            "total_rounds": "?",
            "n_backtest": 0,
            "n_rob": 0,
            "n_custom": 0,
            "n_step": 0,
            "winner_spec": "",
            "winner_score": None,
            "cost_eur": None,
            "cost_usd": None,
            "has_blocks": False,
        }
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            ev = e.get("event", "")
            _ts = e.get("ts", "")
            # M251: step olaylarının ts'i 'HH:MM:SS' (tarihsiz — _add_step ISO'yu
            # ezer); son satır step ise ts_end saat-olarak görünüp elapsed'i
            # bozuyordu. last_ts'i yalnız TAM ISO ts'lerden (tarih içeren)
            # güncelle — step'lerin kısa ts'ini yok say.
            if _ts and ("T" in _ts or _ts[:4].isdigit()):
                last_ts = _ts
            if ev == "session_start":
                start_ev = e
            elif ev == "session_end":
                end_ev = e
            elif ev == "token_snapshot":
                tok_ev = e
            elif ev == "winner":
                winner_ev = e
            elif ev == "backtest_result":
                n_backtest += 1
            elif ev == "phase_change":
                n_phase += 1
            elif ev == "step":
                n_step += 1
            elif ev == "robustness_result":
                n_rob += 1
            elif ev == "custom_block_generated":
                n_custom += 1

    ts_start = (start_ev or {}).get("ts", "")
    ts_end = last_ts
    # Elapsed
    elapsed = ""
    try:
        if ts_start and ts_end:
            a = datetime.fromisoformat(ts_start)
            b = datetime.fromisoformat(ts_end)
            secs = int((b - a).total_seconds())
            elapsed = f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"
    except Exception:
        pass

    outcome = (end_ev or {}).get("outcome", "running" if not end_ev else "unknown")
    if winner_ev and not end_ev:
        outcome = "winner_found"

    total_rounds = (
        (tok_ev or {}).get("round") or (end_ev or {}).get("total_rounds") or "?"
    )

    summary = {
        "run_id": run_id,
        "ts_start": ts_start[:19].replace("T", " ") if ts_start else "—",
        "ts_end": ts_end[:19].replace("T", " ") if ts_end else "—",
        "elapsed": elapsed,
        "size_mb": size_mb,
        "symbol": (start_ev or {}).get("symbol", "—"),
        "intervals": (start_ev or {}).get("intervals", []),
        "n_iterations": (start_ev or {}).get("n_iterations", "—"),
        "continuous": (start_ev or {}).get("continuous_mode", False),
        "hint": (start_ev or {}).get("hint", ""),
        "outcome": outcome,
        "total_rounds": total_rounds,
        "n_backtest": n_backtest,
        "n_rob": n_rob,
        "n_custom": n_custom,
        "n_step": n_step,
        "winner_spec": (winner_ev or {}).get("spec_name", ""),
        "winner_score": (winner_ev or {}).get("score"),
        "cost_eur": (tok_ev or {}).get("cost_eur"),
        "cost_usd": (tok_ev or {}).get("cost_usd"),
        "has_blocks": (SESSION_LOG_DIR / f"{run_id}_blocks").exists(),
    }
    if cache_key is not None:
        _SUMMARY_CACHE[run_id] = (cache_key, summary)
    return summary


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def sessions_list(request: Request):
    import asyncio

    from server import get_market_info, templates

    if not SESSION_LOG_DIR.exists():
        sessions = []
    else:
        jsonl_files = sorted(
            SESSION_LOG_DIR.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        # Run blocking file I/O in thread pool to avoid blocking the event loop
        sessions = await asyncio.gather(
            *[asyncio.to_thread(_session_summary, p.stem) for p in jsonl_files]
        )

    return templates.TemplateResponse(
        request,
        "sessions.html",
        {
            "active": "sessions",
            "page_title": "Session Logları",
            "market": get_market_info(),
            "sessions": sessions,
        },
    )


@router.get("/{run_id}", response_class=HTMLResponse)
async def session_detail(request: Request, run_id: str):
    import asyncio

    from server import get_market_info, templates

    # Güvenlik: sadece hex run_id'lere izin ver
    if not all(c in "0123456789abcdef" for c in run_id) or len(run_id) != 8:
        return HTMLResponse("Geçersiz run_id", status_code=400)

    path = SESSION_LOG_DIR / f"{run_id}.jsonl"
    if not path.exists():
        return HTMLResponse("Session bulunamadı", status_code=404)

    # H7: RAM koruması step'lere uygulanır (en yeni 20k step, deque);
    # yapısal olaylar (backtest_result/winner/token_snapshot…) HER ZAMAN
    # dosya sonuna dek okunur — continuous oturumların sonraki turları
    # artık kaybolmaz. truncated bayrağı şablonda uyarı basar.
    events, steps_truncated = await asyncio.to_thread(_read_events, run_id, 20_000)

    # Event tipine göre grupla — step'leri round'a göre grupla
    # (M13: filtreler .get ile — 'event'siz satır zaten parse guard'ında elendi)
    session_start = next((e for e in events if e.get("event") == "session_start"), {})
    session_end = next((e for e in events if e.get("event") == "session_end"), None)
    token_snaps = [e for e in events if e.get("event") == "token_snapshot"]
    winners = [e for e in events if e.get("event") == "winner"]
    backtests = [e for e in events if e.get("event") == "backtest_result"]
    # Audit-91 öncesi log dosyalarında backtest_result olaylarında 'score'
    # alanı olmayabilir; template'in sort(attribute='score') çağrısı Undefined
    # karşılaştırmasında sayfayı 500'e düşürürdü. Eksik/None skoru sıralanabilir
    # -inf ile doldur (mevcut kayıtlarda no-op).
    for _bt in backtests:
        if _bt.get("score") is None:
            _bt["score"] = float("-inf")
    robustness = [e for e in events if e.get("event") == "robustness_result"]
    proposals = [e for e in events if e.get("event") == "strategy_proposed"]
    custom_blocks = [e for e in events if e.get("event") == "custom_block_generated"]
    steps = [e for e in events if e.get("event") == "step"]

    # Round bazında backtest özetleri
    rounds: dict[int, dict] = {}
    for bt in backtests:
        r = bt.get("round") or 1
        if r not in rounds:
            rounds[r] = {
                "backtests": [],
                "robustness": [],
                "proposals": [],
                "custom_blocks": [],
            }
        rounds[r]["backtests"].append(bt)
    for rob in robustness:
        r = rob.get("round") or 1
        if r not in rounds:
            rounds[r] = {
                "backtests": [],
                "robustness": [],
                "proposals": [],
                "custom_blocks": [],
            }
        rounds[r]["robustness"].append(rob)
    for pr in proposals:
        r = pr.get("round") or 1
        if r not in rounds:
            rounds[r] = {
                "backtests": [],
                "robustness": [],
                "proposals": [],
                "custom_blocks": [],
            }
        rounds[r]["proposals"].append(pr)
    for cb in custom_blocks:
        r = cb.get("round") or 1
        if r not in rounds:
            rounds[r] = {
                "backtests": [],
                "robustness": [],
                "proposals": [],
                "custom_blocks": [],
            }
        rounds[r]["custom_blocks"].append(cb)

    # Blocks dizini
    blocks_dir = SESSION_LOG_DIR / f"{run_id}_blocks"
    block_files = sorted(blocks_dir.glob("*.py")) if blocks_dir.exists() else []
    block_codes = []
    for bf in block_files:
        try:
            block_codes.append({"name": bf.stem, "code": bf.read_text()})
        except Exception:
            block_codes.append({"name": bf.stem, "code": "(okunamadı)"})

    # Son token snapshot
    last_tok = token_snaps[-1] if token_snaps else {}

    # Elapsed
    elapsed = ""
    ts_start = session_start.get("ts", "")
    ts_last = events[-1].get("ts", "") if events else ""
    try:
        if ts_start and ts_last:
            a = datetime.fromisoformat(ts_start)
            b = datetime.fromisoformat(ts_last)
            secs = int((b - a).total_seconds())
            elapsed = f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"
    except Exception:
        pass

    # Adım timeline (son 200 — büyük dosyalarda hepsini gösterme)
    step_timeline = steps[-200:]

    # Zaman çizelgesi replay — tur başına render modeli
    from web.viewmodels import associate_steps, timeline_view

    spans = _build_timeline_spans(events)
    tl_by_round: dict[int, dict] = {}
    for r in {sp.get("round", 1) for sp in spans}:
        tl = timeline_view(spans, round_num=r)
        if tl:
            tl_by_round[r] = {
                "tl": tl,
                "steps_by_key": associate_steps(spans, steps, round_num=r),
            }
    for r, rd in rounds.items():
        rd["timeline"] = tl_by_round.get(r)
    # Span'lı ama backtest'siz turlar (ör. erken hata) da görünsün
    for r, tlr in tl_by_round.items():
        if r not in rounds:
            rounds[r] = {
                "backtests": [],
                "robustness": [],
                "proposals": [],
                "custom_blocks": [],
                "timeline": tlr,
            }

    return templates.TemplateResponse(
        request,
        "session_detail.html",
        {
            "active": "sessions",
            "page_title": f"Session {run_id}",
            "market": get_market_info(),
            "run_id": run_id,
            "session_start": session_start,
            "session_end": session_end,
            "elapsed": elapsed,
            "last_tok": last_tok,
            "winners": winners,
            "rounds": dict(sorted(rounds.items())),
            "block_codes": block_codes,
            "step_timeline": step_timeline,
            "n_events": len(events),
            "n_steps": len(steps),
            "steps_truncated": steps_truncated,
            "size_mb": round(path.stat().st_size / 1_048_576, 1),
        },
    )


@router.get("/{run_id}/block/{block_name}", response_class=Response)
async def get_block_code(run_id: str, block_name: str):
    """Tek blok Python kodunu döndür."""
    if not all(c in "0123456789abcdef" for c in run_id) or len(run_id) != 8:
        return Response("Geçersiz run_id", status_code=400)
    # block_name sanitize
    safe = "".join(c for c in block_name if c.isalnum() or c == "_")
    path = SESSION_LOG_DIR / f"{run_id}_blocks" / f"{safe}.py"
    if not path.exists():
        return Response("Bulunamadı", status_code=404)
    return Response(path.read_text(), media_type="text/plain")
