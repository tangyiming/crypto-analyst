#!/usr/bin/env python3
"""Eric 周线超卖波段计划回测（复现 eric_swing.py 的规则，日线撮合）。

用法（项目根）：
  .venv/bin/python scripts/backtest_eric_swing.py                # BTC/USDT ETH/USDT 现货 2018-
  .venv/bin/python scripts/backtest_eric_swing.py SOL/USDT --days 2500

规则：周线过滤器超卖 → 读数拐头开多（收盘）；止损=段最低−3%；一半在 +25% / 周EMA21(≥+8%) /
日线超买(≥+5%) 任一止盈并把止损提到成本；余仓从最高回撤 20% 离场；最长 52 周。
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from analyst.compute.eric_swing import advance  # noqa: E402
from analyst.data.fetcher import CandleSeries, fetch_candles_history  # noqa: E402


def to_weekly(daily: CandleSeries) -> CandleSeries:
    from analyst.data.fetcher import Candle

    wk: dict = {}
    for c in daily.candles:
        mon = (c.timestamp - timedelta(days=c.timestamp.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        if mon not in wk:
            wk[mon] = [c.open, c.high, c.low, c.close, c.volume]
        else:
            w = wk[mon]
            w[1] = max(w[1], c.high)
            w[2] = min(w[2], c.low)
            w[3] = c.close
            w[4] += c.volume
    return CandleSeries(daily.symbol, "1w", [Candle(timestamp=k, open=v[0], high=v[1], low=v[2], close=v[3], volume=v[4]) for k, v in sorted(wk.items())])


def run(symbol: str, days: int, market: str) -> None:
    daily = fetch_candles_history(symbol, "1d", days=days, market=market)
    weekly_all = to_weekly(daily)
    state: dict = {}
    trades: list[dict] = []
    open_trade: dict | None = None
    d = daily.candles
    for i in range(60, len(d)):
        # 当前日线收盘时刻可用的已收盘周线：周一开盘时间 + 7d <= 该日收盘
        cutoff = d[i].timestamp + timedelta(days=1)
        wk_closed = [w for w in weekly_all.candles if w.timestamp + timedelta(days=7) <= cutoff]
        if len(wk_closed) < 60:
            continue
        weekly = CandleSeries(symbol, "1w", wk_closed)
        events, state = advance(state, weekly, CandleSeries(symbol, "1d", d[: i + 1]))
        for ev in events:
            if ev.kind == "entry_half":
                open_trade = {"entry_date": ev.marker_time, "entry": ev.price, "size": 0.5, "legs": [], "half": True}
            elif ev.kind == "entry":
                if open_trade and open_trade.get("half"):
                    open_trade["entry"] = float(ev.plan.get("entry") or ev.price)  # 均价
                    open_trade["size"] = 1.0
                else:
                    open_trade = {"entry_date": ev.marker_time, "entry": ev.price, "size": 1.0, "legs": []}
            elif ev.kind in ("tp1",) and open_trade:
                open_trade["legs"].append((0.5, ev.price / open_trade["entry"] - 1))
            elif ev.kind in ("tp2", "stop") and open_trade:
                w = 0.5 if open_trade["legs"] else 1.0
                open_trade["legs"].append((w, ev.price / open_trade["entry"] - 1))
                open_trade["exit_kind"] = ev.kind
                open_trade["exit_date"] = ev.marker_time
                trades.append(open_trade)
                open_trade = None
    from datetime import datetime, timezone

    print(f"\n== {symbol} ({market}, {days}d) 已平仓 {len(trades)} 笔" + ("，另有 1 笔持仓中" if open_trade else ""))
    eq = 1.0
    for t in trades:
        r = sum(w * x for w, x in t["legs"]) * float(t.get("size") or 1.0)
        eq *= 1 + r
        print(
            f"  {datetime.fromtimestamp(t['entry_date'], tz=timezone.utc):%Y-%m-%d} @{t['entry']:.6g} "
            f"{'半仓' if float(t.get('size') or 1) < 1 else ('半仓→加满' if t.get('half') else '  全仓  ')} → "
            f"{datetime.fromtimestamp(t['exit_date'], tz=timezone.utc):%Y-%m-%d} {t['exit_kind']:4} {r*100:+6.1f}%"
        )
    if trades:
        rs = [sum(w * x for w, x in t["legs"]) * float(t.get("size") or 1.0) for t in trades]
        print(f"  复利 {(eq-1)*100:+.1f}%  胜率 {sum(1 for r in rs if r>0)/len(rs)*100:.0f}%  最差 {min(rs)*100:+.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", default=["BTC/USDT", "ETH/USDT"])
    ap.add_argument("--days", type=int, default=3000)
    ap.add_argument("--market", default="spot")
    a = ap.parse_args()
    for sym in a.symbols:
        run(sym, a.days, a.market)


if __name__ == "__main__":
    main()
