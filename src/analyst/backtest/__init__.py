"""历史 K 线回测：双线反转策略交易模拟 + 规则告警前瞻命中率 + 经典组合策略。"""

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
    Trade,
    run_backtest,
)

__all__ = [
    "BacktestReport",
    "RuleStat",
    "Trade",
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
