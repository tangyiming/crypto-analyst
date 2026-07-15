"""历史 K 线回测：双线反转策略交易模拟 + 规则告警前瞻命中率。"""

from analyst.backtest.engine import (
    BacktestReport,
    RuleStat,
    Trade,
    run_backtest,
)

__all__ = ["BacktestReport", "RuleStat", "Trade", "run_backtest"]
