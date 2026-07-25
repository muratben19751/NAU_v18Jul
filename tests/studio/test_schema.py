import pytest
from pydantic import ValidationError

from scripts.seed_studio import build_fixture
from strategy_studio.schema import (
    OptimizeRange,
    StrategyDefinition,
)


def test_roundtrip_json():
    d = build_fixture()
    j = d.model_dump_json(by_alias=True)
    d2 = StrategyDefinition.model_validate_json(j)
    assert d2.model_dump(by_alias=True) == d.model_dump(by_alias=True)
    assert d2.regime.else_ == "flat"


def test_optimize_range_min_gt_max_rejected():
    with pytest.raises(ValidationError):
        OptimizeRange(min=10, step=1, max=5)


def test_optimize_range_step_positive():
    with pytest.raises(ValidationError):
        OptimizeRange(min=1, step=0, max=5)


def test_n_values_and_sweep_size():
    r = OptimizeRange(min=6, step=2, max=14)
    assert r.n_values == 5
    d = build_fixture()
    expected = 1
    for *_x, p in d.optimized_params():
        expected *= p.optimize.n_values
    assert d.sweep_size() == expected > 1


def test_optimized_params_includes_risk_and_target():
    d = build_fixture()
    keys = {(b, n) for b, _r, n, _p in d.optimized_params()}
    assert ("risk", "stop_loss_atr_mult") in keys
    assert ("regime", "target") in keys  # ADX threshold sweep


def test_rule_ids_unique():
    d = build_fixture()
    ids = [r.id for _b, r in d.iter_rules()]
    assert len(ids) == len(set(ids))
