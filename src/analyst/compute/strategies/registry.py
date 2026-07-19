"""策略库目录。

实时策略（monitor / hub 收盘评估）：
  · cycle_switch  — 牛熊周期切换（4h 收盘）

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
        id="cycle_switch",
        name="牛熊周期切换（D）",
        kind="portfolio",
        module="analyst.compute.strategies.cycle_switch",
        description="减半日历×200日线双确认；牛市唐奇安只多，熊市反弹空+破位空（半仓）",
        cli="analyst backtest-classic BTC -s cycle_switch --days 1825",
    ),
    StrategyInfo(
        id="bull_trend",
        name="牛市腿：唐奇安只多",
        kind="portfolio",
        module="analyst.backtest.classic",
        description="确认牛市/筑底后手选执行；突破 40 根高点进、跌破 20 根低点出",
        cli="analyst backtest-classic BTC -s bull_trend --days 1825",
    ),
    StrategyInfo(
        id="bear_defense",
        name="熊市腿：只空半仓",
        kind="portfolio",
        module="analyst.backtest.classic",
        description="确认熊市后手选执行；z>1.5 反弹空 + 唐奇安破位空，绝不做多",
        cli="analyst backtest-classic BTC -s bear_defense --days 1825",
    ),
    StrategyInfo(
        id="chop_range",
        name="震荡腿：布林均值回归",
        kind="portfolio",
        module="analyst.backtest.classic",
        description="确认震荡后手选执行；z±2 反向半仓、回中轨平、3×ATR 硬止损；趋势段大亏勿裸跑",
        cli="analyst backtest-classic BTC -s chop_range --days 365",
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
    StrategyInfo(
        id="xs_momentum",
        name="横截面动量",
        kind="portfolio",
        module="analyst.compute.strategies.xs_momentum",
        description="多币动量排序：做多最强 top2、熊市空最弱；14 天窗口（12~25 天参数平原）",
        cli="analyst backtest-xs --days 1825",
    ),
    StrategyInfo(
        id="funding_carry",
        name="资金费套利",
        kind="portfolio",
        module="analyst.compute.strategies.funding_carry",
        description="delta 中性：现货多+永续空收资金费；方向无关，5 年回撤 <1.1%",
        cli="analyst backtest-carry BTC --days 1825",
    ),
]


def list_strategies(*, kind: str | None = None) -> list[StrategyInfo]:
    if kind:
        return [s for s in STRATEGY_CATALOG if s.kind == kind]
    return list(STRATEGY_CATALOG)
