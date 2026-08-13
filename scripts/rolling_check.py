"""滚动窗口稳健性对比：cycle_switch vs tsmom（180 天窗 × 5 年 × 4 币）。

tsmom 已从策略库删除，这里内联定义仅作对照验证。
用法：.venv/bin/python scripts/rolling_check.py
"""
from __future__ import annotations

from analyst.backtest.classic import (
    CostModel,
    rolling_window_report,
)
from analyst.compute.strategies.cycle_switch import (
    build_cycle_regime,
    positions_cycle_switch,
)
from analyst.data.fetcher import Candle, fetch_candles_history

SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "UNI/USDT"]


def positions_tsmom(
    candles: list[Candle],
    lookback: int = 180,
    band: float = 0.05,
    short_size: float = 0.5,
) -> list[float]:
    closes = [c.close for c in candles]
    pos = 0.0
    out: list[float] = []
    for i in range(len(closes)):
        if i < lookback:
            out.append(0.0)
            continue
        base = closes[i - lookback]
        ret = closes[i] / base - 1.0 if base > 0 else 0.0
        if ret > band:
            pos = 1.0
        elif ret < -band:
            pos = -short_size
        elif pos > 0 and ret < 0:
            pos = 0.0
        elif pos < 0 and ret > 0:
            pos = 0.0
        out.append(pos)
    return out


series_map = {
    s: fetch_candles_history(s, "4h", days=1825, market="futures").candles
    for s in SYMBOLS
}
regime = build_cycle_regime(series_map["BTC/USDT"])
cost = CostModel()

for name, fn in (
    ("cycle_switch", lambda c: positions_cycle_switch(c, regime)),
    ("tsmom", positions_tsmom),
):
    all_windows: list[float] = []
    for sym, candles in series_map.items():
        reps = rolling_window_report(
            candles, fn(candles),
            strategy=name, symbol=sym, timeframe="4h",
            window_days=180, cost=cost,
        )
        all_windows.extend(r.total_return_pct for r in reps)
    all_windows.sort()
    n = len(all_windows)
    pos_share = sum(1 for r in all_windows if r > 0) / n
    median = all_windows[n // 2]
    print(
        f"{name:14s} 窗口数={n:3d} 正收益窗口={pos_share:5.0%} "
        f"中位数={median:+6.1f}% 最差={all_windows[0]:+7.1f}% "
        f"最好={all_windows[-1]:+7.1f}% 最差5均={sum(all_windows[:5]) / 5:+7.1f}%"
    )
