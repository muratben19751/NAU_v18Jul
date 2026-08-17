"""Autonomous Backtest Agent — research loop.

Pipeline:
  0. Load data (full range from cache)
  1. Generate strategy (Claude → ComposedStrategySpec)
  2. N × (backtest → Claude refinement)
  3. Ranking (Sharpe + PnL + WinRate score)
  4. Robustness scan (in order: IS/OOS + WFO + MC)
  5. Save the winner (catalog + log)

Endpoints:
    GET  /agent               Page
    POST /agent/run           Start pipeline (returns immediately)
    GET  /agent/progress/{id} Status polling (HTMX every 1s)

Wiki References
---------------
See: [[auto_mission_control]] (AUTO kokpitinin ?view=mission sunumu),
[[auto_kapi_ve_geri_bildirim]] (kapı hangi seriyi okur — `_wfo_test`, neden
`test_metrics_naive`; modele giden geçmişte TF/DD/komisyon; oturum logu
indirgemesi `_compact_wfo_windows` + `_is_progress_noise`),
[[auto_arama_ekonomisi]] (aramanın işlem ekonomisi: `_clamp_spec_trade_size`
neden 1 hisseye sabitlemeyip `percent_equity`'ye geçiriyor, komisyon eşiği,
`_MIN_TRADES` kapısının NAU_ev paritesi ve `_score`'daki çifte ceza),
[[nau_performans_denetimi]] (`_thin_curves` — oturum loguna yazılan equity
eğrilerinin indirgenmesi; ham hâli olay başına 3,5 MB yazıyordu; ayrıca
2026-08-11: `_validate_external_data` artık `asyncio.to_thread` altında —
koroutin gövdesinde tam parquet okuduğu için POST /agent/run tüm sunucunun
HTMX poll'larını donduruyordu; tarih daraltması da yükleyiciye itildi),
[[webapp_module_map]], [[backtesting_guide]], [[strategy_and_actor]],
[[nau_deepr_ucuncu_tur_2026_08_09]]

`_robustness_passed`'in fail-closed `excess_return_fraction`/`max_dd_p95`
kuralları (a6ddb5a, 2026-08-07) ile `tests/test_regression_anchors_high.py`
fixture'ının senkronsuzluğu — DeepR'ın yakaladığı 2 test FAIL — 2026-08-08'de
düzeltildi; kapı mantığı değişmedi, yalnız test fixture'ı güncellendi. Bkz.
[[auto_kapi_ve_geri_bildirim]] ve [[nau_deepr_toplu_sertlestirme_2026_08]].

`_agent_worker` (DeepR #47, 2026-08-08) kademeli olarak modül-seviyesi
fonksiyonlara bölünüyor: `_market_for`/`_iv_for`/`_recipe`, `_WorkerState`
(round-persistent durum), `_cleanup_generated`/`_winless_bump`/
`_winless_stop`, `_make_llm_control`, `_rank_and_filter`,
`_propose_initial_strategy`, `_scan_one_candidate`, `_run_promotion_gate`
(sealed-holdout finansal bütünlük kapısı), `_run_backtest_iteration`,
Faz 0'ın veri/holdout yükleyicisi (`_load_timeframe_bars` + `_TfLoader` —
planın en riskli adımı, A11a/A11b), ve Faz 2'nin lookahead-generation
bloğu (`_propose_next_strategy`, Adım 10 — spec'in aynı loop değişkenine
yeniden atanması + 3 katmanlı exception hiyerarşisi; `degraded_terminal`
Faz-1'in aksine burada PROPAGATE OLMAZ, `(LLMCallCancelled,
AgentBudgetReached)` dışındaki her hata "önceki stratejiyle devam" olarak
yutulur — bilinçli, çünkü burada bir önceki spec'e düşülebilir, Faz-1'de
düşülemez) çıkarıldı — her biri kendi birim testiyle, davranış korunarak;
`TestContinuousCircuitBreaker` (tek Faz-0 regresyon kanıtı) hâlâ yeşil.
Yalnız alt-paket bölmesi (Adım 12) bilinçli olarak planın YÜRÜTÜLEN
kapsamı dışında — gelecekte ayrı bir iş. Bkz.
[[nau_deepr_ikinci_tur_2026_08_08]].

2026-08-09 DeepR turu dosyayı yine (5000+ satır) çok büyük diye işaretledi
([DÜŞÜK]) — A0-A11b sonrası hâlâ büyük çünkü ~40 modül-seviyesi yardımcı
BİLİNÇLİ OLARAK aynı dosyada tutuluyor. Adım 10 sonradan (aynı gün)
tamamlandı — dosya bilinçli olarak yine de büyük.

2026-08-11 [YÜKSEK] düzeltmesi: robustluk suite'i (`_run_full_robustness`) ve
saf yardımcıları (`peer_exclusions`, `wfo_test`, `valid_wfo_windows`, MC
eşikleri) `auto/robustness.py`'ye taşındı. Gerekçe bir satır sayısı değil,
bağımlılık YÖNÜ: `sandbox._robustness_child` ayrı bir süreçte suite'i çağırmak
için bu modülü import edip `_IPC_Q` private global'ini dışarıdan set ediyordu —
HTTP servis etmeyen bir worker tüm router ağacını yüklüyordu ve statik grafikte
`sandbox -> web.routes.agent_backtest -> sandbox` döngüsü vardı. Suite artık
altta duruyor, route ve sandbox onu çağırıyor; ilerleme bildirimi modül-global
kuyruk yerine açık bir `progress_fn` parametresi. Yukarıdaki eski
"`tests/test_lock_nesting.py` bölmeyi engelliyor" gerekçesi düşürüldü: o testin
`_TARGETS`'ı zaten çok dosyalı bir sözlük, yeni yola bir satır eklemek yeterli
(taşınan kod `_AGENT_LOCK`'a hiç dokunmadığı için bu turda gerekmedi).
Katman kuralını `tests/test_auto_layer_is_web_free.py` bekçilik ediyor.
`_agent_worker` hâlâ burada — bir sonraki doğal kesme çizgisi o.
"""

from __future__ import annotations

import gzip
import hashlib
import itertools
import json
import logging
import math
import os
import re
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import NamedTuple

import pandas as pd
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app_constants import DATA_DIR, benchmark_rejection, env_float

# Robustluk suite'i ve saf yardımcıları artık web'den bağımsız `auto` paketinde
# (DeepR 2026-08-11 [YÜKSEK]: sandbox child'ı sırf suite için tüm router ağacını
# import ediyordu). Alt tireli takma adlar KASITLI: bu modülün tarihsel import
# yüzeyi (testler `ab._peer_exclusions`, `ab._MC_DD_LIMIT` … diye erişiyor)
# taşıma yüzünden değişmesin — davranış birebir korunuyor.
from auto.log_thinning import (
    downsample_indices,
    is_point_series,
    spark_points,
    thin_curves,
    thin_pair,
)
from auto.robustness import MC_DD_LIMIT as _MC_DD_LIMIT
from auto.robustness import MC_DD_TAIL_LIMIT as _MC_DD_TAIL_LIMIT
from auto.robustness import bare_ticker as _bare_ticker  # noqa: F401
from auto.robustness import (  # noqa: F401
    multi_symbol_definitive_failure as _multi_symbol_definitive_failure,
)
from auto.robustness import peer_exclusions as _peer_exclusions  # noqa: F401
from auto.robustness import (  # noqa: F401
    split_definitive_failure as _split_definitive_failure,
)
from auto.robustness import valid_wfo_windows as _valid_wfo_windows
from auto.robustness import wfo_test as _wfo_test
from nau_provenance import process_rss_bytes
from web.templating import get_market_info, templates

router = APIRouter(prefix="/agent")

from web.shared import (
    MAX_LLM_TEXT_LEN,  # noqa: E402
    SESSION_LOG_DIR,  # noqa: E402
    ProgressStore,  # noqa: E402
    error_html,  # noqa: E402
)
from web.shared import chart_url as _chart_url  # noqa: E402
from web.shared import log_backtest as _log_backtest  # noqa: E402
from web.shared import log_robustness as _log_robustness  # noqa: E402

# ProgressStore holds dict + lock + capped eviction; aliases keep the many
# existing direct-access sites (worker ↔ pollers ↔ session log) unchanged.
_AGENT_STORE = ProgressStore(50)
_AGENT_PROGRESS = _AGENT_STORE.raw()
_AGENT_LOCK = _AGENT_STORE.lock


def newest_active_run_id() -> str | None:
    """The newest still-running AUTO run, or None (first done=False from the
    end of the insertion-ordered store).

    No leading underscore: web/routes/studio.py's /studio page calls this
    too (DeepR 2026-08-09 [YÜKSEK] — it used to import the raw _AGENT_LOCK/
    _AGENT_PROGRESS and scan them itself, a second, unmonitored acquirer of
    a lock with a documented deadlock incident, see tests/test_lock_nesting.py).
    This module's own page() below used to duplicate the identical scan
    inline; both now call this one function instead. Mirrors the
    lock-guarded-snapshot-then-handoff shape already used for the mission
    view (search this file for 'Same locked-snapshot pattern as /progress')
    — the lock is acquired and released here, the caller never touches it.
    """
    with _AGENT_LOCK:
        for rid, st in reversed(_AGENT_PROGRESS.items()):
            if not st.get("done"):
                return rid
    return None


def live_run_ids() -> set[str]:
    """Bu SÜREÇTE hâlâ koşan run_id'ler.

    `newest_active_run_id`'nin çoğul kardeşi; aynı gerekçeyle alt tiresiz ve
    aynı biçimde: kilit burada alınır ve bırakılır, çağıran ona hiç dokunmaz.
    `web/routes/sessions.py`'nin uzlaştırması kullanıyor — "logu yarım kalmış"
    ile "hâlâ yazıyor" ayrımını yapmanın tek doğru yolu bellekteki koşu
    kaydıdır; dosyanın son satırına bakmak canlı bir koşuyu ölü ilan ederdi.
    """
    with _AGENT_LOCK:
        return {rid for rid, st in _AGENT_PROGRESS.items() if not st.get("done")}


# AUTO can fan out into LLM, backtest, and robustness workers. Keep the default
# deliberately small so two browser clicks cannot oversubscribe the host or
# make token/cost attribution ambiguous. Operators may raise this explicitly
# only after sizing the worker pools for their machine.
try:
    _MAX_CONCURRENT_AUTO_RUNS = max(
        1, int(os.environ.get("AGENT_MAX_CONCURRENT_RUNS", "1"))
    )
except ValueError:
    _MAX_CONCURRENT_AUTO_RUNS = 1
_AUTO_RUN_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENT_AUTO_RUNS)
# Minimum number of trades to be considered statistically reliable. Runs below
# this are eliminated with -inf in _score (cannot be a winner). The value is
# aligned with the NAU_ev backtest optimizer's JUNK_MIN_TRADES=20 threshold (see
# NAU wiki backtest-optimizer.md: "trade < 20 → eliminated + not stored").
# L28: adjustable via AGENT_MIN_TRADES env var; default 20 → behavior unchanged.
#
# 2026-08-04 denetim notu (değer BİLEREK 20'de bırakıldı): az-işlem cezası
# `_score` içinde zaten sürekli bir çarpanla da uygulanıyor (`n/(n+20)`), yani
# eşik ikinci bir cezadır ve bilgiyi yok ederek uygular — ölçülen bir koşuda
# net pozitif üç adayın ikisi (17'şer işlem) bu kapıda elenmişti. Ancak o
# koşudaki asıl çarpıklık pozisyon boyutuydu (1 hisse + sabit komisyon →
# düşük frekansa yapay avantaj); `_clamp_spec_trade_size` düzeltilince bu
# baskı kalktı. Eşiği düşürmek NAU_ev paritesini bozacağı ve bu bir yöntem
# kararı olduğu için varsayılan korunuyor. Denemek için: AGENT_MIN_TRADES=10.
_MIN_TRADES = int(os.environ.get("AGENT_MIN_TRADES", "20"))

# M26+M31: instead of "first passer wins", collect at most the first 3
# candidates that pass Phase 4 robustness; the winner is chosen by the
# effective score weighted by the multi-symbol pass_rate factor. If the
# scan ends before 3 passers are found, decide with what's on hand.
_MAX_PASSERS = 3

# Session JSONL is an audit index, not the full chart store.  A 120-point
# forensic curve preserves shape/endpoints while bounding continuous AUTO disk
# growth across the top-level and nested MTM curves in every candidate event.
_SESSION_CURVE_CAP = 120

# Exception policy for a catalog history that was independently reviewed. This
# is deliberately a fingerprint, not a generic "ignore old gap" switch: an
# unknown calendar or any intraday gap remains a hard refusal.
_KNOWN_CONTINUOUS_TAIL_GAPS = {
    ("QQQ.NASDAQ", "2004-11-30", "2011-03-23", 2304): "2011-03-23",
}

# L32: Sealed holdout — the last N days of data are completely withheld from the
# iteration + robustness phases; tested exactly once only AFTER the winner is
# declared. The result is NOT BOUND to the decision, shown for information only.
OOS_HOLDOUT_DAYS = 60

# Sealed-holdout warmup lead-in (bars). Custom-block indicators need up to
# NAU_WINDOW=260 bars before their FIRST signal can exist; running the winner
# on the bare 60-day slice left coarser TFs below warmup (4h≈120, 1d≈42 bars)
# → every holdout reported 0 trades and silently validated nothing (2026-08-02
# vakası). The holdout run gets this many PRE-slice bars purely to warm
# indicators; METRICS count only entries INSIDE the sealed window.
HOLDOUT_WARMUP_BARS = 300

# Entries required inside the sealed window before the holdout counts as
# MEASURED. Per-trade Sharpe needs two observations for a standard deviation, so
# one trade cannot produce a dispersion figure at all — it produces a single
# outcome and the appearance of validation.
# A sealed validation sample with only a handful of entries cannot support a
# promotion decision. Keep the research result visible, but require the same
# minimum evidence as the main ranking gate before catalog publication.
HOLDOUT_MIN_TRADES = int(os.environ.get("AGENT_HOLDOUT_MIN_TRADES", "20"))
HOLDOUT_REQUIRE_POSITIVE_PNL = True
HOLDOUT_REQUIRE_POSITIVE_SHARPE = True
HOLDOUT_REQUIRE_POSITIVE_EXCESS = True

# Benchmark kapısının ÖLÇÜSÜ (kullanıcı kararı, 2026-08-15) ve kuralın kendisi
# artık app_constants'ta: aynı felsefeyi uygulaması gereken ikinci kapı
# (backtest_robustness.peer_is_superior) buradan İTHAL EDEMEZ — bu modül zaten
# robustness'ı içeri alıyor, ters yön döngü olurdu. Kural iki yere kopyalanınca
# ıraksadı (önce ölçüt, sonra geri düşme basamağı); tek kopya orada duruyor.
# Mod `AGENT_BENCHMARK_GATE` ile seçilir ve ÇAĞRI ANINDA okunur.

# Continuous AUTO is bounded by default. Operators can choose other positive
# values through the form/env; an explicit zero no longer creates an accidental
# unbounded bill/worker loop.
DEFAULT_CONTINUOUS_MAX_HOURS = float(
    os.environ.get("AGENT_DEFAULT_CONTINUOUS_MAX_HOURS", "4")
)
DEFAULT_CONTINUOUS_MAX_TOKENS = int(
    os.environ.get("AGENT_DEFAULT_CONTINUOUS_MAX_TOKENS", "250000")
)
# Form values may request a smaller budget, but may never create an unbounded
# billable/process-spawning AUTO run. Operators can tighten these ceilings via
# environment configuration; posting 0 does not disable them.
HARD_MAX_AUTO_HOURS = max(
    0.01,
    float(
        os.environ.get("AGENT_HARD_MAX_AUTO_HOURS", str(DEFAULT_CONTINUOUS_MAX_HOURS))
    ),
)
# ── İKİ TAVAN, İKİ BİRİM (2026-08-15) ─────────────────────────────────────
#
# Token sayısı, TEK sağlayıcı varken faturanın iyi bir vekiliydi ve yukarıdaki
# tavan o varsayımla kalibre edildi. Amaç-başına model eşlemesi bedava bir uç
# ekleyince bağ koptu: koşu 0057a0cd'de bütçenin %92'sini (194.375/210.411
# token) hiç para harcamayan YEREL model yedi, gerçek fatura 1,03 USD'ydi ve
# tur 28 dakikada tek round kapatamadan `outcome: budget` ile kesildi. Yerele
# geçmenin gerekçesi "token bedava → daha çok iterasyon"du; bedava model
# BEDAVA OLDUĞU İÇİN değil SAYILDIĞI İÇİN koşuyu kısalttı.
#
# Çözüm iki sayacı ayırmak — amaçları farklı olduğu için birimleri de farklı:
#   · PARA tavanı  (AGENT_DEFAULT_MAX_COST_USD): faturayı sınırlar.
#   · TOKEN tavanı (yukarıdaki): artık kaçak döngü/sonsuz koşu emniyeti.
#
# KÖRLÜK ŞARTI: para tavanı ancak maliyeti GÖREBİLDİĞİ kadar korur. Fiyatı
# bilinmeyen paralı bir uçta (`price_table` None döner, sağlayıcı da maliyet
# bildirmez) para tavanı hiç tetiklenmez. O durumda token tavanı eski
# para-vekili değerine iner — gevşetme yalnız parayı GÖREBİLDİĞİMİZDE geçerli.
DEFAULT_MAX_COST_USD = float(os.environ.get("AGENT_DEFAULT_MAX_COST_USD", "5"))
HARD_MAX_COST_USD = max(
    0.01, float(os.environ.get("AGENT_HARD_MAX_COST_USD", str(DEFAULT_MAX_COST_USD)))
)
# Kaçak-döngü emniyeti: para tavanı çalışırken token tavanı bu ölçeğe çıkar.
RUNAWAY_MAX_TOKENS = int(os.environ.get("AGENT_RUNAWAY_MAX_TOKENS", "2000000"))
# Maliyet görünmüyorken geri düşülen sıkı tavan (eski davranış).
BLIND_MAX_TOKENS = int(
    os.environ.get("AGENT_BLIND_MAX_TOKENS", str(DEFAULT_CONTINUOUS_MAX_TOKENS))
)


# Token tavanı artık faturanın vekili DEĞİL, kaçak döngü emniyeti — sert tavanı
# da o ölçekten türer. Operatör AGENT_HARD_MAX_AUTO_TOKENS'ı açıkça verirse
# elbette ona uyulur.
HARD_MAX_AUTO_TOKENS = max(
    1,
    int(os.environ.get("AGENT_HARD_MAX_AUTO_TOKENS", str(RUNAWAY_MAX_TOKENS))),
)


def _default_cost_cap() -> float:
    """Bu koşuya uygulanacak PARA tavanı (0 = kapalı)."""
    return min(DEFAULT_MAX_COST_USD, HARD_MAX_COST_USD)


def _continuous_token_cap() -> int:
    """Sürekli modun token tavanı.

    Para tavanı devredeyse token yalnız kaçak döngüye bakar (gevşek); para
    tavanı kapalıysa token yine faturanın tek vekilidir (eski sıkı değer).
    """
    return (
        RUNAWAY_MAX_TOKENS if _default_cost_cap() > 0 else DEFAULT_CONTINUOUS_MAX_TOKENS
    )


# M22 extra circuit breaker: threshold of consecutive winnerless rounds
# (continuous mode). Three complete rounds are enough to identify a stalled
# strategy family without spending the remainder of a four-hour/token budget.
# Counter resets when a winner appears. 0 = explicit operator opt-out.
_WINLESS_ROUND_LIMIT = int(os.environ.get("AGENT_WINLESS_ROUND_LIMIT", "3"))

# L26: lightweight best-effort SQLite index of winner + robustness moments.
_AGENT_INDEX_DB = DATA_DIR / "agent_index.db"

# ── Session Logger ────────────────────────────────────────────────────────────
# SESSION_LOG_DIR moved to web/shared.py (DeepR 2026-08-09 [YÜKSEK]) — the
# `from web.shared import SESSION_LOG_DIR` near the top of this file's other
# web.shared imports is the definition now; this module keeps its own bound
# name (tests patch `ab.SESSION_LOG_DIR` directly) via that import.
_SESSION_LOG_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOG_META: threading.Lock = threading.Lock()  # guards _SESSION_LOG_LOCKS


class AgentBudgetReached(RuntimeError):
    """Raised between LLM calls when the run's token ceiling is reached."""

    llm_control_abort = True


def _remaining_phase_timeout(
    started_at: float,
    max_hours: float,
    default_seconds: float,
    *,
    now: float | None = None,
) -> float:
    """Bound one isolated phase by the remaining run-wide wall-clock budget."""

    if max_hours <= 0:
        return float(default_seconds)
    elapsed = (time.monotonic() if now is None else now) - started_at
    remaining = max_hours * 3600 - elapsed
    if remaining <= 0:
        raise AgentBudgetReached(f"time ceiling ({max_hours:g} hours) reached")
    # Keep a tiny positive value so sandbox cleanup takes its normal timeout
    # path rather than receiving an invalid deadline.
    return max(0.1, min(float(default_seconds), remaining))


def _market_for(is_external: bool, instrument_id: str, iv: str) -> str | None:
    """Market context passed to the LLM — None on Bybit (existing prompt
    preserved byte-for-byte).

    Names the ONE bar the next spec will be scored on, not the whole
    multi-TF list: the loop assigns timeframes round-robin and the caller
    always knows which one comes next, so there is no reason to make the
    model guess among four.
    """
    if not is_external:
        return None
    return f"US equity {instrument_id} ({iv} bars, USD cash account)"


def _iv_for(i: int, run_number: int, intervals: list[str]) -> str:
    """Timeframe of iteration i under the round-robin in the worker loop.

    The round offsets the start. Without it the index is `i % len(intervals)`
    with `i` restarting at 0 every round, so any timeframe at position >=
    n_iterations is NEVER selected — not "tested less often", never at all.
    Measured on run 3467219a: 5 timeframes, n_iterations=4, and 1-DAY could
    not have been reached by any round of an unbounded continuous loop.
    Shifting by the round covers the whole selection over successive rounds
    while keeping one timeframe per iteration.
    """
    return intervals[(i + run_number - 1) % len(intervals)]


def _recipe(
    is_external: bool, instrument_id: str, symbol: str, category: str, iv: str
) -> dict:
    """String recipe from which the sandbox/robustness child rebuilds the instrument."""
    if is_external:
        return {
            "source": "external",
            "instrument_id": instrument_id,
            "granularity": iv,
        }
    return {"symbol": symbol, "interval": iv, "category": category}


@dataclass
class _WorkerState:
    """Round-persistent state for one `_agent_worker` invocation.

    Survives across `while True` rounds (continuous mode); round-LOCAL
    variables (tf_cache, spec, history, results, ranked/eligible, winner_*)
    are NOT here — they reset every round already and don't need a container.
    """

    last_err_str: str | None = None
    consec_err: int = 0
    worker_t0: float = field(default_factory=time.monotonic)
    winless_rounds: int = 0
    degraded_spec_ids: set[str] = field(default_factory=set)
    holdout_consumed: set[str] = field(default_factory=set)
    seen_candidate_fingerprints: set[str] = field(default_factory=set)
    zero_trade_families: set[str] = field(default_factory=set)
    last_started_round: int = 0
    completed_rounds: int = 0
    retained_block_names: set[str] = field(default_factory=set)
    # Transkript artifact'larının sıra numarası. `itertools.count` seçildi çünkü
    # gözlemci worker dışındaki bir iş parçacığından da çağrılabiliyor ve
    # `next()` CPython'da atomik — kilit kurmadan çakışmasız isim üretir.
    llm_seq: Iterator[int] = field(default_factory=lambda: itertools.count(1))


def _cleanup_generated(run_id: str, wstate: _WorkerState, keep_spec=None) -> None:
    """Delete this run's disposable generated blocks, retaining a promotion."""
    try:
        from custom_block_store import cleanup_agent_run

        wstate.retained_block_names.update(
            str(block.type) for block in (getattr(keep_spec, "blocks", None) or [])
        )
        cleanup_agent_run(run_id, keep_names=wstate.retained_block_names)
    except Exception:  # best effort; cleanup must never mask a terminal result
        logging.exception("AUTO generated-block cleanup failed for run %s", run_id)


def _winless_bump(wstate: _WorkerState) -> bool:
    """Increment the winnerless-round counter; returns True if the limit is exceeded.

    M22: previously it only incremented in the 'no eligible candidate' branch;
    the 'candidates exist but none passed robustness' branch skipped the
    counter, leaving an infinite-loop risk. Now both winnerless branches call
    this.
    """
    wstate.winless_rounds += 1
    return bool(_WINLESS_ROUND_LIMIT and wstate.winless_rounds >= _WINLESS_ROUND_LIMIT)


def _winless_stop(run_id: str, run_number: int, wstate: _WorkerState) -> None:
    _add_step(
        run_id,
        f"⏹ {_WINLESS_ROUND_LIMIT} consecutive winnerless rounds — "
        "circuit breaker stopping continuous mode.",
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
        started_round=wstate.last_started_round,
        completed_rounds=wstate.completed_rounds,
        total_rounds=wstate.completed_rounds,
    )


def _make_llm_control(
    run_id: str, max_hours: float, wstate: _WorkerState
) -> tuple[Callable[[], bool], Callable[[dict], None]]:
    """Build the (cancelled_fn, observer_fn) pair `set_thread_llm_control` needs.

    Token-cap check runs BEFORE the wall-clock check — order matters for the
    exact AgentBudgetReached message a caller sees when both ceilings are hit
    at once.
    """

    def _llm_cancelled() -> bool:
        with _AGENT_LOCK:
            state = _AGENT_PROGRESS.get(run_id)
            if state is None or state.get("stop_requested"):
                return True
            snapshot = dict(state)
        breach = _budget_breach(snapshot)
        if breach:
            raise AgentBudgetReached(breach)
        # The old wall-clock ceiling was checked only between rounds. A long
        # provider request could therefore begin just before the deadline and
        # continue spending well past it. The LLM wrapper polls this callback,
        # so reject cooperatively before the next provider step/retry.
        if max_hours > 0 and (time.monotonic() - wstate.worker_t0) >= max_hours * 3600:
            raise AgentBudgetReached(f"time ceiling ({max_hours:g} hours) reached")
        return False

    def _llm_observer(event: dict) -> None:
        usage = event.get("usage")
        if usage:
            _add_tokens(run_id, usage, str(event.get("model") or ""))
        _session_log(run_id, "llm_usage", **_offload_transcript(run_id, wstate, event))

    return _llm_cancelled, _llm_observer


def _effective_run_config() -> dict:
    """The constants that decided this run's outcomes, as they actually were.

    Not decoration: every one of these is env-overridable, so two runs with
    identical logs can have applied different gates. Without the snapshot,
    "why did this candidate pass" is answerable only by reading today's code —
    which is not the code that ran.
    """
    from app_constants import (
        BENCHMARK_DIVIDEND_YIELD_ANNUAL,
        BENCHMARK_ROUND_TRIP_COST_FRACTION,
        STARTING_CASH,
    )

    return {
        "min_trades": _MIN_TRADES,
        "max_passers": _MAX_PASSERS,
        "winless_round_limit": _WINLESS_ROUND_LIMIT,
        "equity_pct": AGENT_EQUITY_PCT,
        "starting_cash": STARTING_CASH,
        "holdout_days": OOS_HOLDOUT_DAYS,
        "holdout_warmup_bars": HOLDOUT_WARMUP_BARS,
        "holdout_min_trades": HOLDOUT_MIN_TRADES,
        "holdout_requires": {
            "positive_pnl": HOLDOUT_REQUIRE_POSITIVE_PNL,
            "positive_sharpe": HOLDOUT_REQUIRE_POSITIVE_SHARPE,
            "positive_excess": HOLDOUT_REQUIRE_POSITIVE_EXCESS,
        },
        "default_continuous_max_hours": DEFAULT_CONTINUOUS_MAX_HOURS,
        "default_continuous_max_tokens": DEFAULT_CONTINUOUS_MAX_TOKENS,
        "default_max_cost_usd": DEFAULT_MAX_COST_USD,
        "hard_max_cost_usd": HARD_MAX_COST_USD,
        "runaway_max_tokens": RUNAWAY_MAX_TOKENS,
        "blind_max_tokens": BLIND_MAX_TOKENS,
        "hard_max_hours": HARD_MAX_AUTO_HOURS,
        "hard_max_tokens": HARD_MAX_AUTO_TOKENS,
        # Bütçe sayacının BİRİMİ: ham token değil girdi-eşdeğeri. Ağırlıklar
        # değişince aynı tavan başka bir koşu uzunluğu demeye başlar.
        "token_usage_weights": dict(_TOKEN_USAGE_WEIGHTS),
        "benchmark_round_trip_cost": BENCHMARK_ROUND_TRIP_COST_FRACTION,
        "benchmark_dividend_yield": BENCHMARK_DIVIDEND_YIELD_ANNUAL,
        "heartbeat_seconds": _HEARTBEAT_SECONDS,
    }


# ── Canlı nabız ─────────────────────────────────────────────────────────────
# Olay kaydı bir koşuyu ancak olay ÜRETTİĞİ sürece anlatır. Asıl sessiz arıza
# tam tersi: hiçbir şey yazılmaması. Takılan bir LLM çağrısı, şişen bir bellek,
# bütçesini dakikada yakan bir tur — hepsi loga "hiçbir şey" olarak düşer ve
# sonradan bakan kişi arada ne olduğunu ölçemez, tahmin eder.
#
# Nabız bu boşluğu doldurur: sabit aralıkla, olay olsun olmasın, koşunun o anki
# TAM hâlini yazar. Süreç ölse bile diskte kalan son nabız "nerede duruyordu"
# sorusunu en fazla bir aralık hatasıyla cevaplar.
_HEARTBEAT_SECONDS = env_float("NAU_AUTO_HEARTBEAT_SEC", 30.0)


def _age(stamp, now: float) -> float:
    """`now - stamp` saniye; damga yoksa 0.

    `float(stamp or now)` YAZILMAZ: `0.0` yanlış-değerdir ve o kısayol sıfır
    damgalı bir işi "az önce başladı" diye raporlar — yani takılmayı en çok
    aradığın yerde gizler.
    """
    if stamp is None:
        return 0.0
    try:
        return round(now - float(stamp), 1)
    except (TypeError, ValueError):
        return 0.0


def _heartbeat_snapshot(state: dict, wstate: _WorkerState) -> dict:
    """Koşunun o anki tam hâli. ÇAĞIRAN `_AGENT_LOCK`'u tutuyor olmalı."""
    now = datetime.now(UTC).timestamp()
    open_spans = [
        {
            "key": sp.get("key"),
            "lane": sp.get("lane"),
            "label": str(sp.get("label") or "")[:60],
            "age_s": _age(sp.get("t0"), now),
        }
        for sp in (state.get("timeline") or [])
        if sp.get("t1") is None
    ]
    steps = state.get("steps") or []
    last = steps[-1] if steps else None
    cap = int(state.get("max_total_tokens") or 0)
    used = _token_usage(state)
    return {
        "elapsed_s": round(time.monotonic() - wstate.worker_t0, 1),
        "round_started": wstate.last_started_round,
        "rounds_completed": wstate.completed_rounds,
        "phase": next(
            (
                f"{p.get('label') or i}: {p.get('detail') or ''}".strip(": ")
                for i, p in enumerate(state.get("phases") or [])
                if p.get("status") == "running"
            ),
            "",
        ),
        "tokens_in": int(state.get("tokens_in") or 0),
        "tokens_out": int(state.get("tokens_out") or 0),
        "tokens_cache_read": int(state.get("tokens_cache_read") or 0),
        "tokens_cache_write": int(state.get("tokens_cache_write") or 0),
        "budget_used": used,
        "budget_cap": cap,
        "budget_pct": round(100 * used / cap, 1) if cap else None,
        "provider_cost_usd": round(float(state.get("provider_cost_usd") or 0.0), 6),
        "open_spans": open_spans,
        # En uzun süredir açık olan iş = koşunun NEREDE beklediği. Tek başına
        # "takıldı mı" sorusunu cevaplayan alan bu.
        "stalled_s": max((s["age_s"] for s in open_spans), default=0.0),
        "last_step": str((last or {}).get("msg") or "")[:120],
        "last_step_age_s": _age((last or {}).get("ts"), now),
        "steps_buffered": len(steps),
        "rss_bytes": process_rss_bytes(),
        "threads": threading.active_count(),
        "audit_degraded": bool(state.get("audit_degraded")),
    }


# run_id → son başarılı `_session_log` yazımının monotonic zamanı.
_LAST_LOG_AT: dict[str, float] = {}

# Kaç saniye sessizlikten sonra thread dökümü alınır. Nabız 30 s'de bir yazar,
# yani 300 s sessizlik nabzın da durduğu anlamına gelir.
_STALL_DUMP_SEC = env_float("NAU_AUTO_STALL_DUMP_SEC", 300.0)


def _start_stall_watchdog(run_id: str) -> threading.Thread:
    """Koşu susarsa TÜM thread'lerin yığın izini dosyaya dök.

    Neden var: 2026-08-15/16'da üç AUTO koşusu sonuçsuz durdu — süreç ayakta
    kaldı (pm2 restart sayacı değişmedi), `session_end` hiç yazılmadı, nabız
    kesildi. Nerede takıldıklarını gösteren TEK bir kayıt yoktu, dolayısıyla
    teşhis hipoteze kalıyordu: `_run_openrouter_killable`'ın `proc.start()`'ı mı,
    nabız thread'inin sessiz ölümü mü, başka bir şey mi. Üç ayrı tahmin, sıfır
    kanıt.

    `faulthandler.dump_traceback` TÜM thread'leri döker (worker, nabız, uvicorn)
    — takılan thread'in tam satırı ilk dökümde görünür. Döküm koşuyu
    ETKİLEMEZ: yalnız okur, hiçbir şeyi öldürmez; asılma sürüyorsa bir sonraki
    dökümle karşılaştırıp "aynı yerde mi duruyor" sorusu da yanıtlanır.

    Döküm dosyası oturum logunun yanına: `<run_id>.stall.txt`.
    """
    started = time.monotonic()
    _LAST_LOG_AT.setdefault(run_id, started)
    dump_path = SESSION_LOG_DIR / f"{run_id}.stall.txt"

    def _watch() -> None:
        import faulthandler

        dumps = 0
        while dumps < 3:
            time.sleep(min(60.0, max(5.0, _STALL_DUMP_SEC / 5.0)))
            with _AGENT_LOCK:
                state = _AGENT_PROGRESS.get(run_id)
            if state is None or state.get("continuous_finished") or state.get("done"):
                return
            idle = time.monotonic() - _LAST_LOG_AT.get(run_id, started)
            if idle < _STALL_DUMP_SEC:
                continue
            dumps += 1
            try:
                SESSION_LOG_DIR.mkdir(parents=True, exist_ok=True)
                with dump_path.open("a", encoding="utf-8") as fh:
                    header = (
                        f"===== STALL DUMP #{dumps} run={run_id} "
                        f"idle={idle:.0f}s at {datetime.now(UTC).isoformat()} ====="
                    )
                    fh.write(os.linesep + header + os.linesep)
                    fh.flush()
                    faulthandler.dump_traceback(file=fh, all_threads=True)
                logging.warning(
                    "AUTO %s: %.0f sn'dir hiçbir oturum logu yazılmadı — thread "
                    "dökümü #%d alındı: %s",
                    run_id,
                    idle,
                    dumps,
                    dump_path,
                )
            except Exception:
                logging.warning("AUTO %s: stall dump alınamadı", run_id, exc_info=True)
                return
            # Sonraki dökümü beklemek için damgayı ilerlet: aksi hâlde üç döküm
            # arka arkaya alınır ve "aynı yerde mi kaldı" sorusu yanıtsız kalır.
            _LAST_LOG_AT[run_id] = time.monotonic()

    thread = threading.Thread(target=_watch, name=f"auto-stall-{run_id}", daemon=True)
    thread.start()
    return thread


def _start_heartbeat(
    run_id: str,
    wstate: _WorkerState,
    *,
    continuous_mode: bool,
    max_hours: float,
    interval: float | None = None,
) -> threading.Thread:
    """Sabit aralıklı nabzı başlat; iş parçacığı koşu bitince KENDİ durur.

    Worker'ın akışına dokunmadan durması bilinçli: `_agent_worker` bir düzine
    yerden çıkıyor ve her birine bir `stop()` eklemek, biri unutulduğunda
    sonsuza kadar yazan bir daemon bırakırdı. Nabız bunun yerine ilerleme
    durumunu okur — koşu bittiğinde son bir nabız yazıp döner. Ayrıca sert bir
    tavan var: durum hiç güncellenmese bile bütçe süresini aşınca durur.
    """
    period = float(interval if interval is not None else _HEARTBEAT_SECONDS)
    deadline = (max_hours if max_hours > 0 else 24.0) * 3600 + 600

    def _beat() -> None:
        while True:
            time.sleep(period)
            with _AGENT_LOCK:
                state = _AGENT_PROGRESS.get(run_id)
                snapshot = _heartbeat_snapshot(state, wstate) if state else None
            finished = state is None or bool(state.get("continuous_finished"))
            if not continuous_mode and state is not None and state.get("done"):
                finished = True
            if snapshot is not None:
                if finished:
                    snapshot["final"] = True
                _session_log(run_id, "run_heartbeat", **snapshot)
                if snapshot["elapsed_s"] > deadline:
                    return
            if finished:
                return

    thread = threading.Thread(
        target=_beat, name=f"auto-heartbeat-{run_id}", daemon=True
    )
    thread.start()
    return thread


def _offload_transcript(run_id: str, wstate: _WorkerState, event: dict) -> dict:
    """Move prompt/completion text out of the JSONL line and into an artifact.

    Inline they would dominate the session log: a single AUTO round makes tens
    of calls whose prompts run to thousands of characters each, and every
    reader of the log — the cockpit, the Sessions page, the postmortem — parses
    every line. The line keeps the *identity* (path, sha256, sizes); the text
    lands in ``<run_id>_artifacts/`` where only a reviewer who asks pays for it.
    """
    if "prompt" not in event and "completion" not in event:
        return event
    event = dict(event)
    payload = {
        "prompt": event.pop("prompt", ""),
        "completion": event.pop("completion", ""),
        "purpose": event.get("purpose"),
        "model": event.get("model"),
        "status": event.get("status"),
        "error": event.get("error"),
    }
    prompt_chars = event.pop("prompt_chars", None)
    completion_chars = event.pop("completion_chars", None)
    try:
        seq = next(wstate.llm_seq)
        purpose = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(payload["purpose"] or "llm"))
        identity = _write_session_artifact(run_id, f"llm_{seq:04d}_{purpose}", payload)
    except Exception:
        # The transcript is evidence about the run, never a part of it.
        logging.getLogger(__name__).warning(
            "AUTO transcript artifact failed for %s", run_id, exc_info=True
        )
        return event
    event["transcript"] = {
        **identity,
        "prompt_chars": prompt_chars,
        "completion_chars": completion_chars,
        # Kırpıldıysa bunu SÖYLE: eksik bir prompt'u tam sanmak, review'da
        # olmayan bir yönergeyi yok saymak demektir.
        "clipped": bool(
            (prompt_chars or 0) > len(payload["prompt"])
            or (completion_chars or 0) > len(payload["completion"])
        ),
    }
    return event


def _json_safe(obj):
    """Reduces NaN/Inf floats to None (recursive) — prevents json.dumps from
    producing non-standard ``NaN``/``Infinity`` literals (part of L33).
    Small helper specific to this file; independent of the one in reports.
    """
    if isinstance(obj, float):  # np.float64 is also a float subclass
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _session_log(run_id: str, event: str, **kwargs) -> None:
    """Append a JSON event line to agent_sessions/{run_id}.jsonl.
    Thread-safe per run_id. Audit logging must never stop research, but a
    write failure is surfaced in the live state instead of being invisible.
    """
    try:
        SESSION_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _SESSION_LOG_META:
            if run_id not in _SESSION_LOG_LOCKS:
                _SESSION_LOG_LOCKS[run_id] = threading.Lock()
            lock = _SESSION_LOG_LOCKS[run_id]
        if event == "session_end":
            # Bozulma sayacı KALICI kayda hiç geçmiyordu: kokpit kapandığı an
            # buharlaşıyor ve ertesi gün Sessions'a bakan kişi rastgele
            # kompozisyonları normal aday sanıyordu. Tek yer burası — 12 ayrı
            # session_end çağrısına elle eklemek biri unutulunca sessizce
            # eksik kalırdı.
            #
            # Kilitsiz okuma bilinçli: `_session_log` bazı çağrı yerlerinde
            # `_AGENT_LOCK` altındayken çağrılıyor ve o kilit reentrant değil —
            # burada yeniden almak kilitlenme riski olurdu. Sayaç int (okuması
            # atomik), liste kopyasında en kötü ihtimalle bir eleman yarışır;
            # denetim kaydı için kabul edilebilir, kilitlenme değil.
            _dstate = _AGENT_PROGRESS.get(run_id)
            if _dstate is not None:
                kwargs.setdefault(
                    "fallback_count", int(_dstate.get("fallback_count", 0) or 0)
                )
                kwargs.setdefault(
                    "fallback_reasons", list(_dstate.get("fallback_reasons") or [])
                )
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "run_id": run_id,
            **kwargs,
        }
        line = json.dumps(_json_safe(record), default=str) + "\n"
        with lock:
            with (SESSION_LOG_DIR / f"{run_id}.jsonl").open(
                "a", encoding="utf-8"
            ) as _f:
                _f.write(line)
                # ``session_end`` is the durable completion boundary used by
                # the Sessions page after a PM2 restart.
                if event == "session_end":
                    _f.flush()
                    os.fsync(_f.fileno())
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "AUTO audit log write failed for %s (%s)", run_id, event, exc_info=True
        )
        # Do not call _add_step here: it writes another audit event and would
        # recurse on a full/unavailable volume.  The progress endpoint copies
        # these scalar fields and renders a non-terminal warning instead.
        with _AGENT_LOCK:
            state = _AGENT_PROGRESS.get(run_id)
            if state is not None:
                state["audit_degraded"] = True
                state["audit_error"] = f"Session audit log unavailable: {exc}"
    # Takılma teşhisi: son BAŞARILI oturum-logu yazımının zamanı. Nabız 30 s'de
    # bir yazdığı için bu damganın uzun süre eskimesi "koşuda hiçbir thread iş
    # yapmıyor" demektir (bkz. _start_stall_watchdog).
    _LAST_LOG_AT[run_id] = time.monotonic()
    # Outside the try on purpose: a failed *review* is not a failed *audit log*,
    # and marking the run's audit degraded for it would send the reader looking
    # for a missing event that is right there on disk.
    if event == "session_end":
        _write_run_review(run_id)


def _write_run_review(run_id: str) -> None:
    """Leave a readable review of the finished run next to its raw log.

    Generated here rather than on demand because the question a run has to
    answer ("was this any good, and what did it cost?") is asked days later,
    when nobody remembers the run id — and a document that must be requested
    is a document nobody reads. Deterministic and LLM-free, so it costs the
    run nothing but a few hundred milliseconds after its last event.
    """
    try:
        from auto_review import write_review

        write_review(run_id, sessions_dir=SESSION_LOG_DIR)
    except Exception:
        logging.getLogger(__name__).warning(
            "AUTO review generation failed for %s", run_id, exc_info=True
        )


def _write_session_artifact(run_id: str, name: str, payload) -> dict:
    """Write a curve-heavy audit payload outside JSONL and return its identity."""
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-.") or "artifact"
    directory = SESSION_LOG_DIR / f"{run_id}_artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(_json_safe(payload), default=str, separators=(",", ":")).encode(
        "utf-8"
    )
    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
    path = directory / f"{safe_name}.json.gz"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(compressed)
    tmp.replace(path)
    return {
        "path": str(path.relative_to(SESSION_LOG_DIR)),
        "sha256": hashlib.sha256(compressed).hexdigest(),
        "bytes": len(compressed),
        "raw_bytes": len(raw),
        "encoding": "gzip+json",
    }


def _robustness_log_summary(rob: dict) -> dict:
    """Scalar decision evidence retained inline; full payload is an artifact."""
    split = rob.get("split") or {}
    mc = rob.get("mc") or {}
    ms = rob.get("multi_symbol") or {}
    return {
        "short_circuit": rob.get("short_circuit"),
        "split": {
            "overfitting_label": split.get("overfitting_label"),
            "overfitting_score": split.get("overfitting_score"),
            "in_sample_metrics": {
                k: (split.get("in_sample_metrics") or {}).get(k)
                for k in _WFO_KEEP_METRICS
            },
            "oos_metrics": {
                k: (split.get("oos_metrics") or {}).get(k) for k in _WFO_KEEP_METRICS
            },
        },
        "wfo": {"windows": len(rob.get("wfo_windows") or [])},
        "mc": {
            k: v
            for k, v in mc.items()
            if k not in {"curves_sample", "percentile_curves", "equity_curves"}
            and not isinstance(v, (list, dict))
        },
        "multi_symbol": {
            k: v
            for k, v in ms.items()
            if k != "results" and not isinstance(v, (list, dict))
        },
    }


def _cleanup_all_agent_blocks() -> None:
    """Legacy hook kept for compatibility; agent blocks are now persistent."""
    return None


_PHASES = [
    "Loading data",
    "Generating strategy",
    "Backtest loop",
    "Ranking",
    "Robustness scan",
    "Completed",
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


def _done_phase(
    run_id: str, idx: int, detail: str = "", degraded: bool = False
) -> None:
    """Fazı kapat. ``degraded=True`` ise ✓ değil ⚠ ile.

    ``status`` yine "done" kalır — ilerleme hesabı (mission_view'daki
    ``all(s == "done")``) faz akışını sayar, kalitesini değil. Bozulma ayrı bir
    bayrakla taşınır ki bir fallback koşusu başarı gibi görünmesin ama koşu da
    yarıda kesilmiş sayılmasın.
    """
    t = _ts()
    label = _PHASES[idx] if 0 <= idx < len(_PHASES) else str(idx)
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if not s:
            return
        p = s["phases"][idx]
        p["status"] = "done"
        p["degraded"] = bool(degraded)
        p["detail"] = detail
        p["ts"] = t
    _session_log(
        run_id,
        "phase_change",
        phase_idx=idx,
        phase_label=label,
        status="degraded" if degraded else "done",
        detail=detail,
    )


def _mark_degraded(run_id: str, reason: str, what: str) -> str:
    """Bir üretim adımı fallback'e düştü: say, kaydet, ekrana taşı.

    Sayılmayan bozulma koşuyu başarı gibi gösterir — üretilen şey modelin önerisi
    değil, rastgele bir kompozisyon ya da kaynak kodda sabit duran bir fikirdir.
    Sayaç koşu durumunda durur; kokpit ve faz satırı onu okur. ``reason`` çağrının
    düştüğü istisnanın adıdır (ör. ``TruncatedResponse``).
    """
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s is not None:
            s["fallback_count"] = int(s.get("fallback_count", 0) or 0) + 1
            reasons = s.setdefault("fallback_reasons", [])
            if reason not in reasons:
                reasons.append(reason)
    # `reason` yalnız istisnanın ADI. Aynı ad ("GeneratedCodeError") bir turda
    # altı kez düşebiliyor ve altısı farklı satırdan gelebiliyor; hangisi
    # olduğu ancak yığın izinden görülür. Aktif bir istisna yoksa
    # `format_exc()` "NoneType: None" döner — o durumda alan hiç yazılmaz.
    stack = traceback.format_exc()
    extra = {"traceback": stack[-4000:]} if stack.startswith("Traceback") else {}
    _session_log(run_id, "degraded", what=what, reason=reason, **extra)
    _add_step(run_id, f"  ⚠ degraded · {what} · fallback ({reason})")
    return reason


_PROGRESS_NOISE = re.compile(
    r"(\b\d+\s*/\s*\d+\s+completed\s*$)|(^\s*window\s+\d+\s+selected:)"
)


def _is_progress_noise(msg: str) -> bool:
    """Is this a live-only progress tick, not a fact worth archiving?

    The robustness suite emits a counter line every 10 units: a single candidate
    produces ~900 of them ("WFO train g4/5: 550/768 completed", "WFO test:
    120/126 completed") plus one "window N selected: {params}" per window. In the
    audited session that was 33,414 step events / 5.1 MB — and the per-window
    parameters are already stored structurally in ``wfo_windows[i].params``, so
    the log kept a second, worse copy.

    They still stream to the live console (the run should look alive); they are
    simply not written to the session log.
    """
    return bool(_PROGRESS_NOISE.search(msg or ""))


def _add_step(run_id: str, msg: str, *, kind: str | None = None) -> None:
    """Bir ilerleme satırı kaydet — metinle BİRLİKTE yapısal ``kind`` alanı.

    DeepR 2026-08-11 [DÜŞÜK] (mimari): AUTO kokpiti (``web/mission.py``) olay
    tipini RENDER anında bu mesajların METNİNDEN çıkarıyordu — "robustness",
    "bar", "block", "claude" gibi anahtar kelimelerle. Yani sunum katmanı, alan
    katmanının serbest metnini geri-mühendislik ediyordu: buradaki bir satırın
    sözcüğünü değiştirmek (ya da Türkçeleştirmek) kokpitin renklendirmesini
    SESSİZCE bozardı ve hiçbir tip kontrolü bunu yakalamazdı.

    Sınıf artık ÜRETİLDİĞİ yerde belirlenir ve hem canlı duruma hem oturum
    loguna yapısal bir alan olarak yazılır. Doğru sınıfı zaten bilen bir çağrı
    yeri ``kind=`` ile açıkça söyler (tahmine hiç girilmez); söylemezse metin
    sınıflandırıcısı BURADA, mesajın yanı başında çalışır — metin ile sınıf
    aynı katmanda durur ve ıraksaması test edilebilir.

    Bu fonksiyon YALNIZ parent (web) sürecinde koşar. Eskiden bir
    `_IPC_Q is not None` dalı vardı: sandbox child'ı bu modülü import edip
    global'i set ediyordu. Child artık `auto.robustness.run_full_robustness`'i
    kendi `progress_fn` kapanışıyla çağırıyor, bu modülü hiç görmüyor.
    """
    from web.mission import STEP_KINDS, classify_step_kind

    if kind not in STEP_KINDS:
        kind = classify_step_kind(msg)
    t = _ts()
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s is not None:
            s["steps"].append({"ts": t, "msg": msg, "kind": kind})
            # Cap to prevent unbounded memory growth in continuous mode.
            if len(s["steps"]) > 500:
                s["steps"] = s["steps"][-500:]
    if not _is_progress_noise(msg):
        _session_log(run_id, "step", ts=t, msg=msg, kind=kind)


# ── Timeline (Gantt) span track ───────────────────────────────────────────────
# Every meaningful operation (data loading, LLM call, backtest #i, robustness
# candidate and its sub-phases) is recorded as a "span"; the SVG timeline on the
# /agent screen and the /sessions replay are fed from this track. Epoch floats
# are carried inside the payload — never via the `ts=` kwarg (which would
# overwrite _session_log's ISO ts).

_TL_MAX_SPANS = 400  # continuous-mode memory ceiling (same spirit as 500-step cap)
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
    """Open a span in the timeline.

    Child sub-phases are derived on the parent side from step markers (see
    `_make_rob_progress`); the timeline itself only ever lives in this process.
    """
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
                # Drop the oldest CLOSED spans; open (running) ones are kept.
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
    """Close the most recent open span with `key`. Unknown/closed key → no-op."""
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
    """Close all spans left open (error / stop / end-of-round cleanup)."""
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


# Robustness sub-phase markers — the FIXED leading glyphs of the _add_step
# messages in _run_full_robustness. Do not change the glyphs: _make_rob_progress
# parses them to open/close sub-spans.
_TL_ROB_MARKERS = {
    "🌐": ("ms", "Multi-Symbol"),
    "📊": ("isoos", "IS/OOS"),
    "📈": ("wfo", "Walk-Forward"),
    "🎲": ("mc", "Monte Carlo"),
}


def _make_rob_progress(run_id: str, cand_idx: int, round_num: int):
    """Step + sub-span bridge for the robustness child's progress relay.

    The returned function forwards each message to _add_step; on the 4 fixed
    phase glyphs (🌐 📊 📈 🎲) it opens a sub-span (closing the previous one with
    ok); a result line starting with "  →" closes the active sub with ok, and
    "  ⚠ Monte Carlo skipped" closes mc with warn.
    """
    state = {"open": None}  # active sub-span key

    def _close(status: str = "ok") -> None:
        if state["open"]:
            _tl_end(run_id, state["open"], status=status)
            state["open"] = None

    def progress(msg: str) -> None:
        # Bu köprüden geçen HER satır robustness çocuğundan gelir — sınıfı
        # metinden tahmin etmeye gerek yok, yapısal olarak biliniyor.
        _add_step(run_id, msg, kind="rob")
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
        elif msg.startswith("  ⚠ Monte Carlo skipped"):
            _close("warn")

    progress.close_open = _close  # called at candidate close
    return progress


def _add_tokens(run_id: str, usage: dict | None, model: str = "") -> None:
    """Accumulate token counters from the Claude API usage dict.

    ``model`` — bu çağrının GERÇEKTEN gittiği model. Amaç-başına eşleme
    (`NAUTILUS_MODEL_BY_PURPOSE`) bir koşuda birden fazla modelin para
    harcamasına izin verdiği için toplamlar tek başına "kim harcadı"yı
    söyleyemez; kırılım `state["by_model"]`'de tutulur ve maliyet oradan
    hesaplanır (bkz. ``_run_cost``). Boş bırakılırsa "?" kovasına yazılır —
    uydurma bir model adına yazmaktansa bilinmezliği göstermek yeğdir.
    """
    if not usage:
        return
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s is not None:
            _slot = s.setdefault("by_model", {}).setdefault(
                (model or "").strip() or "?",
                {
                    "calls": 0,
                    "input": 0,
                    "output": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "provider_cost_usd": 0.0,
                },
            )
            _slot["calls"] += 1
            _slot["input"] += usage.get("input_tokens") or 0
            _slot["output"] += usage.get("output_tokens") or 0
            _slot["cache_read"] += usage.get("cache_read_input_tokens") or 0
            _slot["cache_write"] += usage.get("cache_creation_input_tokens") or 0
            if usage.get("cost_usd") is not None:
                _slot["provider_cost_usd"] += float(usage["cost_usd"])
            s["tokens_in"] = s.get("tokens_in", 0) + (usage.get("input_tokens") or 0)
            s["tokens_out"] = s.get("tokens_out", 0) + (usage.get("output_tokens") or 0)
            s["tokens_cache_read"] = s.get("tokens_cache_read", 0) + (
                usage.get("cache_read_input_tokens") or 0
            )
            s["tokens_cache_write"] = s.get("tokens_cache_write", 0) + (
                usage.get("cache_creation_input_tokens") or 0
            )
            provider_cost = usage.get("cost_usd")
            if provider_cost is not None:
                s["provider_cost_usd"] = s.get("provider_cost_usd", 0.0) + float(
                    provider_cost
                )


# Bütçe, faturayı sınırlamak için var (bkz. DEFAULT_CONTINUOUS_MAX_TOKENS
# yorumu: "unbounded bill"). Dolayısıyla sayacın birimi ham token değil,
# GİRDİ-EŞDEĞERİ token: her kalem kendi fiyat oranıyla toplanır.
#
# DeepR 2026-08-14 [YÜKSEK]: dördü de 1:1 toplanıyordu. Cache okuması girdinin
# ~%10'u kadar fiyatlanır (CLI aboneliğinde hiç faturalanmaz), ama tavana tam
# fiyattan yazılıyordu. Canlı ölçüm (koşu 65a87244): 235.222/250.000'in %77'si
# cache kalemiydi ve sağlayıcının bildirdiği gerçek maliyet 2,30 USD'ydi; 4
# saatlik bir koşu 15. dakikada, hedeflenen paranın ~1/9'unda kesildi. Sonuç
# yalnız erken bitiş değildi: prompt-cache'i İYİLEŞTİRMEK bile koşuyu
# uzatmıyordu, çünkü kazanılan her cache-hit tavana ceza olarak yazılıyordu.
#
# Düzeltme BİLEREK dar tutuldu: yalnız cache okuması indiriliyor, diğer üç
# kalem 1,0'da bırakılıyor. Tam fiyat-oranlı bir tavan (çıktı 5x, cache yazımı
# 1,25x) daha "doğru" bir birim verirdi ama sayacı SIKARDI: aynı canlı koşu
# 235.222 yerine 388.565 sayardı ve 250.000'lik varsayılan altında daha da
# erken kesilirdi. Varsayılan tavan (DEFAULT_CONTINUOUS_MAX_TOKENS) bu eski
# ölçeğe göre kalibre edilmiş; birimi değiştirmek onu da yeniden kalibre
# etmeyi, yani "bir AUTO koşusuna ne kadar para" sorusuna karar vermeyi
# gerektirir — o ayrı bir karar. Buradaki değişiklik yalnızca GEVŞETİR:
# hiçbir koşu bu yüzden erken bitmez.
_TOKEN_USAGE_WEIGHTS = {
    "tokens_in": 1.0,
    "tokens_out": 1.0,
    "tokens_cache_write": 1.0,
    "tokens_cache_read": 0.1,
}

_TOKEN_USAGE_FIELDS = tuple(_TOKEN_USAGE_WEIGHTS)


def _token_usage(state: dict) -> int:
    """Bütçeye sayılan GİRDİ-EŞDEĞERİ token (ham toplam değil).

    Ham kalemleri okumak isteyen (rozet, snapshot, defter) `state`'teki
    `tokens_*` alanlarını doğrudan okumalı; burası yalnız tavan kıyaslaması
    içindir.
    """
    return int(sum(int(state.get(k) or 0) * w for k, w in _TOKEN_USAGE_WEIGHTS.items()))


def _budget_breach(state: dict) -> str | None:
    """Aşılan tavanın sebebi — hiçbiri aşılmadıysa None.

    İki tavan, iki birim (bkz. DEFAULT_MAX_COST_USD yorumu):
      · PARA  — faturayı sınırlar, `_run_cost` ile ölçülür.
      · TOKEN — kaçak döngü emniyeti; para GÖRÜLEBİLİYORSA gevşek
        (RUNAWAY_MAX_TOKENS), görülemiyorsa eski sıkı değere iner.

    Körlük şartı kasıtlı: maliyet hiçbir modelde okunamıyorsa para tavanı asla
    tetiklenmez ve tek koruma token tavanıdır — orada gevşemek sessizce
    sınırsız bir fatura yaratırdı.
    """
    cost_cap = float(state.get("max_total_cost_usd") or 0.0)
    spent = _run_cost(state).get("cost_usd")
    if cost_cap > 0 and spent is not None and spent >= cost_cap:
        return f"cost ceiling (${cost_cap:,.2f}) reached at ${spent:,.2f}"

    used = _token_usage(state)
    cap = int(state.get("max_total_tokens") or 0)
    if not spent:  # None ya da 0.0 → parayı göremiyoruz
        cap = min(cap, BLIND_MAX_TOKENS) if cap > 0 else BLIND_MAX_TOKENS
    if cap > 0 and used >= cap:
        return f"token ceiling ({cap:,}) reached at {used:,}"
    return None


def _enforce_token_budget(run_id: str) -> None:
    """Stop before another LLM call once the persisted per-run ceiling is hit."""

    with _AGENT_LOCK:
        s = dict(_AGENT_PROGRESS.get(run_id) or {})
    breach = _budget_breach(s)
    if breach:
        raise AgentBudgetReached(breach)


def _admit_llm_budget(run_id: str, request: dict) -> None:
    """Reject a provider request unless its conservative bound fits the cap.

    İKİ TAVAN, İKİ KONTROL NOKTASI — ve para yanlış olanındaydı. `_budget_breach`
    parayı da token'ı da sayıyor ama TURLAR ARASI çağrılıyor; bu fonksiyon HER
    sağlayıcı çağrısından önce çalışıyor ve yalnız token'a bakıyordu. Bir turun
    içinde onlarca çağrı var, dolayısıyla para tavanı aşıldıktan sonra koşu
    turun sonuna kadar harcamaya devam edebiliyordu. Tavan, aşıldığını
    öğrendiğin an değil, bir sonraki harcamayı durdurduğun an tavandır.

    PROJEKSİYON YOK, BİLİNÇLİ. Sıradaki çağrının parasal maliyetini tahmin
    etmek cazip ama elimizdeki tek sınır `total_token_bound` ve o BAYT sayıyor
    (`llm_client._llm_request_token_bound`: `len(text.encode("utf-8"))`),
    token değil — token başına ~4 kat fazla. Token yolunda bu tutarlı bir
    ihtiyat payı ve sistem ona göre kalibre; aynı sayıyı parayla çarpmak
    kullanıcının 20 dolarlık tavanını sessizce 5 dolara indirirdi. Harcanan
    parayı okumak tahmin gerektirmiyor.
    """

    reserve = max(0, int(request.get("total_token_bound") or 0))
    with _AGENT_LOCK:
        s = dict(_AGENT_PROGRESS.get(run_id) or {})
    cap = int(s.get("max_total_tokens") or 0)
    used = _token_usage(s)

    cost_cap = float(s.get("max_total_cost_usd") or 0.0)
    spent = _run_cost(s).get("cost_usd")
    if cost_cap > 0 and spent is not None and spent >= cost_cap:
        _session_log(
            run_id,
            "llm_budget_rejected",
            reason="cost_ceiling",
            spent_usd=round(float(spent), 6),
            cost_cap_usd=cost_cap,
            requested_bound=reserve,
        )
        raise AgentBudgetReached(
            f"cost ceiling (${cost_cap:,.2f}) cannot admit next call: "
            f"${float(spent):,.2f} already spent"
        )

    if cap > 0 and used + reserve > cap:
        remaining = max(0, cap - used)
        _session_log(
            run_id,
            "llm_budget_rejected",
            used=used,
            remaining=remaining,
            requested_bound=reserve,
            input_bound=int(request.get("input_token_bound") or 0),
            output_bound=int(request.get("output_token_bound") or 0),
        )
        raise AgentBudgetReached(
            f"token ceiling ({cap:,}) cannot admit next call: "
            f"{remaining:,} remaining, {reserve:,} reserved"
        )


def _set_robustness_scan(run_id: str, current: int, total: int) -> None:
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s is not None:
            s["rob_scan_current"] = current
            s["rob_scan_total"] = total


# ── Core helpers ──────────────────────────────────────────────────────────────


# Eğri indirgeme `auto/log_thinning.py`'ye taşındı: saf fonksiyonlar, HTTP ile
# ilgileri yok, ama burada durdukları için `compact_sessions.py` (bir CLI aracı)
# onları çağırabilmek adına tüm router ağacını import etmek zorundaydı — üstelik
# alt çizgili adları başka bir modülün private yüzeyinden çekerek
# (DeepR 2026-08-11 [ORTA]). Alt çizgili adlar burada alias olarak kalıyor:
# bu dosyada onlarca çağrı yeri ve testlerde doğrudan referanslar var.
_downsample_indices = downsample_indices
_spark_points = spark_points
_thin_pair = thin_pair
_thin_curves = thin_curves


_WFO_KEEP_METRICS = (
    "pnl",
    "pnl_pct",
    "sharpe",
    "sharpe_per_trade",
    "max_dd",
    "n_trades",
    "win_rate",
    "profit_factor",
)


def _compact_wfo_windows(windows) -> list:
    """WFO windows → the forensic subset, for the session log only.

    ``_thin_curves`` cut this payload from 2.39 MB to 303 KB by shrinking the
    curves, but the remainder is structural: 88 windows × three ~30-field metric
    dicts. Nothing reads those fields back from the log — the decision path and
    the UI both work off the live ``rob`` dict — so the log keeps the window
    identity, the selected parameters, and the handful of metrics a human would
    reconstruct the verdict from.

    NOTE both series survive: ``test_metrics`` (re-optimized) and
    ``test_metrics_naive`` (the spec that gets saved). Dropping the naive one to
    save bytes would delete exactly the number the gate now decides on — see
    ``_wfo_test``.
    """
    if not isinstance(windows, list):
        return windows

    def _slim(m):
        if not isinstance(m, dict):
            return m
        return {k: m[k] for k in _WFO_KEEP_METRICS if k in m}

    out = []
    for w in windows:
        if not isinstance(w, dict):
            out.append(w)
            continue
        row = {
            k: w[k]
            for k in (
                "window",
                "train_start",
                "train_end",
                "test_start",
                "test_end",
                "chosen_params",
                "train_objective",
                "test_objective",
                "objective_metric",
                "train_n_trades",
                "test_n_trades",
            )
            if k in w
        }
        for key in ("train_metrics", "test_metrics", "test_metrics_naive"):
            if key in w:
                row[key] = _slim(w[key])
        out.append(row)
    return out


_is_point_series = is_point_series  # auto.log_thinning'e taşındı (alias)


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


def _candidate_fingerprint(spec, *, family: bool = False) -> str:
    """Stable identity for duplicate and known-dead candidate suppression.

    Exact fingerprints include parameters. Family fingerprints intentionally
    omit them so a zero-trade indicator/role topology is not retried with tiny
    parameter variations. Generated custom blocks retain their concrete name;
    treating every custom function as one family would suppress genuinely new
    code merely because an earlier generated function emitted no signals.
    """

    blocks_by_role: dict[str, list[dict]] = {}
    for block in getattr(spec, "blocks", []) or []:
        row = {
            "type": str(getattr(block, "type", "")),
            "role": str(getattr(block, "role", "")),
        }
        if not family:
            row["params"] = getattr(block, "params", {}) or {}
        blocks_by_role.setdefault(row["role"], []).append(row)

    # Signal evaluation aggregates the entry and exit partitions with all()/any()
    # (ComposerStrategy.on_bar); AND and OR are commutative. Fingerprinting their
    # emitted order as if it were a strategy difference lets an LLM spend a full
    # backtest on the same topology merely because it swapped two blocks.
    #
    # Keep roles partitioned: entry/exit are different contracts, while sort_keys
    # also makes nested parameter dict ordering irrelevant.
    blocks = [
        row
        for role in sorted(blocks_by_role)
        for row in sorted(
            blocks_by_role[role],
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"), default=str
            ),
        )
    ]
    payload = {
        "blocks": blocks,
        "entry_logic": str(getattr(spec, "entry_logic", "OR")),
        "exit_logic": str(getattr(spec, "exit_logic", "OR")),
        "allow_short": bool(getattr(spec, "allow_short", False)),
        "use_bracket": bool(getattr(spec, "use_bracket", False)),
        "sl_type": str(getattr(spec, "sl_type", "off")),
        "sl_value": float(getattr(spec, "sl_value", 0.0) or 0.0),
        "tp_type": str(getattr(spec, "tp_type", "off")),
        "tp_value": float(getattr(spec, "tp_value", 0.0) or 0.0),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def _deduplicate_candidate(
    spec,
    *,
    seen: set[str],
    zero_trade_families: set[str],
    run_id: str,
    round_num: int,
    iteration: int,
):
    """Replace a repeated/dead candidate with a no-token builtin proposal."""

    exact = _candidate_fingerprint(spec)
    family = _candidate_fingerprint(spec, family=True)
    reason = "exact_duplicate" if exact in seen else None
    if reason is None and family in zero_trade_families:
        reason = "known_zero_trade_family"
    if reason is None:
        return spec

    _discard_unrun_custom_blocks(spec, run_id)
    from agent import _fallback_composed

    for _ in range(64):
        replacement = _proposal_to_spec(_fallback_composed())
        replacement_exact = _candidate_fingerprint(replacement)
        replacement_family = _candidate_fingerprint(replacement, family=True)
        if replacement_exact in seen or replacement_family in zero_trade_families:
            continue
        _session_log(
            run_id,
            "candidate_deduplicated",
            round=round_num,
            iteration=iteration,
            reason=reason,
            rejected_spec_id=getattr(spec, "id", ""),
            rejected_fingerprint=exact,
            replacement_spec_id=replacement.id,
            replacement_fingerprint=replacement_exact,
        )
        _add_step(
            run_id,
            f"  ↻ Replaced {reason.replace('_', ' ')} candidate with "
            f"{replacement.name}",
        )
        return replacement
    raise RuntimeError("candidate fallback pool exhausted after 64 attempts")


_STARTING_CASH: float | None = None
_PNL_FALLBACK_WARNED = False


def _starting_cash() -> float:
    """Lazily fetch the STARTING_CASH constant (keep the module import light).

    Contract: try ``app_constants`` first (a parallel refactor is creating this
    module); on ImportError fall back to the existing ``backtest`` source.
    """
    global _STARTING_CASH
    if _STARTING_CASH is None:
        try:
            from app_constants import STARTING_CASH
        except ImportError:
            from backtest import STARTING_CASH
        _STARTING_CASH = float(STARTING_CASH)
    return _STARTING_CASH


def _excess_return(m: dict, *, fallback_to_pnl_pct: bool = False) -> float | None:
    """Preferred excess-return field, with legacy-key fallback.

    Both scoring gates below need "how much did this beat buy-and-hold" and
    used to each hand-roll the same is-None fallback chain — a metrics-producer
    change adding a new preferred key had to be edited in both to stay in sync.
    """
    v = m.get("excess_return_fraction")
    if v is None:
        v = m.get("excess_pnl_pct")
    if v is None and fallback_to_pnl_pct:
        v = m.get("pnl_pct")
    return v


def _score(result) -> float:
    """NAU composite ranking score (H9/M30/M32):

        calmar = clamp(pnl_pct / max(|max_dd|, 0.01), -10, 10)
        base   = 0.7 × calmar + 0.3 × clamp(sharpe_per_trade, -10, 10)
        k      = n_trades / (n_trades + 20)             ← confidence
        score  = base × k   (base ≥ 0)
        score  = base ÷ k   (base < 0)                  ← SİMETRİK (2026-08-11)

    NAU parity: the sharpe term is PER-TRADE sharpe ((mean/std)×√n, NAU
    backtest.py:89) — NOT the annualized 252-day Sharpe. NAU's fold_quality
    composite also reads m['sharpe'] on a per-trade basis; here the annualized
    'sharpe' is a separate field, so sharpe_per_trade is used explicitly here.

    - pnl_pct and max_dd are in the FRACTION convention (0.1 = 10%; max_dd < 0 is
      healthy).
    - The old WinRate×0.2 term and the fixed 0.1 confidence bonus are REMOVED:
      WR info is already embedded in PnL/Calmar, and the fixed bonus was
      distorting the ranking.
    - The overtrading log-penalty is DELIBERATELY kept: if n_trades > 2000,
      -0.3×log10(n/2000) is added to the score (suppresses commission-heavy
      noise strategies; also necessary because the confidence multiplier
      saturates to 1 at large n).

    JUNK elimination → -inf (aligned with the NAU backtest optimizer):
    error OR < _MIN_TRADES trades OR degenerate drawdown. The NAU rule is
    ``trade < 20 OR max_drawdown <= 0 → eliminated``. In this project max_dd is
    in the negative convention (healthy = <0); 0/NaN/missing = no real drawdown =
    data/computation degeneration, so ``max_dd >= 0`` is the mirror of NAU's
    ``<= 0``.
    """
    global _PNL_FALLBACK_WARNED
    if result.error:
        return float("-inf")
    m = result.metrics
    if not m or (m.get("n_trades") or 0) < _MIN_TRADES:
        return float("-inf")
    max_dd = m.get("max_dd")
    # NAU: max_drawdown <= 0 → junk (degenerate). M1: NaN is also degenerate.
    if max_dd is None or math.isnan(max_dd) or max_dd >= 0:
        return float("-inf")
    n_trades = m.get("n_trades") or 0
    # NAU parity: the composite's 0.3 term is PER-TRADE sharpe. Annualized
    # 'sharpe' (252-day) is on a different scale; NAU uses per-trade
    # ((mean/std)×√n).
    sharpe = m.get("sharpe_per_trade")
    if sharpe is None:
        sharpe = m.get("sharpe") or 0.0  # backward-compat (old metrics dicts)
    # Use explicit None check to avoid treating pnl_pct=0.0 (break-even) as missing
    # Rank edge over the investable buy-and-hold baseline when the run stamped
    # one. Absolute long-market profit is not evidence of strategy alpha.
    _pnl_pct = _excess_return(m, fallback_to_pnl_pct=True)
    if _pnl_pct is not None:
        pnl_pct = _pnl_pct
    else:
        # M12+L1: pnl is absolute USDT — divide by STARTING_CASH to convert to
        # fraction (scale-correct fallback). Warn once: a missing pnl_pct
        # indicates a regression in the metrics producer.
        pnl_pct = (m.get("pnl") or 0.0) / _starting_cash()
        if not _PNL_FALLBACK_WARNED:
            _PNL_FALLBACK_WARNED = True
            logging.warning(
                "_score: no pnl_pct in metrics — used pnl/STARTING_CASH "
                "fallback (logged once)"
            )
    if math.isnan(sharpe) or math.isinf(sharpe):
        sharpe = 0.0
    if math.isnan(pnl_pct) or math.isinf(pnl_pct):
        pnl_pct = 0.0
    calmar = pnl_pct / max(abs(max_dd), 0.01)
    calmar = max(-10.0, min(10.0, calmar))
    base = 0.7 * calmar + 0.3 * max(-10.0, min(10.0, sharpe))
    # Confidence multiplier: continuously reduces the score of low-trade results
    # (n=20 → ×0.5, n=180 → ×0.9); replaces the old stepwise 0.1 bonus.
    #
    # SİMETRİ (2026-08-11): çarpanı negatif `base`'e de uygulamak, az işlemli
    # KÖTÜ bir adayın cezasını azaltıyordu — yani düşük örneklem, iyi tarafta
    # cezalanırken kötü tarafta ödüllendiriliyordu. Ölçüm (koşu d5f7fd06):
    # sıralamada #1 = 27 işlem/+244 USD, #2 = 135 işlem/+17.562 USD/Sharpe 0,44.
    # Negatif tarafta BÖLMEK aynı belirsizliği aynı yönde ifade eder: az
    # örneklem her iki yönde de "emin değiliz" demektir, "daha az kötü" değil.
    confidence = n_trades / (n_trades + 20)
    score = base * confidence if base >= 0 else base / max(confidence, 1e-9)
    # Documented exception: overtrading log penalty (see docstring).
    if n_trades > 2000:
        score += -0.3 * math.log10(n_trades / 2000)
    return score


def _pre_robustness_eligible(result) -> bool:
    """Cheap fail-closed alpha gate before the expensive robustness suite.

    Tek kaynak `_ineligibility_reason`: kapı ile onun teşhis kırılımı ayrı
    yazılsaydı sessizce ıraksarlardı ve faz satırı kapının reddettiğinden
    başka bir gerekçe gösterirdi.
    """
    return _ineligibility_reason(result) is None


def _ineligibility_reason(result) -> str | None:
    """Bu aday alfa kapısında NEDEN elendi — geçtiyse None.

    `_pre_robustness_eligible`'ın bool'u karar için yeter, teşhis için yetmez:
    "koşamadı" ile "koştu ama eşiği geçemedi" tamamen farklı iki dünyadır ve
    faz satırı ikisini "all iterations failed" diye tek torbaya koyuyordu.
    """
    if result is None or result.error or not result.metrics:
        return "crashed"
    m = result.metrics
    if int(m.get("n_trades") or 0) < _MIN_TRADES:
        return "too_few_trades"
    try:
        pnl = float(m.get("pnl"))
        sharpe = float(m.get("sharpe_per_trade"))
    except (TypeError, ValueError):
        return "no_metrics"
    if not math.isfinite(pnl) or pnl <= 0:
        return "negative_pnl"
    if not math.isfinite(sharpe) or sharpe <= 0:
        return "negative_sharpe"
    # `excess` ARTIK zorunlu değil: yıllıklandırılmış alfa varsa karar ondan
    # verilir ve kümülatif alan hiç okunmayabilir. Eskiden burada float()
    # ediliyordu, yani alanı olmayan bir kayıt alfa bacağına HİÇ ulaşmadan
    # "no_metrics" ile eleniyordu.
    return _benchmark_rejection(m, _excess_return(m))


# Kapının kuralı app_constants'ta TEK kopya (bkz. yukarıdaki BENCHMARK notu ve
# app_constants.benchmark_rejection'ın docstring'i). Buradaki ad, çağıranları ve
# testleri kırmamak için duruyor — gövde taşındı, kopyalanmadı.
_benchmark_rejection = benchmark_rejection


# Teşhis satırında kullanılan insan-okur karşılıklar (sıra = raporlama sırası).
_INELIGIBILITY_LABELS = (
    ("crashed", "crashed"),
    ("no_metrics", "no metrics"),
    ("too_few_trades", f"<{_MIN_TRADES} trades"),
    ("negative_pnl", "PnL ≤ 0"),
    ("negative_sharpe", "Sharpe ≤ 0"),
    ("negative_alpha", "no annualized alpha"),
    ("worse_risk_adjusted", "worse Calmar than buy&hold"),
    ("below_benchmark", "below buy&hold (legacy cumulative)"),
    ("no_benchmark", "benchmark not measured"),
)


def _no_eligible_phase_label(results: list[tuple]) -> str:
    """Kazanansız turun faz satırı — GERÇEK gerekçeyle.

    Eski metin her turda "⚠ No eligible result — all iterations failed" diyordu;
    oysa ölçülen koşuda 8 backtest'in 8'i de BAŞARIYLA koştu (en iyisi
    PnL +22.656, Sharpe 0.56) ve eleme tamamen alfa kapısında oldu. Kullanıcı
    bu yüzden altyapı arızası sanıp modeli/veriyi kurcalıyordu; oysa ayar
    edilecek şey eşikti. Kırılım, hangisi olduğunu tek bakışta söyler.
    """
    counts: dict[str, int] = {}
    for entry in results:
        reason = _ineligibility_reason(entry[1])
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    total = len(results)
    ran = total - counts.get("crashed", 0) - counts.get("no_metrics", 0)
    parts = [
        f"{counts[key]} {label}"
        for key, label in _INELIGIBILITY_LABELS
        if counts.get(key)
    ]
    breakdown = ", ".join(parts) or "no results"
    return (
        f"⚠ 0/{total} candidates passed the alpha gate "
        f"({ran} ran successfully) — {breakdown}"
    )


def _stamp_buy_hold_benchmark(result, bars_df) -> None:
    """Attach deterministic buy-and-hold and excess-return metrics in-place."""
    if result.error or not result.metrics:
        return
    from app_constants import stamp_buy_hold_benchmark

    stamp_buy_hold_benchmark(result.metrics, bars_df, label="buy_and_hold")


def _rank_results(results: list[tuple]) -> list[tuple]:
    """Sort (spec, IterationResult) pairs by composite score descending."""
    return sorted(results, key=lambda x: _score(x[1]), reverse=True)


def _rank_and_filter(
    run_id: str, results: list[tuple]
) -> tuple[list[tuple], list[tuple]]:
    """Rank one round's iteration results and filter to the robustness-eligible
    subset, logging the qualify-count and top-5 summary. Winnerless
    return/continue control flow stays in the caller — this is the pure
    ranking/filtering/logging half of Phase 3."""
    ranked = _rank_results(results)
    eligible = [
        (s, r, iv)
        for s, r, iv in ranked
        if _score(r) > float("-inf") and _pre_robustness_eligible(r)
    ]

    _add_step(
        run_id,
        f"{len(eligible)}/{len(results)} results qualify (≥{_MIN_TRADES} trades, "
        "positive PnL/Sharpe and positive benchmark excess)",
    )
    for rank_i, (s, r, iv) in enumerate(ranked[:5]):
        sc = _score(r)
        m = r.metrics or {}
        _add_step(
            run_id,
            f"  #{rank_i + 1} {s.name} [{iv}] · score={sc:.3f} · "
            f"PnL={m.get('pnl', 0):+.2f} · "
            f"Sharpe={m.get('sharpe', float('nan')):.2f}",
        )
    return ranked, eligible


def _propose_initial_strategy(
    run_id: str,
    run_number: int,
    hint: str,
    web_research: bool,
    is_external: bool,
    instrument_id: str,
    intervals: list[str],
    trend_filter: bool,
    trend_interval: str,
    wstate: _WorkerState,
):
    """Phase 1: the round's first LLM proposal, deduped and stamped.

    Raises TerminalLLMError uncaught on a terminal provider failure — the
    caller must not swallow it. Mirrors _generate_custom_spec's established
    pattern of a standalone module function doing its own local imports.
    """
    from agent import TerminalLLMError, propose_composed_strategy
    from composer import load_catalog

    _set_phase(run_id, 1, "Claude is generating a strategy…")

    catalog = load_catalog()
    dummy_history: list = []

    if web_research:
        # 🌐 glifi robustness'ın multi-symbol satırında da var; sınıfı metinden
        # değil buradan söyle (aksi hâlde iki farklı olay aynı kelimeye bağlı).
        _add_step(run_id, "🌐 Running web research…", kind="llm")
    _tl_begin(
        run_id,
        "llm",
        f"llm-propose-r{run_number}",
        "Initial strategy (Claude)",
        round_num=run_number,
    )
    # Iteration 0 runs on the first TF of the round-robin — tell the model
    # which bar it is designing for instead of letting it guess.
    _iv0 = _iv_for(0, run_number, intervals)
    proposal, _usage1 = propose_composed_strategy(
        dummy_history,
        catalog,
        hint=hint,
        web_research=web_research,
        market=_market_for(is_external, instrument_id, _iv0),
        timeframe=_iv0,
    )
    _enforce_token_budget(run_id)
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
    degraded = proposal.get("degraded") or ""
    if degraded:
        _mark_degraded(run_id, degraded, "initial strategy")
        wstate.degraded_spec_ids.add(spec.id)
        if proposal.get("degraded_terminal"):
            raise TerminalLLMError(
                proposal.get("degraded_detail")
                or f"terminal provider failure: {degraded}"
            )
    _tl_end(
        run_id,
        f"llm-propose-r{run_number}",
        status="warn" if degraded else "ok",
        name=spec.name,
    )
    spec = _deduplicate_candidate(
        spec,
        seen=wstate.seen_candidate_fingerprints,
        zero_trade_families=wstate.zero_trade_families,
        run_id=run_id,
        round_num=run_number,
        iteration=0,
    )
    if is_external:
        _exposure_change = _clamp_spec_trade_size(spec)
        if _exposure_change:
            _session_log(
                run_id,
                "exposure_normalized",
                round=run_number,
                iteration=0,
                spec_id=spec.id,
                **_exposure_change,
            )
    spec.trend_filter = trend_filter
    spec.trend_interval = trend_interval
    spec.model_slippage = True
    _done_phase(
        run_id,
        1,
        (f"⚠ degraded ({degraded}) · {spec.name}" if degraded else f"✓ {spec.name}")
        + (" · trend filter ON" if trend_filter else ""),
        degraded=bool(degraded),
    )
    return spec


class _ScanOutcome(NamedTuple):
    passed: bool
    log_entry: dict
    passer_entry: dict | None


def _scan_one_candidate(
    run_id: str,
    run_number: int,
    rank_i: int,
    n_eligible: int,
    cand_spec,
    cand_result,
    cand_iv: str,
    cand_df,
    strict_mode: bool,
    wstate: _WorkerState,
    is_external: bool,
    instrument_id: str,
    symbol: str,
    category: str,
    max_hours: float,
    n_passers_so_far: int,
) -> _ScanOutcome:
    """Phase 4's per-candidate robustness check (loop control, stop-check,
    and cache loading stay in the caller). A `run_robustness_guarded`
    exception is caught and reported as a failed, non-raising outcome —
    never propagates out of this function."""
    from sandbox import ROBUSTNESS_TIMEOUT_S, run_robustness_guarded

    _set_robustness_scan(run_id, rank_i + 1, n_eligible)
    _add_step(
        run_id,
        f"[{rank_i + 1}/{n_eligible}] Robustness: {cand_spec.name} [{cand_iv}]",
    )
    _set_phase(run_id, 4, f"{rank_i + 1}/{n_eligible} trying: {cand_spec.name}")

    _rob_key = f"rob-r{run_number}-c{rank_i + 1}"
    _tl_begin(
        run_id,
        "robustness",
        _rob_key,
        f"Robustness {rank_i + 1}/{n_eligible} · {cand_spec.name}",
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
            _recipe(is_external, instrument_id, symbol, category, cand_iv),
            cand_result.trades,
            symbol=instrument_id if is_external else symbol,
            interval=cand_iv,
            progress_fn=_rob_progress,
            timeout_s=_remaining_phase_timeout(
                wstate.worker_t0, max_hours, ROBUSTNESS_TIMEOUT_S
            ),
        )
    except Exception as rob_exc:
        _rob_progress.close_open("fail")
        _tl_end(run_id, _rob_key, status="fail")
        _add_step(run_id, f"  ⚠ Robustness error: {rob_exc} — skipping")
        return _ScanOutcome(
            passed=False,
            log_entry={
                "rank": rank_i + 1,
                "name": cand_spec.name,
                "score": round(_score(cand_result), 3),
                "passed": False,
                "overfitting_label": f"error: {type(rob_exc).__name__}",
                "mc_dd_p50": None,
                "wf_pass": "—",
                "ms_label": "—",
            },
            passer_entry=None,
        )

    passed = _robustness_passed(rob, strict=strict_mode, run_id=run_id)
    if cand_spec.id in wstate.degraded_spec_ids:
        passed = False
        _add_step(
            run_id,
            "  ❌ Degraded/fallback candidate is research-only and "
            "cannot enter the winner pool",
        )

    split_label = (rob.get("split") or {}).get("overfitting_label", "?")
    mc_dd = (rob.get("mc") or {}).get("max_dd_p50", None)
    wfo = rob.get("wfo_windows") or []
    # Naive series — the one _robustness_passed decided on.
    valid_wfo = _valid_wfo_windows(wfo)
    wf_pos = sum(1 for w in valid_wfo if _wfo_test(w).get("pnl", 0) > 0)
    wf_str = f"{wf_pos}/{len(valid_wfo)}" if valid_wfo else "—"
    ms_label = (rob.get("multi_symbol") or {}).get("generalization_label", "—")

    try:
        rob_artifact = _write_session_artifact(
            run_id,
            f"robustness-r{run_number}-c{rank_i + 1}",
            rob,
        )
    except Exception as artifact_exc:
        rob_artifact = {"error": str(artifact_exc)[:300]}
    # JSONL remains a compact event index. The full, curve-heavy
    # robustness object is gzip-compressed and content-addressed by
    # digest so replay/audit retains every value without 100KB lines.
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
        summary=_robustness_log_summary(rob),
        artifact=rob_artifact,
    )
    # L26: lightweight SQLite index (best-effort, errors swallowed)
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

    log_entry = {
        "rank": rank_i + 1,
        "name": cand_spec.name,
        "score": round(_score(cand_result), 3),
        "passed": passed,
        "overfitting_label": split_label,
        "mc_dd_p50": round(mc_dd, 1) if mc_dd is not None else None,
        "wf_pass": wf_str,
        "ms_label": ms_label,
    }

    _rob_progress.close_open("ok")
    if passed:
        _tl_end(run_id, _rob_key, status="ok", name=cand_spec.name)
        # M26+M31: a passing candidate enters the pool; effective
        # score = _score × multi-symbol pass_rate factor (0.15…1.0).
        raw_score = _score(cand_result)
        factor = _ms_score_factor(rob)
        # Sign-safe MS penalty: for a positive score, exactly equals
        # raw*factor (raw - (1-factor)*raw); for a negative score, it
        # prevents a small factor from making the score LESS negative
        # and inverting the ranking (the penalty always pulls the
        # score DOWN).
        effective = raw_score - (1.0 - factor) * abs(raw_score)
        passer_entry = {
            "spec": cand_spec,
            "result": cand_result,
            "rob": rob,
            "iv": cand_iv,
            "score": raw_score,
            "factor": factor,
            "effective": effective,
        }
        _add_step(
            run_id,
            f"  ✅ ALL TESTS PASSED! "
            f"IS/OOS: {split_label} · WFO: {wf_str} · Multi-symbol: {ms_label}",
        )
        _add_step(
            run_id,
            f"  ⚖ Effective score: {raw_score:.3f} × MS-factor "
            f"{factor:.3f} = {effective:.3f} "
            f"({n_passers_so_far + 1}/{_MAX_PASSERS} passing candidates)",
        )
        return _ScanOutcome(passed=True, log_entry=log_entry, passer_entry=passer_entry)

    _tl_end(run_id, _rob_key, status="warn", name=cand_spec.name)
    _add_step(
        run_id,
        (
            f"  ❌ Failed — IS/OOS: {split_label} · "
            f"WFO: {wf_str} · MC median DD: {mc_dd:.1f}% · Multi-symbol: {ms_label}"
            if mc_dd is not None
            else f"  ❌ Failed — IS/OOS: {split_label} · "
            f"WFO: {wf_str} · Multi-symbol: {ms_label}"
        ),
    )
    return _ScanOutcome(passed=False, log_entry=log_entry, passer_entry=None)


class _PromotionGateResult(NamedTuple):
    winner_holdout: dict | None
    promotion_passed: bool
    promotion_reason: str


def _run_promotion_gate(
    run_id: str,
    run_number: int,
    winner_spec,
    winner_iv: str,
    winner_rob: dict,
    holdout_cache: dict,
    wstate: _WorkerState,
    is_external: bool,
    instrument_id: str,
    symbol: str,
    category: str,
    max_hours: float,
    research_only: bool,
) -> _PromotionGateResult:
    """Phase 5's sealed-holdout promotion gate — the financial-integrity
    checkpoint that decides whether the round's winner may be published to
    the catalog. A sealed holdout window is consumed AT MOST ONCE per
    timeframe per session (wstate.holdout_consumed); a research-only run can
    never pass regardless of the holdout result (known-unadjusted data).

    Catalog append, the _AGENT_PROGRESS winner-fields write, and the final
    winner/finalist_rejected + session_end logging stay in the caller —
    those are terminal round bookkeeping, not part of the gate decision.
    """
    from sandbox import run_backtest_guarded

    # H4/H8: the winner may be on a different TF (winner_iv); cand_iv is
    # the TF of the LAST scanned candidate left over from the loop. The
    # robustness log and the sealed holdout must use the winner's OWN TF —
    # otherwise the log identity is overwritten and the holdout runs with
    # the wrong slice/recipe.
    _log_robustness(
        winner_spec.id,
        winner_spec.name,
        winner_rob,
        symbol=instrument_id if is_external else symbol,
        category=category,
        interval=winner_iv,
    )
    _add_step(run_id, "✓ Robustness result → robustness_log.jsonl")
    # Sealed holdout is a publication gate and each timeframe's slice is
    # consumed at most once per AUTO session. Reusing the same 60 days
    # for many finalists turns a holdout into another selection set.
    winner_holdout = None
    promotion_passed = False
    promotion_reason = "sealed holdout unavailable"
    _hold = holdout_cache.get(winner_iv)  # H4: the winner's TF
    if winner_iv in wstate.holdout_consumed:
        promotion_reason = "sealed holdout already consumed for this timeframe"
        _add_step(run_id, f"⚠ {promotion_reason} — catalog promotion refused")
    elif _hold is not None:
        wstate.holdout_consumed.add(winner_iv)
        _hold_run_df, _hold_start = _hold
        try:
            _hold_res = run_backtest_guarded(
                winner_spec,
                _hold_run_df,
                _recipe(is_external, instrument_id, symbol, category, winner_iv),
                iteration_id=999,
                rationale="sealed holdout (L32)",
                timeout_s=_remaining_phase_timeout(wstate.worker_t0, max_hours, 150.0),
                force_subprocess=True,
            )
            _hm = _hold_res.metrics or {}
            if _hold_res.error is None and _hm:
                # Score only entries INSIDE the sealed window — the
                # lead-in bars exist purely for indicator warmup.
                _n, _pnl_fr, _sharpe = _sealed_holdout_stats(
                    _hold_res.trades, int(_hold_start.timestamp())
                )
                _sealed_prices = _hold_run_df[_hold_run_df.index >= _hold_start][
                    "close"
                ]
                from app_constants import benchmark_and_excess

                _bench_excess = (
                    benchmark_and_excess(
                        float(_sealed_prices.iloc[0]),
                        float(_sealed_prices.iloc[-1]),
                        _pnl_fr,
                    )
                    if len(_sealed_prices) >= 2
                    else None
                )
                _benchmark_fr, _excess_fr = _bench_excess or (0.0, _pnl_fr)
                winner_holdout = {
                    "sharpe": round(_sharpe, 4) if _sharpe is not None else None,
                    "pnl_pct": round(_pnl_fr, 6),
                    "benchmark_return_fraction": round(_benchmark_fr, 6),
                    "excess_return_fraction": round(_excess_fr, 6),
                    "benchmark_return_pct": round(_benchmark_fr, 6),
                    "excess_pnl_pct": round(_excess_fr, 6),
                    "n_trades": _n,
                    "days": OOS_HOLDOUT_DAYS,
                    "warmup_bars": HOLDOUT_WARMUP_BARS,
                    # A count, not a boolean question. 0 entries means
                    # the layer measured nothing; but so, in practice,
                    # does 1 (run 3cad3325's round-2 winner reported
                    # measured=True on a SINGLE trade, with sharpe=None
                    # because a standard deviation needs two). The
                    # sealed holdout is the run's one unbiased forward
                    # estimate — claiming it exists on one trade
                    # overstates it. Below HOLDOUT_MIN_TRADES the flag
                    # stays False and the count is reported as-is.
                    "measured": _n >= HOLDOUT_MIN_TRADES,
                }
                promotion_passed, promotion_reason = _holdout_promotion_verdict(
                    _n, _pnl_fr, _sharpe, _excess_fr, metrics=_hm
                )
                if _n < HOLDOUT_MIN_TRADES:
                    _add_step(
                        run_id,
                        f"⚠ Sealed OOS ({OOS_HOLDOUT_DAYS}d): {_n} trade "
                        f"— ÖLÇÜLEMEDİ (en az {HOLDOUT_MIN_TRADES} giriş "
                        "gerekiyor; warmup lead-in dahil)",
                    )
                else:
                    _sh_txt = f"{_sharpe:.2f}" if _sharpe is not None else "—"
                    _add_step(
                        run_id,
                        f"🔒 Sealed OOS ({OOS_HOLDOUT_DAYS}d): "
                        f"Sharpe {_sh_txt} · "
                        f"PnL {100 * _pnl_fr:.1f}% · "
                        f"Excess {100 * _excess_fr:+.1f}% · "
                        f"{_n} trades · promotion "
                        f"{'PASSED' if promotion_passed else 'FAILED'}",
                    )
                _session_log(
                    run_id,
                    "holdout_result",
                    round=run_number,
                    spec_id=winner_spec.id,
                    **winner_holdout,
                )
            else:
                promotion_reason = f"sealed holdout error: {_hold_res.error}"
                _add_step(
                    run_id,
                    f"⚠ Sealed OOS run returned an error: {_hold_res.error}",
                )
        except Exception as _hold_err:
            promotion_reason = f"sealed holdout exception: {_hold_err}"
            _add_step(run_id, f"⚠ Could not run sealed OOS: {_hold_err}")

    if research_only and promotion_passed:
        promotion_passed = False
        promotion_reason = "research-only run used known-unadjusted external data"
        _add_step(
            run_id,
            "❌ Research-only/unadjusted run cannot publish to the strategy catalog",
        )

    return _PromotionGateResult(
        winner_holdout=winner_holdout,
        promotion_passed=promotion_passed,
        promotion_reason=promotion_reason,
    )


def _run_backtest_iteration(
    run_id: str,
    run_number: int,
    i: int,
    n_iterations: int,
    spec,
    iter_iv: str,
    iter_df,
    is_external: bool,
    symbol: str,
    instrument_id: str,
    category: str,
    wstate: _WorkerState,
    max_hours: float,
):
    """Phase 2's run-backtest-and-log-result half of one iteration (the
    'propose next spec' lookahead-generation block stays in the caller — see
    #47-A9/A10 in the decomposition plan). Loop control, stop-check, and
    history/results accumulation also stay in the caller."""
    from sandbox import run_backtest_guarded

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
        _recipe(is_external, instrument_id, symbol, category, iter_iv),
        iteration_id=i,
        rationale=f"agent-run · iter {i + 1}/{n_iterations} · {run_label} {iter_iv}",
        # Backtest çocuğunun ilerleme satırları — sınıf yapısal olarak belli.
        progress_fn=lambda m, _i=i: _add_step(run_id, f"  └ {m}", kind="test"),
        timeout_s=_remaining_phase_timeout(wstate.worker_t0, max_hours, 150.0),
        force_subprocess=True,
    )
    _bt_elapsed = time.perf_counter() - _bt_t0
    # Add the block type list to the strategy field → Claude sees this info
    block_types_str = "+".join(b.type for b in spec.blocks)
    r.strategy = f"composed:{spec.name} [{block_types_str}]"
    _stamp_buy_hold_benchmark(r, iter_df)
    # Stamp the run context onto the result: _summarize_composed_history
    # reads the timeframe from here. Without it the model saw only
    # "pnl=-8514" with no idea the spec had been scored on 15m bars.
    r.bars_info = dict(iter_bars_info)
    wstate.seen_candidate_fingerprints.add(_candidate_fingerprint(spec))
    if int((r.metrics or {}).get("n_trades") or 0) == 0:
        wstate.zero_trade_families.add(_candidate_fingerprint(spec, family=True))
    # The returned ts is this iteration's key into the backtest log
    # — the cockpit card links its tear sheet with it.
    _bt_log_ts = ""
    try:
        _bt_log_ts = _log_backtest(
            spec,
            r,
            "External" if is_external else "Bybit",
            iter_bars_info,
            elapsed_sec=_bt_elapsed,
            run_id=run_id,
        )
    except Exception as log_exc:
        _add_step(run_id, f"  ⚠ Could not write log: {log_exc}")

    sc = _score(r)
    # Add to state for the live backtest table
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
                    "ts": _bt_log_ts,
                    "score": round(sc, 4) if sc > float("-inf") else None,
                    "pnl": m_bt.get("pnl"),
                    "pnl_pct": m_bt.get("pnl_pct"),
                    "benchmark_return_fraction": m_bt.get("benchmark_return_fraction"),
                    "excess_return_fraction": m_bt.get("excess_return_fraction"),
                    "sharpe": m_bt.get("sharpe"),
                    "profit_factor": m_bt.get("profit_factor"),
                    "max_dd": m_bt.get("max_dd"),
                    "win_rate": m_bt.get("win_rate"),
                    "n_trades": m_bt.get("n_trades", 0),
                    "avg_dur": m_bt.get("avg_duration_mins"),
                    "error": r.error,
                    # AUTO Mission Control sparkline: the raw curve
                    # can hold 100k+ points — downsample to ~40 so
                    # a 1s poll never ships a megabyte of JSON.
                    "equity": _spark_points(r.equity_curve),
                }
            )
    # Session log: backtest result. The equity curve and its date
    # axis are reduced TOGETHER (see _thin_pair) — raw, they made
    # this the heaviest event in the log at ~301 KB apiece.
    _eq_curve, _eq_dates = _thin_pair(
        r.equity_curve, r.equity_dates, cap=_SESSION_CURVE_CAP
    )
    _session_log(
        run_id,
        "backtest_result",
        iteration=i,
        round=run_number,
        interval=iter_iv,
        spec_name=spec.name,
        spec_id=spec.id,
        spec_blocks=[
            {"type": b.type, "role": b.role, "params": b.params} for b in spec.blocks
        ],
        score=round(sc, 4),
        # Metrics can themselves contain bar-resolution equity
        # arrays (equity_curve_mtm/realized). Thin recursively; the
        # previous top-level-only reduction left ~47 MB/hour growth.
        metrics=_thin_curves(r.metrics, cap=_SESSION_CURVE_CAP),
        equity_curve=_eq_curve,
        equity_dates=_eq_dates,
        n_trades=len(r.trades) if r.trades else 0,
        bars_info=iter_bars_info,
        error=r.error,
    )
    if r.error:
        _add_step(run_id, f"  ✗ Error: {r.error[:80]}")
        _tl_end(run_id, f"bt-r{run_number}-i{i + 1}", status="fail")
    else:
        m = r.metrics or {}
        _add_step(
            run_id,
            f"  ✓ PnL={m.get('pnl', 0):+.2f} · "
            f"Sharpe={m.get('sharpe', float('nan')):.2f} · "
            f"Trades={m.get('n_trades', 0)} · Score={sc:.3f}",
        )
        _tl_end(
            run_id,
            f"bt-r{run_number}-i{i + 1}",
            status="warn" if (m.get("n_trades", 0) or 0) == 0 else "ok",
            pnl=m.get("pnl"),
            score=round(sc, 4) if sc > float("-inf") else None,
        )

    _set_phase(run_id, 2, f"{i + 1}/{n_iterations} completed")
    return r


def _propose_next_strategy(
    run_id: str,
    run_number: int,
    i: int,
    n_iterations: int,
    intervals: list[str],
    hint: str,
    history: list,
    used_concepts: list[str],
    spec,
    is_external: bool,
    instrument_id: str,
    trend_filter: bool,
    trend_interval: str,
    wstate: _WorkerState,
):
    """Phase 2's lookahead-generation half of one iteration (#47-A10 — the
    run-backtest-and-log half is `_run_backtest_iteration` above, A9).
    Proposes the spec for iteration i+1 while i's backtest just finished.

    Reassigns and returns `spec` (the caller's loop variable) on success.
    On a non-fatal error, logs a warning and returns the unchanged `spec`
    passed in — the search continues with the previous strategy instead of
    aborting. LLMCallCancelled/AgentBudgetReached propagate unchanged:
    those must abort the whole run, not just this one generation attempt.
    """
    from agent import LLMCallCancelled, TerminalLLMError, propose_composed_strategy
    from composer import load_catalog

    next_i = i + 1
    use_custom = next_i % 2 == 1
    _add_step(
        run_id,
        f"[{next_i + 1}/{n_iterations}] "
        + (
            "Generating custom block…"
            if use_custom
            else "Claude is generating a new strategy…"
        ),
    )
    _tl_begin(
        run_id,
        "llm",
        f"llm-refine-r{run_number}-i{next_i + 1}",
        ("Custom block generation" if use_custom else "Claude new strategy"),
        round_num=run_number,
        iter=next_i + 1,
    )
    # The TF the NEXT iteration will use is already decided by the
    # round-robin — pass it down so periods/thresholds are sized
    # for that bar rather than for an unknown one.
    next_iv = _iv_for(next_i, run_number, intervals)
    next_market = _market_for(is_external, instrument_id, next_iv)
    try:
        if use_custom:
            custom_spec = _generate_custom_spec(
                run_id,
                next_i,
                hint,
                history,
                used_concepts=used_concepts,
                round_num=run_number,
                market=next_market,
                timeframe=next_iv,
            )
            if custom_spec is not None:
                spec = custom_spec
                # Accumulate the generated block labels
                for b in spec.blocks:
                    used_concepts.append(b.type)
                _add_step(run_id, f"  → {spec.name} (custom)")
            else:
                proposal, _u = propose_composed_strategy(
                    history,
                    load_catalog(),
                    hint=hint,
                    market=next_market,
                    timeframe=next_iv,
                )
                _enforce_token_budget(run_id)
                spec = _proposal_to_spec(proposal)
                if proposal.get("degraded"):
                    _mark_degraded(
                        run_id,
                        proposal["degraded"],
                        "strategy proposal",
                    )
                    wstate.degraded_spec_ids.add(spec.id)
                    if proposal.get("degraded_terminal"):
                        raise TerminalLLMError(
                            proposal.get("degraded_detail")
                            or "terminal LLM provider failure"
                        )
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
                history,
                load_catalog(),
                hint=hint,
                market=next_market,
                timeframe=next_iv,
            )
            _enforce_token_budget(run_id)
            spec = _proposal_to_spec(proposal)
            if proposal.get("degraded"):
                _mark_degraded(run_id, proposal["degraded"], "strategy proposal")
                wstate.degraded_spec_ids.add(spec.id)
                if proposal.get("degraded_terminal"):
                    raise TerminalLLMError(
                        proposal.get("degraded_detail")
                        or "terminal LLM provider failure"
                    )
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
        spec = _deduplicate_candidate(
            spec,
            seen=wstate.seen_candidate_fingerprints,
            zero_trade_families=wstate.zero_trade_families,
            run_id=run_id,
            round_num=run_number,
            iteration=next_i,
        )
        # H9: trend_filter/trend_interval was only applied to the
        # first (phase 1) spec; EVERY spec generated in refine
        # kept the composer default (trend_filter=False), making
        # inter-iteration comparison inconsistent and saving the
        # winner without a filter. Apply it to every spec.
        spec.trend_filter = trend_filter
        spec.trend_interval = trend_interval
        spec.model_slippage = True
        if is_external:
            _exposure_change = _clamp_spec_trade_size(spec)
            if _exposure_change:
                _session_log(
                    run_id,
                    "exposure_normalized",
                    round=run_number,
                    iteration=next_i,
                    spec_id=spec.id,
                    **_exposure_change,
                )
        with _AGENT_LOCK:
            if run_id in _AGENT_PROGRESS:
                _AGENT_PROGRESS[run_id]["strategy_name"] = spec.name
        _tl_end(
            run_id,
            f"llm-refine-r{run_number}-i{next_i + 1}",
            status="ok",
            name=spec.name,
        )
        return spec
    except (LLMCallCancelled, AgentBudgetReached):
        raise
    except Exception as e:
        # "Önceki strateji ile devam" SESSİZ bir bozulmaydı: `_mark_degraded`
        # çağrılmadığı için fallback sayacına girmiyordu ve aynı spec ikinci kez
        # backtest edilip sıralamada iki slot işgal ediyordu (denetim ölçümü:
        # 24 backtest / 22 benzersiz spec_id, ikisi bu yoldan). Bozulmuş üretim
        # kazanan havuzuna giremez — spec'i degraded olarak işaretle ki
        # tekrarlanan aday bir "kazanan" olarak ilan edilemesin.
        _mark_degraded(run_id, type(e).__name__, "strategy proposal (reused previous)")
        wstate.degraded_spec_ids.add(spec.id)
        _add_step(
            run_id,
            f"  ⚠ Could not get proposal: {e} — continuing with previous strategy",
        )
        _tl_end(
            run_id,
            f"llm-refine-r{run_number}-i{next_i + 1}",
            status="warn",
        )
        return spec


def _load_timeframe_bars(
    run_id: str,
    is_external: bool,
    instrument_id: str,
    symbol: str,
    category: str,
    iv: str,
    range_start: str,
    range_end: str,
) -> pd.DataFrame:
    """Phase 0's source-agnostic bar loader (the #47-A11a extraction of
    `_load_tf_uncached`). No cache dict mutation here — the caller
    (`_TfLoader`) owns tf_cache/holdout_cache."""
    from datetime import timedelta

    from data import load_bybit_bars

    if is_external:
        from data import load_external_bars

        _add_step(run_id, f"Loading catalog data ({instrument_id}, {iv})…")
        df = load_external_bars(instrument_id, iv)  # full catalog range
        if range_start or range_end:
            # Explicit user window (MAX butonu / elle tarih):
            # inclusive [start, end] day slice, tz of the frame.
            _tz = df.index.tz
            if range_start:
                df = df[df.index >= pd.Timestamp(range_start, tz=_tz)]
            if range_end:
                df = df[
                    df.index < pd.Timestamp(range_end, tz=_tz) + pd.Timedelta(days=1)
                ]
            _add_step(
                run_id,
                f"Date window {range_start or '…'} → {range_end or '…'}"
                f" ({len(df):,} bars)",
            )
        if len(df) < 100:
            raise RuntimeError(
                f"Insufficient data ({len(df)} bars, {instrument_id} {iv})."
            )
        # 1-MINUTE guard: ~23 years >2M bars — crop to the last 2 years
        # to keep the engine responsive (mirror of the Bybit 1m guard).
        # An EXPLICIT user window is honored as-is — the user chose it.
        if iv == "1-MINUTE" and len(df) > 1_000_000 and not (range_start or range_end):
            cutoff = df.index[-1] - pd.Timedelta(days=730)
            df = df[df.index >= cutoff]
            _add_step(
                run_id, f"1-MINUTE cropped to the last 2 years ({len(df):,} bars)"
            )
        return df

    from data import _bybit_cache_path

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
    # Explicit user window overrides the default lookback; an
    # earlier start triggers load_bybit_bars' cache backfill.
    if range_start:
        start_dt = datetime.strptime(range_start, "%Y-%m-%d").replace(tzinfo=UTC)
    if range_end:
        end_dt = datetime.strptime(range_end, "%Y-%m-%d").replace(
            tzinfo=UTC
        ) + timedelta(days=1)
    _add_step(
        run_id,
        f"Date window {range_start} → {range_end} requested…"
        if (range_start or range_end)
        else f"Loading widest range ({iv}, ~{lookback_days}d)…",
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
                f"Could not load {symbol}/{category}/{iv} data: {fetch_exc}"
            ) from fetch_exc
        _add_step(run_id, f"Fetch error ({iv}), falling back to cache: {fetch_exc}")
        df = pd.read_parquet(cache_path)

    if len(df) < 100:
        raise RuntimeError(
            f"Insufficient data ({len(df)} bars, {iv}). Fetch it from the Data page."
        )
    # 1m guard: cap at last 2 years so backtests stay responsive.
    if iv == "1" and len(df) > 1_000_000:
        cutoff = df.index[-1] - pd.Timedelta(days=730)
        df = df[df.index >= cutoff]
        _add_step(run_id, f"1m cropped to the last 2 years ({len(df):,} bars)")
    return df


class _TfLoader:
    """Phase 0's cache/holdout-split wrapper around `_load_timeframe_bars`
    (the #47-A11b extraction of `_load_tf`). Owns tf_cache/holdout_cache as
    instance attributes instead of closure-captured locals — one fresh
    instance is constructed per round (the caches must NOT survive into the
    next round, matching the original per-round local dicts)."""

    def __init__(
        self,
        run_id: str,
        run_number: int,
        is_external: bool,
        instrument_id: str,
        symbol: str,
        category: str,
        range_start: str,
        range_end: str,
    ) -> None:
        self.run_id = run_id
        self.run_number = run_number
        self.is_external = is_external
        self.instrument_id = instrument_id
        self.symbol = symbol
        self.category = category
        self.range_start = range_start
        self.range_end = range_end
        # L32: tf_cache holds the TRIMMED data (excluding the sealed holdout);
        # the holdout slice is stored in holdout_cache and used ONLY in a
        # single validation run after the winner is declared.
        self.tf_cache: dict[str, pd.DataFrame] = {}  # interval → trimmed_df
        # interval → (warmup lead-in + sealed df, sealed start ts) | None
        self.holdout_cache: dict[str, tuple[pd.DataFrame, pd.Timestamp] | None] = {}

    def get(self, iv: str) -> pd.DataFrame:
        if iv in self.tf_cache:
            return self.tf_cache[iv]
        # Timeline: cache-miss load is tracked as a "data" span.
        _tl_key = f"data-{iv}-r{self.run_number}"
        _tl_begin(
            self.run_id,
            "data",
            _tl_key,
            f"Data: {self.instrument_id if self.is_external else self.symbol} {iv}",
            round_num=self.run_number,
        )
        try:
            df = _load_timeframe_bars(
                self.run_id,
                self.is_external,
                self.instrument_id,
                self.symbol,
                self.category,
                iv,
                self.range_start,
                self.range_end,
            )
        except Exception:
            _tl_end(self.run_id, _tl_key, status="fail")
            raise
        # L32: sealed holdout — the last OOS_HOLDOUT_DAYS days are withheld
        # from the iteration + robustness phases. Skip if remaining data < 200 bars.
        trimmed, hold_df = _split_holdout(df)
        if hold_df is not None:
            # Holdout RUN frame = warmup lead-in + sealed slice; the
            # sealed boundary rides along so scoring counts only
            # in-window entries (indicators need ~NAU_WINDOW bars
            # before the first signal — the bare slice produced
            # 0-trade holdouts on 4h/1d, validating nothing).
            _lead = df[df.index < hold_df.index[0]].tail(HOLDOUT_WARMUP_BARS)
            self.holdout_cache[iv] = (pd.concat([_lead, hold_df]), hold_df.index[0])
            df = trimmed
            self.tf_cache[iv] = df
            _add_step(
                self.run_id,
                f"🔒 Sealed holdout separated ({iv}): the last "
                f"{OOS_HOLDOUT_DAYS} days ({len(hold_df):,} bars) will be "
                f"withheld until a winner is declared — {len(df):,} bars remaining",
            )
        else:
            self.holdout_cache[iv] = None
            self.tf_cache[iv] = df
            _add_step(
                self.run_id,
                f"⚠ insufficient data for holdout ({iv}) — sealed OOS skipped",
            )
        _tl_end(self.run_id, _tl_key, status="ok", n_bars=len(df))
        return df


# Hisse senedi koşularında hesabın ne kadarı tek pozisyona girsin (yüzde).
# Sistem aynı anda tek pozisyon tutar, dolayısıyla "tam yatırım" doğal taban
# çizgisidir; %5 gibi bir değer $10k hesapta ~2 hisse eder ve sabit komisyonu
# tekrar bağlayıcı kısıt yapardı.
AGENT_EQUITY_PCT = float(os.environ.get("AGENT_EQUITY_PCT", "95"))
NORMALIZE_EXTERNAL_EXPOSURE = (
    os.environ.get("AGENT_NORMALIZE_EXTERNAL_EXPOSURE", "1").strip() != "0"
)


def _clamp_spec_trade_size(spec):
    """Normalize every external-equity AUTO candidate to equal exposure.

    Signal research must not let the LLM choose the leverage of its own exam.
    Comparing a 5%/10%/one-share candidate with a 100%-invested buy-and-hold
    benchmark mixes signal quality with sizing and makes the excess-return gate
    structurally unfair. AUTO therefore scores all external candidates at the
    same near-fully-invested exposure. Deployment can choose risk sizing after
    promotion.

    Returns a before/after audit record when a mutation was made.
    """
    if not NORMALIZE_EXTERNAL_EXPOSURE:
        return None
    before = {
        "trade_size_mode": str(getattr(spec, "trade_size_mode", "fixed")),
        "trade_size_percent": float(getattr(spec, "trade_size_percent", 0.0) or 0.0),
        "trade_size": float(getattr(spec, "trade_size", 0.0) or 0.0),
    }
    spec.trade_size_mode = "percent_equity"
    spec.trade_size_percent = AGENT_EQUITY_PCT
    # Defensive floor for an engine path that temporarily falls back to fixed.
    spec.trade_size = 1.0
    after = {
        "trade_size_mode": spec.trade_size_mode,
        "trade_size_percent": float(spec.trade_size_percent),
        "trade_size": float(spec.trade_size),
    }
    return None if before == after else {"before": before, "after": after}


def _robustness_passed(
    rob: dict, strict: bool = True, run_id: str | None = None
) -> bool:
    """Robustness decision — with an evaluated-criteria counter (H4+L18).

    The 4 criteria (IS/OOS, WFO, Multi-Symbol, Monte Carlo) are each classified
    one by one as ``evaluated | failed | skipped``. The old version silently
    counted missing/faulty sections as "passed"; now:

    - strict:  at least 3 criteria must be ACTUALLY evaluated and none may be
      failed.
    - relaxed: at least 2 criteria must be evaluated, none failed
      ('⚠' labels accepted in both modes — not counted as failed).

    For each skipped criterion (if run_id is given) a warning
    '⚠ <criterion> could not be evaluated: <reason>' is added to the step log.
    """
    if not rob or rob.get("error"):
        return False

    def _skip(name: str, why: str) -> None:
        if run_id:
            _add_step(run_id, f"⚠ {name} could not be evaluated: {why}")

    evaluated = 0
    failed = 0

    # 1) IS/OOS overfitting criterion
    split = rob.get("split") or {}
    split_label = split.get("overfitting_label", "")
    if split.get("error") or not split_label:
        _skip("IS/OOS", str(split.get("error") or "no label"))
    elif "ölçülemedi" in split_label:
        # NOT MEASURED — the ratio could not be formed at all (a Sharpe was
        # missing, or the in-sample Sharpe was indistinguishable from zero, so
        # there is no in-sample edge to generalize from). Treated as SKIPPED,
        # like the multi-symbol criterion has always treated its own
        # insufficient-data marker; the old single label made one of these a
        # FAILURE and the other a skip. A real in-sample loss now arrives as
        # "✗ IS negatif" and still falls through to the failure branch below.
        _skip("IS/OOS", split_label)
    else:
        evaluated += 1
        # The "robust" and "caution" labels are accepted; a '✗' or the
        # 'yetersiz' (insufficient) marker → failed. NOTE: the label substrings
        # are produced by backtest_robustness.py (out of scope) and matched
        # verbatim here — do not translate the "yetersiz" literal below.
        if "✗" in split_label or "yetersiz" in split_label:
            failed += 1

    # 2) Walk-Forward: ≥50% valid windows with positive OOS alpha.
    # Windows with <3 test trades are statistically unreliable → invalid.
    wfo = rob.get("wfo_windows") or []
    # `test_n_trades` is kept as the fallback count: a window may carry it
    # without a metrics dict (older payloads, and the parallel path's slim rows).
    valid_windows = _valid_wfo_windows(wfo)
    # Does this payload actually HAVE the naive series? If it does, the optimized
    # aggregate must never be used (that is the bug being fixed). If it doesn't —
    # a run from before the naive series existed, or a spec with no optimizable
    # parameter, where the optimized run IS the unmodified spec — the optimized
    # aggregate is the only measurement there is, and it means the same thing.
    _has_naive = any((w.get("test_metrics_naive") or {}) for w in wfo)
    if not wfo or not valid_windows:
        _skip(
            "Walk-Forward",
            "no windows" if not wfo else "no valid window with ≥3 trades",
        )
    else:
        evaluated += 1
        # M28/M594: NAU dispersion-penalized OOS Sharpe (mean − 0.5·std). The
        # manual suite produces this in wfo_aggregate; the agent path
        # (_run_full_robustness) does not call wfo_aggregate, so the field was
        # empty and this branch was dead code — compute it INLINE from
        # wfo_windows here. Otherwise fall back to the positive-window ratio.
        # 2026-08-04: the NAIVE series is read (see _wfo_test) — the optimized
        # aggregate (`oos_sharpe_penalized`) describes a strategy re-fitted every
        # window, which is not what gets saved.
        pen = rob.get("oos_sharpe_naive_penalized")
        if pen is None and not _has_naive:
            pen = rob.get("oos_sharpe_penalized")
        if pen is None:
            _sh = [
                float(_wfo_test(w).get("sharpe"))
                for w in valid_windows
                if _wfo_test(w).get("sharpe") is not None
                and math.isfinite(_wfo_test(w).get("sharpe", float("nan")))
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
        # A positive return in a broad bull market is not a strategy edge.
        # ``run_walk_forward`` stamps buy-and-hold excess for the exact test
        # slice.  Missing alpha is a fail-closed robustness error: otherwise
        # archived/slim rows could silently re-open the old nominal-PnL gate.
        _excess = []
        for w in valid_windows:
            try:
                value = float(_wfo_test(w).get("excess_return_fraction"))
            except (TypeError, ValueError):
                value = float("nan")
            _excess.append(value)
        if any(not math.isfinite(value) for value in _excess):
            failed += 1
            _skip("Walk-Forward alpha", "missing slice-local benchmark/excess")
            _excess = []
        positive = sum(1 for value in _excess if value > 0)
        positive_ratio = positive / len(valid_windows)
        # UI and gate now implement one contract: at least half of the valid
        # windows must be profitable. Penalized OOS Sharpe is an additional
        # stability requirement when available, never an alternative that can
        # make 0/88 profitable windows pass.
        wfo_failed = positive_ratio < 0.5 or (pen is not None and pen <= 0)
        if wfo_failed:
            failed += 1

    # 3) Multi-symbol generalizability
    ms = rob.get("multi_symbol") or {}
    ms_label = ms.get("generalization_label", "")
    # 'yetersiz' substring is produced by backtest_robustness.py; kept verbatim.
    if not ms or not ms_label or ("yetersiz" in ms_label and "✗" not in ms_label):
        _skip("Multi-Symbol", "no section" if not ms else "insufficient data")
    else:
        evaluated += 1
        if "✗" in ms_label:  # symbol-specific strategy is not accepted
            failed += 1

    # 4) Monte Carlo: median drawdown check, plus strict adverse-tail check.
    mc = rob.get("mc") or {}
    if not mc or mc.get("error"):
        _skip("Monte Carlo", str(mc.get("error") or "no section"))
    else:
        evaluated += 1
        dd_p50 = mc.get("max_dd_p50")
        if dd_p50 is not None and dd_p50 < _MC_DD_LIMIT:
            failed += 1
        # ``max_dd_p95`` is deliberately named for the 95% confidence band but
        # is the 5th percentile of signed (negative) max-DD values: the adverse
        # tail.  Old/partial payloads cannot satisfy strict publication because
        # their tail risk is unknown; relaxed research keeps median-only parity.
        if strict:
            dd_p95 = mc.get("max_dd_p95")
            try:
                dd_p95 = float(dd_p95)
            except (TypeError, ValueError):
                dd_p95 = None
            if dd_p95 is None or not math.isfinite(dd_p95):
                failed += 1
                _skip("Monte Carlo tail risk", "missing max_dd_p95")
            elif dd_p95 < _MC_DD_TAIL_LIMIT:
                failed += 1

    if failed:
        return False
    min_required = 3 if strict else 2
    if evaluated < min_required:
        if run_id:
            _add_step(
                run_id,
                f"⚠ Only {evaluated}/4 criteria could be evaluated "
                f"(need ≥{min_required}) — candidate cannot pass",
            )
        return False
    return True


def _holdout_promotion_verdict(
    n_trades: int,
    pnl_pct: float,
    sharpe: float | None,
    excess_pnl_pct: float,
    metrics: dict | None = None,
) -> tuple[bool, str]:
    """Single publication policy for sealed holdout results — pass/fail AND why.

    The reason string must be derived from the exact same HOLDOUT_REQUIRE_*
    flags as the boolean, or the two can disagree: a flag flip changes what
    passes but not the sentence a human reads about why it did or didn't.
    """
    if n_trades < HOLDOUT_MIN_TRADES:
        return False, f"only {n_trades} holdout trades; need {HOLDOUT_MIN_TRADES}"
    if HOLDOUT_REQUIRE_POSITIVE_PNL and pnl_pct <= 0:
        return False, "sealed holdout PnL is not positive"
    if HOLDOUT_REQUIRE_POSITIVE_SHARPE and (sharpe is None or sharpe <= 0):
        return False, "sealed holdout Sharpe is not positive"
    # Mühürlü pencere kapısı, alfa kapısıyla AYNI ölçüyü kullanmalı: ikisi
    # ıraksarsa bir aday sıralamayı geçip yayımda takılır ve kullanıcı iki
    # farklı "buy&hold'u geçti mi" cevabı görür. `metrics` verildiğinde ölçü
    # `_benchmark_rejection` (risk-ayarlı mod dahil); verilmediğinde eski
    # kümülatif kural — çağıranı kırmadan geçiş.
    if HOLDOUT_REQUIRE_POSITIVE_EXCESS:
        if metrics:
            rej = _benchmark_rejection(metrics, excess_pnl_pct)
            if rej is not None:
                return False, f"sealed holdout benchmark leg: {rej}"
        elif excess_pnl_pct <= 0:
            return False, "sealed holdout did not beat buy-and-hold"
    return True, "passed"


def _ms_score_factor(rob: dict | None) -> float:
    """M26+M31: effective-score multiplier ∈ [0.15, 1.0] from multi-symbol pass_rate.

    x = pass_rate - 0.5 (if pass_rate missing, x=0 → neutral 0.575);
    factor = 0.15 + (clamp(x, -0.5, 0.5) + 0.5) × 0.85.
    """
    ms = (rob or {}).get("multi_symbol") or {}
    pr = ms.get("pass_rate")
    # M653: on insufficient data run_multi_symbol returns pass_rate=0.0 + the
    # label '— (yetersiz veri)' — this is NOT a real 0, it means COULD NOT BE
    # EVALUATED. Feeding 0.0 into the factor computation applied the minimum
    # 0.15 penalty instead of neutral (0.575): a candidate whose MS was never
    # measured got clipped by 85% as if it lost on all symbols, losing the
    # among-passers selection. Treat insufficient-data as neutral.
    # ('yetersiz' substring is produced by backtest_robustness.py; kept verbatim.)
    ms_label = ms.get("generalization_label", "") or ""
    insufficient = ("yetersiz" in ms_label) or ms.get("n_valid", 1) == 0
    try:
        if pr is None or insufficient:
            x = 0.0  # neutral → factor 0.575
        else:
            x = float(pr) - 0.5
        if not math.isfinite(x):
            x = 0.0
    except (TypeError, ValueError):
        x = 0.0
    x = max(-0.5, min(0.5, x))
    return 0.15 + (x + 0.5) * 0.85


def _split_holdout(df, min_bars: int = 200):
    """L32: seal off the last OOS_HOLDOUT_DAYS days as a sealed slice.

    Returns ``(trimmed_df, holdout_df | None)``. If the remaining (trimmed) data
    falls below ``min_bars``, the holdout is SKIPPED and the full df is returned
    unchanged.
    """
    import pandas as pd

    from backtest_robustness import WF_EMBARGO_DAYS

    if df is None or len(df) == 0:
        return df, None
    cutoff = df.index[-1] - pd.Timedelta(days=OOS_HOLDOUT_DAYS)
    # M674: NAU purge gap (WF_EMBARGO_DAYS) between train and holdout — so
    # patterns/lookback learned around the cutoff don't leak into the first days
    # of the holdout (NAU fit_end = oos_start − WF_EMBARGO_DAYS).
    train_end = cutoff - pd.Timedelta(days=WF_EMBARGO_DAYS)
    trimmed = df[df.index < train_end]
    if len(trimmed) < min_bars:
        return df, None
    return trimmed, df[df.index >= cutoff]


def _sealed_holdout_stats(
    trades: list[dict] | None, holdout_start_unix: int
) -> tuple[int, float, float | None]:
    """Score ONLY entries inside the sealed window → (n, pnl_fraction, sharpe).

    The holdout run frame carries a warmup lead-in (HOLDOUT_WARMUP_BARS) so
    indicators can produce signals at all; trades ENTERED before the sealed
    boundary belong to that lead-in and must not count. ``pnl_fraction`` is
    against the engine's configured starting capital (``_starting_cash()`` — the
    literal 10_000.0 that used to sit here would have silently misreported the
    sealed holdout the moment STARTING_CASH changed, and this number is the one
    unbiased forward estimate the run produces); sharpe is per-trade
    ((mean/std)·√n), None under 2 trades.
    """
    pnls = [
        float(t.get("pnl") or 0.0)
        for t in (trades or [])
        if int(t.get("entry_time") or 0) >= holdout_start_unix
    ]
    n = len(pnls)
    pnl_fraction = sum(pnls) / _starting_cash()
    if n < 2:
        return n, pnl_fraction, None
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / n
    if var <= 0:
        return n, pnl_fraction, None
    return n, pnl_fraction, (mean / var**0.5) * n**0.5


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
    """L26: best-effort SQLite index at winner + robustness moments.

    An error never breaks the worker: the first error is logged once, later ones
    are swallowed.
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
            logging.warning("could not write agent_index.db (logged once)")


def _llm_cost_usd(
    ti: int, to: int, tcr: int, tcw: int, model: str = ""
) -> tuple[str, float | None]:
    """L38: returns (pricing_model, NOTIONAL Anthropic-list cost USD).

    Pricing comes from ``token_ledger.cost_usd`` — the same single source the
    sidebar badge uses (unlabeled cache writes price at the CLI's 1h TTL).
    Subscription/CLI calls are not billed per token; this is a money SCALE for
    the run, not an invoice — the old code returned None on the CLI backend,
    which left every run's cost line permanently empty. None now only means an
    unpriced model. The model is ``current_model()`` (thread pin dahil).
    """
    if not model:
        try:
            from agent import current_model

            model = current_model()
        except Exception:
            model = "claude-fable-5"
    import token_ledger

    return model, token_ledger.cost_usd(
        {"input": ti, "output": to, "cache_read": tcr, "cache_write": tcw}, model
    )


def _run_cost(state: dict, fallback_model: str = "") -> dict:
    """Koşunun maliyeti — MODEL BAZINDA kırılmış.

    Amaç-başına model eşlemesi (`NAUTILUS_MODEL_BY_PURPOSE`, 2026-08-15) bir
    koşuda birden fazla modelin para harcamasına izin veriyor. Maliyet satırı
    bunu bilmiyordu: toplam token'lar TEK bir modelle fiyatlanıp o modele
    yazılıyordu.

    Ölçülen vaka (koşu 14ff96e7): `pricing_model: 'or:qwen3.8-27b'`,
    `cost_usd: 1.019011`. Sayı doğruydu, etiket yanlıştı — o 1,02 USD tamamen
    Claude'un 7 `custom_block` çağrısının bedeliydi; yerel model bedava. Ekranda
    ise "yerel Qwen 1 dolar yaktı" gibi görünüyordu, yani satır tam da "yerel
    model bedava" olan kararı çürütür gibi duruyordu.

    Burada her modelin dilimi KENDİ fiyatıyla değerlenir: o dilim için sağlayıcı
    maliyet bildirdiyse o kullanılır, yoksa fiyat tablosu. `pricing_model` tek
    model harcadıysa onun adı, birden fazlaysa ``"hibrit (N model)"`` — tek bir
    ad yazmak yalan olurdu.

    Kırılım yoksa (eski koşu kaydı, ya da hiç `by_model` biriktirmemiş bir
    durum) eski tek-model yoluna düşülür: davranış aynen korunur.
    """
    by = state.get("by_model") or {}
    ti = int(state.get("tokens_in") or 0)
    to = int(state.get("tokens_out") or 0)
    tcr = int(state.get("tokens_cache_read") or 0)
    tcw = int(state.get("tokens_cache_write") or 0)
    provider_total = float(state.get("provider_cost_usd") or 0.0)

    if not by:
        model, estimated = _llm_cost_usd(ti, to, tcr, tcw, model=fallback_model)
        cost = provider_total if provider_total > 0 else estimated
        return {
            "pricing_model": model,
            "cost_usd": cost,
            "cost_source": "provider_reported" if provider_total > 0 else "price_table",
            "by_model": {},
        }

    import token_ledger

    breakdown: dict[str, dict] = {}
    total = 0.0
    any_reported = False
    for name, sl in by.items():
        reported = float(sl.get("provider_cost_usd") or 0.0)
        if reported > 0:
            cost, source = reported, "provider_reported"
            any_reported = True
        else:
            cost = token_ledger.cost_usd(
                {
                    "input": sl.get("input", 0),
                    "output": sl.get("output", 0),
                    "cache_read": sl.get("cache_read", 0),
                    "cache_write": sl.get("cache_write", 0),
                },
                name,
            )
            source = "price_table"
        breakdown[name] = {
            **{
                k: sl.get(k, 0)
                for k in ("calls", "input", "output", "cache_read", "cache_write")
            },
            "cost_usd": round(cost, 6) if cost is not None else None,
            "cost_source": source,
        }
        if cost:
            total += float(cost)

    spenders = [n for n, b in breakdown.items() if (b.get("cost_usd") or 0) > 0]
    if len(spenders) == 1:
        label = spenders[0]
    elif len(spenders) > 1:
        label = f"hibrit ({len(spenders)} model)"
    else:
        # Kimse para harcamadıysa (ör. hepsi yerel/bedava) modeli yine de söyle.
        label = next(iter(breakdown), fallback_model or "?")
    return {
        "pricing_model": label,
        "cost_usd": total if breakdown else None,
        "cost_source": "provider_reported" if any_reported else "price_table",
        "by_model": breakdown,
    }


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
    range_start: str = "",
    range_end: str = "",
    model: str = "",
    effort: str = "",
    research_only: bool = False,
    data_truncation: dict | None = None,
) -> None:

    from agent import (
        LLMCallCancelled,
        TerminalLLMError,
        set_thread_effort,
        set_thread_llm_control,
        set_thread_model,
        set_thread_random_seed,
    )

    # Pin THIS worker thread's LLM calls to the picked model (unknown/"" =
    # app default). Thread-local — concurrent surfaces are unaffected.
    set_thread_model(model or None)
    # Aynı kapsamda düşünme bütçesi: model kadar büyük bir maliyet kolu ve
    # ondan bağımsız — ikisi ÇARPILIR (ölçülen: sonnet-5 −61%, üstüne low −52%,
    # bileşik −81%). "" = CLI/uç kendi varsayılanını uygular.
    set_thread_effort(effort or None)
    set_thread_random_seed(run_id)

    is_external = source == "external"
    # Para tavanı faturayı, token tavanı kaçak döngüyü sınırlar (iki birim,
    # bkz. DEFAULT_MAX_COST_USD yorumu). Form bir değer vermiyorsa varsayılan;
    # verdiyse de sert tavanı aşamaz.
    max_total_cost_usd = min(DEFAULT_MAX_COST_USD, HARD_MAX_COST_USD)
    if continuous_mode:
        max_hours = max_hours if max_hours > 0 else DEFAULT_CONTINUOUS_MAX_HOURS
        # Token tavanı artık PARA vekili değil; para tavanı devredeyken kaçak
        # döngü ölçeğine çıkar. Maliyet görünmüyorsa `_budget_breach` bunu
        # BLIND_MAX_TOKENS'a geri indirir — koruma kaybolmaz.
        max_total_tokens = (
            max_total_tokens
            if max_total_tokens > 0
            else (
                RUNAWAY_MAX_TOKENS
                if max_total_cost_usd > 0
                else DEFAULT_CONTINUOUS_MAX_TOKENS
            )
        )

    # M22: optional budget ceilings (0 = unlimited) + winnerless-round breaker.
    # Built AFTER the continuous_mode default-adjustment above — _make_llm_control
    # captures max_hours BY VALUE (unlike the closures it replaced, which read the
    # enclosing scope live), so it must see the already-defaulted value.
    wstate = _WorkerState()
    _llm_cancelled, _llm_observer = _make_llm_control(run_id, max_hours, wstate)

    # Log the session start
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
        # Which endpoint produced the strategies, and at what thinking budget.
        # Both live in `brief` for the cockpit but were missing from the session
        # log, so an archived run could not answer "which model wrote this?" —
        # the one question a research log has to answer.
        model=model,
        effort=effort,
        search_seed=run_id,
        research_only=research_only,
        data_truncation=data_truncation,
    )
    # Which code produced this run. Written as its own event rather than more
    # session_start fields: it is the one record that must survive even when
    # the run dies in its first second, and it is the first thing a reviewer
    # needs before trusting any number below it.
    try:
        from nau_provenance import run_provenance

        _session_log(run_id, "run_env", **run_provenance())
        _session_log(run_id, "run_config", **_effective_run_config())
    except Exception:
        # Provenance is evidence, not a precondition — a run must not fail
        # because git is missing on the host.
        logging.getLogger(__name__).warning(
            "AUTO provenance capture failed for %s", run_id, exc_info=True
        )
    if data_truncation:
        _session_log(run_id, "data_segment_truncated", **data_truncation)
        _add_step(
            run_id,
            "⚠ Catalog history gap: using verified continuous tail "
            f"from {data_truncation['effective_start']}",
        )

    # Sabit aralıklı nabız: olay üretilmeyen aralıklar da kayda geçsin.
    # Progress durumu ZATEN kurulmuş olmalı (route worker'ı başlatmadan önce
    # kuruyor); kurulmamışsa iş parçacığı ilk turda kendini kapatır.
    _start_heartbeat(
        run_id, wstate, continuous_mode=continuous_mode, max_hours=max_hours
    )
    # Nabız "koşu yaşıyor" der; bu ise "yaşamıyorsa NEREDE öldü" der. İkisi ayrı
    # thread — nabzın kendisi sustuğunda da döküm alınabilsin diye.
    _start_stall_watchdog(run_id)

    run_number = 0
    # Round count itself is not the budget; continuous mode is bounded by the
    # normalized time/token ceilings plus the two circuit breakers below.
    _MAX_CONTINUOUS_ROUNDS = 0
    # Circuit breaker: the same error text N consecutive rounds → stop. In
    # unlimited mode this is the ONLY automatic safety net (the 886f439b session
    # kept retrying a persistent "Cache too little data" error in a useless loop
    # — this cuts that off).
    _CONSEC_ERR_LIMIT = 3

    # Continuous mode with no ceiling has exactly two brakes: the winnerless-round
    # breaker and the identical-error breaker. Neither bounds cost — every round
    # spends LLM calls whether or not it produces a candidate. Say so once, in the
    # run's own log, instead of letting it be discovered from the bill.
    if continuous_mode and not max_hours and not max_total_tokens:
        _add_step(
            run_id,
            "⚠ Continuous mode with no time or token ceiling — this run stops "
            f"only on the stop button, {_WINLESS_ROUND_LIMIT} winnerless rounds, "
            f"or {_CONSEC_ERR_LIMIT} identical consecutive errors. Set a limit in "
            "the brief to bound the cost.",
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
                f"Continuous mode: maximum of {_MAX_CONTINUOUS_ROUNDS} rounds reached, stopping.",
            )
            break

        # M22: budget check at round start — if the time/token ceiling is exceeded, finish gracefully.
        _elapsed_h = (time.monotonic() - wstate.worker_t0) / 3600.0
        with _AGENT_LOCK:
            _bs = dict(_AGENT_PROGRESS.get(run_id) or {})
        _budget_reason = None
        if max_hours and max_hours > 0 and _elapsed_h >= max_hours:
            _budget_reason = f"time ceiling ({max_hours:g} hours) reached"
        else:
            # Tek sayaç yerine iki tavan — para ve kaçak döngü ayrı ayrı.
            _budget_reason = _budget_breach(_bs)
        if _budget_reason:
            _add_step(
                run_id,
                f"⏹ Budget: {_budget_reason} — ending the run gracefully.",
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
                started_round=wstate.last_started_round,
                completed_rounds=wstate.completed_rounds,
                total_rounds=wstate.completed_rounds,
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
                s["winner_narrative"] = None  # M2625: new round → fresh narrative
                s["rob_scan_log"] = []
                s["backtest_results"] = []
                for p in s["phases"]:
                    p["status"] = "pending"
                    p["detail"] = ""
            # Timeline: the previous round's open spans are closed with warn; spans
            # are NOT DELETED (they remain with the round tag) — the live view
            # filters to the active round.
            _tl_close_open(run_id, status="warn")
            _add_step(run_id, f"━━━ Continuous mode: round {run_number} starting ━━━")

        # Count only rounds that actually enter the work section. The old final
        # session_end used the pre-incremented run_number, so a stop between
        # rounds reported one phantom round.
        wstate.last_started_round = run_number

        set_thread_llm_control(
            _llm_cancelled,
            _llm_observer,
            lambda request: _admit_llm_budget(run_id, request),
        )

        try:
            # ── Phase 0: Data ────────────────────────────────────────────────────
            # Multi-TF: lazy-load cache per TF. Pre-load the first TF here.
            # Fresh loader every round — its tf_cache/holdout_cache must NOT
            # survive into the next round (matches the original per-round
            # local dicts this replaced).
            tf_loader = _TfLoader(
                run_id,
                run_number,
                is_external,
                instrument_id,
                symbol,
                category,
                range_start,
                range_end,
            )

            if is_external:
                # Narrow down to the timeframes the instrument actually has.
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
                        f"⚠ missing TF skipped for {instrument_id}: {', '.join(skipped)}",
                    )
                if not avail:
                    raise RuntimeError(
                        f"No catalog data for {instrument_id} in the "
                        "selected timeframes"
                    )
                intervals = avail

            first_iv = intervals[0]
            _set_phase(
                run_id,
                0,
                (
                    f"Reading catalog: {instrument_id}/{first_iv}"
                    if is_external
                    else f"Reading cache: {symbol}/{category}/{first_iv}"
                )
                + (f" + {len(intervals) - 1} more TF" if len(intervals) > 1 else ""),
            )
            first_df = tf_loader.get(first_iv)
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
                # Index convention: NO "symbol" key — Bybit-specific chart and
                # robustness OOB panels are triggered via bars_info["symbol"], so
                # they safely stay disabled on external runs.
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

            # ── Phase 1: Initial strategy ────────────────────────────────────────
            spec = _propose_initial_strategy(
                run_id,
                run_number,
                hint,
                web_research,
                is_external,
                instrument_id,
                intervals,
                trend_filter,
                trend_interval,
                wstate,
            )

            with _AGENT_LOCK:
                if run_id in _AGENT_PROGRESS:
                    _AGENT_PROGRESS[run_id]["strategy_name"] = spec.name

            # ── Phase 2: Backtest loop ───────────────────────────────────────────
            # Backtests run in killable child processes (sandbox); the child
            # rebuilds the instrument/bar_type from the recipe, so the worker
            # thread never touches Nautilus objects (nor the GIL) directly.
            _set_phase(run_id, 2, f"0/{n_iterations} completed")

            history: list = []
            results: list[tuple] = []  # (spec, result, iter_iv)
            used_concepts: list[str] = []  # accumulate custom block labels

            for i in range(n_iterations):
                # Check stop signal between iterations in continuous mode.
                # _add_step OUTSIDE THE LOCK: it also acquires _AGENT_LOCK;
                # calling it from inside the block was a re-entrant deadlock (the
                # 2026-07-14 live incident that froze the whole server).
                with _AGENT_LOCK:
                    s = _AGENT_PROGRESS.get(run_id)
                    stop_hit = bool(s is not None and s.get("stop_requested"))
                if stop_hit:
                    _discard_unrun_custom_blocks(spec, run_id)
                    _add_step(run_id, "  ⏹ Stop signal received — breaking the loop")
                    break

                # Round-robin TF selection — one source of truth with the
                # generation side (_iv_for), which the loop must agree with:
                # the spec was written for THIS bar.
                iter_iv = _iv_for(i, run_number, intervals)
                iter_df = tf_loader.get(iter_iv)
                r = _run_backtest_iteration(
                    run_id,
                    run_number,
                    i,
                    n_iterations,
                    spec,
                    iter_iv,
                    iter_df,
                    is_external,
                    symbol,
                    instrument_id,
                    category,
                    wstate,
                    max_hours,
                )
                history.append(r)
                results.append((spec, r, iter_iv))

                if i < n_iterations - 1:
                    spec = _propose_next_strategy(
                        run_id,
                        run_number,
                        i,
                        n_iterations,
                        intervals,
                        hint,
                        history,
                        used_concepts,
                        spec,
                        is_external,
                        instrument_id,
                        trend_filter,
                        trend_interval,
                        wstate,
                    )

            with _AGENT_LOCK:
                _stopped_during_iterations = bool(
                    (_AGENT_PROGRESS.get(run_id) or {}).get("stop_requested")
                )
            _completed_iterations = len(results)
            _done_phase(
                run_id,
                2,
                (
                    f"⏹ {_completed_iterations}/{n_iterations} iterations completed"
                    if _stopped_during_iterations
                    else f"✓ {_completed_iterations}/{n_iterations} iterations completed"
                ),
            )

            # ── Phase 3: Ranking ─────────────────────────────────────────────────
            if _stopped_during_iterations:
                # STOP is terminal for the search. Do not rank a partial round
                # or enter robustness after the user has cancelled it.
                break

            _set_phase(run_id, 3, "Ranking results…")
            _tl_begin(
                run_id,
                "backtest",
                f"rank-r{run_number}",
                "Ranking",
                round_num=run_number,
            )
            ranked, eligible = _rank_and_filter(run_id, results)

            if not eligible:
                _tl_end(run_id, f"rank-r{run_number}", status="warn")
                _done_phase(run_id, 3, _no_eligible_phase_label(results))
                wstate.completed_rounds = run_number
                _cleanup_generated(run_id, wstate)
                if not continuous_mode:
                    with _AGENT_LOCK:
                        if run_id in _AGENT_PROGRESS:
                            _AGENT_PROGRESS[run_id]["done"] = True
                    _session_log(
                        run_id,
                        "session_end",
                        round=run_number,
                        outcome="no_eligible_candidate",
                        started_round=wstate.last_started_round,
                        completed_rounds=wstate.completed_rounds,
                        total_rounds=wstate.completed_rounds,
                    )
                    return
                wstate.consec_err = (
                    0  # round ended without exception — error streak broken
                )
                wstate.last_err_str = None
                if _winless_bump(wstate):
                    _winless_stop(run_id, run_number, wstate)
                    return
                continue
            _tl_end(run_id, f"rank-r{run_number}", status="ok")
            _done_phase(run_id, 3, f"✓ {len(eligible)} candidates ranked")

            # ── Phase 4: Robustness scan ─────────────────────────────────────────
            _set_phase(run_id, 4, f"0/{len(eligible)} trying")
            _set_robustness_scan(run_id, 0, len(eligible))

            winner_spec = None
            winner_result = None
            winner_rob = None
            winner_iv = None
            rob_scan_log: list[dict] = []
            passers: list[dict] = []

            for rank_i, (cand_spec, cand_result, cand_iv) in enumerate(eligible):
                # The stop signal is also checked INSIDE the robustness scan:
                # otherwise, even if the iteration loop is cut by 'stop', the flow
                # would enter the full robustness scan that takes minutes per
                # candidate.
                with _AGENT_LOCK:
                    _rs = _AGENT_PROGRESS.get(run_id)
                    _rob_stop = bool(_rs is not None and _rs.get("stop_requested"))
                if _rob_stop:
                    _add_step(
                        run_id,
                        "  ⏹ Stop signal — breaking the robustness scan",
                    )
                    break
                cand_df = tf_loader.get(cand_iv)
                outcome = _scan_one_candidate(
                    run_id,
                    run_number,
                    rank_i,
                    len(eligible),
                    cand_spec,
                    cand_result,
                    cand_iv,
                    cand_df,
                    strict_mode,
                    wstate,
                    is_external,
                    instrument_id,
                    symbol,
                    category,
                    max_hours,
                    len(passers),
                )
                rob_scan_log.append(outcome.log_entry)
                # Flush partial results after each candidate so data is preserved
                # even if an exception aborts the loop later.
                with _AGENT_LOCK:
                    if run_id in _AGENT_PROGRESS:
                        _AGENT_PROGRESS[run_id]["rob_scan_log"] = list(rob_scan_log)

                if outcome.passed:
                    passers.append(outcome.passer_entry)
                    if len(passers) >= _MAX_PASSERS:
                        _add_step(
                            run_id,
                            f"  {_MAX_PASSERS} passing candidates collected — scan ending",
                        )
                        break

            if passers:
                # The passing candidate with the highest effective score wins.
                best = max(passers, key=lambda p: p["effective"])
                winner_spec = best["spec"]
                winner_result = best["result"]
                winner_rob = best["rob"]
                winner_iv = best["iv"]
                if len(passers) > 1:
                    _add_step(
                        run_id,
                        "🏁 Selection among passers: "
                        + " · ".join(
                            f"{p['spec'].name}={p['effective']:.3f}"
                            f" ({p['score']:.3f}×{p['factor']:.2f})"
                            for p in passers
                        ),
                    )
                _add_step(
                    run_id,
                    f"🏆 Winner: {winner_spec.name} "
                    f"(effective score {best['effective']:.3f})",
                )
                # Update bars_info with the winner's actual TF — not only the TF
                # key but also n_bars/start/end are rebuilt from the winner's df
                # (in multi-TF the first TF's range was shifting the chart URL
                # window and the winner session-log to the wrong range).
                bars_info["granularity" if is_external else "interval"] = winner_iv
                _win_df = tf_loader.get(winner_iv)
                bars_info["n_bars"] = len(_win_df)
                bars_info["start"] = str(_win_df.index[0].date())
                bars_info["end"] = str(_win_df.index[-1].date())

            if winner_spec is None:
                _done_phase(
                    run_id, 4, f"✗ None of the {len(eligible)} candidates passed"
                )
                wstate.completed_rounds = run_number
                _cleanup_generated(run_id, wstate)
                if not continuous_mode:
                    with _AGENT_LOCK:
                        if run_id in _AGENT_PROGRESS:
                            _AGENT_PROGRESS[run_id]["done"] = True
                    _session_log(
                        run_id,
                        "session_end",
                        round=run_number,
                        outcome="no_winner",
                        started_round=wstate.last_started_round,
                        completed_rounds=wstate.completed_rounds,
                        total_rounds=wstate.completed_rounds,
                    )
                    return
                wstate.consec_err = (
                    0  # round ended without exception — error streak broken
                )
                wstate.last_err_str = None
                # M22: 'candidates exist but none passed robustness' is also a
                # winnerless round — the counter must increment here too (the most
                # common infinite-loop scenario).
                if _winless_bump(wstate):
                    _winless_stop(run_id, run_number, wstate)
                    return
                with _AGENT_LOCK:
                    _stop_before_next = bool(
                        (_AGENT_PROGRESS.get(run_id) or {}).get("stop_requested")
                    )
                if _stop_before_next:
                    break
                _add_step(run_id, "Continuous mode: new round starting…")
                continue

            _done_phase(run_id, 4, f"✓ Robustness finalist: {winner_spec.name}")

            # ── Phase 5: Save ────────────────────────────────────────────────────
            _set_phase(run_id, 5, "Running sealed holdout promotion gate…")
            _tl_begin(
                run_id,
                "data",
                f"save-r{run_number}",
                "Sealed holdout promotion gate",
                round_num=run_number,
                name=winner_spec.name,
            )
            gate = _run_promotion_gate(
                run_id,
                run_number,
                winner_spec,
                winner_iv,
                winner_rob,
                tf_loader.holdout_cache,
                wstate,
                is_external,
                instrument_id,
                symbol,
                category,
                max_hours,
                research_only,
            )
            winner_holdout = gate.winner_holdout
            promotion_passed = gate.promotion_passed
            promotion_reason = gate.promotion_reason
            promotion_limit_hit = False

            if promotion_passed:
                # M14: locked append only after the independent promotion gate.
                from composer import append_to_catalog

                append_to_catalog(winner_spec)
                _add_step(run_id, f"✓ {winner_spec.name} → strategy_catalog.json")
                _tl_end(run_id, f"save-r{run_number}", status="ok")
                _done_phase(run_id, 5, f"✓ {winner_spec.name} promoted and saved")
                wstate.winless_rounds = 0
            else:
                _add_step(
                    run_id,
                    f"❌ {winner_spec.name} not published: {promotion_reason}",
                )
                _session_log(
                    run_id,
                    "promotion_rejected",
                    round=run_number,
                    spec_id=winner_spec.id,
                    reason=promotion_reason,
                )
                _tl_end(run_id, f"save-r{run_number}", status="warn")
                _done_phase(
                    run_id, 5, f"❌ Not published: {promotion_reason}", degraded=True
                )
                promotion_limit_hit = _winless_bump(wstate)

            with _AGENT_LOCK:
                if run_id in _AGENT_PROGRESS:
                    # Update bars_info with the winner's TF — for the chart URL
                    winner_result.bars_info = bars_info
                    _AGENT_PROGRESS[run_id]["winner_result"] = winner_result
                    _AGENT_PROGRESS[run_id]["winner_spec_name"] = winner_spec.name
                    _AGENT_PROGRESS[run_id]["winner_spec_id"] = winner_spec.id
                    _AGENT_PROGRESS[run_id]["winner_rob"] = winner_rob
                    _AGENT_PROGRESS[run_id]["winner_holdout"] = winner_holdout
                    _AGENT_PROGRESS[run_id]["done"] = (
                        True  # always set so polling shows result
                    )

            # Only a promoted artifact is a winner. Rejected finalists keep a
            # distinct event so session summaries/catalog counts cannot confuse
            # research output with a publishable strategy.
            _session_log(
                run_id,
                "winner" if promotion_passed else "finalist_rejected",
                round=run_number,
                spec_name=winner_spec.name,
                spec_id=winner_spec.id,
                score=round(_score(winner_result), 4),
                metrics=_thin_curves(winner_result.metrics, cap=_SESSION_CURVE_CAP),
                equity_curve=_thin_pair(
                    winner_result.equity_curve,
                    winner_result.equity_dates,
                    cap=_SESSION_CURVE_CAP,
                )[0],
                bars_info=bars_info,
                promoted=promotion_passed,
                promotion_reason=promotion_reason,
            )
            _cleanup_generated(
                run_id, wstate, winner_spec if promotion_passed else None
            )
            wstate.consec_err = 0  # round finished successfully — error streak broken
            wstate.completed_rounds = run_number
            wstate.last_err_str = None

            if not continuous_mode:
                # Terminal event: all other exit paths (no_winner/error/stopped)
                # write session_end; let the winner path write it too, so external
                # watchers and the /sessions summary can see the session ended.
                _session_log(
                    run_id,
                    "session_end",
                    round=run_number,
                    outcome="winner" if promotion_passed else "promotion_rejected",
                    started_round=wstate.last_started_round,
                    completed_rounds=wstate.completed_rounds,
                    total_rounds=wstate.completed_rounds,
                )
                return

            if promotion_limit_hit:
                _winless_stop(run_id, run_number, wstate)
                return

            # In continuous mode: briefly expose the result then continue
            import time as _time

            _time.sleep(3)  # give polling a chance to render the result
            with _AGENT_LOCK:
                _stop_after_result = bool(
                    (_AGENT_PROGRESS.get(run_id) or {}).get("stop_requested")
                )
            if _stop_after_result:
                break
            _add_step(
                run_id, f"Continuous mode: round {run_number} completed, continuing…"
            )

        except LLMCallCancelled as e:
            _tl_close_open(run_id, status="warn")
            with _AGENT_LOCK:
                if run_id in _AGENT_PROGRESS:
                    _AGENT_PROGRESS[run_id]["done"] = True
                    _AGENT_PROGRESS[run_id]["continuous_finished"] = True
            _add_step(run_id, f"⏹ {e} — provider call cancelled")
            _session_log(
                run_id,
                "session_end",
                round=run_number,
                outcome="stopped",
                started_round=wstate.last_started_round,
                completed_rounds=wstate.completed_rounds,
                total_rounds=wstate.completed_rounds,
            )
            _cleanup_generated(run_id, wstate)
            return
        except AgentBudgetReached as e:
            _tl_close_open(run_id, status="warn")
            with _AGENT_LOCK:
                if run_id in _AGENT_PROGRESS:
                    _AGENT_PROGRESS[run_id]["done"] = True
                    _AGENT_PROGRESS[run_id]["continuous_finished"] = True
            _add_step(run_id, f"⏹ Budget: {e} — ending the run gracefully.")
            _session_log(
                run_id,
                "session_end",
                round=run_number,
                outcome="budget",
                reason=str(e),
                started_round=wstate.last_started_round,
                completed_rounds=wstate.completed_rounds,
                total_rounds=wstate.completed_rounds,
            )
            _cleanup_generated(run_id, wstate)
            return
        except TerminalLLMError as e:
            _tl_close_open(run_id, status="fail")
            err_str = f"{type(e).__name__}: {e}"
            with _AGENT_LOCK:
                if run_id in _AGENT_PROGRESS:
                    _AGENT_PROGRESS[run_id]["error"] = err_str
                    _AGENT_PROGRESS[run_id]["done"] = True
                    _AGENT_PROGRESS[run_id]["continuous_finished"] = True
            _add_step(
                run_id,
                f"⏹ Terminal LLM provider failure — AUTO stopped without fallback: {e}",
            )
            _session_log(
                run_id,
                "session_end",
                round=run_number,
                outcome="terminal_llm_error",
                error=err_str,
                started_round=wstate.last_started_round,
                completed_rounds=wstate.completed_rounds,
                total_rounds=wstate.completed_rounds,
            )
            _cleanup_generated(run_id, wstate)
            return
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
                # `type: message` alone names the symptom; the stack names the
                # LINE. Without it a round that dies in a helper five frames
                # down is only ever debuggable while the process is still up.
                traceback=traceback.format_exc()[-4000:],
                started_round=wstate.last_started_round,
                completed_rounds=wstate.completed_rounds,
                total_rounds=wstate.completed_rounds,
            )
            _cleanup_generated(run_id, wstate)
            if not continuous_mode:
                break
            # Circuit breaker: the same error _CONSEC_ERR_LIMIT consecutive rounds
            # → persistent problem (missing data, config) — retrying is pointless, stop.
            wstate.consec_err = (
                wstate.consec_err + 1 if err_str == wstate.last_err_str else 1
            )
            wstate.last_err_str = err_str
            if wstate.consec_err >= _CONSEC_ERR_LIMIT:
                _add_step(
                    run_id,
                    f"⏹ {_CONSEC_ERR_LIMIT} consecutive identical errors — stopping "
                    f"continuous mode: {err_str[:100]}",
                )
                with _AGENT_LOCK:
                    if run_id in _AGENT_PROGRESS:
                        _AGENT_PROGRESS[run_id]["done"] = True
                break
            _add_step(run_id, f"⚠ Round {run_number} error: {e} — restarting…")
        finally:
            # Token snapshot + session_end at the end of each round
            with _AGENT_LOCK:
                s = _AGENT_PROGRESS.get(run_id) or {}
            _ti = s.get("tokens_in", 0) or 0
            _to = s.get("tokens_out", 0) or 0
            _tcr = s.get("tokens_cache_read", 0) or 0
            _tcw = s.get("tokens_cache_write", 0) or 0
            # Maliyet MODEL BAZINDA kırılır: hibritte (amaç-başına eşleme) bir
            # koşuda birden fazla model harcar ve toplamı tek bir modele yazmak
            # yanlış atıf üretir — bkz. _run_cost.
            _cost = _run_cost(s, fallback_model=model)
            _model = _cost["pricing_model"]
            _cost_usd = _cost["cost_usd"]
            _cost_source = _cost["cost_source"]
            _session_log(
                run_id,
                "token_snapshot",
                round=run_number,
                total_input=_ti,
                total_output=_to,
                cache_read=_tcr,
                cache_write=_tcw,
                pricing_model=_model,
                cost_by_model=_cost["by_model"],
                cost_source=_cost_source if _cost_usd is not None else None,
                cost_usd=round(_cost_usd, 6) if _cost_usd is not None else None,
                cost_eur=round(_cost_usd * 0.91, 6) if _cost_usd is not None else None,
            )
            set_thread_llm_control(None, None, None)

    # Continuous mode exited (stop/budget/winless/error — all breaks land here).
    _tl_close_open(run_id, status="warn")
    with _AGENT_LOCK:
        if run_id in _AGENT_PROGRESS:
            _AGENT_PROGRESS[run_id]["done"] = True
            # Permanent end: the progress route stops polling when it sees this
            # (a separate flag to distinguish it from the inter-round done=True window).
            _AGENT_PROGRESS[run_id]["continuous_finished"] = True
    _cleanup_generated(run_id, wstate)
    _session_log(
        run_id,
        "session_end",
        outcome="stopped",
        started_round=wstate.last_started_round,
        completed_rounds=wstate.completed_rounds,
        total_rounds=wstate.completed_rounds,
    )


def _pick_best_exit_from_history(history: list) -> str | None:
    """Return the exit block type of the highest-scoring strategy in history."""
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
    timeframe: str = "",
):
    """Odd iterations: ask Claude for a novel idea, generate custom entry+exit blocks,
    register them, and return a ComposedStrategySpec built from those blocks.
    Returns None if block generation fails (caller falls back to builtin).

    ``market`` — optional market context (external US equity runs); passed to the
    idea generator, and if None the crypto phrasing is preserved.
    ``timeframe`` — the bar this spec will be scored on (round-robin, known by
    the caller); the idea generator sizes its logic for it.
    """
    from agent import (
        TerminalLLMError,
        _propose_agent_strategy_idea,
        _raise_if_llm_control_abort,
        propose_custom_block,
    )
    from composer import (
        ComposedStrategySpec,
        SignalBlock,
        new_spec_id,
        register_custom_from_disk,
        unregister_custom_block,
    )
    from custom_block_store import save_custom_batch

    try:
        idea = _propose_agent_strategy_idea(
            hint,
            history,
            used_concepts=used_concepts,
            market=market,
            timeframe=timeframe,
        )
        # M1583: the tokens from the idea + custom-block LLM calls should be
        # added to the counters — previously only the builtin proposal was
        # counted; since custom generation is the most token-heavy call, the cost
        # and the max_total_tokens budget breaker were seriously undercounting.
        _enforce_token_budget(run_id)
        # Fikir çağrısı düştüyse buradan çıkan blok, modelin fikri değil kaynak
        # koddaki sabit bir fikrin kodudur — sayılmazsa makul bir yedek gerçek
        # önerilerle aynı sıralamada yarışır ve "kazanan" ilan edilir.
        if idea.get("degraded"):
            _mark_degraded(run_id, idea["degraded"], "strategy idea")
            if idea.get("degraded_terminal"):
                raise TerminalLLMError(
                    idea.get("degraded_detail") or "terminal LLM provider failure"
                )
            # A hard-coded idea is useful as a visible diagnostic, but generating
            # more paid code around it and letting it compete creates a false
            # autonomous winner. Let the caller use its role-safe builtin path.
            return None
        entry_label = idea.get("entry_label", "Agent Entry")
        exit_label = idea.get("exit_label", "Agent Exit")

        # The ROUND belongs in the name. Without it, continuous mode reuses the
        # same identity every round and `save_custom` is last-write-wins: on run
        # 3cad3325 the name `agnt_e_3cad3325_1` was written 7 times with 7
        # different bodies. Two winners saved to the catalog in rounds 2 and 4
        # both pointed at that name, so by round 7 the strategies that had
        # passed robustness no longer existed — the catalog entry resolved, ran,
        # and silently executed someone else's logic.
        # (Names stay within custom_block_store.is_valid_name's 40 chars:
        #  "agnt_e_" + 8 hex + "_r" + round + "_" + iter ≈ 21.)
        entry_name = f"agnt_e_{run_id}_r{round_num}_{iter_idx}"
        exit_name = f"agnt_x_{run_id}_r{round_num}_{iter_idx}"

        _add_step(run_id, f"  ⚙ Generating custom entry block: {entry_label}…")
        entry_block = propose_custom_block(entry_label, idea["entry_desc"], "entry")
        _enforce_token_budget(run_id)
        entry_block["name"] = entry_name

        # With 50% probability use the best builtin exit from history (instead of custom)
        # This combines a proven exit mechanism with new entry ideas.
        # SEEDED on (run_id, iteration): an unseeded coin flip made a run
        # impossible to reproduce — replaying a session could take the other
        # branch and produce a different strategy, so a finding could not be
        # re-derived. Same run + same iteration → same branch, while different
        # iterations and different runs still explore both.
        import random as _random

        best_builtin_exit = _pick_best_exit_from_history(history)
        _coin = _random.Random(f"{run_id}:{iter_idx}").random()
        use_builtin_exit = best_builtin_exit is not None and _coin < 0.5

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
                f"  → Using builtin exit: {best_builtin_exit} (successful in the past)",
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
            _add_step(run_id, f"  ⚙ Generating custom exit block: {exit_label}…")
            exit_block = propose_custom_block(exit_label, idea["exit_desc"], "exit")
            _enforce_token_budget(run_id)
            exit_block["name"] = exit_name

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
        # Entry and exit form one candidate transaction. A new custom type must
        # be registered before ComposedStrategySpec.validate can resolve it;
        # validating first caused the live ``Unknown block type`` fallback.
        # save_custom_batch keeps the disk registry transaction open while the
        # callback registers both types and validates the composed spec.
        generated = [
            {
                "name": entry_name,
                "meta": entry_block["meta"],
                "code": entry_block["code"],
                "prompt": hint,
                "role": "entry",
                "label": entry_label,
                "usage": entry_block.get("usage"),
                "repair": entry_block.get("repair"),
            }
        ]
        if not use_builtin_exit:
            generated.append(
                {
                    "name": exit_name,
                    "meta": exit_block["meta"],
                    "code": exit_block["code"],
                    "prompt": hint,
                    "role": "exit",
                    "label": exit_label,
                    "usage": exit_block.get("usage"),
                    "repair": exit_block.get("repair"),
                }
            )
        registered: list[str] = []

        def _register_and_validate() -> None:
            try:
                for block in generated:
                    register_custom_from_disk(block["name"])
                    registered.append(block["name"])
                err = spec.validate()
                if err:
                    raise RuntimeError(f"Custom spec invalid: {err}")
            except Exception:
                for name in reversed(registered):
                    unregister_custom_block(name)
                registered.clear()
                raise

        save_custom_batch(generated, validate=_register_and_validate)
        for block in generated:
            _add_step(
                run_id, f"  ✓ {block['role'].title()} block saved: {block['name']}"
            )
            _session_log(
                run_id,
                "custom_block_generated",
                iteration=iter_idx,
                round=round_num,
                name=block["name"],
                role=block["role"],
                label=block["label"],
                meta=block["meta"],
                code=block["code"],
                usage=block["usage"],
                repair=block.get("repair"),
            )
        try:
            blocks_dir = SESSION_LOG_DIR / f"{run_id}_blocks"
            blocks_dir.mkdir(parents=True, exist_ok=True)
            for block in generated:
                (blocks_dir / f"{block['name']}.py").write_text(
                    block["code"], encoding="utf-8"
                )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "AUTO forensic .py copy write failed for %s", run_id, exc_info=True
            )
            with _AGENT_LOCK:
                state = _AGENT_PROGRESS.get(run_id)
                if state is not None:
                    state["audit_degraded"] = True
                    state["audit_error"] = f"Forensic block copy unavailable: {exc}"
        return spec

    except (TerminalLLMError, AgentBudgetReached):
        raise
    except Exception as e:
        # `except (GeneratedCodeError, Exception)` was the same thing written
        # twice — Exception already covers GeneratedCodeError. The name is kept
        # in the message so a code-generation failure stays distinguishable from
        # an infrastructure one in the log.
        #
        # STOP bir bozulma DEĞİL: `LLMCallCancelled` bu demette olmadığı için
        # geniş `except` onu yutuyor ve iptal "builtin'e düşüldü" uyarısına
        # iniyordu — kullanıcı Stop'a bastıktan sonra koşu bir tam backtest
        # turu daha harcıyordu. `agent.py` aynı durumda bu yardımcıyı çağırıyor.
        _raise_if_llm_control_abort(e)
        # Bu yol sayaca hiç girmiyordu: ölçülen koşuda 6 kez tetiklendi ve
        # `fallback_count` 5'te kaldı (gerçek bozulma 13). Sayılmayan bozulma
        # koşuyu başarı gibi gösterir — üretilen blok modelin özgün fikri değil,
        # katalogdan hazır bir bloktur.
        _mark_degraded(run_id, type(e).__name__, "custom block generation")
        _add_step(
            run_id,
            f"  ⚠ Could not generate custom block ({type(e).__name__}): {e} "
            "— falling back to builtin",
        )
        return None


def _cleanup_agent_blocks(run_id: str) -> None:
    """Legacy hook kept for compatibility; run-specific blocks are retained."""
    return None


def _discard_unrun_custom_blocks(spec, run_id: str) -> None:
    """Delete only this run's generated blocks when stop precedes backtest."""
    from custom_block_store import delete_custom

    prefix_entry = f"agnt_e_{run_id}_"
    prefix_exit = f"agnt_x_{run_id}_"
    for block in getattr(spec, "blocks", []) or []:
        name = str(getattr(block, "type", ""))
        if name.startswith((prefix_entry, prefix_exit)):
            delete_custom(name)
            _session_log(run_id, "custom_block_discarded", name=name, reason="not run")


def _agent_worker_with_slot(**kwargs) -> None:
    """Release the process-wide AUTO slot even if the worker fails unexpectedly."""

    try:
        _agent_worker(**kwargs)
    finally:
        _AUTO_RUN_SLOTS.release()


@router.get("/tokens")
async def tokens(request: Request):
    """Per-model LLM token usage from the persistent ledger (token_ledger).

    Unlike the in-memory AUTO-loop counters (per-run, single-model, lost on
    restart), this reads ``~/.cache/nautilus_web_app/token_usage.jsonl`` where
    EVERY LLM call (AUTO + describe/plan/chat + narratives) is recorded with the
    model the API actually answered with. ``?format=json`` returns the raw
    breakdown; the default is a plain-text table (curl-friendly)."""
    from fastapi.responses import JSONResponse, PlainTextResponse

    import token_ledger

    if request.query_params.get("format") == "json":
        return JSONResponse(token_ledger.summary())
    return PlainTextResponse(token_ledger.format_table())


@router.get("", response_class=HTMLResponse)
async def page(request: Request):

    # If there is an unfinished run, AUTOMATICALLY bind the page to it — so after
    # a server restart / tab refresh the user sees the running run (even if the
    # run was started from the API, i.e. not from this browser).
    active_run_id = newest_active_run_id()

    from agent import selectable_models

    return templates.TemplateResponse(
        request,
        "agent_backtest.html",
        {
            "active": "agent",
            "page_title": "Autonomous Backtest Agent",
            "market": get_market_info(),
            "active_run_id": active_run_id,
            "llm_models": selectable_models(),
        },
    )


class _ExternalDataDecision(NamedTuple):
    intervals: list[str]
    trend_interval: str
    research_only: bool
    auto_continuous_start: str | None
    data_truncation: dict | None


def _validate_external_data(
    instrument_id: str,
    chosen: list[str],
    is_multi_tf: bool,
    ext_interval: str,
    ext_trend_interval: str,
    range_start: str,
    range_end: str,
) -> _ExternalDataDecision | HTMLResponse:
    """Fail-closed integrity gate for external (non-Bybit) instrument data.

    Known-unadjusted equity series, intraday/day gaps, and stale catalog
    segments must all block autonomous strategy selection before a worker is
    even launched — split/dividend jumps or a spliced-in disconnected segment
    can be ranked as false edge. Returns an HTMLResponse directly on any
    refusal so the route can just `return` it, or the decision run() needs to
    proceed.
    """
    from data import (
        EXTERNAL_GRAN_BY_BYBIT_CODE,
        _external_adjusted_flag,
        _external_bar_dir,
        external_data_gap_report,
        load_external_bars,
    )

    mapped = [
        EXTERNAL_GRAN_BY_BYBIT_CODE[t]
        for t in chosen
        if t in EXTERNAL_GRAN_BY_BYBIT_CODE
    ]
    intervals: list[str] = (
        ["1-HOUR", "4-HOUR", "1-DAY"] if is_multi_tf else (mapped or [ext_interval])
    )
    trend_interval = ext_trend_interval
    research_only = False
    auto_continuous_start: str | None = None
    data_truncation: dict | None = None

    # Known-unadjusted equity series are unsuitable for autonomous strategy
    # selection: splits/dividends can be ranked as edge. Fail closed before
    # launching a worker. A deliberate research-only override is available
    # via env and remains visible in the run log through data.py warnings.
    # Two-key opt-in: a stale PM2 environment containing the historical
    # one-key override must not silently reopen unadjusted AUTO after a
    # restart. Research mode has to be asserted independently each time.
    _allow_unadjusted = (
        os.environ.get("AGENT_ALLOW_UNADJUSTED", "").strip() == "1"
        and os.environ.get("AGENT_RESEARCH_MODE", "").strip() == "1"
    )
    for _iv in intervals:
        _located = _external_bar_dir(instrument_id, _iv)
        _known_unadjusted = bool(
            _located is not None
            and _external_adjusted_flag(_located[0], instrument_id) is False
        )
        if _known_unadjusted:
            if not _allow_unadjusted:
                return error_html(
                    "⚠ AUTO refused known-unadjusted equity data for {iid} ({iv}). "
                    "Use an adjusted catalog; split/dividend jumps can create "
                    "false edge. Research-only override requires "
                    "AGENT_ALLOW_UNADJUSTED=1 and AGENT_RESEARCH_MODE=1.",
                    400,
                    iid=instrument_id,
                    iv=_iv,
                )
            research_only = True
        try:
            # Aralık daraltması ARTIK yükleyicinin içinde: eskiden tüm seri
            # okunup SONRADAN iki pandas maskesiyle kırpılıyordu, yani 1-DAKİKA
            # bir seride milyonlarca satırlık iki tam kare kopyası boşuna
            # üretiliyordu. `load_external_bars` start/end'i kendi içinde
            # uyguluyor (aynı UTC indeksi, aynı karşılaştırma).
            #
            # `end` neden "+1 gün − 1 ns": eski maske ŞU KESİN eşitsizlikti
            # `index < range_end + 1 gün`; yükleyici ise KAPSAYICI `index <= end`
            # uyguluyor. Nautilus/pandas indeksi nanosaniye çözünürlüklü olduğu
            # için `< X` ile `<= X - 1ns` birebir aynı kümeyi seçer.
            # Yan etki (kasıtlı): data.py'nin günlük "split şüphesi" uyarısı
            # artık tüm tarihi değil KOŞUNUN KULLANACAĞI pencereyi tarıyor.
            # Sert kapı olan `_external_adjusted_flag` yukarıda ayrıca ve tam
            # seri üzerinde kontrol ediliyor, o zayıflamıyor.
            _start_ts = pd.Timestamp(range_start, tz="UTC") if range_start else None
            _end_ts = (
                pd.Timestamp(range_end, tz="UTC")
                + pd.Timedelta(days=1)
                - pd.Timedelta(nanoseconds=1)
                if range_end
                else None
            )
            _validation_df = load_external_bars(
                instrument_id, _iv, start=_start_ts, end=_end_ts
            )
            _gap = external_data_gap_report(_validation_df, granularity=_iv)
        except Exception as _gap_exc:
            # `instrument_id` bir Form alanı ve bu dal, YÜKLEYİCİ PATLADIĞINDA
            # çalışıyor — yani var olmayan (dolayısıyla tamamen serbest) bir id
            # buraya düşer. `_gap_exc` metni de aynı id'yi taşıyor.
            return error_html(
                "⚠ AUTO could not validate external data integrity for "
                "{iid} ({iv}): {exc}",
                400,
                iid=instrument_id,
                iv=_iv,
                exc=_gap_exc,
            )
        if _gap:
            _fingerprint = (
                instrument_id,
                _gap.get("from"),
                _gap.get("to"),
                int(_gap.get("days") or 0),
            )
            _tail_start = _KNOWN_CONTINUOUS_TAIL_GAPS.get(_fingerprint)
            # A UI may submit a default end date even when the operator did
            # not choose a historical window.  It is still safe to retain
            # the known recent tail whenever the requested start precedes
            # it and the requested end does not exclude it.
            if (
                _tail_start
                and (not range_start or range_start < _tail_start)
                and (not range_end or range_end >= _tail_start)
            ):
                _tail = _validation_df[
                    _validation_df.index
                    >= pd.Timestamp(_tail_start, tz=_validation_df.index.tz)
                ]
                if len(_tail) < 200 or external_data_gap_report(_tail, granularity=_iv):
                    return HTMLResponse(
                        "<div class='empty-state'>⚠ AUTO could not retain a sufficiently "
                        "long, continuous post-gap external segment.</div>",
                        status_code=400,
                    )
                if auto_continuous_start is None or _tail_start > auto_continuous_start:
                    auto_continuous_start = _tail_start
                data_truncation = {
                    "policy": "known_continuous_tail_v1",
                    "instrument_id": instrument_id,
                    "granularity": _iv,
                    "gap": _gap,
                    "effective_start": _tail_start,
                    "discarded_bars": int(len(_validation_df) - len(_tail)),
                }
                continue
            if _gap.get("kind") == "intraday":
                _gap_text = (
                    f"a {_gap['gap_minutes']}-minute intraday gap for "
                    f"{instrument_id} ({_iv}), {_gap['from']} → {_gap['to']}"
                )
            else:
                _gap_text = (
                    f"a {_gap['days']}-day gap for {instrument_id} ({_iv}), "
                    f"{_gap['from']} → {_gap['to']}"
                )
            return error_html(
                "⚠ AUTO refused external data with {gap}. Repair/backfill the "
                "catalog before autonomous research.",
                400,
                gap=_gap_text,
            )
    return _ExternalDataDecision(
        intervals, trend_interval, research_only, auto_continuous_start, data_truncation
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
    # Boş kutu = "formdan tavan koyma". `float`/`int` olarak bildirildiklerinde
    # boş bir number input'u ("" gönderir) 422 veriyordu, yani START hiç
    # koşmuyordu; brief bu iki alanı boş açtığı için tolerans burada.
    max_hours: str = Form(default=""),
    max_total_tokens: str = Form(default=""),
    range_start: str = Form(default=""),
    range_end: str = Form(default=""),
    tfs: list[str] = Form(default=[]),
    model: str = Form(default=""),
    effort: str = Form(default=""),
    view: str = Form(default=""),
):
    from agent import resolve_effort

    # `hint` her iterasyonun prompt'una yeniden ekleniyor: burada sınır YOKTU,
    # yani yapıştırılan çok megabaytlık bir metnin maliyeti tur sayısıyla
    # ÇARPILIYORDU. Diğer LLM uçlarının hepsi 4000'de kesiyordu; AUTO'nunki
    # kesmiyordu (DeepR 2026-08-11 [DÜŞÜK]).
    hint = hint.strip()
    if len(hint) > MAX_LLM_TEXT_LEN:
        return error_html(
            "Hint is too long ({n} characters, max {cap}) — it is re-sent with "
            "every iteration, so the cost multiplies by the round count.",
            400,
            n=len(hint),
            cap=MAX_LLM_TEXT_LEN,
        )

    # Form değeri ile FİİLEN uygulanacak değer aynı şey değil — brief'e (yani
    # kokpitin gösterdiği şeye) çözülmüş hâli yazılır, ham girdi değil.
    effort = resolve_effort(effort)

    # Optional [start, end] data window (blank side = open-ended). Validate up
    # front — a malformed date should fail the request, not the worker thread.
    range_start, range_end = range_start.strip(), range_end.strip()
    for _v in (range_start, range_end):
        if _v:
            try:
                datetime.strptime(_v, "%Y-%m-%d")
            except ValueError:
                # En keskin sink buydu: `_v` serbest bir Form değeri, dal tam
                # olarak "tarih ayrıştırılamadı"da çalışıyor VE studio.html
                # /agent/run'ın 4xx gövdesini bilerek DOM'a basıyor
                # (`shouldSwap = true`, `allowScriptTags` açık).
                return error_html("invalid date: {v}", 400, v=_v)
    if range_start and range_end and range_start > range_end:
        return HTMLResponse(
            "<div class='empty-state'>Start date is after end date.</div>",
            status_code=400,
        )

    # Seçilen model gerçekten koşabiliyor mu? Koşamıyorsa (ör. OpenRouter
    # anahtarı/paketi yok) koşuyu BAŞLATMA: LLM'siz bir AUTO turu, seçilen
    # modelin önerileri yerine rastgele kompozisyon üretip bunu normal bir koşu
    # gibi gösteriyor. Ağa çıkmaz — yalnız yapılandırma yoklanır.
    model = model.strip()
    from agent import model_label, model_unavailable_reason

    _why = model_unavailable_reason(model)
    if _why:
        # `model_label` bilinmeyen bir id'yi `OR · <girdi>` diye AYNEN geri
        # veriyor — yani Form değeri buradan yansıyor. `<small>` etiketi
        # şablonun kendi HTML'i, `{why}` ise veri: safe_html tam olarak bu
        # ayrımı koruyor.
        return error_html(
            "⚠ {label} kullanılamıyor — koşu başlatılmadı.<br><small>{why}</small>",
            400,
            label=model_label(model),
            why=_why,
        )

    n_iterations = max(2, min(15, n_iterations))
    # Boş/eksik alan 0'a çözülür (aşağıdaki "0 = güvenli azami" yolu). Gerçekten
    # bozuk bir değer sessizce 0 OLMAZ: sessiz 0, kullanıcının yazdığı tavanı
    # kaldırıp koşuyu sert tavana kadar açardı — bu yüzden 400.
    try:
        parsed_hours = float(max_hours.strip() or 0)
        parsed_tokens = int(max_total_tokens.strip() or 0)
        # NEGATİF de bozuktur ve `float("-1")` istisna atmaz. Aşağıdaki
        # "0 = güvenli azami" yolu `> 0` diye baktığı için negatif bir değer
        # sessizce SERT TAVANA açardı — yani yukarıdaki gerekçenin (yazılan
        # tavanın sessizce kalkması) tam olarak gerçekleştiği hâl, sadece
        # farklı bir girdiyle. Tarayıcı `min=` ile engelliyor; bu kapı zaten
        # elle kurulmuş istekler için var.
        if parsed_hours < 0 or parsed_tokens < 0:
            raise ValueError("negative budget")
    except ValueError:
        return error_html(
            "invalid budget: max_hours={h} · max_total_tokens={t}",
            400,
            h=max_hours,
            t=max_total_tokens,
        )
    max_hours, max_total_tokens = parsed_hours, parsed_tokens
    # A client may lower the budget but cannot disable server-side safety caps.
    # ``0`` selects the safe maximum for both single and continuous AUTO runs.
    max_hours = min(
        HARD_MAX_AUTO_HOURS,
        max_hours if max_hours > 0 else HARD_MAX_AUTO_HOURS,
    )
    max_total_tokens = min(
        HARD_MAX_AUTO_TOKENS,
        max_total_tokens if max_total_tokens > 0 else HARD_MAX_AUTO_TOKENS,
    )
    is_strict = strict_mode != "relaxed"
    use_trend_filter = trend_filter == "1"
    is_continuous = continuous == "1"
    is_multi_tf = multi_tf == "1"
    use_web_research = web_research == "1"
    if is_continuous:
        # Preserve the existing continuous defaults while the hard caps above
        # protect every run type from oversized hand-crafted form values.
        max_hours = min(max_hours, DEFAULT_CONTINUOUS_MAX_HOURS)
        # Token tavanı para vekili olmaktan çıktı: para tavanı devredeyken
        # kaçak-döngü ölçeğine çıkar (bkz. _continuous_token_cap).
        max_total_tokens = min(max_total_tokens, _continuous_token_cap())
    # App-wide convention: a DOTTED id (QQQ.NASDAQ) is an external-catalog
    # instrument — the studio AUTO cockpit posts it through the plain symbol
    # select, so promote it here instead of requiring a separate source field.
    symbol = symbol.strip()
    instrument_id = instrument_id.strip()
    if source != "external" and "." in symbol:
        source, instrument_id = "external", symbol
    is_external = source == "external"

    if is_external and not instrument_id:
        return HTMLResponse(
            "<div class='empty-state'>Select an instrument for the catalog source.</div>",
            status_code=400,
        )

    # Intervals to try in Multi-TF mode
    # 15m removed (6% success), Daily added (clean signal, few trades but quality)
    # Explicit TF multi-select (studio AUTO tab chips) beats the single
    # `interval` select; the legacy Multi-TF checkbox keeps its curated trio.
    # Unknown codes are dropped (defense against hand-crafted posts).
    _allowed = ("1", "5", "15", "30", "60", "240", "720", "D")
    chosen = [t for t in tfs if t in _allowed]
    research_only = False
    # A catalog can contain an old, disconnected segment (for example a ticker
    # rename).  Do not splice it into the current series.  When the user did
    # not request a window, AUTO may safely use the newest continuous segment;
    # an explicit historical request still fails closed below.
    auto_continuous_start: str | None = None
    data_truncation: dict | None = None
    if is_external:
        # `_validate_external_data` her interval için parquet'ten tam seri
        # okuyup pandas ile tarıyor (multi-TF'te üç kez) — koroutin gövdesinde
        # çalışırken POST /agent/run saniyelerce event loop'u tutuyordu ve açık
        # HER sekmedeki 1 saniyelik HTMX poll'u bu süre boyunca donuyordu.
        # Bu dosyadaki diğer ağır işlerle aynı kalıp: iş parçacığına taşı.
        import asyncio

        decision = await asyncio.to_thread(
            _validate_external_data,
            instrument_id,
            chosen,
            is_multi_tf,
            ext_interval,
            ext_trend_interval,
            range_start,
            range_end,
        )
        if isinstance(decision, HTMLResponse):
            return decision
        intervals = decision.intervals
        trend_interval = decision.trend_interval
        research_only = decision.research_only
        auto_continuous_start = decision.auto_continuous_start
        data_truncation = decision.data_truncation
        if auto_continuous_start:
            range_start = auto_continuous_start
    else:
        intervals = ["60", "240", "D"] if is_multi_tf else (chosen or [interval])

    if not _AUTO_RUN_SLOTS.acquire(blocking=False):
        return HTMLResponse(
            "<div class='empty-state'>⚠ AUTO capacity reached — another autonomous "
            "research run is active. Stop it or wait for completion before starting "
            "a new run.</div>",
            status_code=429,
        )

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
            # Audit logging is best-effort for liveness, but its failure must
            # remain visible because JSONL is the post-restart evidence trail.
            "audit_degraded": False,
            "audit_error": None,
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
            # Token usage (accumulated from each Claude API call)
            "tokens_in": 0,
            "tokens_out": 0,
            "tokens_cache_read": 0,
            "tokens_cache_write": 0,
            "provider_cost_usd": 0.0,
            # Degradasyon sayacı: kaç üretim adımı fallback'e düştü ve neden.
            # Bir koşunun "başarılı" görünmesi fazların ✓ kapanmasına bakar; bu
            # sayaç o görüntünün karşı ağırlığı (bkz. _mark_degraded).
            "fallback_count": 0,
            "fallback_reasons": [],
            # For the live backtest results table (agent screen top panel)
            "backtest_results": [],
            # AUTO Mission Control: read-only BRIEF rail + budget gauges.
            # Snapshotted at start so the cockpit never re-derives the request
            # form; the worker only reads it.
            "brief": {
                "symbol": instrument_id if is_external else symbol,
                "category": category,
                "model": model.strip(),
                # Effort KOŞU DURUMUNA yazılır (thread-local'dan yeniden
                # çözülmez: kokpiti besleyen HTTP thread'i worker'ın pin'ini
                # göremez — maliyet rozetinin 3,33× şişmesi tam olarak buydu).
                # Değer route girişinde `resolve_effort`'tan geçti: ekran
                # istenen değil UYGULANAN ayarı gösterir.
                "effort": effort,
                "robustness": "Strict" if is_strict else "Relaxed",
                "range": (
                    f"{range_start or '…'} → {range_end or '…'}"
                    if (range_start or range_end)
                    else "max"
                ),
                "range_start": range_start,
                "range_end": range_end,
                "data_truncation": data_truncation,
                "iterations": n_iterations,
                "guidance": hint.strip(),
                "tfs": list(chosen) or [interval],
                "intervals": list(intervals),
                "continuous": is_continuous,
            },
            "started_at": datetime.now(UTC).timestamp(),
            "max_hours": max_hours,
            "max_total_tokens": max_total_tokens,
            "max_total_cost_usd": _default_cost_cap(),
            "research_only": research_only,
            # Timeline spans (SVG Gantt — see _tl_begin/_tl_end)
            "timeline": [],
        },
        on_evict=_release_session_lock,
    )
    if not created:
        _AUTO_RUN_SLOTS.release()
        return HTMLResponse(
            "<div class='empty-state'>⚠ 50 active runs limit — could not start a "
            "new run. Stop one of the existing runs first.</div>",
            status_code=429,
        )

    threading.Thread(
        target=_agent_worker_with_slot,
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
            range_start=range_start,
            range_end=range_end,
            model=model.strip(),
            effort=effort.strip(),
            research_only=research_only,
            data_truncation=data_truncation,
        ),
        daemon=True,
    ).start()

    # Same locked-snapshot pattern as /progress: render a consistent copy instead
    # of handing the live dict to the template while the worker mutates it concurrently.
    with _AGENT_LOCK:
        _raw0 = _AGENT_PROGRESS[run_id]
        _initial_state = {
            **_raw0,
            "phases": [dict(p) for p in _raw0["phases"]],
            "steps": list(_raw0["steps"]),
        }
    if view == "mission":
        from web.mission import mission_view

        return templates.TemplateResponse(
            request,
            "fragments/auto_mission.html",
            {"mv": mission_view(_initial_state, run_id=run_id)},
        )

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
    """Send a stop signal to the running agent (continuous mode AND single run).

    stop_requested; checked in the iteration loop, at round start, and in the
    robustness candidate scan → stops cleanly once the current step finishes. The
    button is rendered in fragments/agent_progress.html (during the run) and in
    agent_result.html (continuous mode, between rounds).
    """
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s:
            s["stop_requested"] = True
    if s is None:
        # L16: state not in memory (restart/eviction) — an honest message instead
        # of a fake "signal sent" badge.
        return HTMLResponse(
            "<span class='badge' style='background:rgba(148,163,184,0.2);"
            "color:#94a3b8;'>⚠ Run not in memory (the server may have been "
            "restarted) — there is no operation to stop</span>"
        )
    return HTMLResponse(
        "<span class='badge' style='background:rgba(251,146,60,0.2);color:#fb923c;'>"
        "⏹ Stop signal sent — the active provider/backtest step is being "
        "cancelled or will finish first</span>"
    )


def _terminal_message(run_id: str) -> str:
    """Honest message for a run not in memory.

    Distinguishes by the last event of the on-disk session log: a run that has
    seen session_end really finished; if the log is cut off mid-way the process
    died (typical cause: the server was restarted) — the run is not 'completed'.
    """
    generic = "The run completed or timed out."
    # DeepR 2026-08-09 [ORTA]: run_id reaches here straight from the
    # /agent/progress/{run_id} path parameter, unvalidated — a path-traversal
    # run_id turned this into a file-existence oracle for arbitrary paths
    # (the message differs by whether SESSION_LOG_DIR/{run_id}.jsonl exists).
    # Same format sessions.py's routes already enforce for the same concept.
    if not all(c in "0123456789abcdef" for c in run_id) or len(run_id) != 8:
        return generic
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
                "The run completed — you can review its history in the "
                "<a href='/sessions'>Session Logs</a>."
            )
        return (
            "⚠ The run was cut off mid-way (most likely the server was "
            "restarted). The steps up to where it stopped are in the "
            "<a href='/sessions'>Session Logs</a>; you can restart the "
            "agent."
        )
    except Exception:
        return generic


@router.get("/progress/{run_id}", response_class=HTMLResponse)
async def progress(request: Request, run_id: str):
    import asyncio

    from web.viewmodels import iteration_row

    # AUTO Mission Control asks for its own fragment (?view=mission); the
    # standalone /agent page keeps the classic vertical progress view. Same
    # state, two presentations — no duplicated polling endpoint.
    is_mission = request.query_params.get("view") == "mission"

    with _AGENT_LOCK:
        raw = _AGENT_PROGRESS.get(run_id)
        if raw is None:
            # Run already cleaned up or unknown — return terminal no-poll frame
            if is_mission:
                return HTMLResponse(
                    "<div id='agent-progress-panel' class='mc'>"
                    "<div class='empty-state' style='margin:auto;'>"
                    f"{_terminal_message(run_id)}"
                    "</div></div>"
                )
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
            "audit_degraded": raw.get("audit_degraded", False),
            "audit_error": raw.get("audit_error"),
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
            # DeepR 2026-08-14: bu ikisi snapshot'a KOPYALANMIYORDU, dolayısıyla
            # aşağıdaki `state.get(...)` okumaları daima None görüyordu.
            #  · continuous_finished: `is_continuous` hiç False'a dönmüyordu, yani
            #    kalıcı biten bir continuous koşu terminal fragmanı sonsuza dek
            #    yeniden render ediyordu (titreme + boşuna CPU/ağ) — tam olarak
            #    satır 4798'deki yorumun ÖNLEMEYİ amaçladığı davranış.
            #  · provider_cost_usd: maliyet rozetinin "provider_reported" dalı ölü
            #    koddu; rozet, sağlayıcı gerçek maliyeti bildirdiğinde bile hep
            #    fiyat-tablosu TAHMİNİNİ gösteriyordu (oturum logundaki
            #    token_snapshot ise doğru kaynağı yazıyordu — iki yüzey aynı koşu
            #    için farklı maliyet gösterebiliyordu).
            "continuous_finished": raw.get("continuous_finished", False),
            "provider_cost_usd": raw.get("provider_cost_usd", 0.0),
            "tokens_in": raw.get("tokens_in", 0),
            "tokens_out": raw.get("tokens_out", 0),
            "tokens_cache_read": raw.get("tokens_cache_read", 0),
            "tokens_cache_write": raw.get("tokens_cache_write", 0),
            "fallback_count": raw.get("fallback_count", 0),
            "fallback_reasons": list(raw.get("fallback_reasons") or []),
            "backtest_results": list(raw.get("backtest_results") or []),
            # Shallow copies — the worker mutates concurrently.
            "timeline": [dict(sp) for sp in raw.get("timeline") or []],
            # AUTO Mission Control inputs. Snapshotted at run start and
            # never mutated by the worker, but copied here so the cockpit
            # always renders one consistent frame.
            "brief": dict(raw.get("brief") or {}),
            "started_at": raw.get("started_at"),
            "max_hours": raw.get("max_hours", 0.0),
            "max_total_tokens": raw.get("max_total_tokens", 0),
        }

    # If continuous mode has PERMANENTLY finished, stop polling — otherwise the
    # terminal result fragment would be re-rendered/chart-rebuilt every 2s forever
    # (flicker + constant CPU/network). continuous_finished is set when the worker
    # leaves the loop for good; it stays False during the inter-round (done=True +
    # 3s sleep) window, so the transition to the next round is preserved.
    is_continuous = state.get("continuous_mode", False) and not state.get(
        "continuous_finished"
    )

    # Timeline render model (filtered to the active round) + span→step mapping.
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

    # L38: token usage + model-based ESTIMATED cost (_MODEL_PRICING).
    # On a claude-cli/OAuth subscription there is NO per-token billing → cost None
    # (templates hide the line; when filled it is shown with an '≈ estimated' label).
    _USD_EUR = 0.91
    _ti = state.get("tokens_in", 0) or 0
    _to = state.get("tokens_out", 0) or 0
    _tcr = state.get("tokens_cache_read", 0) or 0
    _tcw = state.get("tokens_cache_write", 0) or 0
    _pricing_model = str((raw.get("brief") or {}).get("model") or "")
    # Oturum logundaki token_snapshot ile AYNI hesap: iki yüzey aynı koşu için
    # farklı maliyet göstermesin (bu daha önce bir kez yaşandı, bkz. 5065
    # civarındaki DeepR notu).
    _cost = _run_cost(state, fallback_model=_pricing_model)
    _model = _cost["pricing_model"]
    _cost_usd = _cost["cost_usd"]
    token_info = {
        "input": _ti,
        "output": _to,
        "cache_read": _tcr,
        "cache_write": _tcw,
        "total": _ti + _to + _tcr + _tcw,
        "pricing_model": _model,
        "cost_by_model": _cost["by_model"],
        "cost_source": _cost["cost_source"],
        "cost_usd": round(_cost_usd, 4) if _cost_usd is not None else None,
        "cost_eur": round(_cost_usd * _USD_EUR, 4) if _cost_usd is not None else None,
    }

    if is_mission:
        from web.mission import mission_view

        mv = mission_view(state, run_id=run_id, token_info=token_info)
        mv["error"] = state.get("error")
        # DeepR 2026-08-14 [YÜKSEK]: kokpit continuous modda KALICI donuyordu.
        # Worker kazananlı her turun sonunda `done=True` yazıp 3 sn uyuyor, sonra
        # yeni turda `done=False`'a dönüyor. Klasik `/agent` görünümü bu pencereyi
        # `is_continuous` ile köprülüyordu; kokpit ise ham `state["done"]`i okuyan
        # `mv["done"]`e bakıyordu ve 1 sn'lik poll aralığı 3 sn'lik pencereye KESİN
        # denk geldiği için tam o karede poll'u kesiyordu — koşu arkada tur tur
        # devam ederken operatör donmuş ekrana bakıyordu. `mv["done"]` şablonda iki
        # yeri sürüyor (poll koşulu ve START/STOP düğmesi) ve ikisinin de doğru
        # cevabı aynı: continuous koşu KALICI bitene dek "bitmedi". Fragmanın kendi
        # render modeli (faz şeridi, tur özeti) `mission_view` içinde ham `done` ile
        # hesaplandığı için etkilenmez — burada yalnız dış sözleşme düzeltiliyor.
        mv["done"] = bool(state["done"]) and not is_continuous
        return templates.TemplateResponse(
            request, "fragments/auto_mission.html", {"mv": mv}
        )

    if state["done"] and state["winner_result"] is not None:
        result = state["winner_result"]
        last_row = iteration_row(result)
        last_row["rationale"] = result.rationale
        last_row["equity_curve"] = result.equity_curve
        last_row["equity_dates"] = result.equity_dates
        last_row["spec_name"] = state["winner_spec_name"]
        last_row["steps"] = state["steps"][-60:]  # cap to avoid huge DOM
        # M2625: generate the narrative once, cache it in the real progress dict.
        # Previously, while done+winner was set, EVERY poll (every 2s in
        # continuous) made a new LLM API call and blocked on the response; since
        # the winner is fixed, a single generation is enough.
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

    # Robustness not found but done=True (winner_result=None, error=None)
    if state["done"]:
        return templates.TemplateResponse(
            request,
            "fragments/agent_progress.html",
            {
                "run_id": run_id,
                "phases": _PHASES,
                "state": state,
                "done": True,
                "error": "No strategy passed the robustness test.",
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
        from agent import _create_message, _get_client

        m = last_row
        client = _get_client()
        resp = _create_message(
            client,
            _purpose="narrative",
            max_tokens=200,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Summarize the winning strategy found by the autonomous backtest agent in 2-3 sentences in English:\n"
                        f"Strategy: {state['winner_spec_name']}\n"
                        f"PnL: {m.get('pnl_fmt', '?')} · Sharpe: {m.get('sharpe_fmt', '?')} · "
                        f"Sortino: {m.get('sortino_fmt', '?')} · Max DD: {m.get('max_dd_fmt', '?')}\n"
                        f"Trades: {m.get('n_trades', 0)} · Win Rate: {m.get('win_rate_fmt', '?')}\n"
                        "Begin with 'This strategy'."
                    ),
                }
            ],
        )
        return resp.content[0].text.strip() if resp.content else ""
    except Exception as e:
        # STOP/bütçe iptali bir ARIZA DEĞİL, kontrol akışıdır: onu şablon
        # cümlesine çevirmek iptali başarılı bir sonuç gibi gösterirdi
        # (llm_client._raise_if_llm_control_abort sözleşmesi).
        try:
            from llm_client import _raise_if_llm_control_abort
        except Exception:  # llm_client yoksa LLM yolu zaten hiç çalışmadı
            pass
        else:
            _raise_if_llm_control_abort(e)
        # Sessiz düşüş, ekranda NORMAL görünen bir cümle üretir: bayrak yok,
        # log yok, kullanıcı LLM'in hiç konuşmadığını bilmez. En az operatör
        # bunu görsün.
        logging.warning(
            "%s: LLM anlatısı üretilemedi (%s) — şablon metnine düşülüyor: %s",
            "winner_narrative",
            type(e).__name__,
            e,
            exc_info=True,
        )
        pnl = last_row.get("pnl") or 0
        return (
            f"This strategy was saved to the catalog as {state['winner_spec_name']}. "
            f"With {last_row.get('n_trades', 0)} trades it "
            f"{'gained' if pnl >= 0 else 'lost'} {last_row.get('pnl_fmt', '?')} "
            f"and passed the robustness test."
        )
