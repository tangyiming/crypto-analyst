"""策略包：周期切换 + 策略库目录（registry）。"""

from analyst.compute.strategies.cycle_switch import (
    CycleSwitchConfig,
    CycleSwitchSignal,
    build_cycle_regime,
    evaluate_cycle_switch,
    halving_phase,
    positions_cycle_switch,
)
from analyst.compute.strategies.registry import STRATEGY_CATALOG, StrategyInfo, list_strategies

__all__ = [
    "CycleSwitchConfig",
    "CycleSwitchSignal",
    "evaluate_cycle_switch",
    "build_cycle_regime",
    "halving_phase",
    "positions_cycle_switch",
    "STRATEGY_CATALOG",
    "StrategyInfo",
    "list_strategies",
]
