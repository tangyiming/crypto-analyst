"""历史 K 线回测：规则告警前瞻命中率 + 经典组合策略。"""

from analyst.backtest.classic import (
    HALVING_DATES,
    STRATEGIES,
    ClassicReport,
    CostModel,
    build_cycle_regime,
    halving_phase,
    label_regimes,
    run_classic_backtest,
)
from analyst.compute.strategies.cycle_switch import (
    CycleSwitchConfig,
    CycleSwitchSignal,
    evaluate_cycle_switch,
)
from analyst.backtest.engine import (
    BacktestReport,
    RuleStat,
    run_backtest,
)

__all__ = [
    "BacktestReport",
    "RuleStat",
    "run_backtest",
    "ClassicReport",
    "CostModel",
    "STRATEGIES",
    "HALVING_DATES",
    "build_cycle_regime",
    "halving_phase",
    "label_regimes",
    "run_classic_backtest",
    "CycleSwitchConfig",
    "CycleSwitchSignal",
    "evaluate_cycle_switch",
]
