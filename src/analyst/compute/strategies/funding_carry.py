"""资金费套利（funding carry）——delta 中性，市场方向无关。

原理：U 本位永续多头付、空头收「资金费」（8h 一档）。费率持续为正时：
  现货买入 1 份 + 永续做空 1 份（等名义）→ 价格涨跌完全对冲，
  每 8h 纯收资金费。加密史上资金费约 85% 时间为正（多头长期付费）。

这是「震荡腿」的正确答案：不判断方向、牛熊震荡都能收，
赚的是杠杆多头的融资成本，而非价差。

信号（带滞回防抖）：
  · 资金费 EMA(ema_n 档) > enter_rate → 建仓收费
  · EMA < exit_rate → 平仓（负费率时空头要倒贴，必须离场）

成本口径：进出各 2 腿（现货+永续），每腿按 CostModel 单边费率。
未建模：现货-永续基差瞬时波动（长期均值≈0）、极端行情移仓风险。

回测：analyst backtest-carry BTC --days 1825
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from analyst.backtest.classic import CostModel


@dataclass
class FundingCarryConfig:
    ema_n: int = 21              # 费率 EMA 档数（21 档 = 7 天）
    enter_rate: float = 0.00005  # 建仓门槛：EMA > 0.005%/8h（≈年化 5.5%）
    exit_rate: float = 0.0       # 平仓门槛：EMA ≤ 0（负费率必须离场）
    legs_per_side: int = 2       # 进/出各 2 腿（现货 + 永续）


@dataclass
class CarryReport:
    symbol: str
    settlements: int
    start: datetime | None
    end: datetime | None
    total_return_pct: float = 0.0
    apr_pct: float = 0.0             # 简单年化（按覆盖天数）
    apr_in_position_pct: float = 0.0 # 在仓时段的年化（真实收费效率）
    max_drawdown_pct: float = 0.0
    exposure: float = 0.0            # 在仓时间占比
    round_trips: int = 0
    cost_paid_pct: float = 0.0
    avg_rate_collected_pct: float = 0.0  # 在仓时平均每档费率
    equity_curve: list[float] = field(default_factory=list, repr=False)

    def to_row(self) -> dict:
        return {
            "strategy": "funding_carry",
            "symbol": self.symbol,
            "total_return_pct": round(self.total_return_pct, 2),
            "apr_pct": round(self.apr_pct, 2),
            "apr_in_position_pct": round(self.apr_in_position_pct, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "exposure": round(self.exposure, 2),
            "round_trips": self.round_trips,
            "cost_paid_pct": round(self.cost_paid_pct, 2),
        }


def backtest_funding_carry(
    symbol: str,
    funding: list[tuple[int, float]],
    cfg: FundingCarryConfig | None = None,
    *,
    cost: CostModel | None = None,
) -> CarryReport:
    """按历史资金费流回测 delta 中性收费策略。

    funding: fetch_funding_history 输出 [(结算ms, 费率), ...] 升序。
    """
    cfg = cfg or FundingCarryConfig()
    cost = cost or CostModel()
    report = CarryReport(
        symbol=symbol,
        settlements=len(funding),
        start=datetime.utcfromtimestamp(funding[0][0] / 1000) if funding else None,
        end=datetime.utcfromtimestamp(funding[-1][0] / 1000) if funding else None,
    )
    if len(funding) < cfg.ema_n + 2:
        return report

    leg_cost = cost.one_way * cfg.legs_per_side
    alpha = 2.0 / (cfg.ema_n + 1)
    ema = funding[0][1]
    equity, peak, max_dd = 1.0, 1.0, 0.0
    in_pos = False
    in_bars = 0
    round_trips = 0
    cost_paid = 0.0
    collected: list[float] = []
    curve: list[float] = [1.0]

    for _, rate in funding:
        # 先按上一档决定的仓位收费，再更新信号（不偷看当档费率）
        if in_pos:
            equity *= 1.0 + rate
            collected.append(rate)
            in_bars += 1
        ema = alpha * rate + (1 - alpha) * ema
        if not in_pos and ema > cfg.enter_rate:
            in_pos = True
            equity *= 1.0 - leg_cost
            cost_paid += leg_cost
        elif in_pos and ema <= cfg.exit_rate:
            in_pos = False
            equity *= 1.0 - leg_cost
            cost_paid += leg_cost
            round_trips += 1
        curve.append(equity)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)

    days = max((funding[-1][0] - funding[0][0]) / 86_400_000, 1e-9)
    report.total_return_pct = (equity - 1.0) * 100
    report.apr_pct = (equity - 1.0) / (days / 365.0) * 100
    in_days = in_bars / 3.0
    if in_days > 1:
        gross = math.prod(1.0 + r for r in collected)
        report.apr_in_position_pct = (gross - 1.0) / (in_days / 365.0) * 100
    report.max_drawdown_pct = max_dd * 100
    report.exposure = in_bars / len(funding)
    report.round_trips = round_trips
    report.cost_paid_pct = cost_paid * 100
    report.avg_rate_collected_pct = (
        sum(collected) / len(collected) * 100 if collected else 0.0
    )
    report.equity_curve = curve
    return report


def current_carry_status(
    funding: list[tuple[int, float]],
    cfg: FundingCarryConfig | None = None,
) -> dict:
    """当前费率 EMA 与建议（监控/CLI 展示用）。"""
    cfg = cfg or FundingCarryConfig()
    if not funding:
        return {"signal": "no_data"}
    alpha = 2.0 / (cfg.ema_n + 1)
    ema = funding[0][1]
    for _, rate in funding:
        ema = alpha * rate + (1 - alpha) * ema
    last_rate = funding[-1][1]
    active = ema > cfg.enter_rate
    return {
        "signal": "carry" if active else "flat",
        "ema_rate_pct": round(ema * 100, 5),
        "ema_apr_pct": round(ema * 3 * 365 * 100, 2),
        "last_rate_pct": round(last_rate * 100, 5),
        "note": (
            "费率 EMA 高于门槛：现货多+永续空收费中" if active
            else "费率 EMA 低于门槛：观望（负费率时空头倒贴）"
        ),
    }
