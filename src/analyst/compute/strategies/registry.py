"""策略库目录。

实时策略（monitor / hub 收盘评估）：
  · cycle_switch  — 牛熊周期切换（4h 收盘）

组合策略（backtest / 多币组合）：
  · cycle_switch  — 牛熊周期切换（本包）
  · xs_momentum   — 横截面动量（多币 top2）
  · funding_carry — 资金费 delta 中性套利

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
        description="减半日历×200日线双确认；牛市唐奇安只多，熊市反弹空+破位空（半仓）；"
                    "提醒附波动率目标化建议仓位（回测 MDD -46%→-28%）",
        cli="analyst backtest-classic BTC -s cycle_switch --days 1825",
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
