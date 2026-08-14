"""Agent Session Logs — view all autonomous session logs.

Endpoints:
    GET /sessions          Session list
    GET /sessions/{run_id} Single session detail

Wiki References
---------------
Bkz: [[nau_performans_denetimi]] (2026-08-04 ikinci turu: liste sayfası her
satırı `json.loads` ettiği için soğuk render 114 sn'ydi; `_session_summary`
artık olay adını ve `ts`'i satır başından regex ile okur, tam parse yalnız
gövdesi gereken dört olay için yapılır — kalan maliyet parse değil I/O),
[[webapp_module_map]] (bu modülün rolü ve tear-sheet indeksi),
[[crash_only_design]] (2026-08-14: sahipsiz oturum loglarının `interrupted` ile
kapatılması — `session_end` hiçbir dış `finally`'de yazılmadığı için süreç
ölünce hiç yazılmıyordu; uzlaştırma geçmişe dokunmaz, su işaretinden sonrasını
kapatır).
Geri kalanı app-spesifik; Nautilus kavramı değil.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from web.shared import SESSION_LOG_DIR
from web.templating import get_market_info, templates

router = APIRouter(prefix="/sessions")

# ── Kesinti uzlaştırması ─────────────────────────────────────────────────────
# `session_end` 12 ayrı çağrı yerinde yazılıyor ve hiçbiri dış bir `finally`
# içinde değil: süreç ölürse (pm2 restart, çökme, kill) hiçbiri koşmaz. Ölçüm
# (2026-08-14): 118 oturumun 62'sinde terminal olay yok, %58'i bir `timeline`
# span'i açıkken kesilmiş.
#
# Bedeli kaybolan tek satır değil, o satırın yokluğunu telafi eden kod yollarının
# ÇOĞALMASI: bu dosya açık span'leri okuma anında `warn` ile kapatıyor,
# `_terminal_message` logun son satırına bakıp "büyük ihtimalle sunucu yeniden
# başladı" diye tahmin yürütüyor, sonradan yazılan her analiz aracı (ör.
# `scripts/auto_postmortem.py`) aynı tahmini bir kez daha yazmak zorunda — ve
# hiçbiri "çöktü mü, hâlâ koşuyor mu" sorusunu kesin cevaplayamıyor.
#
# Crash-only cevabı bu depoda ZATEN var ama yalnız veritabanı tarafında
# (`StrategyStore.interrupt_job` + `_reconcile_studio_jobs`); oturum loglarına
# yayılmamıştı. Desen buraya birebir taşınıyor.
#
# `interrupted`, `error` DEĞİL: başarısızlık işin kendisi hakkında bir yargıdır
# ("bu koşu patladı"), kesinti ise iş hakkında hiçbir şey söylemez — yalnızca
# sonucunu asla öğrenemeyeceğimizi söyler (`store.interrupt_job`'ın gerekçesi).
_RECONCILE_LOCK = threading.Lock()
_RECONCILED: set[str] = set()

# Uzlaştırma GEÇMİŞE DOKUNMAZ. İlk koşuda yalnız bir su işareti bırakır; o andan
# ÖNCE yarım kalmış loglar tarihsel kayıt olarak olduğu gibi durur. Append-only
# bir günlüğe bugünkü tarihle "şu tarihte kesildi" yazmak kaydı düzeltmek değil
# bulandırmaktır — 13 Temmuz'da ölen bir koşuya 14 Ağustos damgası vurmak,
# okuyanı yanıltacak yeni bir yalan üretirdi.
_WATERMARK_NAME = ".reconcile_watermark"

_INTERRUPT_REASON = (
    "process died before writing a terminal event "
    "(server restart / crash); reconciled on the next /sessions visit"
)


def _last_event(path: Path) -> dict | None:
    """Logun SON olayı — dosyanın tamamını okumadan (kuyruk taraması)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 8192))
            tail = fh.read().decode("utf-8", errors="replace").strip().splitlines()
    except OSError:
        return None
    for line in reversed(tail):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # kırpılmış ilk satır olabilir
        if isinstance(rec, dict) and rec.get("event"):
            return rec
    return None


def _reconcile_session_logs() -> int:
    """Sahipsiz oturum loglarını `interrupted` ile kapat; kaç tane olduğunu dön."""
    from web.routes.agent_backtest import live_run_ids

    watermark = SESSION_LOG_DIR / _WATERMARK_NAME
    now = datetime.now().timestamp()
    if not watermark.exists():
        # İlk koşu: mevcut her şey "geçmiş" sayılır, hiçbirine dokunulmaz.
        watermark.write_text(str(now), encoding="utf-8")
        logging.info(
            "session log reconciliation armed; %d existing log(s) grandfathered",
            len(list(SESSION_LOG_DIR.glob("*.jsonl"))),
        )
        return 0
    try:
        cutoff = float(watermark.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        # Bozuk su işareti: yeniden kur, yine geçmişe dokunma.
        watermark.write_text(str(now), encoding="utf-8")
        return 0

    live = live_run_ids()
    closed = 0
    for path in SESSION_LOG_DIR.glob("*.jsonl"):
        try:
            if path.stat().st_mtime <= cutoff:
                continue  # su işaretinden eski — tarihsel kayıt
        except OSError:
            continue
        if path.stem in live:
            continue  # bellekte hâlâ koşuyor
        last = _last_event(path)
        if last is None or last.get("event") == "session_end":
            continue
        record = {
            # `ts` KASITLI olarak son olayın zamanı: oturum o an sustu, biz
            # sonradan fark ettik. Tespit zamanını `ts`'e yazmak, koşuyu saatler
            # sonra bitmiş gibi gösterip zaman çizgisini bozardı.
            "ts": last.get("ts"),
            "event": "session_end",
            "run_id": path.stem,
            "outcome": "interrupted",
            "reason": _INTERRUPT_REASON,
            "last_event": last.get("event"),
            "detected_at": datetime.now().isoformat(),
        }
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            logging.warning(
                "could not reconcile session log %s", path.name, exc_info=True
            )
            continue
        closed += 1
    if closed:
        logging.warning("%d orphaned AUTO session log(s) marked interrupted", closed)
        _invalidate_detail_cache()
    return closed


def _reconcile_session_logs_once() -> None:
    """İlk liste isteğinde bir kez koş; kendi arızası sayfayı düşürmesin."""
    key = str(SESSION_LOG_DIR)
    with _RECONCILE_LOCK:
        if key in _RECONCILED:
            return
        # Bayrak ÖNCE: uzlaştırma patlarsa her istekte yeniden denenip sayfayı
        # yavaşlatmasın (`_reconcile_studio_jobs_once` ile aynı gerekçe).
        _RECONCILED.add(key)
    try:
        SESSION_LOG_DIR.mkdir(parents=True, exist_ok=True)
        _reconcile_session_logs()
    except Exception:  # noqa: BLE001 — bayat bir satır sayfayı düşürmemeli
        logging.warning("session log reconciliation failed", exc_info=True)


# ── Helpers ───────────────────────────────────────────────────────────────────


# H7: structural events are ALWAYS kept — the cap applies only to 'step's.
# The old line-count cap silently dropped later rounds'
# backtest_result/winner/token_snapshot events (which arrive AFTER thousands
# of steps) in continuous sessions: the page would show a 10-round session as 2 rounds.
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
        # Bozulma olayı yapısal: uzun oturumlarda `steps` deque'i taşınca
        # eleniyordu, yani koşu ne kadar uzunsa fallback kanıtı o kadar kesin
        # kayboluyordu — tam da en çok gerektiği yerde.
        "degraded",
    }
)

# ── Liste özetinin ucuz yolu ──────────────────────────────────────────────
# Yazıcı (agent_backtest._session_log) kaydı json.dumps ile serileştirir ve
# "ts" ile "event"i ilk iki anahtar olarak koyar; dolayısıyla ikisi de satırın
# başındadır. Bu pencere onları tam parse etmeden okumaya yeter.
_HEAD_SCAN = 400
_EVENT_RE = re.compile(r'"event"\s*:\s*"([A-Za-z_]+)"')
_TS_RE = re.compile(r'"ts"\s*:\s*"([^"]{0,40})"')

# Özet için GÖVDESİ gereken olaylar — yalnız bunlar tam parse edilir. Kalan
# olaylar (step/backtest_result/robustness_result/…) sadece SAYILIR, ki
# 3,5 MB'lık bir robustness satırı adını saymak için parse edilmesin.
_BODY_NEEDED = frozenset({"session_start", "session_end", "token_snapshot", "winner"})


def _read_events(run_id: str, max_lines: int | None = None) -> tuple[list[dict], bool]:
    """Read JSONL events → (events, truncated).

    H7: ``max_lines`` limits only step-like events (the NEWEST steps are kept
    via deque); structural events are collected all the way to end of file.
    M13: only dict lines with an ``'event'`` key are accepted — a schemaless
    but valid-JSON single line was dropping the whole page to 500.
    """
    path = SESSION_LOG_DIR / f"{run_id}.jsonl"
    if not path.exists():
        return [], False
    structural: list[tuple[int, dict]] = []
    steps: deque[tuple[int, dict]] = deque(maxlen=max_lines or None)
    n_steps_seen = 0
    read_failed = False
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
    except OSError as e:
        # Mid-read I/O failure (permission, disk, concurrent truncate): what we
        # collected so far is a genuine partial read, not "few events happened".
        # Fold into `truncated` so the template's existing warning fires instead
        # of presenting an incomplete list as a clean/complete one.
        logging.warning("_read_events(%s): read failed: %s", run_id, e, exc_info=True)
        read_failed = True
    truncated = read_failed or (bool(max_lines) and n_steps_seen > len(steps))
    merged = sorted([*structural, *steps], key=lambda t: t[0])
    return [e for _, e in merged], truncated


def _build_timeline_spans(events: list[dict]) -> list[dict]:
    """Rebuild timeline spans from JSONL events (replay).

    Primary source: ``timeline`` events (op=begin/end — epochs in payload).
    Spans still open at EOF (crash/kill) are closed as ``warn`` with the last
    event's ISO ts. Fallback for old sessions: coarse phase spans are
    synthesized from ``phase_change`` pairs.
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
            # Session interrupted midway — close open ones with the last event's time.
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

    # ── Fallback: no timeline events (old session) → from phase_change ──
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
        if ev == "step" and "Continuous mode: round" in str(e.get("msg", "")):
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
                "label": e.get("phase_label", f"Phase {idx}"),
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


# M24: summary cache keyed by (mtime_ns, size) — /sessions was re-parsing the
# ENTIRE corpus (jsonl's that can be hundreds of MB) on every visit. Closed
# sessions' files don't change → their summaries are computed once; only the
# active (growing) file is re-read. Invalidation is in the key itself.
_SUMMARY_CACHE: dict[str, tuple[tuple, dict]] = {}


def _env_int(
    name: str, default: int, *, lo: int | None = None, hi: int | None = None
) -> int:
    """Parse an int env var with clamping and a safe fallback on a bad value."""
    try:
        v = int(os.environ.get(name, str(default)))
    except ValueError:
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _query_int(
    request: Request,
    name: str,
    default: int,
    *,
    lo: int | None = None,
    hi: int | None = None,
) -> int:
    """Parse an int query param with clamping and a safe fallback on a bad value."""
    try:
        v = int(request.query_params.get(name, str(default)))
    except ValueError:
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


# The session corpus is historical evidence, not a reason to fan out hundreds
# of simultaneous disk reads on a page render.  Cache hits are cheap; cold
# summaries of very large legacy JSONLs remain I/O-bound, so keep the read
# pressure bounded while preserving every visible session.
_SESSION_SUMMARY_CONCURRENCY = _env_int(
    "NAUTILUS_SESSION_SUMMARY_CONCURRENCY", 8, lo=1, hi=32
)

# A historical session may contain hundreds of MB of old, curve-heavy JSONL.
# The list page is navigation, not forensic replay: it must stay responsive
# without asking the disk to parse the whole corpus.  The detail route remains
# the explicit full-history view.
_SESSION_SUMMARY_FULL_SCAN_MAX_BYTES = _env_int(
    "NAUTILUS_SESSION_SUMMARY_FULL_SCAN_MAX_BYTES", 8 * 1024 * 1024, lo=0
)
_SUMMARY_EDGE_BYTES = 128 * 1024
_SESSION_DETAIL_FULL_SCAN_MAX_BYTES = _env_int(
    "NAUTILUS_SESSION_DETAIL_FULL_SCAN_MAX_BYTES", 64 * 1024 * 1024, lo=0
)
_DETAIL_EDGE_BYTES = 2 * 1024 * 1024

# backtest_result/robustness_result bodies can carry an MB-scale embedded
# equity curve; every other structural event is a few hundred bytes. Only the
# heavy ones need to stay bounded to the head/tail windows below.
_HEAVY_BODY_EVENTS = frozenset({"backtest_result", "robustness_result"})


def _parse_jsonl_blob(blob: bytes, *, require_event_key: bool = True) -> list[dict]:
    """Decode a raw JSONL byte slice into dicts, skipping torn/invalid lines.

    Shared by the head/tail forensic reader and the large-session summary —
    both need "best-effort parse of a byte window that may start or end
    mid-line" and previously reimplemented this loop separately.
    """
    parsed: list[dict] = []
    for line in blob.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if require_event_key and "event" not in event:
            continue
        parsed.append(event)
    return parsed


def _read_events_head_tail(
    run_id: str, edge_bytes: int = _DETAIL_EDGE_BYTES
) -> tuple[list[dict], int]:
    """Bounded forensic preview for a huge session detail request.

    The normal detail replay intentionally reads all events. For multi-GB
    legacy JSONLs, fully materializing every heavy-bodied event (backtest/
    robustness results, which can each carry an MB-scale equity curve) would
    turn one browser request into an I/O/RAM denial of service, so those stay
    bounded to the head/tail windows. Every OTHER structural event (winner,
    session_end, token_snapshot, phase_change, …) is lightweight, so a single
    cheap streaming middle-pass (event name read from the line head only, the
    same technique _session_summary uses) captures it wherever it falls
    instead of silently dropping it when it lands outside the two windows —
    a mid-file 'winner' from an earlier round in a long continuous session
    must not vanish from the replay just because the log grew past 64MB.

    Returns (events, n_heavy_skipped) — the count of heavy-bodied structural
    events outside the head/tail windows, so callers can show an honest notice.
    """
    path = SESSION_LOG_DIR / f"{run_id}.jsonl"
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(edge_bytes)
            if size <= edge_bytes:
                return _parse_jsonl_blob(head), 0
            tail_start = max(0, size - edge_bytes)
            fh.seek(tail_start)
            tail = fh.read(edge_bytes)
    except OSError:
        return [], 0

    # Head ends and tail begins may contain one cut JSON line; the parser rejects it.
    events = [*_parse_jsonl_blob(head), *_parse_jsonl_blob(tail)]

    light_events = _STRUCTURAL_EVENTS - _HEAVY_BODY_EVENTS
    n_heavy_skipped = 0
    try:
        with path.open("rb") as fh:
            fh.seek(len(head))
            for raw_line in fh:
                if fh.tell() >= tail_start:
                    break
                line = raw_line.decode("utf-8", errors="replace")
                m_ev = _EVENT_RE.search(line[:_HEAD_SCAN])
                if m_ev is None:
                    continue
                ev = m_ev.group(1)
                if ev in _HEAVY_BODY_EVENTS:
                    n_heavy_skipped += 1
                elif ev in light_events:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(obj, dict) and "event" in obj:
                        events.append(obj)
    except OSError:
        pass
    return events, n_heavy_skipped


# ── Detay sayfasının olay önbelleği ───────────────────────────────────────
# DeepR 2026-08-11 [ORTA]: liste sayfasının özetleri `_SUMMARY_CACHE` ile
# (mtime_ns, size) anahtarlı önbellekleniyordu ama DETAY yolunda hiçbir
# önbellek yoktu — sayfa her yenilendiğinde tam maliyet yeniden ödeniyor,
# üstelik `to_thread` havuzundan bir iş parçacığı o süre boyunca tutuluyordu.
# Ölçüldü (sıcak sayfa önbelleğiyle; soğuk diskte kat kat fazla):
#   • 267 MB `6a6ab8e4.jsonl` → head/tail yolu 702 ms
#   •  47 MB `c1de959a.jsonl` → tam okuma  466 ms
# Kapanmış oturum dosyaları hiç değişmez, yani (mtime_ns, size) bu iş için
# birebir — `_SUMMARY_CACHE` ile aynı anahtar, aynı sözleşme.
#
# GEÇERSİZLEŞME SÖZLEŞMESİ:
#   • Anahtar (mtime_ns, size). AKTİF (büyüyen) oturumun dosyası her yazımda
#     değişir → o oturum hiçbir zaman bayat gösterilmez, her istekte yeniden
#     okunur. Kapanmış oturumlar bir kez okunur.
#   • Bayatlama penceresi YOK; dosya değişmediği sürece taze sayılır.
#   • Girdiler paylaşılan `events` listesini döndürür. Detay rotası bu
#     sözlükleri yalnız FİKİRSİZ (idempotent) alanlarla damgalar
#     (`score` yalnız None ise -inf, `ev_i` = değişmeyen sıra numarası), o
#     yüzden paylaşım güvenli — yeni bir mutasyon eklenirse burası kopyalamalı.
#
# RAM SINIRI ŞART: aynı denetimin 4. bulgusu (reports._PARSE_CACHE) tam olarak
# "sınırsız parse önbelleği" idi. Ölçülen yükler: 267 MB head/tail → 30 MB
# heap, 47 MB tam okuma → 143,7 MB heap (ikisi de kaynak baytın ≈3 katı).
# Bu yüzden bütçe KAYNAK BAYT üzerinden tutuluyor: tam-okuma yolunda dosya
# boyu, head/tail yolunda ölçüme dayalı sabit bir yük. Bütçeye sığmayan bir
# oturum SESSİZCE bozulmaz — önbelleğe alınmaz ve nedeni loglanır (sayfa
# doğru çalışmaya devam eder, sadece her yenilemede maliyeti öder).
_DETAIL_CACHE: dict[str, tuple[tuple, tuple, int]] = {}
_DETAIL_CACHE_LOCK = threading.Lock()
_DETAIL_CACHE_MAX_ENTRIES = _env_int(
    "NAUTILUS_SESSION_DETAIL_CACHE_MAX_ENTRIES", 8, lo=0, hi=64
)
_DETAIL_CACHE_MAX_SOURCE_BYTES = _env_int(
    "NAUTILUS_SESSION_DETAIL_CACHE_MAX_BYTES", 24 * 1024 * 1024, lo=0
)
# head/tail yolu yapısal olarak sınırlı (2 MB baş + 2 MB son + yalnız hafif
# gövdeli orta olaylar); korpustaki EN BÜYÜK dosya (267 MB) için ölçülen
# 30 MB heap'e karşılık gelen kaynak-bayt karşılığı ~10 MB — 12 MB temkinli.
_DETAIL_TRUNCATED_COST_BYTES = 12 * 1024 * 1024


def _detail_events(run_id: str, truncated: bool) -> tuple[list[dict], int, bool]:
    """(events, n_heavy_skipped, steps_truncated) — (mtime_ns, size) önbellekli."""
    path = SESSION_LOG_DIR / f"{run_id}.jsonl"
    try:
        st = path.stat()
        key: tuple | None = (st.st_mtime_ns, st.st_size)
        size = st.st_size
    except OSError:
        key, size = None, 0

    if key is not None:
        with _DETAIL_CACHE_LOCK:
            hit = _DETAIL_CACHE.get(run_id)
            if hit is not None and hit[0] == key:
                return hit[1]

    if truncated:
        events, n_heavy_skipped = _read_events_head_tail(run_id)
        payload = (events, n_heavy_skipped, False)
        cost = _DETAIL_TRUNCATED_COST_BYTES
    else:
        events, steps_truncated = _read_events(run_id, 20_000)
        payload = (events, 0, steps_truncated)
        cost = size

    if key is not None:
        _detail_cache_store(run_id, key, payload, cost)
    return payload


def _detail_cache_store(run_id: str, key: tuple, payload: tuple, cost: int) -> None:
    """Store under the byte budget, evicting oldest-first and saying so."""
    if _DETAIL_CACHE_MAX_ENTRIES <= 0 or _DETAIL_CACHE_MAX_SOURCE_BYTES <= 0:
        return
    with _DETAIL_CACHE_LOCK:
        _DETAIL_CACHE.pop(run_id, None)
        if cost > _DETAIL_CACHE_MAX_SOURCE_BYTES:
            logging.warning(
                "session detail cache SKIPPED for %s: %.1f MB source > %.1f MB "
                "budget — every refresh of this session re-reads the log "
                "(raise NAUTILUS_SESSION_DETAIL_CACHE_MAX_BYTES to cache it)",
                run_id,
                cost / 1_048_576,
                _DETAIL_CACHE_MAX_SOURCE_BYTES / 1_048_576,
            )
            return
        total = sum(e[2] for e in _DETAIL_CACHE.values()) + cost
        while _DETAIL_CACHE and (
            total > _DETAIL_CACHE_MAX_SOURCE_BYTES
            or len(_DETAIL_CACHE) >= _DETAIL_CACHE_MAX_ENTRIES
        ):
            oldest, dropped = next(iter(_DETAIL_CACHE.items()))
            del _DETAIL_CACHE[oldest]
            total -= dropped[2]
            logging.info(
                "session detail cache evicted %s (%.1f MB) to make room for %s",
                oldest,
                dropped[2] / 1_048_576,
                run_id,
            )
        _DETAIL_CACHE[run_id] = (key, payload, cost)


def _invalidate_detail_cache() -> None:
    with _DETAIL_CACHE_LOCK:
        _DETAIL_CACHE.clear()


def _tail_scan_for_events(
    path: Path, size: int, wanted: frozenset[str], max_bytes: int
) -> dict[str, dict]:
    """Find the last occurrence of each wanted event type near EOF.

    A fixed-size tail slice can miss a terminal event (session_end, winner,
    token_snapshot) when it's preceded by an oversized line — e.g. a
    backtest_result carrying an embedded equity curve — pushing the actual
    event past the window and making a completed session misreport as
    'running'. This grows the read window (bounded doubling, capped at
    max_bytes) until every wanted event is found or the cap is hit, using the
    same cheap event-name regex _session_summary relies on so a big scan
    doesn't mean a big parse.
    """
    found: dict[str, dict] = {}
    window = _SUMMARY_EDGE_BYTES
    while True:
        window = min(window, max_bytes, size)
        try:
            with path.open("rb") as fh:
                fh.seek(max(0, size - window))
                blob = fh.read(window)
        except OSError:
            return found
        for line in blob.decode("utf-8", errors="replace").splitlines():
            m = _EVENT_RE.search(line[:_HEAD_SCAN])
            if not m or m.group(1) not in wanted:
                continue
            try:
                obj = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(obj, dict) and "event" in obj:
                found[obj["event"]] = obj  # scanned oldest→newest: last one wins
        if len(found) >= len(wanted) or window >= max_bytes or window >= size:
            return found
        window *= 4


_SUMMARY_TAIL_TERMINAL_EVENTS = frozenset({"session_end", "token_snapshot", "winner"})


def _large_session_summary(run_id: str, path: Path, size_mb: float) -> dict:
    """Build a bounded summary from the JSONL head and a grown-as-needed tail.

    Counts are intentionally unknown rather than guessed.  Completion, winner
    and latest token data are terminal events and normally live at the tail;
    start metadata is at the head.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(_SUMMARY_EDGE_BYTES)
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
            "n_backtest": "?",
            "n_rob": "?",
            "n_custom": "?",
            "n_step": "?",
            "winner_spec": "",
            "winner_score": None,
            "cost_eur": None,
            "cost_usd": None,
            "cost_source": None,
            "has_blocks": False,
        }

    head_events = _parse_jsonl_blob(head, require_event_key=False)
    terminal = _tail_scan_for_events(
        path,
        size,
        _SUMMARY_TAIL_TERMINAL_EVENTS,
        max(_SESSION_SUMMARY_FULL_SCAN_MAX_BYTES, _SUMMARY_EDGE_BYTES),
    )
    start_ev = next((e for e in head_events if e.get("event") == "session_start"), {})
    end_ev = terminal.get("session_end", {})
    tok_ev = terminal.get("token_snapshot", {})
    winner_ev = terminal.get("winner", {})
    ts_start = start_ev.get("ts", "")
    ts_end = (end_ev or tok_ev or {}).get("ts", "")
    elapsed = ""
    try:
        elapsed_seconds = int(
            (
                datetime.fromisoformat(ts_end) - datetime.fromisoformat(ts_start)
            ).total_seconds()
        )
        elapsed = f"{elapsed_seconds // 3600:02d}:{(elapsed_seconds % 3600) // 60:02d}:{elapsed_seconds % 60:02d}"
    except (TypeError, ValueError):
        pass
    return {
        "run_id": run_id,
        "ts_start": ts_start[:19].replace("T", " ") if ts_start else "—",
        "ts_end": ts_end[:19].replace("T", " ") if ts_end else "—",
        "elapsed": elapsed,
        "size_mb": size_mb,
        "symbol": start_ev.get("symbol", "—"),
        "intervals": start_ev.get("intervals", []),
        "n_iterations": start_ev.get("n_iterations", "—"),
        "continuous": start_ev.get("continuous_mode", False),
        "hint": start_ev.get("hint", ""),
        "outcome": end_ev.get("outcome", "running"),
        "total_rounds": end_ev.get("completed_rounds", end_ev.get("total_rounds", "?")),
        "n_backtest": "?",
        "n_rob": "?",
        "n_custom": "?",
        "n_step": "?",
        "winner_spec": winner_ev.get("spec_name", ""),
        "winner_score": winner_ev.get("score"),
        "cost_eur": tok_ev.get("cost_eur"),
        "cost_usd": tok_ev.get("cost_usd"),
        "cost_source": tok_ev.get("cost_source"),
        "has_blocks": (SESSION_LOG_DIR / f"{run_id}_blocks").exists(),
    }


def _session_summary(run_id: str) -> dict:
    """Fast summary for the session list — read only key events."""
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

    if (
        path.exists()
        and _SESSION_SUMMARY_FULL_SCAN_MAX_BYTES
        and path.stat().st_size > _SESSION_SUMMARY_FULL_SCAN_MAX_BYTES
    ):
        summary = _large_session_summary(run_id, path, size_mb)
        if cache_key is not None:
            _SUMMARY_CACHE[run_id] = (cache_key, summary)
        return summary

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
            # Liste sayfası bu döngüde yalnız (a) olay adlarını sayar ve (b) son
            # ISO zaman damgasını okur; ikisi de satırın ilk ~100 baytındadır
            # (yazıcı "ts" ve "event"i ilk iki anahtar olarak koyar). Her satıra
            # json.loads uygulamak, 3,5 MB'lık robustness satırlarını yalnız
            # ADINI saymak için tam parse etmek demekti — /sessions'ın soğuk
            # açılışını 114 saniyeye çıkaran maliyet buydu. Tam parse artık
            # SADECE gövdesi gereken dört olay için yapılır.
            head = line[:_HEAD_SCAN]
            m_ev = _EVENT_RE.search(head)
            e: dict | None = None
            if m_ev is None:
                # Beklenmeyen biçim → doğruluk hızdan önce gelir, eski yola dön.
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                ev = e.get("event", "")
                _ts = e.get("ts", "")
            else:
                ev = m_ev.group(1)
                m_ts = _TS_RE.search(head)
                _ts = m_ts.group(1) if m_ts else ""
                if ev in _BODY_NEEDED:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                else:
                    e = None
            # M251: step events' ts is 'HH:MM:SS' (dateless — _add_step overwrites
            # ISO); if the last line was a step, ts_end appeared as a clock value and
            # broke elapsed. Update last_ts only from FULL ISO ts's (containing a date)
            # — ignore steps' short ts.
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

    # A token snapshot can belong to an admitted-but-incomplete next round.
    # Session completion fields are authoritative; falling back to snapshot
    # round recreated the phantom round seen in d4b86c48 (2 completed, shown 3).
    if end_ev and end_ev.get("completed_rounds") is not None:
        total_rounds = end_ev["completed_rounds"]
    elif end_ev and end_ev.get("total_rounds") is not None:
        total_rounds = end_ev["total_rounds"]
    else:
        total_rounds = (tok_ev or {}).get("round") or "?"

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
        "cost_source": (tok_ev or {}).get("cost_source"),
        "has_blocks": (SESSION_LOG_DIR / f"{run_id}_blocks").exists(),
    }
    if cache_key is not None:
        _SUMMARY_CACHE[run_id] = (cache_key, summary)
    return summary


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def sessions_list(request: Request):
    import asyncio

    limit = _query_int(request, "limit", 25, lo=1, hi=100)
    offset = _query_int(request, "offset", 0, lo=0)

    # Sahipsiz logları kapat (süreç başına bir kez). Liste özetleri okunmadan
    # ÖNCE: aksi hâlde uzlaştırılan oturum bu sayfada bir kez daha terminal
    # olaysız görünürdü.
    await asyncio.to_thread(_reconcile_session_logs_once)

    if not SESSION_LOG_DIR.exists():
        sessions = []
        total_files = 0
    else:
        jsonl_files = sorted(
            SESSION_LOG_DIR.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        total_files = len(jsonl_files)
        # The recent sessions are the useful operational view, one page at a
        # time — offset paging keeps older archives reachable instead of
        # falling off the list entirely once the corpus outgrows one page.
        page_files = jsonl_files[offset : offset + limit]
        # Run blocking file I/O in a bounded thread pool.  The old all-at-once
        # gather could start one full scan per historical session (93 files / a
        # multi-GB corpus in the live review), causing disk thrash and a slow
        # Studio response even though each individual summary is cache-aware.
        semaphore = asyncio.Semaphore(_SESSION_SUMMARY_CONCURRENCY)

        async def _one_summary(path: Path) -> dict:
            async with semaphore:
                return await asyncio.to_thread(_session_summary, path.stem)

        sessions = await asyncio.gather(*[_one_summary(p) for p in page_files])

    return templates.TemplateResponse(
        request,
        "sessions.html",
        {
            "active": "sessions",
            "page_title": "Session Logs",
            "market": get_market_info(),
            "sessions": sessions,
            "limit": limit,
            "offset": offset,
            "total_sessions": total_files,
            "has_prev": offset > 0,
            "has_next": offset + limit < total_files,
        },
    )


@router.get("/{run_id}", response_class=HTMLResponse)
async def session_detail(request: Request, run_id: str):
    import asyncio

    # Security: only allow hex run_id's
    if not all(c in "0123456789abcdef" for c in run_id) or len(run_id) != 8:
        return HTMLResponse("Invalid run_id", status_code=400)

    path = SESSION_LOG_DIR / f"{run_id}.jsonl"
    if not path.exists():
        return HTMLResponse("Session not found", status_code=404)

    # H7: RAM protection applies to steps (newest 20k steps, deque); structural
    # events are ALWAYS read to end of file — later rounds of continuous
    # sessions are no longer lost — but only on the branch below the size cap.
    # Past the cap, _read_events_head_tail still catches every lightweight
    # structural event via a full middle-pass; only backtest/robustness_result
    # bodies (n_heavy_skipped) stay bounded to the head/tail windows.
    detail_truncated = bool(
        _SESSION_DETAIL_FULL_SCAN_MAX_BYTES
        and path.stat().st_size > _SESSION_DETAIL_FULL_SCAN_MAX_BYTES
    )
    # DeepR 2026-08-11 [ORTA]: her yenilemede tam maliyeti ödemek yerine
    # (mtime_ns, size) anahtarlı önbellek — bkz. _detail_events.
    events, n_heavy_skipped, steps_truncated = await asyncio.to_thread(
        _detail_events, run_id, detail_truncated
    )

    # Group by event type — group steps by round
    # (M13: filters use .get — a line without 'event' was already dropped by the parse guard)
    session_start = next((e for e in events if e.get("event") == "session_start"), {})
    session_end = next((e for e in events if e.get("event") == "session_end"), None)
    token_snaps = [e for e in events if e.get("event") == "token_snapshot"]
    winners = [e for e in events if e.get("event") == "winner"]
    backtests = [e for e in events if e.get("event") == "backtest_result"]
    # In log files before Audit-91, backtest_result events may lack the 'score'
    # field; the template's sort(attribute='score') call would drop the page to
    # 500 on an Undefined comparison. Fill missing/None score with a sortable
    # -inf (no-op on existing records).
    for _i, _bt in enumerate(backtests):
        if _bt.get("score") is None:
            _bt["score"] = float("-inf")
        # Position in the file's backtest_result sequence — the tear sheet
        # addresses an iteration by it. Stamped before the template sorts by
        # score, which would otherwise lose the ordering the key refers to.
        _bt["ev_i"] = _i
    robustness = [e for e in events if e.get("event") == "robustness_result"]
    proposals = [e for e in events if e.get("event") == "strategy_proposed"]
    custom_blocks = [e for e in events if e.get("event") == "custom_block_generated"]
    steps = [e for e in events if e.get("event") == "step"]

    # Per-round backtest summaries
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

    # Blocks directory
    blocks_dir = SESSION_LOG_DIR / f"{run_id}_blocks"
    block_files = sorted(blocks_dir.glob("*.py")) if blocks_dir.exists() else []
    block_codes = []
    for bf in block_files:
        try:
            block_codes.append({"name": bf.stem, "code": bf.read_text()})
        except Exception:
            block_codes.append({"name": bf.stem, "code": "(could not read)"})

    # Last token snapshot
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

    # Step timeline (last 200 — don't show all on large files)
    step_timeline = steps[-200:]

    # Timeline replay — per-round render model
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
    # Rounds with spans but no backtests (e.g. early error) should also show
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
            "detail_truncated": detail_truncated,
            "n_heavy_skipped": n_heavy_skipped,
            "size_mb": round(path.stat().st_size / 1_048_576, 1),
        },
    )


@router.get("/{run_id}/block/{block_name}", response_class=Response)
async def get_block_code(run_id: str, block_name: str):
    """Return a single block's Python code."""
    if not all(c in "0123456789abcdef" for c in run_id) or len(run_id) != 8:
        return Response("Invalid run_id", status_code=400)
    # sanitize block_name
    safe = "".join(c for c in block_name if c.isalnum() or c == "_")
    path = SESSION_LOG_DIR / f"{run_id}_blocks" / f"{safe}.py"
    if not path.exists():
        return Response("Not found", status_code=404)
    return Response(path.read_text(), media_type="text/plain")
