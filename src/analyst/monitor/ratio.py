"""汇率对（相对强弱）监控：ETH/BTC 等合成序列 + 状态机信号。

不交易、不进策略，只做监控视野：
  · ema_state — 汇率 vs 200 日 EMA（带迟滞防抖）：above=资金外溢山寨，below=BTC 独强
  · break    — N 日新高/新低突破

合成方式：ETH/BTC = ETH/USDT 收盘 ÷ BTC/USDT 收盘（按时间戳对齐取交集）。
只用收盘价：两腿的 high/low 不同时发生，合成 OHLC 没有意义。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from analyst.compute.indicators import ema
from analyst.data.fetcher import Candle


@dataclass(frozen=True)
class RatioState:
    ratio: float
    ema_state: str          # above | below
    ema_dist_pct: float     # 距 EMA 百分比（正=上方）
    breakout: str | None    # high | low | None（当根是否创 N 根新高/新低）


def parse_ratio_pair(pair: str) -> tuple[str, str] | None:
    """'ETH/BTC' → ('ETH/USDT', 'BTC/USDT')；非法返回 None。"""
    p = pair.strip().upper()
    if "/" not in p:
        return None
    num, den = p.split("/", 1)
    if not num or not den or num == den:
        return None
    return f"{num}/USDT", f"{den}/USDT"


def build_ratio_closes(
    num_candles: Sequence[Candle],
    den_candles: Sequence[Candle],
) -> tuple[list[datetime], list[float]]:
    """按时间戳交集合成汇率收盘序列（时间升序）。"""
    den_by_ts = {c.timestamp: c.close for c in den_candles if c.close > 0}
    ts_list: list[datetime] = []
    closes: list[float] = []
    for c in num_candles:
        d = den_by_ts.get(c.timestamp)
        if d:
            ts_list.append(c.timestamp)
            closes.append(c.close / d)
    return ts_list, closes


def evaluate_ratio_state(
    closes: Sequence[float],
    *,
    ema_n: int,
    band: float = 0.02,
    break_n: int = 240,
) -> RatioState | None:
    """从头走一遍带迟滞的 EMA 状态机，返回最新状态；历史不足返回 None。

    迟滞：> EMA×(1+band) 才翻 above，< EMA×(1-band) 才翻 below，
    带内沿用旧状态（同 cycle_switch 的 200 日线防抖口径）。
    """
    if len(closes) < max(ema_n, break_n) + 1:
        return None
    e = ema(list(closes), ema_n)
    state = "above"
    for i, c in enumerate(closes):
        if c > e[i] * (1 + band):
            state = "above"
        elif c < e[i] * (1 - band):
            state = "below"
    last = closes[-1]
    window = closes[-break_n - 1 : -1]
    breakout: str | None = None
    if last > max(window):
        breakout = "high"
    elif last < min(window):
        breakout = "low"
    return RatioState(
        ratio=last,
        ema_state=state,
        ema_dist_pct=round((last / e[-1] - 1.0) * 100, 2),
        breakout=breakout,
    )
