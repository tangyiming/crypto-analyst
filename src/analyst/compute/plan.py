"""交易计划生成器 - 规则基线方案。

这是给 AI 之外的"基线"，让用户对比 AI 是否真的更聪明，
还是只是看起来更聪明。
可选融合「波段锁点」(JackLevels)：日线定调 + 反弹 0.382/0.618 目标。
"""

from dataclasses import dataclass

from analyst.compute.fibonacci import FibLevels
from analyst.compute.structure import Structure

try:
    from analyst.compute.jack_levels import JackLevels
except ImportError:  # pragma: no cover
    JackLevels = None  # type: ignore


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


def _fmt(x: float) -> str:
    return f"{x:.4f}" if abs(x) < 1000 else f"{x:.2f}"


def generate_baseline_plan(
    current_price: float,
    fib: FibLevels,
    structure: Structure,
    min_rr: float = 2.0,
    jack: "JackLevels | None" = None,
) -> TradePlan:
    """规则基线计划。

    规则：
    - 大方向跟随 trend（若有 jack，则日线定调优先）
    - 入场区 = 0.5-0.618 fib
    - 止损 = 0.786 外 / jack.defense
    - 止盈 1 = 最近反向关键位或 rebound_382/618
    - 止盈 2 = 1.272 扩展或 rebound_618
    - R:R < min_rr → wait
    - jack.htf_ready=False 时 rationale 标明「只做短线反抽」
    """
    bias = structure.trend
    if jack is not None and jack.daily_bias in ("up", "down", "range"):
        bias = (
            "up" if jack.daily_bias == "up"
            else "down" if jack.daily_bias == "down"
            else "range"
        )

    horizon_note = ""
    if jack is not None and not jack.htf_ready:
        horizon_note = "高周期未成熟，只做短线反抽。"

    if bias == "up":
        entry_low = fib.retr_618
        entry_high = fib.retr_500
        stop_loss = jack.defense_level if jack is not None else fib.retr_786
        # 下半区=超卖反弹锁点；上半区=趋势回踩，目标用结构阻力/扩展
        range_mid = (fib.high + fib.low) / 2
        bounce_mode = current_price <= range_mid
        if jack is not None and bounce_mode:
            # 已越过 0.382 则主看 0.618；仍在下方则近压 0.382
            if current_price >= jack.rebound_382:
                target1 = jack.rebound_618
                target2 = structure.resistances[0] if structure.resistances else fib.high
                if target2 <= target1:
                    target2 = fib.high
            else:
                target1 = jack.rebound_382
                target2 = jack.rebound_618
                if structure.resistances:
                    r0 = structure.resistances[0]
                    if current_price < r0 < target1:
                        target1 = r0
        elif jack is not None:
            target1 = structure.resistances[0] if structure.resistances else fib.high
            target2 = jack.rebound_618 if jack.rebound_618 > current_price else fib.ext_1272
        else:
            target1 = structure.resistances[0] if structure.resistances else fib.high
            target2 = fib.ext_1272

        entry_mid = (entry_low + entry_high) / 2
        if bounce_mode and jack is not None:
            # 反弹模式：现价附近低多，目标锁 0.382/0.618
            trial_entry = current_price
            rr = calculate_rr(trial_entry, stop_loss, target1, "long")
            if rr >= min_rr and stop_loss < trial_entry < target1:
                conf = ""
                if jack.confluence_382 or jack.confluence_618:
                    conf = "斐波与 BOLL 中轨共振。"
                return TradePlan(
                    direction="long",
                    entry_low=min(trial_entry * 0.997, trial_entry),
                    entry_high=trial_entry,
                    stop_loss=stop_loss,
                    take_profit_1=target1,
                    take_profit_2=target2,
                    rr_ratio=rr,
                    rationale=(
                        f"日线偏多·反弹锁点：近目标={target1:.4f}，"
                        f"延伸={target2:.4f}；防守 {stop_loss:.4f}（R:R={rr:.2f}）。"
                        f"{conf}{horizon_note}"
                    ),
                )
            return _wait_plan(
                f"反弹锁点已给出，但现价相对防守 R:R={rr:.2f} 不足；"
                f"观察 {_fmt(target1)} / {_fmt(target2)}。{horizon_note}",
                rr=rr,
            )

        # 仅在有 jack 时允许「现价头仓」；否则保持经典回踩区入场
        if current_price > entry_high and jack is not None:
            trial_entry = current_price
            rr = calculate_rr(trial_entry, stop_loss, target1, "long")
            if rr >= min_rr and stop_loss < trial_entry < target1:
                return TradePlan(
                    direction="long",
                    entry_low=min(trial_entry * 0.998, trial_entry),
                    entry_high=trial_entry,
                    stop_loss=stop_loss,
                    take_profit_1=target1,
                    take_profit_2=target2,
                    rr_ratio=rr,
                    rationale=(
                        f"日线偏多，现价未回踩至 0.5-0.618；"
                        f"可用小头仓防踏空，止损 {stop_loss:.4f}，"
                        f"近压 {target1:.4f} / 主目标 {target2:.4f}（R:R={rr:.2f}）。"
                        f"{horizon_note}"
                    ),
                )
            return _wait_plan(
                f"上涨定调但已离开回踩区且 R:R={rr:.2f} 不足，等待回踩 "
                f"{entry_low:.4f}-{entry_high:.4f} 或观望。{horizon_note}",
                rr=rr,
            )

        rr = calculate_rr(entry_mid, stop_loss, target1, "long")
        if rr < min_rr:
            return _wait_plan(
                f"上涨结构但 R:R={rr:.2f} 不足 {min_rr}，建议观望。{horizon_note}",
                rr=rr,
            )

        conf = ""
        if jack is not None and (jack.confluence_382 or jack.confluence_618):
            conf = "斐波与 BOLL 中轨共振。"
        return TradePlan(
            direction="long",
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profit_1=target1,
            take_profit_2=target2,
            rr_ratio=rr,
            rationale=(
                f"日线偏多，回踩 0.5-0.618 低多，"
                f"止损/防守 {stop_loss:.4f}，"
                f"近压 {target1:.4f}、主目标 {target2:.4f}（R:R={rr:.2f}）。"
                f"{conf}{horizon_note}"
            ),
        )

    if bias == "down":
        entry_low = fib.rebound_500
        entry_high = fib.rebound_618
        stop_loss = jack.defense_level if jack is not None else fib.rebound_786
        target1 = structure.supports[0] if structure.supports else fib.low
        target2 = fib.low - fib.range * 0.272

        entry_mid = (entry_low + entry_high) / 2
        rr = calculate_rr(entry_mid, stop_loss, target1, "short")

        if rr < min_rr:
            return _wait_plan(
                f"下跌结构但 R:R={rr:.2f} 不足 {min_rr}，建议观望。{horizon_note}",
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
                f"日线偏空，反弹 0.5-0.618 高空，"
                f"止损/防守 {stop_loss:.4f}，"
                f"目标 {target1:.4f}（R:R={rr:.2f}）。{horizon_note}"
            ),
        )

    if jack is not None:
        return _wait_plan(
            f"震荡定调。反抽观察 {_fmt(jack.rebound_382)}，"
            f"大反弹观察 {_fmt(jack.rebound_618)}；"
            f"上破/下破边界再动手。{horizon_note}"
        )
    return _wait_plan("震荡市无明确方向，建议观望。")
