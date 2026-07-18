"""Otonom Backtest Ajanı — araştırma döngüsü.

Pipeline:
  0. Veri yükle (cache'den tam aralık)
  1. Strateji üret (Claude → ComposedStrategySpec)
  2. N × (backtest → Claude iyileştirme)
  3. Sıralama (Sharpe + PnL + WinRate skoru)
  4. Robustness tarama (sırayla: IS/OOS + WFO + MC)
  5. Kazananı kaydet (catalog + log)

Endpoints:
    GET  /agent               Sayfa
    POST /agent/run           Pipeline başlat (hemen döner)
    GET  /agent/progress/{id} Durum polling (HTMX every 1s)
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/agent")

from web.shared import ProgressStore  # noqa: E402
from web.shared import chart_url as _chart_url  # noqa: E402
from web.shared import log_backtest as _log_backtest  # noqa: E402
from web.shared import log_robustness as _log_robustness  # noqa: E402

# ProgressStore holds dict + lock + capped eviction; aliases keep the many
# existing direct-access sites (worker ↔ pollers ↔ session log) unchanged.
_AGENT_STORE = ProgressStore(50)
_AGENT_PROGRESS = _AGENT_STORE.raw()
_AGENT_LOCK = _AGENT_STORE.lock
# İstatistiksel olarak güvenilir sayılması için minimum trade sayısı. Bunun
# altındaki koşular _score'da -inf ile elenir (kazanan olamaz). Değer NAU_ev
# backtest optimizer'ının JUNK_MIN_TRADES=20 eşiğiyle hizalı (bkz. NAU wiki
# backtest-optimizer.md: "trade < 20 → elenir + saklanmaz").
# L28: AGENT_MIN_TRADES env var ile ayarlanabilir; default 20 → davranış aynı.
_MIN_TRADES = int(os.environ.get("AGENT_MIN_TRADES", "20"))

# L2: Monte Carlo medyan drawdown limiti (%). Hem _robustness_passed kıyası hem
# kullanıcıya gösterilen açıklama metni bu TEK sabitten türetilir.
_MC_DD_LIMIT = -25.0

# L32: Mühürlü holdout — verinin son N günü iterasyon + robustness fazlarından
# tamamen saklanır; yalnız kazanan ilan edildikten SONRA bir kez test edilir.
# Sonuç karara BAĞLANMAZ, yalnız bilgi amaçlı gösterilir.
OOS_HOLDOUT_DAYS = 60

# M22 ek devre kesicisi: ardışık kazanansız tur sayısı eşiği (sürekli mod).
# Kazanan çıkınca sayaç sıfırlanır. 0 = kapalı; default 25.
_WINLESS_ROUND_LIMIT = int(os.environ.get("AGENT_WINLESS_ROUND_LIMIT", "25"))

# L38: model → ($/MTok input, output, cache_read, cache_write). agent.MODEL'e
# göre seçilir; bilinmeyen model Sonnet oranlarına düşer. Backend claude-cli /
# OAuth (abonelik) ise token başına fatura YOK → maliyet None (UI gizler).
_MODEL_PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5": (10.0, 50.0, 1.00, 12.50),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75),
}
_DEFAULT_PRICING_MODEL = "claude-sonnet-4-6"

# L26: kazanan + robustness anlarının hafif SQLite indeksi (best-effort).
_AGENT_INDEX_DB = Path.home() / ".cache" / "nautilus_web_app" / "agent_index.db"

# When this module runs inside a sandbox child process (robustness offloaded to
# a subprocess so it can't freeze the web server), progress steps are relayed to
# the parent via this queue instead of the child's own _AGENT_PROGRESS.
_IPC_Q = None

# ── Session Logger ────────────────────────────────────────────────────────────
SESSION_LOG_DIR = Path.home() / ".cache" / "nautilus_web_app" / "agent_sessions"
_SESSION_LOG_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOG_META: threading.Lock = threading.Lock()  # guards _SESSION_LOG_LOCKS


def _json_safe(obj):
    """NaN/Inf float'ları None'a indirger (rekürsif) — json.dumps'un standart
    dışı ``NaN``/``Infinity`` literal'leri üretmesini engeller (L33 payı).
    Küçük, bu dosyaya özel yardımcı; reports'unkinden bağımsız.
    """
    if isinstance(obj, float):  # np.float64 da float alt sınıfı
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _session_log(run_id: str, event: str, **kwargs) -> None:
    """Append a JSON event line to agent_sessions/{run_id}.jsonl.
    Thread-safe per run_id. Silently ignores all errors so logging never
    breaks the agent worker.
    """
    try:
        SESSION_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _SESSION_LOG_META:
            if run_id not in _SESSION_LOG_LOCKS:
                _SESSION_LOG_LOCKS[run_id] = threading.Lock()
            lock = _SESSION_LOG_LOCKS[run_id]
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "run_id": run_id,
            **kwargs,
        }
        line = json.dumps(_json_safe(record), default=str) + "\n"
        with lock:
            with (SESSION_LOG_DIR / f"{run_id}.jsonl").open("a") as _f:
                _f.write(line)
    except Exception:
        pass


def _cleanup_all_agent_blocks() -> None:
    """Legacy hook kept for compatibility; agent blocks are now persistent."""
    return None


_PHASES = [
    "Veri yükleniyor",
    "Strateji üretiliyor",
    "Backtest döngüsü",
    "Sıralama",
    "Robustness tarama",
    "Tamamlandı",
]


# ── Progress helpers ──────────────────────────────────────────────────────────


def _ts() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


def _set_phase(run_id: str, idx: int, detail: str = "") -> None:
    t = _ts()
    label = _PHASES[idx] if 0 <= idx < len(_PHASES) else str(idx)
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if not s:
            return
        for i, p in enumerate(s["phases"]):
            if i < idx:
                p["status"] = "done"
            elif i == idx:
                p["status"] = "running"
                p["detail"] = detail
                p["ts"] = t
            else:
                p["status"] = "pending"
                p["detail"] = ""
    _session_log(
        run_id,
        "phase_change",
        phase_idx=idx,
        phase_label=label,
        status="running",
        detail=detail,
    )


def _done_phase(run_id: str, idx: int, detail: str = "") -> None:
    t = _ts()
    label = _PHASES[idx] if 0 <= idx < len(_PHASES) else str(idx)
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if not s:
            return
        p = s["phases"][idx]
        p["status"] = "done"
        p["detail"] = detail
        p["ts"] = t
    _session_log(
        run_id,
        "phase_change",
        phase_idx=idx,
        phase_label=label,
        status="done",
        detail=detail,
    )


def _add_step(run_id: str, msg: str) -> None:
    # In a sandbox child, relay the step to the parent (tag matches _run_in_child).
    if _IPC_Q is not None:
        try:
            _IPC_Q.put(("progress", msg))
        except Exception:
            pass
        return
    t = _ts()
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s is not None:
            s["steps"].append({"ts": t, "msg": msg})
            # Cap to prevent unbounded memory growth in continuous mode.
            if len(s["steps"]) > 500:
                s["steps"] = s["steps"][-500:]
    _session_log(run_id, "step", ts=t, msg=msg)


# ── Timeline (Gantt) span track ───────────────────────────────────────────────
# Her anlamlı işlem (veri yükleme, LLM çağrısı, backtest #i, robustness adayı ve
# alt-aşamaları) bir "span" olarak kaydedilir; /agent ekranındaki SVG zaman
# çizelgesi ve /sessions replay'i bu track'ten beslenir. Epoch float'lar payload
# içinde taşınır — asla `ts=` kwarg'ı ile değil (_session_log'un ISO ts'ini ezer).

_TL_MAX_SPANS = 400  # continuous-mode bellek tavanı (500-step cap ile aynı ruh)
_TL_LANES = ("data", "llm", "backtest", "robustness")


def _tl_begin(
    run_id: str,
    lane: str,
    key: str,
    label: str,
    *,
    sub: bool = False,
    round_num: int = 1,
    **meta,
) -> None:
    """Zaman çizelgesinde bir span aç. Sandbox child içinde no-op (_IPC_Q set) —
    child alt-aşamaları parent tarafında step marker'larından türetilir."""
    if _IPC_Q is not None:
        return
    t0 = datetime.now(UTC).timestamp()
    span = {
        "key": key,
        "lane": lane,
        "label": label,
        "t0": t0,
        "t1": None,
        "status": "running",
        "sub": sub,
        "round": round_num,
        "meta": dict(meta),
    }
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s is not None:
            tl = s.setdefault("timeline", [])
            tl.append(span)
            if len(tl) > _TL_MAX_SPANS:
                # En eski KAPALI span'ları düşür; açıklar (running) korunur.
                closed_idx = next(
                    (i for i, sp in enumerate(tl) if sp["t1"] is not None), None
                )
                if closed_idx is not None:
                    tl.pop(closed_idx)
                else:
                    tl.pop(0)
    _session_log(
        run_id,
        "timeline",
        op="begin",
        key=key,
        lane=lane,
        label=label,
        t0=t0,
        sub=sub,
        round=round_num,
        meta=dict(meta),
    )


def _tl_end(run_id: str, key: str, *, status: str = "ok", **meta) -> None:
    """`key`'li en son açık span'ı kapat. Bilinmeyen/kapalı key → no-op."""
    if _IPC_Q is not None:
        return
    t1 = datetime.now(UTC).timestamp()
    found = False
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s is not None:
            for sp in reversed(s.get("timeline") or []):
                if sp["key"] == key and sp["t1"] is None:
                    sp["t1"] = t1
                    sp["status"] = status
                    if meta:
                        sp["meta"].update(meta)
                    found = True
                    break
    if found:
        _session_log(
            run_id, "timeline", op="end", key=key, t1=t1, status=status, meta=dict(meta)
        )


def _tl_close_open(run_id: str, *, status: str = "fail") -> None:
    """Açık kalan tüm span'ları kapat (hata / stop / tur sonu temizliği)."""
    if _IPC_Q is not None:
        return
    t1 = datetime.now(UTC).timestamp()
    closed: list[str] = []
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s is not None:
            for sp in s.get("timeline") or []:
                if sp["t1"] is None:
                    sp["t1"] = t1
                    sp["status"] = status
                    closed.append(sp["key"])
    for key in closed:
        _session_log(
            run_id, "timeline", op="end", key=key, t1=t1, status=status, meta={}
        )


# Robustness alt-aşama marker'ları — _run_full_robustness'taki _add_step
# mesajlarının SABİT ön-glyph'leri. Glyph'leri değiştirmeyin: _make_rob_progress
# bunları parse ederek sub-span açar/kapar.
_TL_ROB_MARKERS = {
    "🌐": ("ms", "Multi-Symbol"),
    "📊": ("isoos", "IS/OOS"),
    "📈": ("wfo", "Walk-Forward"),
    "🎲": ("mc", "Monte Carlo"),
}


def _make_rob_progress(run_id: str, cand_idx: int, round_num: int):
    """Robustness child'ının progress relay'i için step + sub-span köprüsü.

    Dönen fonksiyon her mesajı _add_step'e iletir; 4 sabit aşama glyph'inde
    (🌐 📊 📈 🎲) bir sub-span açar (öncekini ok ile kapatır); "  →" ile
    başlayan sonuç satırı aktif sub'ı ok, "  ⚠ Monte Carlo atlandı" mc'yi
    warn kapatır.
    """
    state = {"open": None}  # aktif sub-span key'i

    def _close(status: str = "ok") -> None:
        if state["open"]:
            _tl_end(run_id, state["open"], status=status)
            state["open"] = None

    def progress(msg: str) -> None:
        _add_step(run_id, msg)
        head = msg.lstrip()[:1]
        if head in _TL_ROB_MARKERS:
            _close("ok")
            slug, label = _TL_ROB_MARKERS[head]
            key = f"rob-r{round_num}-c{cand_idx}-{slug}"
            _tl_begin(
                run_id,
                "robustness",
                key,
                label,
                sub=True,
                round_num=round_num,
            )
            state["open"] = key
        elif msg.startswith("  →"):
            _close("ok")
        elif msg.startswith("  ⚠ Monte Carlo atlandı"):
            _close("warn")

    progress.close_open = _close  # aday kapanışında çağrılır
    return progress


def _add_tokens(run_id: str, usage: dict | None) -> None:
    """Claude API usage dict'inden token sayaçlarını biriktir."""
    if not usage:
        return
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s is not None:
            s["tokens_in"] = s.get("tokens_in", 0) + (usage.get("input_tokens") or 0)
            s["tokens_out"] = s.get("tokens_out", 0) + (usage.get("output_tokens") or 0)
            s["tokens_cache_read"] = s.get("tokens_cache_read", 0) + (
                usage.get("cache_read_input_tokens") or 0
            )
            s["tokens_cache_write"] = s.get("tokens_cache_write", 0) + (
                usage.get("cache_creation_input_tokens") or 0
            )


def _set_robustness_scan(run_id: str, current: int, total: int) -> None:
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s is not None:
            s["rob_scan_current"] = current
            s["rob_scan_total"] = total


# ── Core helpers ──────────────────────────────────────────────────────────────


def _proposal_to_spec(proposal: dict):
    """Convert propose_composed_strategy output to ComposedStrategySpec."""
    from composer import ComposedStrategySpec, SignalBlock, new_spec_id

    opts = proposal.get("strategy_options") or {}
    blocks = [
        SignalBlock(type=b["type"], role=b["role"], params=b.get("params", {}))
        for b in proposal.get("blocks", [])
    ]
    return ComposedStrategySpec(
        id=new_spec_id(),
        name=proposal.get("name", "Agent Strategy"),
        description=proposal.get("description", ""),
        blocks=blocks,
        trade_size=float(opts.get("trade_size", 0.01)),
        entry_logic=opts.get("entry_logic", "OR"),
        exit_logic=opts.get("exit_logic", "OR"),
        order_type="market",  # agent always uses market — limit rarely fills on backtests
        limit_offset_bps=0.0,
        use_bracket=bool(opts.get("use_bracket", False)),
        sl_type=opts.get("sl_type", "percent"),
        sl_value=float(opts.get("sl_value", 2.0)),
        tp_type=opts.get("tp_type", "off"),
        tp_value=float(opts.get("tp_value", 4.0)),
        atr_period=int(opts.get("atr_period", 14)),
        allow_short=bool(opts.get("allow_short", False)),
        trade_size_mode=opts.get("trade_size_mode", "fixed"),
        trade_size_percent=float(opts.get("trade_size_percent", 5.0)),
        trade_size_atr_risk=float(opts.get("trade_size_atr_risk", 1.0)),
        trade_size_usdt=float(opts.get("trade_size_usdt", 1000.0)),
    )


_STARTING_CASH: float | None = None
_PNL_FALLBACK_WARNED = False


def _starting_cash() -> float:
    """STARTING_CASH sabitini lazy getir (modül import'u hafif kalsın).

    Sözleşme: önce ``app_constants`` (paralel refaktor bu modülü oluşturuyor),
    ImportError'da mevcut ``backtest`` kaynağına düşülür.
    """
    global _STARTING_CASH
    if _STARTING_CASH is None:
        try:
            from app_constants import STARTING_CASH
        except ImportError:
            from backtest import STARTING_CASH
        _STARTING_CASH = float(STARTING_CASH)
    return _STARTING_CASH


def _score(result) -> float:
    """NAU kompozit sıralama skoru (H9/M30/M32):

        calmar = clamp(pnl_pct / max(|max_dd|, 0.01), -10, 10)
        base   = 0.7 × calmar + 0.3 × clamp(sharpe_per_trade, -10, 10)
        score  = base × n_trades / (n_trades + 20)      ← güven çarpanı

    NAU paritesi: sharpe terimi PER-TRADE sharpe ((mean/std)×√n, NAU
    backtest.py:89) — annualized 252-gün Sharpe DEĞİL. NAU fold_quality composite'i
    de m['sharpe']'i per-trade tabanında okur; bizde annualized 'sharpe' ayrı
    alandır, o yüzden burada açıkça sharpe_per_trade kullanılır.

    - pnl_pct ve max_dd KESİR konvansiyonunda (0.1 = %10; max_dd < 0 sağlıklı).
    - Eski WinRate×0.2 terimi ve 0.1'lik sabit confidence bonus'u KALDIRILDI:
      WR bilgisi zaten PnL/Calmar'a gömülü, sabit bonus sıralamayı bozuyordu.
    - Overtrading log-cezası BİLİNÇLİ olarak korunuyor: n_trades > 2000 ise
      score'a -0.3×log10(n/2000) eklenir (komisyon-ağır gürültü stratejilerini
      bastırır; güven çarpanı büyük n'de 1'e doyduğu için ayrıca gerekli).

    JUNK elemesi → -inf (NAU backtest optimizer'ıyla hizalı):
    hata VEYA < _MIN_TRADES trade VEYA dejenere drawdown. NAU kuralı
    ``trade < 20 VEYA max_drawdown <= 0 → elenir``. Bu projede max_dd negatif
    konvansiyonda (sağlıklı = <0); 0/NaN/eksik = gerçek drawdown yok = veri/
    hesap dejenerasyonu, o yüzden ``max_dd >= 0`` NAU'nun ``<= 0``'ının aynası.
    """
    global _PNL_FALLBACK_WARNED
    if result.error:
        return float("-inf")
    m = result.metrics
    if not m or (m.get("n_trades") or 0) < _MIN_TRADES:
        return float("-inf")
    max_dd = m.get("max_dd")
    # NAU: max_drawdown <= 0 → junk (dejenere). M1: NaN da dejenere sayılır.
    if max_dd is None or math.isnan(max_dd) or max_dd >= 0:
        return float("-inf")
    n_trades = m.get("n_trades") or 0
    # NAU paritesi: composite'in 0.3 terimi PER-TRADE sharpe. Annualized 'sharpe'
    # (252-gün) farklı ölçekte; NAU per-trade ((mean/std)×√n) kullanır.
    sharpe = m.get("sharpe_per_trade")
    if sharpe is None:
        sharpe = m.get("sharpe") or 0.0  # geriye-uyum (eski metrics dict'leri)
    # Use explicit None check to avoid treating pnl_pct=0.0 (break-even) as missing
    _pnl_pct = m.get("pnl_pct")
    if _pnl_pct is not None:
        pnl_pct = _pnl_pct
    else:
        # M12+L1: pnl mutlak USDT — STARTING_CASH'e bölerek kesire çevir
        # (ölçek-doğru fallback). Bir kez uyar: pnl_pct'in eksik olması
        # metrics üreticisinde bir gerilemeye işaret eder.
        pnl_pct = (m.get("pnl") or 0.0) / _starting_cash()
        if not _PNL_FALLBACK_WARNED:
            _PNL_FALLBACK_WARNED = True
            logging.warning(
                "_score: metrics'te pnl_pct yok — pnl/STARTING_CASH "
                "fallback'i kullanıldı (bir kez loglanır)"
            )
    if math.isnan(sharpe) or math.isinf(sharpe):
        sharpe = 0.0
    if math.isnan(pnl_pct) or math.isinf(pnl_pct):
        pnl_pct = 0.0
    calmar = pnl_pct / max(abs(max_dd), 0.01)
    calmar = max(-10.0, min(10.0, calmar))
    base = 0.7 * calmar + 0.3 * max(-10.0, min(10.0, sharpe))
    # Güven çarpanı: az trade'li sonuçların skorunu sürekli biçimde kısar
    # (n=20 → ×0.5, n=180 → ×0.9); eski basamaklı 0.1 bonus'un yerini alır.
    score = base * (n_trades / (n_trades + 20))
    # Belgeli istisna: aşırı-trade log cezası (bkz. docstring).
    if n_trades > 2000:
        score += -0.3 * math.log10(n_trades / 2000)
    return score


def _rank_results(results: list[tuple]) -> list[tuple]:
    """Sort (spec, IterationResult) pairs by composite score descending."""
    return sorted(results, key=lambda x: _score(x[1]), reverse=True)


# Harici (US equity) koşularda multi-symbol robustness için likit eş sepeti.
# Katalogdaki gerçek instrument id'leri — SPY/IWM ARCA venue'lu, NASDAQ değil.
EXTERNAL_PEER_BASKET = [
    "SPY.ARCA",
    "QQQ.NASDAQ",
    "IWM.ARCA",
    "AAPL.NASDAQ",
    "MSFT.NASDAQ",
]


def _clamp_spec_trade_size(spec):
    """Equity (size_precision=0) tam sayı hisse ister — agent'ın ürettiği
    kesirli kripto trade_size (0.01) 0 hisseye yuvarlanıp 0 trade üretir.
    Spec taze bir nesne olduğundan yerinde mutasyon güvenli; robustness fazı
    da aynı nesneyi kullandığı için tek noktada düzeltmek her tüketiciyi kapsar.
    """
    if float(spec.trade_size) < 1:
        spec.trade_size = 1.0
    return spec


def _robustness_passed(
    rob: dict, strict: bool = True, run_id: str | None = None
) -> bool:
    """Robustness kararı — değerlendirilen-kriter sayaçlı (H4+L18).

    4 kriter (IS/OOS, WFO, Multi-Symbol, Monte Carlo) tek tek
    ``evaluated | failed | skipped`` olarak sınıflanır. Eski sürüm eksik/hatalı
    bölümleri sessizce "geçti" sayıyordu; artık:

    - strict:  en az 3 kriter GERÇEKTEN değerlendirilmiş olmalı ve hiçbiri
      failed olmamalı.
    - relaxed: en az 2 kriter değerlendirilmiş olmalı, hiçbiri failed olmamalı
      ('⚠' etiketler her iki modda da kabul — failed sayılmaz).

    Atlanan her kriter için (run_id verilmişse) adım günlüğüne
    '⚠ <kriter> değerlendirilemedi: <neden>' uyarısı düşülür.
    """
    if not rob or rob.get("error"):
        return False

    def _skip(name: str, why: str) -> None:
        if run_id:
            _add_step(run_id, f"⚠ {name} değerlendirilemedi: {why}")

    evaluated = 0
    failed = 0

    # 1) IS/OOS overfitting kriteri
    split = rob.get("split") or {}
    split_label = split.get("overfitting_label", "")
    if split.get("error") or not split_label:
        _skip("IS/OOS", str(split.get("error") or "etiket yok"))
    else:
        evaluated += 1
        # '✓ Sağlam' ve '⚠ Dikkat' kabul; '✗' veya 'yetersiz' → failed
        if "✗" in split_label or "yetersiz" in split_label:
            failed += 1

    # 2) Walk-Forward: ≥%50 geçerli pencerede pozitif test PnL.
    # <3 test trade'li pencereler istatistiksel olarak güvenilmez → geçersiz.
    wfo = rob.get("wfo_windows") or []
    valid_windows = [
        w
        for w in wfo
        if (w.get("test_n_trades") or w.get("test_metrics", {}).get("n_trades", 0) or 0)
        >= 3
    ]
    if not wfo or not valid_windows:
        _skip(
            "Walk-Forward",
            "pencere yok" if not wfo else "≥3 trade'li geçerli pencere yok",
        )
    else:
        evaluated += 1
        # M28/M594: NAU dağılım-cezalı OOS Sharpe (mean − 0.5·std). Manuel
        # suite bunu wfo_aggregate'te üretir; ajan yolu (_run_full_robustness)
        # wfo_aggregate'i çağırmadığından alan boştu ve bu dal ölü koddu —
        # burada wfo_windows'tan INLINE hesapla. Yoksa pozitif-pencere oranı.
        pen = rob.get("oos_sharpe_penalized")
        if pen is None:
            _sh = [
                float((w.get("test_metrics") or {}).get("sharpe"))
                for w in valid_windows
                if (w.get("test_metrics") or {}).get("sharpe") is not None
                and math.isfinite(
                    (w.get("test_metrics") or {}).get("sharpe", float("nan"))
                )
            ]
            if len(_sh) >= 2:
                _m = sum(_sh) / len(_sh)
                _var = sum((s - _m) ** 2 for s in _sh) / len(_sh)
                pen = _m - 0.5 * (_var**0.5)
        try:
            pen = float(pen) if pen is not None else None
            if pen is not None and not math.isfinite(pen):
                pen = None
        except (TypeError, ValueError):
            pen = None
        if pen is not None:
            wfo_failed = pen <= 0
        else:
            positive = sum(
                1
                for w in valid_windows
                if (w.get("test_metrics") or {}).get("pnl", 0) > 0
            )
            wfo_failed = positive / len(valid_windows) < 0.5
        if wfo_failed:
            failed += 1

    # 3) Multi-symbol genellenebilirlik
    ms = rob.get("multi_symbol") or {}
    ms_label = ms.get("generalization_label", "")
    if not ms or not ms_label or ("yetersiz" in ms_label and "✗" not in ms_label):
        _skip("Multi-Symbol", "bölüm yok" if not ms else "yetersiz veri")
    else:
        evaluated += 1
        if "✗" in ms_label:  # sembol-spesifik strateji kabul edilmez
            failed += 1

    # 4) Monte Carlo: medyan drawdown kontrolü
    mc = rob.get("mc") or {}
    if not mc or mc.get("error"):
        _skip("Monte Carlo", str(mc.get("error") or "bölüm yok"))
    else:
        evaluated += 1
        dd_p50 = mc.get("max_dd_p50")
        if dd_p50 is not None and dd_p50 < _MC_DD_LIMIT:
            failed += 1

    if failed:
        return False
    min_required = 3 if strict else 2
    if evaluated < min_required:
        if run_id:
            _add_step(
                run_id,
                f"⚠ Yalnız {evaluated}/4 kriter değerlendirilebildi "
                f"(gereken ≥{min_required}) — aday geçemez",
            )
        return False
    return True


def _ms_score_factor(rob: dict | None) -> float:
    """M26+M31: multi-symbol pass_rate'ten etkin-skor çarpanı ∈ [0.15, 1.0].

    x = pass_rate - 0.5 (pass_rate yoksa x=0 → nötr 0.575);
    factor = 0.15 + (clamp(x, -0.5, 0.5) + 0.5) × 0.85.
    """
    ms = (rob or {}).get("multi_symbol") or {}
    pr = ms.get("pass_rate")
    # M653: run_multi_symbol yetersiz veride pass_rate=0.0 + etiket
    # '— (yetersiz veri)' döndürür — bu GERÇEK 0 değil, DEĞERLENDİRİLEMEDİ
    # demek. 0.0'ı çarpan hesabına sokmak nötr (0.575) yerine minimum 0.15
    # cezası uyguluyordu: MS'i hiç ölçülememiş aday, tüm sembollerde batmış
    # gibi %85 kırpılıp geçenler-arası seçimi kaybediyordu. Yetersiz-veriyi
    # nötr say.
    ms_label = ms.get("generalization_label", "") or ""
    insufficient = ("yetersiz" in ms_label) or ms.get("n_valid", 1) == 0
    try:
        if pr is None or insufficient:
            x = 0.0  # nötr → factor 0.575
        else:
            x = float(pr) - 0.5
        if not math.isfinite(x):
            x = 0.0
    except (TypeError, ValueError):
        x = 0.0
    x = max(-0.5, min(0.5, x))
    return 0.15 + (x + 0.5) * 0.85


def _split_holdout(df, min_bars: int = 200):
    """L32: son OOS_HOLDOUT_DAYS günü mühürlü dilim olarak ayır.

    Returns ``(trimmed_df, holdout_df | None)``. Kalan (kırpılmış) veri
    ``min_bars``'ın altına düşerse holdout ATLANIR ve tam df aynen döner.
    """
    import pandas as pd

    from backtest_robustness import WF_EMBARGO_DAYS

    if df is None or len(df) == 0:
        return df, None
    cutoff = df.index[-1] - pd.Timedelta(days=OOS_HOLDOUT_DAYS)
    # M674: train ile holdout arasına NAU purge boşluğu (WF_EMBARGO_DAYS) —
    # cutoff civarında öğrenilen desen/lookback holdout'un ilk günlerine
    # sızmasın (NAU fit_end = oos_start − WF_EMBARGO_DAYS).
    train_end = cutoff - pd.Timedelta(days=WF_EMBARGO_DAYS)
    trimmed = df[df.index < train_end]
    if len(trimmed) < min_bars:
        return df, None
    return trimmed, df[df.index >= cutoff]


_INDEX_DB_WARNED = False


def _index_insert(
    run_id: str,
    round_num: int,
    spec_name: str,
    spec_id: str,
    score,
    passed: bool,
    symbol: str,
    interval: str,
) -> None:
    """L26: kazanan + robustness anlarında best-effort SQLite indeksi.

    Hata worker'ı asla kırmaz: ilk hata bir kez loglanır, sonrakiler yutulur.
    """
    global _INDEX_DB_WARNED
    try:
        import sqlite3

        sc = None
        if score is not None:
            try:
                f = float(score)
                sc = f if math.isfinite(f) else None
            except (TypeError, ValueError):
                sc = None
        _AGENT_INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(_AGENT_INDEX_DB), timeout=5.0)
        try:
            with con:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS results ("
                    "run_id TEXT, round INTEGER, ts TEXT, spec_name TEXT, "
                    "spec_id TEXT, score REAL, passed INT, symbol TEXT, "
                    "interval TEXT)"
                )
                con.execute(
                    "INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        int(round_num),
                        datetime.now(UTC).isoformat(),
                        spec_name,
                        spec_id,
                        sc,
                        int(bool(passed)),
                        symbol,
                        interval,
                    ),
                )
        finally:
            con.close()
    except Exception:
        if not _INDEX_DB_WARNED:
            _INDEX_DB_WARNED = True
            logging.warning("agent_index.db yazılamadı (bir kez loglanır)")


def _llm_cost_usd(ti: int, to: int, tcr: int, tcw: int) -> tuple[str, float | None]:
    """L38: (pricing_model, tahmini maliyet USD) döndürür.

    Model agent.MODEL'den okunur; fiyat _MODEL_PRICING'den (bilinmeyen model →
    Sonnet oranları). Backend claude-cli / OAuth aboneliği ise token başına
    fatura YOK → maliyet None (UI cost satırını gizler). Backend tespiti
    agent._build_client'ın aynası: NAUTILUS_LLM_BACKEND=api → API;
    =claude-cli → CLI; auto → ANTHROPIC_API_KEY (veya ~/.nautilus_proxy_key)
    varsa API, yoksa CLI.
    """
    try:
        from agent import MODEL as model
    except Exception:
        model = _DEFAULT_PRICING_MODEL
    pi, po, pcr, pcw = _MODEL_PRICING.get(model, _MODEL_PRICING[_DEFAULT_PRICING_MODEL])
    backend = os.environ.get("NAUTILUS_LLM_BACKEND", "auto").strip().lower()
    if backend == "claude-cli":
        return model, None
    if backend != "api":
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY", ""))
        if not has_key:
            try:
                has_key = (Path.home() / ".nautilus_proxy_key").exists()
            except Exception:
                has_key = False
        if not has_key:  # auto → key yok → claude CLI (abonelik)
            return model, None
    return model, (ti * pi + to * po + tcr * pcr + tcw * pcw) / 1_000_000


def _run_full_robustness(
    run_id: str,
    spec,
    bars_df,
    instrument,
    bar_type,
    venue,
    trades: list,
    symbol: str = "BTCUSDT",
    interval: str = "1",
    category: str = "linear",
    source: str = "bybit",
) -> dict:
    """Run Multi-Symbol → IS/OOS → WFO → MC sıralamasıyla robustness.

    ``source="external"``: ``symbol`` harici katalog instrument id'sidir
    (örn. "QQQ.NASDAQ"), ``interval`` katalog DSL'idir ("1-DAY"); multi-symbol
    evreni EXTERNAL_PEER_BASKET'ten seçilir.

    NAUTILUS_PARALLEL=1 (varsayılan) iken bağımsız backtest birimleri
    (multi-symbol, IS/OOS çifti, WFO pencere×aday) bir süreç havuzuna dağıtılır;
    havuz kurulamaz ya da bir aşama havuzda patlarsa o aşama dokunulmamış sıralı
    yolla yeniden koşulur. NAUTILUS_PARALLEL=0 → tamamen sıralı (eski davranış).
    """
    import shutil as _shutil

    from backtest import STARTING_CASH
    from backtest_robustness import (
        run_insample_oos_split,
        run_monte_carlo,
        run_multi_symbol,
        run_walk_forward,
    )

    pf = lambda m: _add_step(run_id, m)  # noqa: E731

    # ── Paralel havuz (opsiyonel) ─────────────────────────────────────────────
    pool = None
    run_many = None
    snapshot_path = None
    try:
        from parallel_exec import (
            BacktestPool,
            get_worker_count,
            make_snapshot,
            parallel_enabled,
        )

        if parallel_enabled():
            # Trend-filter cache'ini fan-out ÖNCESİ ısıt: soğuk cache'te birden
            # çok worker aynı parquet'e yazmaya yarışır (data.py to_parquet).
            if getattr(spec, "trend_filter", False):
                try:
                    if source == "external":
                        from data import load_external_bars

                        load_external_bars(
                            symbol, getattr(spec, "trend_interval", "1-DAY")
                        )
                    else:
                        from data import load_bybit_bars

                        load_bybit_bars(
                            symbol=symbol,
                            interval=getattr(spec, "trend_interval", "60"),
                            category=category,
                            start=bars_df.index[0].to_pydatetime(),
                            end=bars_df.index[-1].to_pydatetime(),
                        )
                except Exception as warm_exc:
                    _add_step(run_id, f"  ⚠ Trend cache ısıtılamadı: {warm_exc}")
            snapshot_path = make_snapshot(bars_df)
            pool_recipe = (
                {"source": "external", "instrument_id": symbol, "granularity": interval}
                if source == "external"
                else {"symbol": symbol, "interval": interval, "category": category}
            )
            pool = BacktestPool(
                snapshot_path,
                pool_recipe,
                max_workers=get_worker_count(),
            )
            run_many = pool.run_units
            _add_step(
                run_id,
                f"⚡ Paralel mod: {pool.max_workers} işçi süreç "
                "(NAUTILUS_PARALLEL=0 ile kapatılabilir)",
            )
    except Exception as pool_exc:
        _add_step(run_id, f"⚠ Paralel havuz kurulamadı ({pool_exc}) — sıralı mod")
        run_many = None

    def _stage(label, fn, /, *args, **kwargs):
        """Bir robustness aşamasını (varsa) havuzla koş; havuz hatasında aynı
        aşamayı sıralı yolla yeniden dene. Sıralı yol her zaman ayakta."""
        if run_many is not None:
            try:
                return fn(*args, run_many=run_many, **kwargs)
            except Exception as par_exc:
                _add_step(
                    run_id,
                    f"  ⚠ {label} paralel aşaması düştü "
                    f"({type(par_exc).__name__}) — sıralı yeniden çalışıyor",
                )
        return fn(*args, **kwargs)

    try:
        # 1) Multi-Symbol — en ucuz test, hızlı eler (önceden IS/OOS ve WFO zamanını kurtarır)
        if source == "external":
            from data import _external_bar_dir

            # Once bu granularity'de verisi OLAN esleri filtrele, SONRA 3'e kirp
            # (skorlama zaten toleransli). Ters sira ilk 3 esin verisi yoksa
            # 4./5. esi hic denemiyordu.
            other_symbols = [
                p
                for p in EXTERNAL_PEER_BASKET
                if p != symbol and _external_bar_dir(p, interval) is not None
            ][:3]
            # 365 takvim günü ≈ 252 hisse barı — _MIN_TRADES eşiği için az; 730 kullan.
            ms_days = 730
        else:
            other_symbols = [
                s for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT") if s != symbol
            ]
            ms_days = 365  # 180→365: daha fazla trade → istatistiksel güvenilirlik
        _add_step(
            run_id,
            f"🌐 Multi-Symbol — strateji {', '.join(other_symbols)} sembollerinde de test ediliyor. "
            "Genellenebilir mi yoksa sadece bu sembole mi özgü?",
        )
        ms = _stage(
            "Multi-Symbol",
            run_multi_symbol,
            spec,
            primary_symbol=symbol,
            symbols=other_symbols,
            interval=interval,
            category=category,
            days=ms_days,
            progress_fn=pf,
            source=source,
        )
        _add_step(
            run_id,
            f"  → {ms.get('symbols_positive', 0)}/{ms.get('symbols_valid', 0)} sembolde pozitif · "
            f"{ms.get('generalization_label', '?')}",
        )

        # 2) IS/OOS Split
        _add_step(
            run_id,
            "📊 IS/OOS Split — verinin %70'i eğitim, %30'u gerçek OOS testi. "
            "OOS/IS Sharpe oranı overfitting'i ölçer (≥0.7 = sağlam).",
        )
        split = _stage(
            "IS/OOS",
            run_insample_oos_split,
            spec,
            bars_df,
            instrument,
            bar_type,
            venue,
            split_pct=0.7,
            progress_fn=pf,
        )
        sp = split or {}
        is_m = sp.get("in_sample_metrics") or {}
        oos_m = sp.get("oos_metrics") or {}
        _add_step(
            run_id,
            f"  IS sonucu: PnL={is_m.get('pnl', 0):+.2f} · "
            f"Sharpe={is_m.get('sharpe', float('nan')):.2f} · "
            f"{is_m.get('n_trades', 0)} trade | "
            f"OOS sonucu: PnL={oos_m.get('pnl', 0):+.2f} · "
            f"Sharpe={oos_m.get('sharpe', float('nan')):.2f} · "
            f"{oos_m.get('n_trades', 0)} trade",
        )
        _add_step(run_id, f"  → Overfitting skoru: {sp.get('overfitting_label', '?')}")

        # 3) Walk-Forward
        _add_step(
            run_id,
            "📈 Walk-Forward — kayan pencere OOS testi. Her pencerede 6 ay eğitim + 2 ay test. "
            "≥%50 pencerede pozitif PnL gerekli.",
        )
        wfo = _stage(
            "Walk-Forward",
            run_walk_forward,
            spec,
            bars_df,
            instrument,
            bar_type,
            venue,
            train_months=6,
            test_months=2,
            step_months=3,  # 2→3: 35 yerine ~24 pencere, %30 daha hızlı
            progress_fn=pf,
        )
        if wfo:
            pos = sum(1 for w in wfo if (w.get("test_metrics") or {}).get("pnl", 0) > 0)
            avg_pnl = sum(
                (w.get("test_metrics") or {}).get("pnl", 0) for w in wfo
            ) / len(wfo)
            _add_step(
                run_id,
                f"  → {pos}/{len(wfo)} pencere pozitif · ortalama test PnL={avg_pnl:+.2f} USDT",
            )

        # 4) Monte Carlo (zaten vektörize numpy — havuza gerek yok)
        mc: dict = {"error": "Trade verisi yok."}
        if trades:
            _add_step(
                run_id,
                f"🎲 Monte Carlo — {len(trades)} trade sırasını {300} kez karıştırarak "
                f"şans faktörünü ölçer. Medyan DD < {_MC_DD_LIMIT:.0f}% ise riskli.",
            )
            mc = run_monte_carlo(
                trades,
                n_sims=300,
                starting_cash=STARTING_CASH,
                progress_fn=pf,
            )
            if not mc.get("error"):
                _add_step(
                    run_id,
                    f"  → Medyan final: ${mc.get('median_final', 0):,.0f} · "
                    f"p5 senaryo: ${mc.get('p5_final', 0):,.0f} · "
                    f"Medyan max DD: {mc.get('max_dd_p50', 0):.1f}%",
                )
        else:
            _add_step(
                run_id, "  ⚠ Monte Carlo atlandı — backtest'te hiç trade açılmadı"
            )

        return {"split": split, "wfo_windows": wfo, "mc": mc, "multi_symbol": ms}
    finally:
        if pool is not None:
            pool.shutdown()
        if snapshot_path is not None:
            _shutil.rmtree(Path(snapshot_path).parent, ignore_errors=True)


# ── Worker ────────────────────────────────────────────────────────────────────


def _agent_worker(
    run_id: str,
    hint: str,
    symbol: str,
    category: str,
    intervals: list[str],
    n_iterations: int,
    strict_mode: bool,
    trend_filter: bool = False,
    trend_interval: str = "60",
    continuous_mode: bool = False,
    web_research: bool = False,
    source: str = "bybit",
    instrument_id: str = "",
    max_hours: float = 0.0,
    max_total_tokens: int = 0,
) -> None:
    import pandas as pd

    from agent import propose_composed_strategy
    from composer import load_catalog
    from data import _bybit_cache_path
    from sandbox import run_backtest_guarded, run_robustness_guarded

    is_external = source == "external"
    # LLM'e geçen pazar bağlamı — Bybit'te None (mevcut prompt bayt-bayt korunur).
    market = (
        f"US equity {instrument_id} ({'/'.join(intervals)} bars, USD cash account)"
        if is_external
        else None
    )

    def _recipe(iv: str) -> dict:
        """Sandbox/robustness child'ın instrument'ı yeniden kurduğu string recipe."""
        if is_external:
            return {
                "source": "external",
                "instrument_id": instrument_id,
                "granularity": iv,
            }
        return {"symbol": symbol, "interval": iv, "category": category}

    # Session başlangıcını logla
    _session_log(
        run_id,
        "session_start",
        hint=hint,
        symbol=symbol,
        category=category,
        intervals=intervals,
        n_iterations=n_iterations,
        strict_mode=strict_mode,
        trend_filter=trend_filter,
        trend_interval=trend_interval,
        continuous_mode=continuous_mode,
        web_research=web_research,
        source=source,
        instrument_id=instrument_id,
        max_hours=max_hours,
        max_total_tokens=max_total_tokens,
    )

    run_number = 0
    # 0 = SINIRSIZ (kullanıcı tercihi): sürekli mod yalnız durdur butonu VEYA
    # devre kesici ile durur. Güvenli bir tavan istenirse pozitif bir sayı yeter.
    _MAX_CONTINUOUS_ROUNDS = 0
    # Devre kesici: aynı hata metni ardışık N tur → dur. Sınırsız modda TEK
    # otomatik güvenlik ağı budur (886f439b oturumu kalıcı bir "Cache çok az
    # veri" hatasını boşa döngüde denemişti — bu onu kesiyor).
    _CONSEC_ERR_LIMIT = 3
    _last_err_str: str | None = None
    _consec_err = 0
    # M22: opsiyonel bütçe tavanları (0 = sınırsız) + kazanansız-tur kesicisi.
    _worker_t0 = time.monotonic()
    _winless_rounds = 0

    def _winless_bump() -> bool:
        """Kazanansız tur sayacını artırır; limit aşıldıysa True döner.

        M22: eskiden yalnız 'hiç uygun aday yok' dalında artıyordu; 'adaylar
        var ama robustness geçemedi' dalı sayacı atlayarak sonsuz döngü riski
        bırakıyordu. Artık iki kazanansız dal da bunu çağırır.
        """
        nonlocal _winless_rounds
        _winless_rounds += 1
        return bool(_WINLESS_ROUND_LIMIT and _winless_rounds >= _WINLESS_ROUND_LIMIT)

    def _winless_stop() -> None:
        _add_step(
            run_id,
            f"⏹ {_WINLESS_ROUND_LIMIT} ardışık kazanansız tur — "
            "devre kesici sürekli modu durduruyor.",
        )
        _tl_close_open(run_id, status="warn")
        with _AGENT_LOCK:
            if run_id in _AGENT_PROGRESS:
                _AGENT_PROGRESS[run_id]["done"] = True
                _AGENT_PROGRESS[run_id]["continuous_finished"] = True
        _session_log(
            run_id,
            "session_end",
            round=run_number,
            outcome="winless_limit",
            total_rounds=run_number,
        )

    while True:
        run_number += 1
        if (
            continuous_mode
            and _MAX_CONTINUOUS_ROUNDS
            and run_number > _MAX_CONTINUOUS_ROUNDS
        ):
            _add_step(
                run_id,
                f"Sürekli mod: maksimum {_MAX_CONTINUOUS_ROUNDS} tura ulaşıldı, durduruluyor.",
            )
            break

        # M22: tur başında bütçe kontrolü — süre/token tavanı aşıldıysa nazikçe bitir.
        _elapsed_h = (time.monotonic() - _worker_t0) / 3600.0
        with _AGENT_LOCK:
            _bs = _AGENT_PROGRESS.get(run_id) or {}
            _tok_total = sum(
                (_bs.get(k, 0) or 0)
                for k in (
                    "tokens_in",
                    "tokens_out",
                    "tokens_cache_read",
                    "tokens_cache_write",
                )
            )
        _budget_reason = None
        if max_hours and max_hours > 0 and _elapsed_h >= max_hours:
            _budget_reason = f"süre tavanı ({max_hours:g} saat) doldu"
        elif (
            max_total_tokens and max_total_tokens > 0 and _tok_total >= max_total_tokens
        ):
            _budget_reason = f"token tavanı ({max_total_tokens:,}) aşıldı"
        if _budget_reason:
            _add_step(
                run_id,
                f"⏹ Bütçe: {_budget_reason} — koşu nazikçe sonlandırılıyor.",
            )
            _tl_close_open(run_id, status="warn")
            with _AGENT_LOCK:
                if run_id in _AGENT_PROGRESS:
                    _AGENT_PROGRESS[run_id]["done"] = True
            _session_log(
                run_id,
                "session_end",
                round=run_number,
                outcome="budget",
                reason=_budget_reason,
                total_rounds=run_number - 1,
            )
            return
        if continuous_mode and run_number > 1:
            with _AGENT_LOCK:
                s = _AGENT_PROGRESS.get(run_id)
                if s is None or s.get("stop_requested"):
                    break
                # Reset for next round
                s["done"] = False
                s["error"] = None
                s["winner_result"] = None
                s["winner_spec_name"] = ""
                s["winner_spec_id"] = ""
                s["winner_rob"] = None
                s["winner_holdout"] = None
                s["winner_narrative"] = None  # M2625: yeni tur → taze narrative
                s["rob_scan_log"] = []
                s["backtest_results"] = []
                for p in s["phases"]:
                    p["status"] = "pending"
                    p["detail"] = ""
            # Timeline: önceki turun açık span'ları warn ile kapanır; span'lar
            # SİLİNMEZ (round etiketiyle kalır) — canlı görünüm aktif tura filtreler.
            _tl_close_open(run_id, status="warn")
            _add_step(run_id, f"━━━ Sürekli mod: tur {run_number} başlıyor ━━━")

        try:
            # ── Faz 0: Veri ──────────────────────────────────────────────────────
            # Multi-TF: her TF için lazy-load cache. İlk TF'yi burada önceden yükle.
            # L32: tf_cache KIRPILMIŞ veriyi tutar (mühürlü holdout hariç);
            # holdout dilimi holdout_cache'te saklanır ve YALNIZ kazanan ilan
            # edildikten sonra tek bir doğrulama koşusunda kullanılır.
            tf_cache: dict[str, pd.DataFrame] = {}  # interval → trimmed_df
            holdout_cache: dict[str, pd.DataFrame | None] = {}  # interval → sealed df

            def _load_tf(iv: str) -> pd.DataFrame:
                if iv in tf_cache:
                    return tf_cache[iv]
                # Timeline: cache-miss yüklemesi bir "data" span'ı olarak izlenir.
                _tl_key = f"data-{iv}-r{run_number}"
                _tl_begin(
                    run_id,
                    "data",
                    _tl_key,
                    f"Veri: {instrument_id if is_external else symbol} {iv}",
                    round_num=run_number,
                )
                try:
                    df = _load_tf_uncached(iv)
                except Exception:
                    _tl_end(run_id, _tl_key, status="fail")
                    raise
                # L32: mühürlü holdout — son OOS_HOLDOUT_DAYS gün iterasyon +
                # robustness fazlarından saklanır. Kalan veri < 200 bar ise atla.
                trimmed, hold_df = _split_holdout(df)
                if hold_df is not None:
                    df = trimmed
                    tf_cache[iv] = df
                    _add_step(
                        run_id,
                        f"🔒 Mühürlü holdout ayrıldı ({iv}): son "
                        f"{OOS_HOLDOUT_DAYS} gün ({len(hold_df):,} bar) kazanan "
                        f"ilan edilene dek saklanacak — kalan {len(df):,} bar",
                    )
                else:
                    tf_cache[iv] = df
                    _add_step(
                        run_id,
                        f"⚠ holdout için veri yetersiz ({iv}) — mühürlü OOS atlandı",
                    )
                holdout_cache[iv] = hold_df
                _tl_end(run_id, _tl_key, status="ok", n_bars=len(df))
                return df

            def _load_tf_uncached(iv: str) -> pd.DataFrame:
                from datetime import timedelta

                from data import load_bybit_bars

                if is_external:
                    from data import load_external_bars

                    _add_step(
                        run_id,
                        f"Katalog verisi yükleniyor ({instrument_id}, {iv})…",
                    )
                    df = load_external_bars(instrument_id, iv)  # tüm katalog aralığı
                    if len(df) < 100:
                        raise RuntimeError(
                            f"Yetersiz veri ({len(df)} bar, {instrument_id} {iv})."
                        )
                    # 1-MINUTE guard: ~23 yıl >2M bar — motoru duyarlı tutmak için
                    # son 2 yıla kırp (Bybit 1m guard'ının aynası).
                    if iv == "1-MINUTE" and len(df) > 1_000_000:
                        cutoff = df.index[-1] - pd.Timedelta(days=730)
                        df = df[df.index >= cutoff]
                        _add_step(
                            run_id, f"1-MINUTE son 2 yıla kırpıldı ({len(df):,} bar)"
                        )
                    tf_cache[iv] = df
                    return df

                cache_path = _bybit_cache_path(category, symbol, iv)
                # Widest available range per TF. 1m is bounded to ~2y (and cropped
                # below) so the engine stays responsive; coarser TFs pull Bybit's
                # full history (bar counts are small). load_bybit_bars now backfills
                # older history when `start` predates the cache, so a narrow cache
                # (e.g. the 7-day startup fetch) is widened here on first run and
                # served from cache afterwards.
                lookback_days = {"1": 730, "5": 1460, "15": 2200}.get(iv, 2200)
                end_dt = datetime.now(UTC)
                start_dt = end_dt - timedelta(days=lookback_days)
                _add_step(
                    run_id,
                    f"En geniş aralık yükleniyor ({iv}, ~{lookback_days}g)…",
                )
                try:
                    df = load_bybit_bars(
                        symbol=symbol,
                        interval=iv,
                        category=category,
                        start=start_dt,
                        end=end_dt,
                    )
                except Exception as fetch_exc:
                    # Network hiccup — fall back to whatever is already cached.
                    if not cache_path.exists():
                        raise RuntimeError(
                            f"{symbol}/{category}/{iv} verisi yüklenemedi: {fetch_exc}"
                        ) from fetch_exc
                    _add_step(
                        run_id, f"Fetch hatası ({iv}), cache'e düşülüyor: {fetch_exc}"
                    )
                    df = pd.read_parquet(cache_path)

                if len(df) < 100:
                    raise RuntimeError(
                        f"Yetersiz veri ({len(df)} bar, {iv}). "
                        "Data sayfasından fetch edin."
                    )
                # 1m guard: cap at last 2 years so backtests stay responsive.
                if iv == "1" and len(df) > 1_000_000:
                    cutoff = df.index[-1] - pd.Timedelta(days=730)
                    df = df[df.index >= cutoff]
                    _add_step(run_id, f"1m son 2 yıla kırpıldı ({len(df):,} bar)")
                tf_cache[iv] = df
                return df

            if is_external:
                # Enstrümanın gerçekten sahip olduğu zaman dilimlerine daralt.
                from data import _external_bar_dir

                avail = [
                    iv
                    for iv in intervals
                    if _external_bar_dir(instrument_id, iv) is not None
                ]
                skipped = [iv for iv in intervals if iv not in avail]
                if skipped:
                    _add_step(
                        run_id,
                        f"⚠ {instrument_id} için eksik TF atlandı: {', '.join(skipped)}",
                    )
                if not avail:
                    raise RuntimeError(
                        f"{instrument_id} için seçilen zaman dilimlerinde "
                        "katalog verisi yok"
                    )
                intervals = avail

            first_iv = intervals[0]
            _set_phase(
                run_id,
                0,
                (
                    f"Katalog okunuyor: {instrument_id}/{first_iv}"
                    if is_external
                    else f"Cache okunuyor: {symbol}/{category}/{first_iv}"
                )
                + (f" + {len(intervals) - 1} TF daha" if len(intervals) > 1 else ""),
            )
            first_df = _load_tf(first_iv)
            date_start = first_df.index[0].date()
            date_end = first_df.index[-1].date()
            _done_phase(
                run_id,
                0,
                f"✓ {len(first_df):,} bar · {date_start} → {date_end}"
                + (
                    f" · Multi-TF: {', '.join(intervals)}" if len(intervals) > 1 else ""
                ),
            )

            if is_external:
                # Index konvansiyonu: "symbol" anahtarı YOK — Bybit'e özel grafik
                # ve robustness OOB panelleri bars_info["symbol"] üzerinden
                # tetiklendiğinden harici koşularda güvenle devre dışı kalır.
                bars_info = {
                    "ticker": instrument_id,
                    "granularity": first_iv,
                    "n_bars": len(first_df),
                    "start": str(date_start),
                    "end": str(date_end),
                }
            else:
                bars_info = {
                    "symbol": symbol,
                    "category": category,
                    "interval": first_iv,
                    "n_bars": len(first_df),
                    "start": str(date_start),
                    "end": str(date_end),
                }

            # ── Faz 1: İlk strateji ──────────────────────────────────────────────
            _set_phase(run_id, 1, "Claude strateji üretiyor…")

            catalog = load_catalog()
            dummy_history: list = []

            if web_research:
                _add_step(run_id, "🌐 Web araştırması yapılıyor…")
            _tl_begin(
                run_id,
                "llm",
                f"llm-propose-r{run_number}",
                "İlk strateji (Claude)",
                round_num=run_number,
            )
            proposal, _usage1 = propose_composed_strategy(
                dummy_history,
                catalog,
                hint=hint,
                web_research=web_research,
                market=market,
            )
            _add_tokens(run_id, _usage1)
            _session_log(
                run_id,
                "strategy_proposed",
                iteration=0,
                round=run_number,
                spec=proposal,
                source="builtin",
                usage=_usage1,
            )

            spec = _proposal_to_spec(proposal)
            _tl_end(run_id, f"llm-propose-r{run_number}", status="ok", name=spec.name)
            if is_external:
                _clamp_spec_trade_size(spec)
            spec.trend_filter = trend_filter
            spec.trend_interval = trend_interval
            _done_phase(
                run_id,
                1,
                f"✓ {spec.name}" + (" · trend filter ON" if trend_filter else ""),
            )

            with _AGENT_LOCK:
                if run_id in _AGENT_PROGRESS:
                    _AGENT_PROGRESS[run_id]["strategy_name"] = spec.name

            # ── Faz 2: Backtest döngüsü ──────────────────────────────────────────
            # Backtests run in killable child processes (sandbox); the child
            # rebuilds the instrument/bar_type from the recipe, so the worker
            # thread never touches Nautilus objects (nor the GIL) directly.
            _set_phase(run_id, 2, f"0/{n_iterations} tamamlandı")

            history: list = []
            results: list[tuple] = []  # (spec, result, iter_iv)
            used_concepts: list[str] = []  # custom blok label'ları biriktir

            for i in range(n_iterations):
                # Check stop signal between iterations in continuous mode.
                # _add_step KİLİDİN DIŞINDA: kendisi de _AGENT_LOCK alır;
                # blok içinden çağırmak re-entrant deadlock'tu (tüm sunucuyu
                # donduran 2026-07-14 canlı olayı).
                with _AGENT_LOCK:
                    s = _AGENT_PROGRESS.get(run_id)
                    stop_hit = bool(s is not None and s.get("stop_requested"))
                if stop_hit:
                    _add_step(run_id, "  ⏹ Durdurma sinyali alındı — döngü kesiliyor")
                    break

                # Round-robin TF seçimi
                iter_iv = intervals[i % len(intervals)]
                iter_df = _load_tf(iter_iv)
                if is_external:
                    iter_bars_info = {
                        "ticker": instrument_id,
                        "granularity": iter_iv,
                        "n_bars": len(iter_df),
                        "start": str(iter_df.index[0].date()),
                        "end": str(iter_df.index[-1].date()),
                    }
                    tf_label = f" [{iter_iv}]"
                else:
                    iter_bars_info = {
                        "symbol": symbol,
                        "category": category,
                        "interval": iter_iv,
                        "n_bars": len(iter_df),
                        "start": str(iter_df.index[0].date()),
                        "end": str(iter_df.index[-1].date()),
                    }
                    tf_label = f" [{iter_iv}m]" if iter_iv != "D" else " [1D]"

                _add_step(
                    run_id,
                    f"[{i + 1}/{n_iterations}] Backtest{tf_label}: {spec.name}…",
                )
                run_label = instrument_id if is_external else symbol
                _tl_begin(
                    run_id,
                    "backtest",
                    f"bt-r{run_number}-i{i + 1}",
                    f"Backtest {i + 1}/{n_iterations} · {spec.name} [{iter_iv}]",
                    round_num=run_number,
                    iter=i + 1,
                    name=spec.name,
                )
                # Run in a killable child process: a Nautilus backtest holds the
                # GIL for its whole run and would otherwise freeze the async web
                # server's event loop. The timeout also kills a hung backtest.
                _bt_t0 = time.perf_counter()
                r = run_backtest_guarded(
                    spec,
                    iter_df,
                    _recipe(iter_iv),
                    iteration_id=i,
                    rationale=f"agent-run · iter {i + 1}/{n_iterations} · {run_label} {iter_iv}",
                    progress_fn=lambda m, _i=i: _add_step(run_id, f"  └ {m}"),
                    timeout_s=150.0,
                    force_subprocess=True,
                )
                _bt_elapsed = time.perf_counter() - _bt_t0
                # strategy alanına block type listesini ekle → Claude bu bilgiyi görür
                block_types_str = "+".join(b.type for b in spec.blocks)
                r.strategy = f"composed:{spec.name} [{block_types_str}]"
                history.append(r)
                results.append((spec, r, iter_iv))
                try:
                    _log_backtest(
                        spec,
                        r,
                        "External" if is_external else "Bybit",
                        iter_bars_info,
                        elapsed_sec=_bt_elapsed,
                    )
                except Exception as log_exc:
                    _add_step(run_id, f"  ⚠ Log yazılamadı: {log_exc}")

                sc = _score(r)
                # Canlı backtest tablosu için state'e ekle
                m_bt = r.metrics or {}
                with _AGENT_LOCK:
                    s = _AGENT_PROGRESS.get(run_id)
                    if s is not None:
                        s["backtest_results"].append(
                            {
                                "iter": i + 1,
                                "round": run_number,
                                "interval": iter_iv,
                                "spec_name": spec.name,
                                "score": round(sc, 4) if sc > float("-inf") else None,
                                "pnl": m_bt.get("pnl"),
                                "pnl_pct": m_bt.get("pnl_pct"),
                                "sharpe": m_bt.get("sharpe"),
                                "max_dd": m_bt.get("max_dd"),
                                "win_rate": m_bt.get("win_rate"),
                                "n_trades": m_bt.get("n_trades", 0),
                                "avg_dur": m_bt.get("avg_duration_mins"),
                                "error": r.error,
                            }
                        )
                # Session log: backtest sonucu (equity_curve dahil)
                _session_log(
                    run_id,
                    "backtest_result",
                    iteration=i,
                    round=run_number,
                    interval=iter_iv,
                    spec_name=spec.name,
                    spec_id=spec.id,
                    spec_blocks=[
                        {"type": b.type, "role": b.role, "params": b.params}
                        for b in spec.blocks
                    ],
                    score=round(sc, 4),
                    metrics=r.metrics,
                    equity_curve=list(r.equity_curve) if r.equity_curve else [],
                    equity_dates=list(r.equity_dates) if r.equity_dates else [],
                    n_trades=len(r.trades) if r.trades else 0,
                    bars_info=iter_bars_info,
                    error=r.error,
                )
                if r.error:
                    _add_step(run_id, f"  ✗ Hata: {r.error[:80]}")
                    _tl_end(run_id, f"bt-r{run_number}-i{i + 1}", status="fail")
                else:
                    m = r.metrics or {}
                    _add_step(
                        run_id,
                        f"  ✓ PnL={m.get('pnl', 0):+.2f} · "
                        f"Sharpe={m.get('sharpe', float('nan')):.2f} · "
                        f"Trades={m.get('n_trades', 0)} · Skor={sc:.3f}",
                    )
                    _tl_end(
                        run_id,
                        f"bt-r{run_number}-i{i + 1}",
                        status="warn" if (m.get("n_trades", 0) or 0) == 0 else "ok",
                        pnl=m.get("pnl"),
                        score=round(sc, 4) if sc > float("-inf") else None,
                    )

                _set_phase(run_id, 2, f"{i + 1}/{n_iterations} tamamlandı")

                if i < n_iterations - 1:
                    next_i = i + 1
                    use_custom = next_i % 2 == 1
                    _add_step(
                        run_id,
                        f"[{next_i + 1}/{n_iterations}] "
                        + (
                            "Custom blok üretiliyor…"
                            if use_custom
                            else "Claude yeni strateji üretiyor…"
                        ),
                    )
                    _tl_begin(
                        run_id,
                        "llm",
                        f"llm-refine-r{run_number}-i{next_i + 1}",
                        (
                            "Custom blok üretimi"
                            if use_custom
                            else "Claude yeni strateji"
                        ),
                        round_num=run_number,
                        iter=next_i + 1,
                    )
                    try:
                        if use_custom:
                            custom_spec = _generate_custom_spec(
                                run_id,
                                next_i,
                                hint,
                                history,
                                used_concepts=used_concepts,
                                round_num=run_number,
                                market=market,
                            )
                            if custom_spec is not None:
                                spec = custom_spec
                                # Üretilen blok label'larını biriktir
                                for b in spec.blocks:
                                    used_concepts.append(b.type)
                                _add_step(run_id, f"  → {spec.name} (custom)")
                            else:
                                proposal, _u = propose_composed_strategy(
                                    history, load_catalog(), hint=hint, market=market
                                )
                                _add_tokens(run_id, _u)
                                spec = _proposal_to_spec(proposal)
                                _session_log(
                                    run_id,
                                    "strategy_proposed",
                                    iteration=next_i,
                                    round=run_number,
                                    spec=proposal,
                                    source="builtin_fallback",
                                    usage=_u,
                                )
                                _add_step(run_id, f"  → {spec.name} (fallback builtin)")
                        else:
                            proposal, _u = propose_composed_strategy(
                                history, load_catalog(), hint=hint, market=market
                            )
                            _add_tokens(run_id, _u)
                            spec = _proposal_to_spec(proposal)
                            _session_log(
                                run_id,
                                "strategy_proposed",
                                iteration=next_i,
                                round=run_number,
                                spec=proposal,
                                source="builtin",
                                usage=_u,
                            )
                            _add_step(run_id, f"  → {spec.name}")
                        # H9: trend_filter/trend_interval yalnız ilk (faz 1)
                        # spec'e uygulanıyordu; refine'de üretilen HER spec
                        # composer default'u (trend_filter=False) ile kalıp
                        # iterasyonlar arası kıyası tutarsız yapıyordu ve
                        # kazanan filtresiz kaydediliyordu. Her spec'e uygula.
                        spec.trend_filter = trend_filter
                        spec.trend_interval = trend_interval
                        if is_external:
                            _clamp_spec_trade_size(spec)
                        with _AGENT_LOCK:
                            if run_id in _AGENT_PROGRESS:
                                _AGENT_PROGRESS[run_id]["strategy_name"] = spec.name
                        _tl_end(
                            run_id,
                            f"llm-refine-r{run_number}-i{next_i + 1}",
                            status="ok",
                            name=spec.name,
                        )
                    except Exception as e:
                        _add_step(
                            run_id,
                            f"  ⚠ Öneri alınamadı: {e} — önceki strateji devam ediyor",
                        )
                        _tl_end(
                            run_id,
                            f"llm-refine-r{run_number}-i{next_i + 1}",
                            status="warn",
                        )

            _done_phase(run_id, 2, f"✓ {n_iterations} iterasyon tamamlandı")

            # ── Faz 3: Sıralama ──────────────────────────────────────────────────
            _set_phase(run_id, 3, "Sonuçlar sıralanıyor…")
            _tl_begin(
                run_id,
                "backtest",
                f"rank-r{run_number}",
                "Sıralama",
                round_num=run_number,
            )
            ranked = _rank_results(results)
            eligible = [(s, r, iv) for s, r, iv in ranked if _score(r) > float("-inf")]

            _add_step(
                run_id,
                f"{len(eligible)}/{len(results)} sonuç uygun (≥{_MIN_TRADES} trade, hata yok)",
            )
            for rank_i, (s, r, iv) in enumerate(ranked[:5]):
                sc = _score(r)
                m = r.metrics or {}
                _add_step(
                    run_id,
                    f"  #{rank_i + 1} {s.name} [{iv}] · skor={sc:.3f} · "
                    f"PnL={m.get('pnl', 0):+.2f} · "
                    f"Sharpe={m.get('sharpe', float('nan')):.2f}",
                )

            if not eligible:
                _tl_end(run_id, f"rank-r{run_number}", status="warn")
                _done_phase(run_id, 3, "⚠ Uygun sonuç yok — tüm iterasyonlar başarısız")
                if not continuous_mode:
                    with _AGENT_LOCK:
                        if run_id in _AGENT_PROGRESS:
                            _AGENT_PROGRESS[run_id]["done"] = True
                    return
                _consec_err = 0  # tur istisnasız bitti — hata serisi kırıldı
                _last_err_str = None
                if _winless_bump():
                    _winless_stop()
                    return
                continue
            _tl_end(run_id, f"rank-r{run_number}", status="ok")
            _done_phase(run_id, 3, f"✓ {len(eligible)} aday sıralandı")

            # ── Faz 4: Robustness tarama ─────────────────────────────────────────
            _set_phase(run_id, 4, f"0/{len(eligible)} deneniyor")
            _set_robustness_scan(run_id, 0, len(eligible))

            winner_spec = None
            winner_result = None
            winner_rob = None
            winner_iv = None
            rob_scan_log: list[dict] = []
            # M26+M31: "ilk geçen kazanır" yerine geçen ilk EN FAZLA 3 adayı
            # topla; kazanan multi-symbol pass_rate faktörüyle ağırlıklı
            # etkin skora göre seçilir. 3 geçen bulunamadan liste biterse
            # eldekiyle karar verilir.
            _MAX_PASSERS = 3
            passers: list[dict] = []

            for rank_i, (cand_spec, cand_result, cand_iv) in enumerate(eligible):
                # Durdurma sinyali robustness taramasi ICINDE de kontrol edilir:
                # aksi halde iterasyon donguesu 'stop' ile kesilse bile akis aday
                # basina dakikalarca suren tam robustness taramasina giriyordu.
                with _AGENT_LOCK:
                    _rs = _AGENT_PROGRESS.get(run_id)
                    _rob_stop = bool(_rs is not None and _rs.get("stop_requested"))
                if _rob_stop:
                    _add_step(
                        run_id,
                        "  ⏹ Durdurma sinyali — robustness taramasi kesiliyor",
                    )
                    break
                _set_robustness_scan(run_id, rank_i + 1, len(eligible))
                _add_step(
                    run_id,
                    f"[{rank_i + 1}/{len(eligible)}] Robustness: {cand_spec.name} [{cand_iv}]",
                )
                _set_phase(
                    run_id,
                    4,
                    f"{rank_i + 1}/{len(eligible)} deneniyor: {cand_spec.name}",
                )

                cand_df = _load_tf(cand_iv)

                _rob_key = f"rob-r{run_number}-c{rank_i + 1}"
                _tl_begin(
                    run_id,
                    "robustness",
                    _rob_key,
                    f"Robustness {rank_i + 1}/{len(eligible)} · {cand_spec.name}",
                    round_num=run_number,
                    name=cand_spec.name,
                )
                _rob_progress = _make_rob_progress(run_id, rank_i + 1, run_number)
                try:
                    # Isolated in a killable child so the suite's many backtests
                    # can't freeze the web server's event loop.
                    rob = run_robustness_guarded(
                        cand_spec,
                        cand_df,
                        _recipe(cand_iv),
                        cand_result.trades,
                        symbol=instrument_id if is_external else symbol,
                        interval=cand_iv,
                        progress_fn=_rob_progress,
                    )
                except Exception as rob_exc:
                    _rob_progress.close_open("fail")
                    _tl_end(run_id, _rob_key, status="fail")
                    _add_step(run_id, f"  ⚠ Robustness hatası: {rob_exc} — atlanıyor")
                    rob_scan_log.append(
                        {
                            "rank": rank_i + 1,
                            "name": cand_spec.name,
                            "score": round(_score(cand_result), 3),
                            "passed": False,
                            "overfitting_label": f"hata: {type(rob_exc).__name__}",
                            "mc_dd_p50": None,
                            "wf_pass": "—",
                            "ms_label": "—",
                        }
                    )
                    with _AGENT_LOCK:
                        if run_id in _AGENT_PROGRESS:
                            _AGENT_PROGRESS[run_id]["rob_scan_log"] = list(rob_scan_log)
                    continue

                passed = _robustness_passed(rob, strict=strict_mode, run_id=run_id)

                split_label = (rob.get("split") or {}).get("overfitting_label", "?")
                mc_dd = (rob.get("mc") or {}).get("max_dd_p50", None)
                wfo = rob.get("wfo_windows") or []
                wf_pos = sum(
                    1 for w in wfo if (w.get("test_metrics") or {}).get("pnl", 0) > 0
                )
                wf_str = f"{wf_pos}/{len(wfo)}" if wfo else "—"
                ms_label = (rob.get("multi_symbol") or {}).get(
                    "generalization_label", "—"
                )

                # Session log: tam robustness sonucu (equity eğrileri + multi_symbol dahil)
                _session_log(
                    run_id,
                    "robustness_result",
                    round=run_number,
                    rank=rank_i + 1,
                    spec_name=cand_spec.name,
                    spec_id=cand_spec.id,
                    score=round(_score(cand_result), 4),
                    passed=passed,
                    overfitting_label=split_label,
                    wf_pass=wf_str,
                    ms_label=ms_label,
                    split=rob.get("split"),
                    wfo_windows=rob.get("wfo_windows"),
                    mc=rob.get("mc"),
                    multi_symbol=rob.get("multi_symbol"),
                )
                # L26: hafif SQLite indeksi (best-effort, hata yutulur)
                _index_insert(
                    run_id,
                    run_number,
                    cand_spec.name,
                    cand_spec.id,
                    _score(cand_result),
                    passed,
                    instrument_id if is_external else symbol,
                    cand_iv,
                )

                rob_scan_log.append(
                    {
                        "rank": rank_i + 1,
                        "name": cand_spec.name,
                        "score": round(_score(cand_result), 3),
                        "passed": passed,
                        "overfitting_label": split_label,
                        "mc_dd_p50": round(mc_dd, 1) if mc_dd is not None else None,
                        "wf_pass": wf_str,
                        "ms_label": ms_label,
                    }
                )
                # Flush partial results after each candidate so data is preserved
                # even if an exception aborts the loop later.
                with _AGENT_LOCK:
                    if run_id in _AGENT_PROGRESS:
                        _AGENT_PROGRESS[run_id]["rob_scan_log"] = list(rob_scan_log)

                _rob_progress.close_open("ok")
                if passed:
                    _tl_end(run_id, _rob_key, status="ok", name=cand_spec.name)
                    # M26+M31: geçen aday havuza girer; etkin skor = _score ×
                    # multi-symbol pass_rate faktörü (0.15…1.0).
                    raw_score = _score(cand_result)
                    factor = _ms_score_factor(rob)
                    # Isaret-guvenli MS cezasi: pozitif skorda raw*factor'a birebir
                    # esit (raw - (1-factor)*raw), negatif skorda ise kucuk factor'un
                    # skoru DAHA AZ negatif yapip siralamayi tersine cevirmesini onler
                    # (ceza her zaman skoru ASAGI ceker).
                    effective = raw_score - (1.0 - factor) * abs(raw_score)
                    passers.append(
                        {
                            "spec": cand_spec,
                            "result": cand_result,
                            "rob": rob,
                            "iv": cand_iv,
                            "score": raw_score,
                            "factor": factor,
                            "effective": effective,
                        }
                    )
                    _add_step(
                        run_id,
                        f"  ✅ TÜM TESTLERİ GEÇTİ! "
                        f"IS/OOS: {split_label} · WFO: {wf_str} · Multi-symbol: {ms_label}",
                    )
                    _add_step(
                        run_id,
                        f"  ⚖ Etkin skor: {raw_score:.3f} × MS-faktör "
                        f"{factor:.3f} = {effective:.3f} "
                        f"({len(passers)}/{_MAX_PASSERS} geçen aday)",
                    )
                    if len(passers) >= _MAX_PASSERS:
                        _add_step(
                            run_id,
                            f"  {_MAX_PASSERS} geçen aday toplandı — tarama bitiyor",
                        )
                        break
                else:
                    _tl_end(run_id, _rob_key, status="warn", name=cand_spec.name)
                    _add_step(
                        run_id,
                        (
                            f"  ❌ Geçemedi — IS/OOS: {split_label} · "
                            f"WFO: {wf_str} · MC medyan DD: {mc_dd:.1f}% · Multi-symbol: {ms_label}"
                            if mc_dd is not None
                            else f"  ❌ Geçemedi — IS/OOS: {split_label} · "
                            f"WFO: {wf_str} · Multi-symbol: {ms_label}"
                        ),
                    )

            if passers:
                # Etkin skoru en yüksek geçen aday kazanır.
                best = max(passers, key=lambda p: p["effective"])
                winner_spec = best["spec"]
                winner_result = best["result"]
                winner_rob = best["rob"]
                winner_iv = best["iv"]
                if len(passers) > 1:
                    _add_step(
                        run_id,
                        "🏁 Geçenler arası seçim: "
                        + " · ".join(
                            f"{p['spec'].name}={p['effective']:.3f}"
                            f" ({p['score']:.3f}×{p['factor']:.2f})"
                            for p in passers
                        ),
                    )
                _add_step(
                    run_id,
                    f"🏆 Kazanan: {winner_spec.name} "
                    f"(etkin skor {best['effective']:.3f})",
                )
                # bars_info'yu kazananın gerçek TF'siyle güncelle — yalnız TF
                # anahtarı değil n_bars/start/end de kazananın df'sinden yeniden
                # kurulur (multi-TF'de ilk TF'nin aralığı chart URL penceresini ve
                # winner session-log'unu yanlış aralığa kaydırıyordu).
                bars_info["granularity" if is_external else "interval"] = winner_iv
                _win_df = _load_tf(winner_iv)
                bars_info["n_bars"] = len(_win_df)
                bars_info["start"] = str(_win_df.index[0].date())
                bars_info["end"] = str(_win_df.index[-1].date())

            if winner_spec is None:
                _done_phase(run_id, 4, f"✗ {len(eligible)} adaydan hiçbiri geçmedi")
                if not continuous_mode:
                    with _AGENT_LOCK:
                        if run_id in _AGENT_PROGRESS:
                            _AGENT_PROGRESS[run_id]["done"] = True
                    _session_log(
                        run_id,
                        "session_end",
                        round=run_number,
                        outcome="no_winner",
                        total_rounds=run_number,
                    )
                    return
                _consec_err = 0  # tur istisnasız bitti — hata serisi kırıldı
                _last_err_str = None
                # M22: 'adaylar var ama robustness geçemedi' de kazanansız tur —
                # sayaç burada da artmalı (en yaygın sonsuz-döngü senaryosu).
                if _winless_bump():
                    _winless_stop()
                    return
                _add_step(run_id, "Sürekli mod: yeni tur başlıyor…")
                continue

            _winless_rounds = 0  # M22: kazanan bulundu — kazanansız seri kırıldı
            _done_phase(run_id, 4, f"✓ Kazanan: {winner_spec.name}")

            # ── Faz 5: Kaydet ────────────────────────────────────────────────────
            _set_phase(run_id, 5, "Catalog'a kaydediliyor…")
            _tl_begin(
                run_id,
                "data",
                f"save-r{run_number}",
                "Kazanan kaydediliyor",
                round_num=run_number,
                name=winner_spec.name,
            )
            # M14: kilitsiz load→append→save yerine kilitli append_to_catalog —
            # eşzamanlı lab/strategy kaydıyla kazanan kaybolmasın.
            from composer import append_to_catalog

            append_to_catalog(winner_spec)
            _add_step(run_id, f"✓ {winner_spec.name} → strategy_catalog.json")

            # H4/H8: kazanan farklı bir TF'de olabilir (winner_iv); cand_iv
            # döngüden artakalan SON taranan adayın TF'sidir. Robustness logu
            # ve mühürlü holdout kazananın KENDİ TF'siyle yapılmalı — yoksa
            # log kimliği ezişir ve holdout yanlış dilim/recipe ile koşar.
            _log_robustness(
                winner_spec.id,
                winner_spec.name,
                winner_rob,
                symbol=instrument_id if is_external else symbol,
                category=category,
                interval=winner_iv,
            )
            _add_step(run_id, "✓ Robustness sonucu → robustness_log.jsonl")
            _tl_end(run_id, f"save-r{run_number}", status="ok")
            _done_phase(run_id, 5, f"✓ {winner_spec.name} kaydedildi")

            # ── L32: mühürlü holdout — kazanan, seçime HİÇ girmemiş son
            # OOS_HOLDOUT_DAYS günlük dilimde BİR KEZ koşulur. Sonuç yalnız
            # raporlanır (tarafsız ileri-dönük tahmin + seçim-yanlılığı
            # dedektörü); hiçbir karara bağlanmaz.
            winner_holdout = None
            _hold_df = holdout_cache.get(winner_iv)  # H4: kazananın TF'si
            if _hold_df is not None and not _hold_df.empty:
                try:
                    _hold_res = run_backtest_guarded(
                        winner_spec,
                        _hold_df,
                        _recipe(winner_iv),
                        iteration_id=999,
                        rationale="mühürlü holdout (L32)",
                        timeout_s=150.0,
                        force_subprocess=True,
                    )
                    _hm = _hold_res.metrics or {}
                    if _hold_res.error is None and _hm:
                        winner_holdout = {
                            "sharpe": _hm.get("sharpe"),
                            "pnl_pct": _hm.get("pnl_pct"),
                            "n_trades": _hm.get("n_trades"),
                            "days": OOS_HOLDOUT_DAYS,
                        }
                        _add_step(
                            run_id,
                            f"🔒 Mühürlü OOS ({OOS_HOLDOUT_DAYS}g): "
                            f"Sharpe {_hm.get('sharpe', 0):.2f} · "
                            f"PnL {100 * (_hm.get('pnl_pct') or 0):.1f}% · "
                            f"{_hm.get('n_trades', 0)} işlem (karara bağlanmaz)",
                        )
                        _session_log(
                            run_id,
                            "holdout_result",
                            round=run_number,
                            spec_id=winner_spec.id,
                            **winner_holdout,
                        )
                    else:
                        _add_step(
                            run_id,
                            f"⚠ Mühürlü OOS koşusu hata verdi: {_hold_res.error}",
                        )
                except Exception as _hold_err:
                    _add_step(run_id, f"⚠ Mühürlü OOS koşulamadı: {_hold_err}")

            with _AGENT_LOCK:
                if run_id in _AGENT_PROGRESS:
                    # bars_info'yu kazananın TF'siyle güncelle — chart URL için
                    winner_result.bars_info = bars_info
                    _AGENT_PROGRESS[run_id]["winner_result"] = winner_result
                    _AGENT_PROGRESS[run_id]["winner_spec_name"] = winner_spec.name
                    _AGENT_PROGRESS[run_id]["winner_spec_id"] = winner_spec.id
                    _AGENT_PROGRESS[run_id]["winner_rob"] = winner_rob
                    _AGENT_PROGRESS[run_id]["winner_holdout"] = winner_holdout
                    _AGENT_PROGRESS[run_id]["done"] = (
                        True  # always set so polling shows result
                    )

            # Session log: kazanan
            _session_log(
                run_id,
                "winner",
                round=run_number,
                spec_name=winner_spec.name,
                spec_id=winner_spec.id,
                score=round(_score(winner_result), 4),
                metrics=winner_result.metrics,
                equity_curve=list(winner_result.equity_curve)
                if winner_result.equity_curve
                else [],
                bars_info=bars_info,
            )
            _consec_err = 0  # tur başarıyla bitti — hata serisi kırıldı
            _last_err_str = None

            if not continuous_mode:
                # Terminal olay: diğer tüm çıkış yolları (no_winner/error/stopped)
                # session_end yazıyor; kazanan yolu da yazsın ki dış izleyiciler
                # ve /sessions özeti oturumun bittiğini görebilsin.
                _session_log(
                    run_id,
                    "session_end",
                    round=run_number,
                    outcome="winner",
                    total_rounds=run_number,
                )
                return

            # In continuous mode: briefly expose the result then continue
            import time as _time

            _time.sleep(3)  # give polling a chance to render the result
            _add_step(
                run_id, f"Sürekli mod: tur {run_number} tamamlandı, devam ediliyor…"
            )

        except Exception as e:
            _tl_close_open(run_id, status="fail")
            err_str = f"{type(e).__name__}: {e}"
            with _AGENT_LOCK:
                if run_id in _AGENT_PROGRESS:
                    _AGENT_PROGRESS[run_id]["error"] = err_str
                    if not continuous_mode:
                        _AGENT_PROGRESS[run_id]["done"] = True
            _session_log(
                run_id,
                "session_end",
                round=run_number,
                outcome="error",
                error=err_str,
            )
            if not continuous_mode:
                break
            # Devre kesici: aynı hata ardışık _CONSEC_ERR_LIMIT tur → kalıcı
            # sorun (eksik veri, konfig) — yeniden denemek anlamsız, dur.
            _consec_err = _consec_err + 1 if err_str == _last_err_str else 1
            _last_err_str = err_str
            if _consec_err >= _CONSEC_ERR_LIMIT:
                _add_step(
                    run_id,
                    f"⏹ {_CONSEC_ERR_LIMIT} ardışık aynı hata — sürekli mod "
                    f"durduruluyor: {err_str[:100]}",
                )
                with _AGENT_LOCK:
                    if run_id in _AGENT_PROGRESS:
                        _AGENT_PROGRESS[run_id]["done"] = True
                break
            _add_step(run_id, f"⚠ Tur {run_number} hata: {e} — yeniden başlıyor…")
        finally:
            # Token snapshot + session_end her tur sonunda
            with _AGENT_LOCK:
                s = _AGENT_PROGRESS.get(run_id) or {}
            _ti = s.get("tokens_in", 0) or 0
            _to = s.get("tokens_out", 0) or 0
            _tcr = s.get("tokens_cache_read", 0) or 0
            _tcw = s.get("tokens_cache_write", 0) or 0
            # L38: model-bazlı tarife; abonelik CLI'da maliyet None.
            _model, _cost_usd = _llm_cost_usd(_ti, _to, _tcr, _tcw)
            _session_log(
                run_id,
                "token_snapshot",
                round=run_number,
                total_input=_ti,
                total_output=_to,
                cache_read=_tcr,
                cache_write=_tcw,
                pricing_model=_model,
                cost_usd=round(_cost_usd, 6) if _cost_usd is not None else None,
                cost_eur=round(_cost_usd * 0.91, 6) if _cost_usd is not None else None,
            )

    # Continuous mode exited (stop/budget/winless/error — tüm break'ler buraya).
    _tl_close_open(run_id, status="warn")
    with _AGENT_LOCK:
        if run_id in _AGENT_PROGRESS:
            _AGENT_PROGRESS[run_id]["done"] = True
            # Kalıcı bitiş: progress route bunu görünce polling'i durdurur
            # (ara-tur done=True penceresinden ayırt etmek için ayrı bayrak).
            _AGENT_PROGRESS[run_id]["continuous_finished"] = True
    _session_log(run_id, "session_end", outcome="stopped", total_rounds=run_number)


def _pick_best_exit_from_history(history: list) -> str | None:
    """Geçmişteki en yüksek skorlu stratejinin exit blok tipini döndür."""
    _EXIT_PRIORITY = [
        "momentum",
        "macd_cross",
        "rsi_threshold",
        "bollinger_break",
        "ema_cross",
    ]
    if not history:
        return None
    best_score = max((_score(r) for r in history), default=float("-inf"))
    if best_score > 0:
        for r in sorted(history, key=lambda x: _score(x), reverse=True)[:3]:
            name = r.strategy.lower()
            for et in _EXIT_PRIORITY:
                if et.replace("_", "") in name.replace("_", "").replace(" ", ""):
                    return et
        return _EXIT_PRIORITY[0]
    return None


def _generate_custom_spec(
    run_id: str,
    iter_idx: int,
    hint: str,
    history: list,
    used_concepts: list | None = None,
    round_num: int = 1,
    market: str | None = None,
):
    """Odd iterations: ask Claude for a novel idea, generate custom entry+exit blocks,
    register them, and return a ComposedStrategySpec built from those blocks.
    Returns None if block generation fails (caller falls back to builtin).

    ``market`` — opsiyonel pazar bağlamı (harici US equity koşuları); fikir
    üreticisine iletilir, None ise kripto ifadesi korunur.
    """
    from agent import (
        GeneratedCodeError,
        _propose_agent_strategy_idea,
        propose_custom_block,
    )
    from composer import (
        ComposedStrategySpec,
        SignalBlock,
        new_spec_id,
        register_custom_from_disk,
    )
    from custom_block_store import save_custom

    try:
        idea = _propose_agent_strategy_idea(
            hint, history, used_concepts=used_concepts, market=market
        )
        # M1583: fikir + custom-blok LLM çağrılarının token'ları sayaçlara
        # eklensin — eskiden yalnız builtin öneri sayılıyordu; custom üretim
        # en token-yoğun çağrı olduğundan maliyet ve max_total_tokens bütçe
        # kesicisi ciddi undercount ediyordu.
        _add_tokens(run_id, idea.get("usage"))
        entry_label = idea.get("entry_label", "Agent Entry")
        exit_label = idea.get("exit_label", "Agent Exit")

        entry_name = f"agnt_e_{run_id}_{iter_idx}"
        exit_name = f"agnt_x_{run_id}_{iter_idx}"

        _add_step(run_id, f"  ⚙ Custom entry block üretiliyor: {entry_label}…")
        entry_block = propose_custom_block(entry_label, idea["entry_desc"], "entry")
        _add_tokens(run_id, entry_block.get("usage"))
        entry_block["name"] = entry_name
        save_custom(entry_name, entry_block["meta"], entry_block["code"], prompt=hint)
        register_custom_from_disk(entry_name)
        _add_step(run_id, f"  ✓ Entry block kaydedildi: {entry_name}")
        # Session log + kodu {run_id}_blocks/ altına kopyala
        _session_log(
            run_id,
            "custom_block_generated",
            iteration=iter_idx,
            round=round_num,
            name=entry_name,
            role="entry",
            label=entry_label,
            meta=entry_block.get("meta"),
            code=entry_block.get("code", ""),
        )
        try:
            blocks_dir = SESSION_LOG_DIR / f"{run_id}_blocks"
            blocks_dir.mkdir(parents=True, exist_ok=True)
            (blocks_dir / f"{entry_name}.py").write_text(entry_block.get("code", ""))
        except Exception:
            pass

        # 50% ihtimalle history'deki en iyi builtin exit'i kullan (custom yerine)
        # Bu, kanıtlanmış çıkış mekanizmasını yeni entry fikirleriyle birleştirir
        best_builtin_exit = _pick_best_exit_from_history(history)
        use_builtin_exit = (
            best_builtin_exit is not None and __import__("random").random() < 0.5
        )

        def _extract_params(blk: dict) -> dict:
            raw = blk["meta"].get("params") or {}
            return {
                k: (v.get("default") if isinstance(v, dict) else v)
                for k, v in raw.items()
            }

        if use_builtin_exit:
            from composer import BLOCK_CATALOG, SignalBlock

            exit_meta = BLOCK_CATALOG.get(best_builtin_exit, {}).get("params", {})
            exit_blk = SignalBlock(
                type=best_builtin_exit,
                role="exit",
                params={k: v["default"] for k, v in exit_meta.items()},
            )
            _add_step(
                run_id,
                f"  → Builtin exit kullanılıyor: {best_builtin_exit} (geçmişte başarılı)",
            )
            spec = ComposedStrategySpec(
                id=new_spec_id(),
                name=idea.get("name", f"Custom {iter_idx}"),
                description=idea.get("description", ""),
                blocks=[
                    SignalBlock(
                        type=entry_name,
                        role="entry",
                        params=_extract_params(entry_block),
                    ),
                    exit_blk,
                ],
                trade_size=0.01,
                order_type="market",
                entry_logic="OR",
                exit_logic="OR",
            )
        else:
            _add_step(run_id, f"  ⚙ Custom exit block üretiliyor: {exit_label}…")
            exit_block = propose_custom_block(exit_label, idea["exit_desc"], "exit")
            _add_tokens(run_id, exit_block.get("usage"))  # M1583
            exit_block["name"] = exit_name
            save_custom(exit_name, exit_block["meta"], exit_block["code"], prompt=hint)
            register_custom_from_disk(exit_name)
            _add_step(run_id, f"  ✓ Exit block kaydedildi: {exit_name}")
            # Session log + kodu {run_id}_blocks/ altına kopyala
            _session_log(
                run_id,
                "custom_block_generated",
                iteration=iter_idx,
                round=round_num,
                name=exit_name,
                role="exit",
                label=exit_label,
                meta=exit_block.get("meta"),
                code=exit_block.get("code", ""),
            )
        try:
            blocks_dir = SESSION_LOG_DIR / f"{run_id}_blocks"
            blocks_dir.mkdir(parents=True, exist_ok=True)
            if not use_builtin_exit:
                (blocks_dir / f"{exit_name}.py").write_text(exit_block.get("code", ""))
        except Exception:
            pass

        if not use_builtin_exit:
            spec = ComposedStrategySpec(
                id=new_spec_id(),
                name=idea.get("name", f"Custom {iter_idx}"),
                description=idea.get("description", ""),
                blocks=[
                    SignalBlock(
                        type=entry_name,
                        role="entry",
                        params=_extract_params(entry_block),
                    ),
                    SignalBlock(
                        type=exit_name, role="exit", params=_extract_params(exit_block)
                    ),
                ],
                trade_size=0.01,
                order_type="market",
                entry_logic="OR",
                exit_logic="OR",
            )
        err = spec.validate()
        if err:
            raise RuntimeError(f"Custom spec geçersiz: {err}")
        return spec

    except (GeneratedCodeError, Exception) as e:
        _add_step(run_id, f"  ⚠ Custom blok üretilemedi: {e} — builtin'e dönülüyor")
        return None


def _cleanup_agent_blocks(run_id: str) -> None:
    """Legacy hook kept for compatibility; run-specific blocks are retained."""
    return None


@router.get("", response_class=HTMLResponse)
async def page(request: Request):
    from server import get_market_info, templates

    # Bitmemiş bir koşu varsa sayfayı ona OTOMATİK bağla — sunucu restart'ı /
    # sekme yenileme sonrası kullanıcı çalışan koşuyu görsün (koşu API'den
    # başlatılmış, yani bu tarayıcıdan başlatılmamış olsa bile). En yeni
    # aktif run'ı seç (insertion-ordered dict'te tersten ilk done=False).
    active_run_id = None
    with _AGENT_LOCK:
        for rid, st in reversed(_AGENT_PROGRESS.items()):
            if not st.get("done"):
                active_run_id = rid
                break

    return templates.TemplateResponse(
        request,
        "agent_backtest.html",
        {
            "active": "agent",
            "page_title": "Otonom Backtest Ajanı",
            "market": get_market_info(),
            "active_run_id": active_run_id,
        },
    )


@router.post("/run", response_class=HTMLResponse)
async def run(
    request: Request,
    hint: str = Form(default=""),
    symbol: str = Form(default="BTCUSDT"),
    category: str = Form(default="linear"),
    interval: str = Form(default="60"),
    multi_tf: str = Form(default=""),
    web_research: str = Form(default=""),
    n_iterations: int = Form(default=5),
    strict_mode: str = Form(default="strict"),
    trend_filter: str = Form(default=""),
    trend_interval: str = Form(default="60"),
    continuous: str = Form(default=""),
    source: str = Form(default="bybit"),
    instrument_id: str = Form(default=""),
    ext_interval: str = Form(default="1-DAY"),
    ext_trend_interval: str = Form(default="1-DAY"),
    max_hours: float = Form(default=0.0),
    max_total_tokens: int = Form(default=0),
):
    from server import get_market_info, templates

    n_iterations = max(2, min(15, n_iterations))
    # M22: opsiyonel bütçe tavanları (0 = sınırsız — varsayılan davranış aynı).
    max_hours = max(0.0, max_hours)
    max_total_tokens = max(0, max_total_tokens)
    is_strict = strict_mode != "relaxed"
    use_trend_filter = trend_filter == "1"
    is_continuous = continuous == "1"
    is_multi_tf = multi_tf == "1"
    use_web_research = web_research == "1"
    is_external = source == "external"
    instrument_id = instrument_id.strip()

    if is_external and not instrument_id:
        return HTMLResponse(
            "<div class='empty-state'>Katalog kaynağı için enstrüman seçin.</div>",
            status_code=400,
        )

    # Multi-TF modunda denenecek interval'lar
    # 15m çıkarıldı (6% başarı), Daily eklendi (temiz sinyal, az trade ama kaliteli)
    if is_external:
        intervals: list[str] = (
            ["1-HOUR", "4-HOUR", "1-DAY"] if is_multi_tf else [ext_interval]
        )
        trend_interval = ext_trend_interval
    else:
        intervals = ["60", "240", "D"] if is_multi_tf else [interval]

    run_id = uuid.uuid4().hex[:8]

    def _release_session_lock(evict_id: str) -> None:
        # L3: an evicted run's session-log lock is released too (no unbounded
        # Lock buildup). Runs under _AGENT_LOCK, preserving the lock-nesting
        # order (_AGENT_LOCK → _SESSION_LOG_META) that test_lock_nesting pins.
        with _SESSION_LOG_META:
            _SESSION_LOG_LOCKS.pop(evict_id, None)

    # L16: done-first — an active (continuous) run's state is never dropped; if
    # every slot is active the new run is refused (429).
    created = _AGENT_STORE.create_or_refuse(
        run_id,
        {
            "phases": [
                {"n": i, "label": lbl, "status": "pending", "detail": "", "ts": ""}
                for i, lbl in enumerate(_PHASES)
            ],
            "steps": [],
            "done": False,
            "error": None,
            "strategy_name": "",
            "stop_requested": False,
            "continuous_mode": is_continuous,
            "winner_result": None,
            "winner_spec_name": "",
            "winner_spec_id": "",
            "winner_rob": None,
            "winner_holdout": None,
            "rob_scan_log": [],
            "rob_scan_current": 0,
            "rob_scan_total": 0,
            "hint": hint.strip(),
            # Token kullanımı (her Claude API çağrısından biriktirilir)
            "tokens_in": 0,
            "tokens_out": 0,
            "tokens_cache_read": 0,
            "tokens_cache_write": 0,
            # Canlı backtest sonuçları tablosu için (agent ekranı üst paneli)
            "backtest_results": [],
            # Zaman çizelgesi span'ları (SVG Gantt — bkz. _tl_begin/_tl_end)
            "timeline": [],
        },
        on_evict=_release_session_lock,
    )
    if not created:
        return HTMLResponse(
            "<div class='empty-state'>⚠ 50 aktif koşu sınırı — yeni koşu "
            "başlatılamadı. Önce mevcut koşulardan birini durdurun.</div>",
            status_code=429,
        )

    threading.Thread(
        target=_agent_worker,
        kwargs=dict(
            run_id=run_id,
            hint=hint.strip(),
            symbol=symbol,
            category=category,
            intervals=intervals,
            n_iterations=n_iterations,
            strict_mode=is_strict,
            trend_filter=use_trend_filter,
            trend_interval=trend_interval,
            continuous_mode=is_continuous,
            web_research=use_web_research,
            source=source,
            instrument_id=instrument_id,
            max_hours=max_hours,
            max_total_tokens=max_total_tokens,
        ),
        daemon=True,
    ).start()

    # /progress ile ayni kilitli-snapshot deseni: worker eszamanli mutate
    # ederken canli dict'i template'e vermek yerine tutarli bir kopya render et.
    with _AGENT_LOCK:
        _raw0 = _AGENT_PROGRESS[run_id]
        _initial_state = {
            **_raw0,
            "phases": [dict(p) for p in _raw0["phases"]],
            "steps": list(_raw0["steps"]),
        }
    return templates.TemplateResponse(
        request,
        "fragments/agent_progress.html",
        {
            "run_id": run_id,
            "phases": _PHASES,
            "state": _initial_state,
            "done": False,
            "error": None,
            "market": get_market_info(),
            "tl": None,
            "steps_by_key": {},
        },
    )


@router.post("/stop/{run_id}", response_class=HTMLResponse)
async def stop(request: Request, run_id: str):
    """Koşan agent'a durma sinyali gönder (sürekli mod VE tek koşu).

    stop_requested; iterasyon döngüsünde, tur başında ve robustness aday
    taramasında kontrol edilir → mevcut adım bitince temiz durur. Düğme
    fragments/agent_progress.html'de (koşu sırasında) ve agent_result.html'de
    (sürekli mod, tur arası) render edilir.
    """
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s:
            s["stop_requested"] = True
    if s is None:
        # L16: state bellekte yok (restart/eviction) — sahte "sinyal gönderildi"
        # rozeti yerine dürüst mesaj.
        return HTMLResponse(
            "<span class='badge' style='background:rgba(148,163,184,0.2);"
            "color:#94a3b8;'>⚠ Koşu bellekte değil (sunucu yeniden başlatılmış "
            "olabilir) — durdurulacak bir işlem yok</span>"
        )
    return HTMLResponse(
        "<span class='badge' style='background:rgba(251,146,60,0.2);color:#fb923c;'>"
        "⏹ Durdurma sinyali gönderildi — mevcut tur bittikten sonra duracak</span>"
    )


def _terminal_message(run_id: str) -> str:
    """Bellekte olmayan run için dürüst mesaj.

    Diskteki session logu son event'iyle ayırt eder: session_end görmüş bir
    koşu gerçekten bitmiştir; log yarıda kesilmişse süreç ölmüştür (tipik
    neden: sunucu yeniden başlatıldı) — koşu 'tamamlandı' değildir.
    """
    generic = "Çalışma tamamlandı veya süresi doldu."
    try:
        log_path = SESSION_LOG_DIR / f"{run_id}.jsonl"
        if not log_path.exists():
            return generic
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8192))
            tail = f.read().decode("utf-8", errors="replace").strip().splitlines()
        last = json.loads(tail[-1]) if tail else {}
        if last.get("event") == "session_end":
            return (
                "Çalışma tamamlandı — geçmişi "
                "<a href='/sessions'>Session Logları</a>'nda inceleyebilirsiniz."
            )
        return (
            "⚠ Koşu yarıda kesildi (büyük olasılıkla sunucu yeniden "
            "başlatıldı). Kaldığı yere kadarki adımlar "
            "<a href='/sessions'>Session Logları</a>'nda; agent'ı yeniden "
            "başlatabilirsiniz."
        )
    except Exception:
        return generic


@router.get("/progress/{run_id}", response_class=HTMLResponse)
async def progress(request: Request, run_id: str):
    import asyncio

    from server import get_market_info, templates
    from web.viewmodels import iteration_row

    with _AGENT_LOCK:
        raw = _AGENT_PROGRESS.get(run_id)
        if raw is None:
            # Run already cleaned up or unknown — return terminal no-poll frame
            return HTMLResponse(
                "<div id='agent-progress-panel'>"
                "<div class='panel'><div class='panel-body empty-state'>"
                f"{_terminal_message(run_id)}"
                "</div></div></div>"
            )
        state = {
            "phases": [dict(p) for p in raw["phases"]],
            "steps": list(raw["steps"]),
            "done": raw["done"],
            "error": raw["error"],
            "strategy_name": raw["strategy_name"],
            "winner_result": raw["winner_result"],
            "winner_spec_name": raw["winner_spec_name"],
            "winner_spec_id": raw.get("winner_spec_id", ""),
            "winner_rob": raw.get("winner_rob"),
            "winner_holdout": raw.get("winner_holdout"),
            "rob_scan_log": list(raw.get("rob_scan_log", [])),
            "rob_scan_current": raw.get("rob_scan_current", 0),
            "rob_scan_total": raw.get("rob_scan_total", 0),
            "stop_requested": raw.get("stop_requested", False),
            "continuous_mode": raw.get("continuous_mode", False),
            "tokens_in": raw.get("tokens_in", 0),
            "tokens_out": raw.get("tokens_out", 0),
            "tokens_cache_read": raw.get("tokens_cache_read", 0),
            "tokens_cache_write": raw.get("tokens_cache_write", 0),
            "backtest_results": list(raw.get("backtest_results") or []),
            # Shallow copies — worker eş zamanlı mutate ediyor.
            "timeline": [dict(sp) for sp in raw.get("timeline") or []],
        }

    # Sürekli mod KALICI bittiyse polling'i durdur — aksi halde terminal
    # sonuç fragmenti her 2 sn'de sonsuza dek yeniden render/chart-rebuild
    # edilir (titreme + sürekli CPU/ağ). continuous_finished, worker döngüden
    # temelli çıkınca set edilir; ara-tur (done=True + 3s uyku) penceresinde
    # False kaldığı için sonraki tura geçiş korunur.
    is_continuous = state.get("continuous_mode", False) and not state.get(
        "continuous_finished"
    )

    # Zaman çizelgesi render modeli (aktif tura filtreli) + span→adım eşlemesi.
    from web.viewmodels import associate_steps, timeline_view

    _cur_round = max((sp.get("round", 1) for sp in state["timeline"]), default=1)
    tl = timeline_view(
        state["timeline"],
        now=None if state["done"] else datetime.now(UTC).timestamp(),
        round_num=_cur_round,
    )
    steps_by_key = associate_steps(
        state["timeline"], state["steps"], round_num=_cur_round
    )

    # L38: token kullanımı + model-bazlı TAHMİNİ maliyet (_MODEL_PRICING).
    # claude-cli/OAuth aboneliğinde token başına fatura YOK → cost None
    # (şablonlar satırı gizler; doluyken '≈ tahmini' etiketiyle gösterilir).
    _USD_EUR = 0.91
    _ti = state.get("tokens_in", 0) or 0
    _to = state.get("tokens_out", 0) or 0
    _tcr = state.get("tokens_cache_read", 0) or 0
    _tcw = state.get("tokens_cache_write", 0) or 0
    _model, _cost_usd = _llm_cost_usd(_ti, _to, _tcr, _tcw)
    token_info = {
        "input": _ti,
        "output": _to,
        "cache_read": _tcr,
        "cache_write": _tcw,
        "total": _ti + _to + _tcr + _tcw,
        "pricing_model": _model,
        "cost_usd": round(_cost_usd, 4) if _cost_usd is not None else None,
        "cost_eur": round(_cost_usd * _USD_EUR, 4) if _cost_usd is not None else None,
    }

    if state["done"] and state["winner_result"] is not None:
        result = state["winner_result"]
        last_row = iteration_row(result)
        last_row["rationale"] = result.rationale
        last_row["equity_curve"] = result.equity_curve
        last_row["equity_dates"] = result.equity_dates
        last_row["spec_name"] = state["winner_spec_name"]
        last_row["steps"] = state["steps"][-60:]  # cap to avoid huge DOM
        # M2625: narrative'i bir kez üret, gerçek progress dict'inde cache'le.
        # Eskiden done+winner doluyken HER poll (continuous'ta 2s'de bir) yeni
        # bir LLM API çağrısı yapıp yanıtı blokluyordu; kazanan sabit olduğundan
        # tek üretim yeter.
        with _AGENT_LOCK:
            _real = _AGENT_PROGRESS.get(run_id)
            _narr = _real.get("winner_narrative") if _real else None
        if _narr is None:
            _narr = await asyncio.to_thread(_winner_narrative, last_row, state)
            with _AGENT_LOCK:
                _real = _AGENT_PROGRESS.get(run_id)
                if _real is not None:
                    _real["winner_narrative"] = _narr
        last_row["narrative"] = _narr

        # Chart URL
        bi = result.bars_info or {}
        if bi.get("symbol"):
            _sid = state.get("winner_spec_id", "")
            last_row["chart_url"] = _chart_url(bi, _sid)
            last_row["chart_symbol"] = bi["symbol"]
            last_row["chart_category"] = bi.get("category", "linear")
            last_row["chart_interval"] = bi.get("interval", "60")

        if not is_continuous:
            # Mark done so the next poll returns the same result (no pop — HTMX
            # may fire one more request after receiving the result fragment)
            pass

        return templates.TemplateResponse(
            request,
            "fragments/agent_result.html",
            {
                "last": last_row,
                "phases": state["phases"],
                "rob_scan_log": state["rob_scan_log"],
                "winner_rob": state["winner_rob"],
                "winner_holdout": state.get("winner_holdout"),
                "market": get_market_info(),
                "run_id": run_id,
                "is_continuous": is_continuous,
                "token_info": token_info,
                "tl": tl,
                "steps_by_key": steps_by_key,
            },
        )

    if state["done"] and state["error"]:
        return templates.TemplateResponse(
            request,
            "fragments/agent_progress.html",
            {
                "run_id": run_id,
                "phases": _PHASES,
                "state": state,
                "done": True,
                "error": state["error"],
                "market": get_market_info(),
                "token_info": token_info,
                "tl": tl,
                "steps_by_key": steps_by_key,
            },
        )

    # Robustness bulunamadı ama done=True (winner_result=None, error=None)
    if state["done"]:
        return templates.TemplateResponse(
            request,
            "fragments/agent_progress.html",
            {
                "run_id": run_id,
                "phases": _PHASES,
                "state": state,
                "done": True,
                "error": "Robustness testini geçen strateji bulunamadı.",
                "rob_scan_log": state["rob_scan_log"],
                "market": get_market_info(),
                "token_info": token_info,
                "tl": tl,
                "steps_by_key": steps_by_key,
            },
        )

    return templates.TemplateResponse(
        request,
        "fragments/agent_progress.html",
        {
            "run_id": run_id,
            "phases": _PHASES,
            "state": state,
            "done": False,
            "error": None,
            "market": get_market_info(),
            "token_info": token_info,
            "tl": tl,
            "steps_by_key": steps_by_key,
        },
    )


def _winner_narrative(last_row: dict, state: dict) -> str:
    try:
        from agent import MODEL, _get_client

        m = last_row
        client = _get_client()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=200,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Otonom backtest ajanının bulduğu kazanan stratejiyi 2-3 cümleyle Türkçe özetle:\n"
                        f"Strateji: {state['winner_spec_name']}\n"
                        f"PnL: {m.get('pnl_fmt', '?')} · Sharpe: {m.get('sharpe_fmt', '?')} · "
                        f"Sortino: {m.get('sortino_fmt', '?')} · Max DD: {m.get('max_dd_fmt', '?')}\n"
                        f"Trades: {m.get('n_trades', 0)} · Win Rate: {m.get('win_rate_fmt', '?')}\n"
                        "Başında 'Bu strateji' ile başla."
                    ),
                }
            ],
        )
        return resp.content[0].text.strip() if resp.content else ""
    except Exception:
        pnl = last_row.get("pnl") or 0
        return (
            f"Bu strateji {state['winner_spec_name']} adıyla catalog'a kaydedildi. "
            f"{last_row.get('n_trades', 0)} trade ile {last_row.get('pnl_fmt', '?')} "
            f"{'kazandı' if pnl >= 0 else 'kaybetti'} ve robustness testini geçti."
        )
