"""Deployment (STUDIO_SPEC Phase 6).

Compiles the SAVED version, enforces the deployment gate server-side, and
persists a deployment record plus the artifact a runner consumes.

The artifact is **runnable**, not a summary: it carries the lowered
``ComposedStrategySpec`` (the same object the backtest path executes) and the
instruments to run it on. Anything ``to_nautilus`` cannot lower is refused at
deploy time with its reasons — deploying a strategy no runner can execute
would record a deployment that is a lie, and the failure would surface later,
detached from the click that caused it.

INTEGRATION POINT for nautilus_web_app: a runner that consumes this artifact.
The strategy layer is venue-agnostic (``ComposedStrategy`` subscribes to bars
and submits orders), so a live/sim ``TradingNode`` can host it unchanged; what
this repo has no infrastructure for yet is the node itself, its market-data
client, and credentials. The kill switch is passed through and **enforced by
the runner**: ``PaperRunner`` arms ``kill_switch_daily_pct`` at launch and
pauses the deployment when the account's PnL for the UTC day breaches it (see
``runner.check_kill_switches``). Any other runner consuming this artifact owes
the same — writing the field without evaluating it is what this line used to
describe, and it made the field decorative for months.

Wiki References
---------------
Bkz: [[strategy_studio]], [[environment_contexts]]
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .backtest import REAL_ENGINE, BacktestMetrics, to_nautilus
from .compiler import CompiledStrategy, compile_strategy
from .schema import StrategyDefinition

DEFAULT_GATE_DSR = 0.8

# Eşik HEDEFE göre değişir, çünkü metriklerin ÖLÇEĞİ ve İŞARETİ farklı.
# `max_dd_pct` negatif yüzde olarak üretilir (-8.0 = %8 düşüş), dolayısıyla
# `+0.8`'lik ortak varsayılan hiçbir gerçek max-dd sonucunun geçemeyeceği bir
# eşikti: pozitif bir maksimum düşüş yoktur. `max_dd` hedefi seçen herkes,
# kapıyı elle negatif bir sayıya çekmediği sürece, mükemmel bir koşuyla bile
# "OOS MAX_DD -8.00 below required 0.80" görüyordu (DeepR 2026-08-17 [ORTA]).
#
# Karşılaştırmanın YÖNÜ (`value < threshold`) doğru ve öyle kalıyor: max-dd'de
# de büyük olan iyidir (-5, -20'den iyidir). Yanlış olan yalnız varsayılandı.
_DEFAULT_GATE_MIN: dict[str, float] = {
    "sharpe": 0.8,
    "max_dd": -20.0,  # "düşüş %20'den kötü olmasın"
}


def default_gate_min(objective: str) -> float:
    """Bu hedef için makul varsayılan kapı eşiği (bilinmeyen hedef → DSR)."""
    return _DEFAULT_GATE_MIN.get(objective, DEFAULT_GATE_DSR)


# Bumped when the artifact's shape changes in a way a runner must notice.
# v1 carried only condition *counts* under "compiled" — not runnable; v2
# carries the lowered spec. A runner must refuse a version it does not know
# rather than guess at missing fields.
ARTIFACT_SCHEMA = 2


class DeployBlocked(Exception):
    pass


@dataclass
class DeployConfig:
    environment: str  # paper | live
    instruments: str  # active | all
    capital: float
    kill_switch_daily_pct: float | None
    gate_enabled: bool
    gate_min_objective: float


def check_gate(
    defn: StrategyDefinition,
    latest_metrics: BacktestMetrics | None,
    cfg: DeployConfig,
    engine: str | None = None,
) -> None:
    """Server-side deployment gate — not just UI.

    ``engine`` koşuyu ÜRETEN motorun kaydı (bkz. ``backtest.engine_name``).
    ``None`` = kayıtta yok (motor sütunundan önceki satırlar).
    """
    if not cfg.gate_enabled:
        return
    if latest_metrics is None:
        raise DeployBlocked(
            "deployment gate: no completed walk-forward run to evaluate"
        )
    # Sayıya bakmadan ÖNCE sayının nereden geldiğine bak. Shipped varsayılanda
    # Studio `StubBacktestAdapter` kullanıyor: metrikler compiled config'in
    # hash'inden türeyen bir rastgele yürüyüş. Kapı bunu gerçek OOS kanıtından
    # ayırt edemiyordu, dolayısıyla olumlu bir SENTETİK DSR artifact üretmeye
    # yetiyordu (DeepR 2026-08-17 [YÜKSEK]).
    #
    # Kapı bir KANITA DAYALI İDDİADIR ("OOS X ≥ Y"); kanıt olmayanı reddetmesi
    # tanımı gereği. Sentetik sayılarla deploy etmek isteyen kapıyı kapatabilir
    # — o zaman ortada bir iddia da olmaz. Bilinmeyen motor da reddediliyor:
    # "belki gerçekti" bir kanıt değildir, bir tahmindir.
    if engine != REAL_ENGINE:
        raise DeployBlocked(
            "deployment gate: the run was measured by "
            f"{engine or 'an unrecorded engine'}, not the real backtest engine "
            f"— set STUDIO_BACKTEST={REAL_ENGINE} and re-run, or turn the gate "
            "off to deploy on simulated numbers"
        )
    objective = defn.walkforward.objective
    metrics_by_objective = {
        "sharpe": ("sharpe", latest_metrics.sharpe),
        "max_dd": ("max_dd", latest_metrics.max_dd_pct),
    }
    # Unknown/unset objectives fall back to DSR — the message must say DSR,
    # not the requested objective's name, or the operator sees a metric label
    # that doesn't match the number next to it.
    metric_label, value = metrics_by_objective.get(
        objective, ("dsr", latest_metrics.dsr)
    )
    if value < cfg.gate_min_objective:
        raise DeployBlocked(
            f"deployment gate: OOS {metric_label.upper()} {value:.2f} "
            f"below required {cfg.gate_min_objective:.2f}"
        )


def artifact_instruments(
    defn: StrategyDefinition, compiled: CompiledStrategy, cfg: DeployConfig
) -> list[dict[str, str]]:
    """The instruments the runner should trade, per ``cfg.instruments``.

    ``compile_strategy`` already narrows to the active ones, so 'active' takes
    the compiler's answer rather than recomputing it (one definition of
    "active"). 'all' has to come from the definition — and it genuinely differs:
    previously the artifact always listed the active set while ``config`` said
    "all", so a runner reading both saw a contradiction.
    """
    if cfg.instruments == "all":
        return [
            {"symbol": i.symbol, "timeframe": i.timeframe} for i in defn.instruments
        ]
    if cfg.instruments != "active":
        raise DeployBlocked(
            f"unknown instrument selection '{cfg.instruments}' "
            "(expected 'active' or 'all')"
        )
    return compiled.instruments


def build_artifact(
    defn: StrategyDefinition, compiled: CompiledStrategy, cfg: DeployConfig
) -> str:
    """The JSON document a runner consumes — runnable, not a description.

    ``spec`` is a serialized ``ComposedStrategySpec``: feed it through
    ``ComposedStrategySpec.from_dict`` and hand it to a ``ComposedStrategy``,
    one per entry in ``instruments``. The spec carries no instrument of its
    own, which is why the two are separate keys.

    Raises:
        UnsupportedStrategy: the strategy cannot be lowered onto a runnable
            spec (regime branch, ranked allocation, an indicator with no engine
            block, …) — with every reason listed.
    """
    spec = to_nautilus(compiled, initial_capital=cfg.capital)
    return json.dumps(
        {
            "artifact_schema": ARTIFACT_SCHEMA,
            "strategy_id": defn.id,
            "version": defn.version,
            "environment": cfg.environment,
            "capital": cfg.capital,
            "kill_switch_daily_pct": cfg.kill_switch_daily_pct,
            "instruments": artifact_instruments(defn, compiled, cfg),
            "risk": compiled.risk,
            "spec": spec.to_dict(),
            "config": asdict(cfg),
        },
        indent=2,
    )


def prepare_deployment(
    defn: StrategyDefinition,
    latest_metrics: BacktestMetrics | None,
    cfg: DeployConfig,
    engine: str | None = None,
) -> str:
    if cfg.environment not in ("paper", "live"):
        raise DeployBlocked(f"unknown environment '{cfg.environment}'")
    check_gate(defn, latest_metrics, cfg, engine)
    compiled = compile_strategy(defn)  # SAVED version, never the draft
    return build_artifact(defn, compiled, cfg)
