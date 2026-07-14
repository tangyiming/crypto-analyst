"""真实走势验证算法 - 核心逻辑。"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from analyst.compute.plan import TradePlan
from analyst.data.fetcher import Candle, fetch_candles


class TradeOutcome(str, Enum):
    NO_TRIGGER = "no_trigger"     # 未入场
    WIN_TP1 = "win_tp1"           # 命中第一目标
    WIN_TP2 = "win_tp2"           # 命中第二目标
    LOSS = "loss"                 # 止损
    OPEN = "open"                 # 仍未结算


@dataclass
class VerifyResult:
    triggered: bool
    outcome: TradeOutcome
    pnl_r: float                  # R 倍数（risk = 1）
    entry_price: float | None
    exit_price: float | None
    duration_hours: float | None


def verify_plan(plan: TradePlan, future_candles: list[Candle]) -> VerifyResult:
    """验证一个计划在未来 K线上的实际表现。

    关键算法：
    1. 找入场时刻
    2. 入场后顺序判定（同根 K线触发止损+止盈，保守按止损）
    3. 计算实际 R 倍数
    """
    if plan.direction == "wait":
        return VerifyResult(
            triggered=False,
            outcome=TradeOutcome.NO_TRIGGER,
            pnl_r=0.0,
            entry_price=None,
            exit_price=None,
            duration_hours=None,
        )

    if not future_candles:
        return VerifyResult(
            triggered=False,
            outcome=TradeOutcome.NO_TRIGGER,
            pnl_r=0.0,
            entry_price=None,
            exit_price=None,
            duration_hours=None,
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 1. 找入场时刻
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    entry_idx: int | None = None
    entry_price: float | None = None

    for i, c in enumerate(future_candles):
        # K线是否穿越入场区
        if c.low <= plan.entry_high and c.high >= plan.entry_low:
            entry_price = (plan.entry_low + plan.entry_high) / 2
            entry_idx = i
            break

    if entry_idx is None or entry_price is None:
        return VerifyResult(
            triggered=False,
            outcome=TradeOutcome.NO_TRIGGER,
            pnl_r=0.0,
            entry_price=None,
            exit_price=None,
            duration_hours=None,
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 2. 入场后顺序判定
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    risk = abs(entry_price - plan.stop_loss)
    if risk == 0:
        return VerifyResult(
            triggered=True,
            outcome=TradeOutcome.OPEN,
            pnl_r=0.0,
            entry_price=entry_price,
            exit_price=None,
            duration_hours=0.0,
        )

    entry_time = future_candles[entry_idx].timestamp

    for i in range(entry_idx + 1, len(future_candles)):
        c = future_candles[i]

        if plan.direction == "long":
            hit_stop = c.low <= plan.stop_loss
            hit_tp1 = c.high >= plan.take_profit_1
            hit_tp2 = (
                plan.take_profit_2 is not None
                and c.high >= plan.take_profit_2
            )
        else:  # short
            hit_stop = c.high >= plan.stop_loss
            hit_tp1 = c.low <= plan.take_profit_1
            hit_tp2 = (
                plan.take_profit_2 is not None
                and c.low <= plan.take_profit_2
            )

        # 保守判定：同根 K线触发止损 + 止盈，按止损算
        if hit_stop:
            return VerifyResult(
                triggered=True,
                outcome=TradeOutcome.LOSS,
                pnl_r=-1.0,
                entry_price=entry_price,
                exit_price=plan.stop_loss,
                duration_hours=_hours_between(entry_time, c.timestamp),
            )

        if hit_tp2 and plan.take_profit_2 is not None:
            reward = abs(plan.take_profit_2 - entry_price)
            return VerifyResult(
                triggered=True,
                outcome=TradeOutcome.WIN_TP2,
                pnl_r=reward / risk,
                entry_price=entry_price,
                exit_price=plan.take_profit_2,
                duration_hours=_hours_between(entry_time, c.timestamp),
            )

        if hit_tp1:
            reward = abs(plan.take_profit_1 - entry_price)
            return VerifyResult(
                triggered=True,
                outcome=TradeOutcome.WIN_TP1,
                pnl_r=reward / risk,
                entry_price=entry_price,
                exit_price=plan.take_profit_1,
                duration_hours=_hours_between(entry_time, c.timestamp),
            )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 3. 没触发任何止损/止盈，仍持有
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    last = future_candles[-1]
    if plan.direction == "long":
        floating_r = (last.close - entry_price) / risk
    else:
        floating_r = (entry_price - last.close) / risk

    return VerifyResult(
        triggered=True,
        outcome=TradeOutcome.OPEN,
        pnl_r=floating_r,
        entry_price=entry_price,
        exit_price=None,
        duration_hours=_hours_between(entry_time, last.timestamp),
    )


def find_optimal_trade(
    plan_direction: str,
    candles: list[Candle],
) -> VerifyResult:
    """计算"完美交易者"的最优收益（参考线）。

    简化模型：假设最优入场最优出场，risk 用 1% 价格基准估算。
    """
    if not candles or plan_direction == "wait":
        return VerifyResult(
            triggered=False,
            outcome=TradeOutcome.NO_TRIGGER,
            pnl_r=0.0,
            entry_price=None,
            exit_price=None,
            duration_hours=None,
        )

    if plan_direction == "long":
        min_low = min(c.low for c in candles)
        max_after = 0.0
        for i, c in enumerate(candles):
            if c.low == min_low:
                rest = candles[i:]
                max_after = max(c2.high for c2 in rest) if rest else c.low
                break
        if max_after <= min_low:
            return VerifyResult(
                triggered=False,
                outcome=TradeOutcome.NO_TRIGGER,
                pnl_r=0.0,
                entry_price=None,
                exit_price=None,
                duration_hours=None,
            )
        risk = min_low * 0.01
        pnl_r = (max_after - min_low) / risk
        return VerifyResult(
            triggered=True,
            outcome=TradeOutcome.WIN_TP2,
            pnl_r=pnl_r,
            entry_price=min_low,
            exit_price=max_after,
            duration_hours=None,
        )

    # short
    max_high = max(c.high for c in candles)
    min_after = float("inf")
    for i, c in enumerate(candles):
        if c.high == max_high:
            rest = candles[i:]
            min_after = min(c2.low for c2 in rest) if rest else c.high
            break
    if min_after >= max_high:
        return VerifyResult(
            triggered=False,
            outcome=TradeOutcome.NO_TRIGGER,
            pnl_r=0.0,
            entry_price=None,
            exit_price=None,
            duration_hours=None,
        )
    risk = max_high * 0.01
    pnl_r = (max_high - min_after) / risk
    return VerifyResult(
        triggered=True,
        outcome=TradeOutcome.WIN_TP2,
        pnl_r=pnl_r,
        entry_price=max_high,
        exit_price=min_after,
        duration_hours=None,
    )


def fetch_future_candles(
    symbol: str,
    since: datetime,
    timeframe: str = "1h",
) -> list[Candle]:
    """获取从 since 到现在的 K线（用于验证）。

    Args:
        since: naive UTC datetime
    """
    series = fetch_candles(symbol, timeframe=timeframe, limit=300, use_cache=False)
    return [c for c in series.candles if c.timestamp > since]


def _hours_between(t1: datetime, t2: datetime) -> float:
    return abs((t2 - t1).total_seconds()) / 3600.0
