"""交易计划生成器 - 规则基线方案。

这是给 AI 之外的"基线"，让用户对比 AI 是否真的更聪明，
还是只是看起来更聪明。
"""

from dataclasses import dataclass

from analyst.compute.fibonacci import FibLevels
from analyst.compute.structure import Structure


@dataclass
class TradePlan:
    direction: str                   # 'long' / 'short' / 'wait'
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None
    rr_ratio: float
    rationale: str


def calculate_rr(entry: float, stop: float, target: float, direction: str) -> float:
    """计算盈亏比。"""
    if direction == "long":
        risk = abs(entry - stop)
        reward = abs(target - entry)
    elif direction == "short":
        risk = abs(stop - entry)
        reward = abs(entry - target)
    else:
        return 0.0
    return reward / risk if risk > 0 else 0.0


def _wait_plan(reason: str, rr: float = 0.0) -> TradePlan:
    return TradePlan(
        direction="wait",
        entry_low=0.0,
        entry_high=0.0,
        stop_loss=0.0,
        take_profit_1=0.0,
        take_profit_2=None,
        rr_ratio=rr,
        rationale=reason,
    )


def generate_baseline_plan(
    current_price: float,
    fib: FibLevels,
    structure: Structure,
    min_rr: float = 2.0,
) -> TradePlan:
    """规则基线计划。

    规则：
    - 大方向跟随 trend
    - 入场区 = 0.5-0.618 fib
    - 止损 = 0.786 外
    - 止盈 1 = 最近反向关键位
    - 止盈 2 = 1.272 扩展
    - R:R < min_rr → wait
    """
    if structure.trend == "up":
        entry_low = fib.retr_618
        entry_high = fib.retr_500
        stop_loss = fib.retr_786
        target1 = structure.resistances[0] if structure.resistances else fib.high
        target2 = fib.ext_1272

        entry_mid = (entry_low + entry_high) / 2
        rr = calculate_rr(entry_mid, stop_loss, target1, "long")

        if rr < min_rr:
            return _wait_plan(
                f"上涨结构但 R:R={rr:.2f} 不足 {min_rr}，建议观望。",
                rr=rr,
            )

        return TradePlan(
            direction="long",
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profit_1=target1,
            take_profit_2=target2,
            rr_ratio=rr,
            rationale=(
                f"上涨趋势，回踩 0.5-0.618 区间低多，"
                f"止损 0.786 下方 {stop_loss:.2f}，"
                f"目标前高 {target1:.2f}（R:R={rr:.2f}）。"
            ),
        )

    if structure.trend == "down":
        entry_low = fib.rebound_500
        entry_high = fib.rebound_618
        stop_loss = fib.rebound_786
        target1 = structure.supports[0] if structure.supports else fib.low
        target2 = fib.low - fib.range * 0.272

        entry_mid = (entry_low + entry_high) / 2
        rr = calculate_rr(entry_mid, stop_loss, target1, "short")

        if rr < min_rr:
            return _wait_plan(
                f"下跌结构但 R:R={rr:.2f} 不足 {min_rr}，建议观望。",
                rr=rr,
            )

        return TradePlan(
            direction="short",
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profit_1=target1,
            take_profit_2=target2,
            rr_ratio=rr,
            rationale=(
                f"下跌趋势，反弹 0.5-0.618 区间高空，"
                f"止损 0.786 上方 {stop_loss:.2f}，"
                f"目标前低 {target1:.2f}（R:R={rr:.2f}）。"
            ),
        )

    return _wait_plan("震荡市无明确方向，建议观望。")
