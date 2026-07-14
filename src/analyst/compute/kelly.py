"""Kelly 仓位建议（仅作提醒，不下单）。

参考视频标签 #kelly（加密大漂亮 / SOLODE 实战）：
https://www.youtube.com/watch?v=fqK-3LK_kF0

加密合约波动大，默认用 1/4 Kelly，并夹在账户风险上限内。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KellySize:
    """仓位建议结果。"""

    win_rate: float
    payoff_ratio: float          # 盈亏比 b = 平均盈利 / 平均亏损
    full_kelly: float            # 全 Kelly 比例 f*
    fraction: float              # 实际使用倍数（如 0.25）
    suggested_fraction: float    # 建议仓位占本金比例（已夹紧）
    risk_budget_pct: float       # 折算后建议单笔风险占本金 %
    note: str


def kelly_fraction(win_rate: float, payoff_ratio: float) -> float:
    """经典 Kelly：f* = (b p - q) / b，其中 q = 1-p。

    负期望返回 0。
    """
    if payoff_ratio <= 0 or not (0.0 < win_rate < 1.0):
        return 0.0
    q = 1.0 - win_rate
    f = (payoff_ratio * win_rate - q) / payoff_ratio
    return max(0.0, f)


def suggest_position(
    win_rate: float,
    payoff_ratio: float,
    *,
    kelly_scale: float = 0.25,
    max_fraction: float = 0.10,
    max_risk_per_trade_pct: float = 1.0,
) -> KellySize:
    """给出保守仓位建议。

    Args:
        win_rate: 预估胜率 p
        payoff_ratio: 预估盈亏比 b（= R:R）
        kelly_scale: 1.0=全Kelly, 0.5=半Kelly, 0.25=四分之一Kelly（默认）
        max_fraction: 仓位占总资金硬上限
        max_risk_per_trade_pct: 单笔风险占本金上限（%）
    """
    full = kelly_fraction(win_rate, payoff_ratio)
    scaled = full * kelly_scale
    capped = min(scaled, max_fraction)
    risk_pct = min(capped * 100.0, max_risk_per_trade_pct)

    if full <= 0:
        note = "期望为负或参数无效，建议观望、不开仓。"
    elif capped < scaled:
        note = f"Kelly×{kelly_scale:g}={scaled:.2%} 触达上限 {max_fraction:.0%}，已夹紧。"
    else:
        note = f"采用 {kelly_scale:g}×Kelly（加密合约常用更保守档）。"

    return KellySize(
        win_rate=win_rate,
        payoff_ratio=payoff_ratio,
        full_kelly=full,
        fraction=kelly_scale,
        suggested_fraction=capped,
        risk_budget_pct=risk_pct,
        note=note,
    )
