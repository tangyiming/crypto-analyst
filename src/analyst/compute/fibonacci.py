"""斐波那契回撤与扩展 - 这部分数学清晰，Sprint 1 直接实现。"""

from dataclasses import dataclass


@dataclass
class FibLevels:
    high: float
    low: float
    range: float

    # 回撤位（上涨后回调）
    retr_236: float
    retr_382: float
    retr_500: float
    retr_618: float
    retr_786: float

    # 反弹位（下跌后反弹）
    rebound_236: float
    rebound_382: float
    rebound_500: float
    rebound_618: float
    rebound_786: float

    # 扩展位（突破前高后的目标）
    ext_1272: float
    ext_1618: float
    ext_2000: float


def compute_fib(high: float, low: float) -> FibLevels:
    """计算斐波关键位。

    输入：波段高低点
    输出：所有关键位
    """
    rng = high - low
    return FibLevels(
        high=high,
        low=low,
        range=rng,
        # 回撤
        retr_236=high - rng * 0.236,
        retr_382=high - rng * 0.382,
        retr_500=high - rng * 0.500,
        retr_618=high - rng * 0.618,
        retr_786=high - rng * 0.786,
        # 反弹
        rebound_236=low + rng * 0.236,
        rebound_382=low + rng * 0.382,
        rebound_500=low + rng * 0.500,
        rebound_618=low + rng * 0.618,
        rebound_786=low + rng * 0.786,
        # 扩展
        ext_1272=high + rng * 0.272,
        ext_1618=high + rng * 0.618,
        ext_2000=high + rng * 1.000,
    )


def find_long_zone(levels: FibLevels) -> tuple[float, float]:
    """找最佳低多区间（0.5-0.618）。"""
    return (levels.retr_618, levels.retr_500)


def find_short_zone(levels: FibLevels) -> tuple[float, float]:
    """找最佳高空区间（0.5-0.618 反弹）。"""
    return (levels.rebound_500, levels.rebound_618)
