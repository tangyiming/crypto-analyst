#!/usr/bin/env python3
"""Jack「强势盘低多」事件式回测（限价回踩入场 / 止损 / 一半止盈 / 4h 中轨离场，含费率+滑点+资金费）。

用法（项目根）：
  .venv/bin/python scripts/backtest_jack_pullback.py                       # BTC ETH SOL, 2023-01 起
  .venv/bin/python scripts/backtest_jack_pullback.py SOL/USDT --start 2024-01-01 --risk 1 --lev 3
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from analyst.compute.strategies.jack_pullback import JackPullbackConfig, run_backtest  # noqa: E402
from analyst.data.fetcher import fetch_candles_history  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", default=["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--days", type=int, default=1400)
    ap.add_argument("--risk", type=float, default=1.0)
    ap.add_argument("--lev", type=float, default=3.0)
    ap.add_argument("--no-funding", action="store_true")
    ap.add_argument("--trades", action="store_true", help="打印每笔")
    a = ap.parse_args()
    cfg = JackPullbackConfig(risk_pct=a.risk, max_leverage=a.lev)
    start = datetime.fromisoformat(a.start)
    for sym in a.symbols:
        hourly = fetch_candles_history(sym, "1h", days=a.days, market="futures")
        h4 = fetch_candles_history(sym, "4h", days=a.days, market="futures")
        daily = fetch_candles_history(sym, "1d", days=a.days + 400, market="futures")
        funding = None
        if not a.no_funding:
            try:
                from analyst.data.derivatives import fetch_funding_history

                funding = fetch_funding_history(sym, days=a.days)
            except Exception as e:  # noqa: BLE001
                print(f"  funding 不可用（{e}），按 0 计")
        r = run_backtest(sym, hourly, h4, daily, cfg=cfg, start=start, funding=funding)
        bh = h4.candles[-1].close / next(c.close for c in h4.candles if c.timestamp >= start) - 1
        print(
            f"\n== {sym} {a.start}→{h4.candles[-1].timestamp.date()}  笔数 {r['n']}  胜率 {r['win_rate']*100:.0f}%  "
            f"总收益 {r['total_return_pct']:+.1f}%  最大回撤 {r['max_dd_pct']:.1f}%  盈亏因子 {r['profit_factor']:.2f}  "
            f"平均每笔 {r['avg_trade_usd']:+.0f}U  (买入持有 {bh*100:+.0f}%)"
        )
        reasons: dict[str, int] = {}
        for t in r["trades"]:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        print("   离场原因分布:", reasons)
        fund = sum(t.funding_usd for t in r["trades"])
        fees = sum(t.fees_usd for t in r["trades"])
        print(f"   费用合计 {fees:.0f}U · 资金费合计 {fund:+.0f}U")
        if a.trades:
            for t in r["trades"]:
                net = t.pnl_usd - t.fees_usd + t.funding_usd
                print(
                    f"   {t.entry_time:%Y-%m-%d %H:%M} 入 {t.entry:.6g} 损 {t.stop:.6g} 盈1 {t.tp1:.6g} "
                    f"→ {t.exit_time:%m-%d %H:%M} {t.exit_reason:14} {net:+8.1f}U"
                )


if __name__ == "__main__":
    main()
