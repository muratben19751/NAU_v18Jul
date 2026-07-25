import pytest

from app.studio.compiler import CompileError, compile_strategy
from app.studio.schema import (
    InstrumentConfig,
    Param,
    RiskBlock,
    Rule,
    RuleGroup,
    StrategyDefinition,
)
from scripts.seed_studio import build_fixture


def _risk():
    return RiskBlock(
        stop_loss_atr_mult=Param(value=2.0), stop_loss_atr_len=Param(value=14),
        take_profit_r=Param(value=1.5), risk_per_trade_pct=Param(value=1.0),
        max_concurrent=Param(value=1))


def _minimal(**kw):
    base = {
        "id": "min", "name": "Minimal", "entry": RuleGroup(rules=[
            Rule(indicator="rsi", params={"len": Param(value=14)},
                 operator="lt", target=Param(value=30))]),
        "exit": RuleGroup(match="any", rules=[
            Rule(indicator="rsi", params={"len": Param(value=14)},
                 operator="gt", target=Param(value=70))]),
        "risk": _risk(),
        "instruments": [InstrumentConfig(symbol="SPY", timeframe="1d", active=True)]}
    base.update(kw)
    return StrategyDefinition(**base)


def test_compiles_fixture_trend_following():
    c = compile_strategy(build_fixture())
    assert c.strategy_id == "wt-funding-v3"
    assert c.regime and c.regime["else"] == "flat"
    assert len(c.entry.conditions) == 3 and len(c.entry.filters) == 1
    wt = c.entry.conditions[0]
    assert wt.indicator == "wavetrend" and wt.params == {"n1": 10, "n2": 21}
    assert c.risk["time_stop_bars"] == 36
    assert c.instruments == [{"symbol": "XAUUSD", "timeframe": "15m"}]


def test_compiles_minimal_mean_reversion():
    c = compile_strategy(_minimal())
    assert c.entry.conditions[0].target == 30.0
    assert c.walkforward["objective"] == "dsr"


def test_defaults_filled():
    d = _minimal(entry=RuleGroup(rules=[
        Rule(indicator="macd", operator="crosses_above", target=Param(value=0))]))
    c = compile_strategy(d)
    assert c.entry.conditions[0].params == {"fast": 12, "slow": 26, "signal": 9}


def test_unknown_indicator_raises_with_rule_id():
    d = _minimal(entry=RuleGroup(rules=[
        Rule(id="badrule01", indicator="supertrend_xxx", operator="gt",
             target=Param(value=1))]))
    with pytest.raises(CompileError) as e:
        compile_strategy(d)
    assert e.value.rule_id == "badrule01"
    assert "unknown indicator" in str(e.value)


def test_param_out_of_bounds_raises():
    d = _minimal(entry=RuleGroup(rules=[
        Rule(indicator="rsi", params={"len": Param(value=999)},
             operator="lt", target=Param(value=30))]))
    with pytest.raises(CompileError) as e:
        compile_strategy(d)
    assert e.value.field_name == "len"


def test_bad_operator_for_indicator_raises():
    d = _minimal(entry=RuleGroup(rules=[
        Rule(indicator="adx", params={"len": Param(value=14)},
             operator="crosses_above", target=Param(value=20))]))
    with pytest.raises(CompileError):
        compile_strategy(d)


def test_empty_entry_raises():
    d = _minimal(entry=RuleGroup(rules=[]))
    with pytest.raises(CompileError):
        compile_strategy(d)


def test_risk_pct_bounds():
    r = _risk(); r.risk_per_trade_pct = Param(value=9.0)
    d = _minimal(risk=r)
    with pytest.raises(CompileError):
        compile_strategy(d)
