"""策略库目录。

实时策略（monitor / hub 收盘评估）：
  · double_line   — 双线反转，15m 形态突破

组合策略（backtest/classic 仓位回测，含交易成本）：
  · cycle_switch  — 牛熊周期切换（本包）
  · donchian      — 唐奇安通道只多
  · ema_cross     — EMA 双均线趋势
  · boll_mr       — 布林均值回归（对照）

列出全部：analyst strategies
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyInfo:
    id: str
    name: str
    kind: str          # realtime | portfolio
    module: str
    description: str
    cli: str | None = None


STRATEGY_CATALOG: list[StrategyInfo] = [
    StrategyInfo(
        id="double_line",
        name="双线反转",
        kind="realtime",
        module="analyst.compute.strategies.double_line_reversal",
        description="K 线形态突破 + EMA200 过滤；适合 15m 盯盘，震荡市期望偏低",
        cli="analyst monitor check BTC",
    ),
    StrategyInfo(
        id="cycle_switch",
        name="牛熊周期切换（D）",
        kind="portfolio",
        module="analyst.compute.strategies.cycle_switch",
        description="减半日历×200日线双确认；牛市唐奇安只多，熊市反弹做空（半仓）",
        cli="analyst backtest-classic BTC -s cycle_switch --days 1825",
    ),
    StrategyInfo(
        id="donchian",
        name="唐奇安通道突破",
        kind="portfolio",
        module="analyst.backtest.classic",
        description="海龟式 40/20 通道；5 年只多基线，低频趋势跟随",
        cli="analyst backtest-classic BTC -s donchian --days 1825",
    ),
    StrategyInfo(
        id="ema_cross",
        name="EMA 双均线趋势",
        kind="portfolio",
        module="analyst.backtest.classic",
        description="EMA 快慢线 always-in；加密市场建议只多",
        cli="analyst backtest-classic BTC -s ema_cross",
    ),
    StrategyInfo(
        id="boll_mr",
        name="布林均值回归",
        kind="portfolio",
        module="analyst.backtest.classic",
        description="z-score 超买超卖回归；加密 5 年回测表现差，作对照",
        cli="analyst backtest-classic BTC -s boll_mr",
    ),
]


def list_strategies(*, kind: str | None = None) -> list[StrategyInfo]:
    if kind:
        return [s for s in STRATEGY_CATALOG if s.kind == kind]
    return list(STRATEGY_CATALOG)
