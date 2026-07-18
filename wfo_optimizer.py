"""Walk-Forward Optimization — train-only parametre araması (hafif GA).

Gerçek WFO, strateji parametrelerini in-sample (train) penceresinde optimize
eder ve *seçilen* parametreleri out-of-sample (test) penceresine uygular. Bu
modül eksik parçaları sağlar:

  - ``build_param_space``  : hangi sayısal knob'lar ayarlanabilir, aralıkları ne
    (``BLOCK_REGISTRY[...]["meta"]["params"]`` + strateji-seviyesi alanlar).
  - ``mutate_spec``        : bir atamayı uygulayan YENİ bir spec üretir
    (deep-copy; çağıranın spec'i asla değişmez).
  - ``optimize_window``    : train barlarında hafif genetik arama (GA).
    Jenerasyon 0 = mevcut spec değerleri + rastgele bireyler; sonraki
    jenerasyonlar = elitizm(1) + turnuva(k=3) seçim + uniform crossover +
    boyut-başına gaussian mutasyon. Her aday k-fold (embargo'lu) koşulur;
    skor = mean − 0.5·std (NAU deseni). Tohumlu ``np.random.default_rng`` ile
    tümüyle deterministtir (aynı seed → aynı kazanan).

Yalnız SAYISAL parametreler optimize edilir. Enum'lar (direction/cross/side,
entry/exit mantığı, order/sl/tp tipi, sizing modu, trend_filter) *stratejinin
ne olduğunu* tanımlar — onları taramak model seçimi olur, kalibrasyon değil;
arama uzayını da patlatır.

Maliyet sınırı
--------------
Pencere başına toplam backtest sayısı = POP × GEN × FOLD.

  - POP  = ``WFO_POP_SIZE``  (default 8,  env: ``NAUTILUS_WFO_POP_SIZE``)
  - GEN  = max(1, round(n_samples / POP))  — ``n_samples`` çağırandan gelir
    (WFO'da ``n_optimize``); ör. n_samples=20 → GEN=3 → 8×3 = 24 aday.
  - FOLD = ``WF_FOLDS``      (default 3,  env: ``NAUTILUS_WF_FOLDS``)

Örnek: n_samples=20 defaults ile → 24 aday × 3 fold = 72 eval/pencere.
Bütçeyi kısmak için env sabitlerini düşürün (örn. POP=4, FOLD=2) ya da
``n_optimize``'ı küçültün.

Metrik uyarısı (M35)
--------------------
``objective``, Engine yolunun BAR-FREKANSLI Sharpe'ı üzerinde koşar; çapraz
runner (BacktestNode) ya da NAU kıyası için ``sharpe_nautilus`` /
``sharpe_per_trade`` kullanın — ölçekler farklıdır, doğrudan kıyaslanmaz.
"""

from __future__ import annotations

import math
import os

import numpy as np


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# ── GA / fold bütçesi (env-override'lı modül sabitleri) ─────────────────────
# Pencere başına maliyet = WFO_POP_SIZE × GEN × WF_FOLDS backtest (bkz. modül
# docstring'i). Testler bu sabitleri monkeypatch'leyebilir; fonksiyonlar
# değerleri çağrı anında modül global'inden okur.
WFO_POP_SIZE = max(1, _env_int("NAUTILUS_WFO_POP_SIZE", 8))
WF_FOLDS = max(1, _env_int("NAUTILUS_WF_FOLDS", 3))
# NAU güven sönümü sabiti: skor *= n / (n + K). K=20 → 5 işlemde ×0.2,
# 20 işlemde ×0.5, 100 işlemde ×0.83 — az işlemle şişen skorlar bastırılır.
WFO_TRADE_CONF_K = max(0, _env_int("NAUTILUS_WFO_TRADE_CONF_K", 20))

# M236: NAU calmar korumaları. DD_FLOOR (kesir; NAU %1 = 0.01) mikro-drawdown
# şişmesini önler; CALMAR_CAP calmar'ı ±10'a kırpar. STARTING_CASH pnl_pct
# türetiminde fallback taban.
_DD_FLOOR = 0.01
_CALMAR_CAP = 10.0
try:
    from app_constants import STARTING_CASH as _STARTING_CASH
except Exception:  # pragma: no cover
    _STARTING_CASH = 10_000.0

# M462: NAU geçerli-fold oranı — 3 fold'un 2'si geçerliyse (>=0.6) aday yaşar;
# TEK -inf fold adayı tümden reddetmesin (seyrek-işlemli sağlam adayları korur).
WF_MIN_VALID_FOLDS_FRAC = _env_float("NAUTILUS_WF_MIN_VALID_FOLDS_FRAC", 0.6)

# GA iç ayarları (davranış sabitleri; determinizm rng'den gelir).
GA_TOURNAMENT_K = 3
GA_MUT_SIGMA = 0.15  # gaussian mutasyon std'si, boyut aralığının kesri


def _isnan(x) -> bool:
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return True


# Strategy-level numeric knobs, each gated on the feature that activates it.
# (field, type, lo, hi, is_active(spec))
_SPEC_DIMS = [
    ("sl_value", "float", 0.5, 5.0, lambda s: bool(getattr(s, "use_bracket", False))),
    (
        "tp_value",
        "float",
        1.0,
        10.0,
        lambda s: (
            bool(getattr(s, "use_bracket", False))
            and getattr(s, "tp_type", "off") != "off"
        ),
    ),
    (
        "atr_period",
        "int",
        7,
        30,
        lambda s: (
            getattr(s, "sl_type", "") == "atr"
            or getattr(s, "tp_type", "") == "atr"
            or getattr(s, "trade_size_mode", "") == "atr_target"
        ),
    ),
    (
        "trend_ema_period",
        "int",
        20,
        200,
        lambda s: bool(getattr(s, "trend_filter", False)),
    ),
]


def build_param_space(spec) -> list[dict]:
    """Return the list of tunable numeric dimensions for ``spec``.

    Each dim: ``{target, type, lo, hi, default, label}`` where ``target`` is
    ``("block", i, key)`` or ``("spec", field)``.
    """
    from composer import BLOCK_REGISTRY

    space: list[dict] = []
    for i, block in enumerate(spec.blocks):
        reg = BLOCK_REGISTRY.get(block.type) or {}
        params_meta = (reg.get("meta") or {}).get("params", {})
        for key, meta in params_meta.items():
            ptype = meta.get("type")
            if ptype not in ("int", "float"):
                continue  # skip enums / non-numeric
            lo, hi = meta.get("min"), meta.get("max")
            if lo is None or hi is None or hi <= lo:
                continue
            space.append(
                {
                    "target": ("block", i, key),
                    "type": ptype,
                    "lo": float(lo),
                    "hi": float(hi),
                    "default": meta.get("default", lo),
                    "label": f"{block.type}[{i}].{key}",
                }
            )

    for field, ptype, lo, hi, is_active in _SPEC_DIMS:
        if is_active(spec):
            space.append(
                {
                    "target": ("spec", field),
                    "type": ptype,
                    "lo": float(lo),
                    "hi": float(hi),
                    "default": getattr(spec, field, lo),
                    "label": field,
                }
            )
    return space


def _current_values(spec, space) -> list:
    vals = []
    for dim in space:
        tgt = dim["target"]
        if tgt[0] == "block":
            _, i, key = tgt
            vals.append(spec.blocks[i].params.get(key, dim["default"]))
        else:
            vals.append(getattr(spec, tgt[1], dim["default"]))
    return vals


def _sample_dim(rng, dim):
    if dim["type"] == "int":
        return int(rng.integers(int(dim["lo"]), int(dim["hi"]) + 1))
    return float(rng.uniform(dim["lo"], dim["hi"]))


def mutate_spec(spec, space, values):
    """Return a NEW spec with ``values`` (aligned to ``space``) applied.

    Deep-copies via to_dict/from_dict so the caller's spec is never touched.
    """
    from composer import ComposedStrategySpec

    new = ComposedStrategySpec.from_dict(spec.to_dict())
    for dim, v in zip(space, values):
        if dim["type"] == "int":
            v = int(round(float(v)))
        else:
            v = float(v)
        tgt = dim["target"]
        if tgt[0] == "block":
            _, i, key = tgt
            new.blocks[i].params[key] = v
        else:
            setattr(new, tgt[1], v)
    return new


def values_to_dict(space, values) -> dict:
    """Human-readable {label: value} for reporting chosen params."""
    out = {}
    for dim, v in zip(space, values):
        out[dim["label"]] = (
            int(round(float(v))) if dim["type"] == "int" else round(float(v), 4)
        )
    return out


def objective_value(result, objective: str = "sharpe", min_trades: int = 5) -> float:
    """Bir backtest sonucunu skorlar. Hatalı / az-işlemli koşular -inf döner —
    böylece 0-1 işlem alıp devasa/NaN Sharpe raporlayan overfit tuzağı asla
    kazanamaz (sert eşik: ``n < min_trades`` → -inf, default 5).

    Eşiği GEÇEN adaylara NAU tarzı işlem-sayısı güven sönümü uygulanır:
    ``val *= n / (n + WFO_TRADE_CONF_K)`` — az işlemle şişen skorlar
    (çoklu-test şişmesi) bastırılır, n büyüdükçe çarpan 1'e yaklaşır.
    NaN-fallback zincirindeki değerlere de aynı sönüm uygulanır.
    """
    if result is None or getattr(result, "error", None):
        return float("-inf")
    m = result.metrics or {}
    n = m.get("n_trades", 0) or 0
    if n < min_trades:
        return float("-inf")
    conf = n / (n + WFO_TRADE_CONF_K) if (n + WFO_TRADE_CONF_K) > 0 else 1.0

    def _calmar() -> float | None:
        # M236: NAU DD_FLOOR (%1) + CALMAR_CAP (±10) korumaları — mikro-drawdown'lu
        # (dd=-0.0001) bir fold sınırsız skor alıp GA turnuvasını domine
        # ediyordu. pnl_pct kullan (mutlak USDT değil — M234), NEGATİF-kesir
        # max_dd için abs (L41), taban 0.01 (=%1), sonuç ±10'a kırp.
        pnl_pct = m.get("pnl_pct")
        if pnl_pct is None:
            pnl = m.get("pnl", 0.0) or 0.0
            pnl_pct = pnl / _STARTING_CASH
        dd = m.get("max_dd")
        if dd is None or _isnan(dd):
            return None
        dd_abs = max(abs(float(dd)), _DD_FLOOR)
        return max(-_CALMAR_CAP, min(_CALMAR_CAP, float(pnl_pct) / dd_abs))

    if objective == "sortino":
        val = m.get("sortino")
    elif objective in ("calmar", "return_dd"):
        val = _calmar()
    else:  # sharpe (default)
        # NAU paritesi: per-trade sharpe ((mean/std)×√n), annualized 252-gün DEĞİL.
        # NAU fold_quality composite'i de per-trade sharpe okur. Geriye-uyum:
        # sharpe_per_trade yoksa (eski/stub metrics) annualized 'sharpe'e düş.
        val = m.get("sharpe_per_trade")
        if val is None:
            val = m.get("sharpe")

    if val is None or _isnan(val):
        # M240: fallback zinciri OLÇEK KARIŞTIRMASIN — sharpe/sortino ~O(1)
        # iken pnl/|dd| ~O(1e4) idi; tek dejenere fold (sharpe NaN) adayın
        # penalized skorunu katlarca şişirip GA kazananını bozuyordu. NaN
        # sortino'ya (aynı ölçek) düş; o da yoksa CAPLİ calmar (±10, aynı
        # mertebe) — sınırsız ham oran DEĞİL.
        alt = m.get("sortino")
        if alt is not None and not _isnan(alt):
            return float(alt) * conf
        cal = _calmar()
        if cal is not None:
            return cal * conf
        return float("-inf")
    return float(val) * conf


# ---------------------------------------------------------------------------
# Hafif GA — popülasyon üretimi (H10)
# ---------------------------------------------------------------------------


def ga_plan(space, n_samples: int) -> tuple[int, int]:
    """(pop_size, n_generations) bütçe eşlemesi.

    GEN = max(1, round(n_samples / POP)) — yarım YUKARI yuvarlanır
    (n_samples=20, POP=8 → GEN=3). Boş uzayda arama anlamsız → (1, 1):
    tek (mevcut) aday koşulur, eski n_samples×özdeş-koşu israfı yok.
    """
    if not space:
        return 1, 1
    pop = max(1, WFO_POP_SIZE)
    return pop, max(1, int(n_samples / pop + 0.5))


def ga_initial_population(spec, space, rng, pop_size: int) -> list[list]:
    """Jenerasyon 0: birey 0 = spec'in MEVCUT değerleri, kalanı rastgele.

    rng parent'ta kalır (paralel yol worker'lara rastgelelik sızdırmaz);
    aynı rng durumu → aynı popülasyon (determinizm sözleşmesi).
    """
    population: list[list] = [_current_values(spec, space)]
    for _ in range(max(0, pop_size - 1)):
        population.append([_sample_dim(rng, dim) for dim in space])
    return population


def _tournament_idx(rng, scores: list[float], k: int = GA_TOURNAMENT_K) -> int:
    """Turnuva seçimi: k rastgele birey; en yüksek skor kazanır
    (eşitlikte düşük indeks — determinist kıyas)."""
    n = len(scores)
    idxs = rng.integers(0, n, size=min(k, n))
    best = int(idxs[0])
    for raw in idxs[1:]:
        i = int(raw)
        if scores[i] > scores[best] or (scores[i] == scores[best] and i < best):
            best = i
    return best


def ga_next_population(
    rng, space, population: list[list], scores: list[float], pop_size: int
) -> list[list]:
    """Sonraki jenerasyon: elitizm(1) + turnuva(k=3) + uniform crossover +
    boyut-başına gaussian mutasyon (int'e yuvarla, [lo, hi] clamp).

    Elit, kesin-büyük kuralıyla seçilir (eşitlikte düşük indeks) — sıralı ve
    paralel yol aynı kazananı üretir. Tüm rastgelelik ``rng``'den gelir.
    """
    elite_idx = max(range(len(population)), key=lambda i: (scores[i], -i))
    new_pop: list[list] = [list(population[elite_idx])]
    while len(new_pop) < pop_size:
        p1 = population[_tournament_idx(rng, scores)]
        p2 = population[_tournament_idx(rng, scores)]
        child: list = []
        for d, dim in enumerate(space):
            v = float(p1[d]) if rng.random() < 0.5 else float(p2[d])
            v += float(rng.normal(0.0, GA_MUT_SIGMA * (dim["hi"] - dim["lo"])))
            v = min(max(v, dim["lo"]), dim["hi"])
            child.append(int(round(v)) if dim["type"] == "int" else v)
        new_pop.append(child)
    return new_pop


# ---------------------------------------------------------------------------
# K-fold train değerlendirmesi (L35)
# ---------------------------------------------------------------------------


def build_fold_bounds(train_bars, n_folds=None, embargo_days=None) -> list[tuple]:
    """Train barlarını k bitişik fold'a böler; fold'lar arasına embargo (purge)
    boşluğu koyar — bir fold'un sonunda açılıp diğerinde kapanan işlem sızmasın.

    Döner: [(start, end), ...] — ``pd.Timestamp`` çiftleri, ``end`` EXCLUSIVE
    (son fold son barı da kapsar). Saf bir train_bars fonksiyonudur: sıralı ve
    paralel yol aynı DataFrame'den AYNI sınırları üretir (parite sözleşmesi).

    Fallback'ler: train_bars None/boş → ``[]`` (çağıran tüm-pencere tek koşuya
    düşer); embargo toplam süreyi yiyorsa ya da herhangi bir fold boş kalıyorsa
    → tek fold (tüm pencere).
    """
    import pandas as pd

    if n_folds is None:
        n_folds = WF_FOLDS
    if embargo_days is None:
        from backtest_robustness import WF_EMBARGO_DAYS as embargo_days

    try:
        if train_bars is None or len(train_bars) == 0:
            return []
        idx = train_bars.index
        t0 = pd.Timestamp(idx[0])
        # end exclusive: son barın damgası + 1ns → son bar dahil.
        t_end = pd.Timestamp(idx[-1]) + pd.Timedelta(1, "ns")
    except (TypeError, AttributeError):
        return []

    if n_folds <= 1:
        return [(t0, t_end)]

    embargo = pd.Timedelta(days=float(embargo_days))
    inner = (t_end - t0) - embargo * (n_folds - 1)
    if inner <= pd.Timedelta(0):
        return [(t0, t_end)]  # pencere embargo'lu k-fold için çok kısa

    seg = inner / n_folds
    bounds: list[tuple] = []
    cursor = t0
    for i in range(n_folds):
        fs = cursor
        fe = fs + seg if i < n_folds - 1 else t_end
        bounds.append((fs, fe))
        cursor = fe + embargo

    # Herhangi bir fold boş kalıyorsa (veri boşlukları) tek fold'a düş —
    # boş dilim backtest'i her adayı -inf'e çekerdi.
    for fs, fe in bounds:
        if not ((idx >= fs) & (idx < fe)).any():
            return [(t0, t_end)]
    return bounds


def penalized_score(fold_objs: list[float]) -> float:
    """Fold skorlarını tek skora indirger: mean − 0.5·std (NAU deseni) —
    yalnızca tek fold'da parlayan aday cezalandırılır. Boş liste → -inf."""
    if not fold_objs:
        return float("-inf")
    arr = np.asarray(fold_objs, dtype=float)
    return float(arr.mean() - 0.5 * arr.std())


# ---------------------------------------------------------------------------
# Pencere optimizasyonu (sıralı yol)
# ---------------------------------------------------------------------------


def optimize_window(
    spec,
    train_bars,
    space,
    instrument,
    bar_type,
    venue,
    *,
    n_samples: int = 20,
    seed: int = 0,
    objective: str = "sharpe",
    min_trades: int = 5,
    run_fn=None,
    iteration_id: int = 0,
) -> dict:
    """TRAIN penceresinde hafif GA araması (sıralı yol).

    Jenerasyon 0 = mevcut spec değerleri + (POP−1) rastgele birey; sonraki
    jenerasyonlar ``ga_next_population`` ile evrilir. Her geçerli aday
    ``build_fold_bounds`` fold'larında koşulur; herhangi bir fold -inf ise aday
    reddedilir, aksi halde skor = mean − 0.5·std. Geçersiz adaylar (örn.
    slow<=fast, ``spec.validate()`` yakalar) koşturulmaz (skor -inf).

    Determinizm: tüm rastgelelik ``np.random.default_rng(seed)``'den gelir —
    aynı seed + aynı veri → aynı kazanan. Döner:
    ``{values, params, objective, spec, metrics, n_evaluated}``.

    Maliyet: bkz. modül docstring'i (POP × GEN × FOLD backtest/pencere).
    """
    if run_fn is None:
        from backtest import run_composed_backtest as run_fn  # type: ignore

    rng = np.random.default_rng(seed)
    pop_size, n_gen = ga_plan(space, n_samples)
    population = ga_initial_population(spec, space, rng, pop_size)
    fold_bounds = build_fold_bounds(train_bars)

    best = None
    evaluated = 0
    for gen in range(n_gen):
        scores: list[float] = []
        for values in population:
            cand = mutate_spec(spec, space, values)
            if cand.validate() is not None:
                scores.append(float("-inf"))
                continue
            _folds = fold_bounds or [None]
            fold_objs: list[float] = []
            last_metrics: dict = {}
            # M462: eskiden İLK -inf fold'da aday tümden reddediliyordu; NAU
            # geçerli-fold oranı (>=0.6) ile 3 fold'un 2'si geçerliyse aday
            # yaşar (seyrek-işlemli sağlam adayları korur). TÜM fold'ları koştur,
            # geçerli olanları topla.
            for fb in _folds:
                fold_bars = (
                    train_bars
                    if fb is None
                    else train_bars.loc[
                        (train_bars.index >= fb[0]) & (train_bars.index < fb[1])
                    ]
                )
                res = run_fn(
                    cand,
                    fold_bars,
                    iteration_id=iteration_id,
                    rationale="WFO train-optimize",
                    instrument=instrument,
                    bar_type=bar_type,
                    venue=venue,
                )
                evaluated += 1
                obj = objective_value(res, objective, min_trades)
                if obj != float("-inf"):
                    fold_objs.append(obj)
                    last_metrics = res.metrics or {}
            score = (
                penalized_score(fold_objs)
                if len(fold_objs) >= WF_MIN_VALID_FOLDS_FRAC * len(_folds) and fold_objs
                else float("-inf")
            )
            scores.append(score)
            if score != float("-inf") and (best is None or score > best["objective"]):
                best = {
                    "values": values,
                    "params": values_to_dict(space, values),
                    "objective": score,
                    "spec": cand,
                    "metrics": last_metrics,
                }
        if gen < n_gen - 1:
            population = ga_next_population(rng, space, population, scores, pop_size)

    if best is None:
        # Hiçbir aday geçerli skor üretemedi — naive spec'e düş.
        cur = _current_values(spec, space)
        best = {
            "values": cur,
            "params": values_to_dict(space, cur),
            "objective": float("-inf"),
            "spec": spec,
            "metrics": {},
        }
    best["n_evaluated"] = evaluated
    return best
