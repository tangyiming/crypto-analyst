#!/usr/bin/env python3
"""对比双线三档配置：基线 / 量能+ADX / 条件胜率。

用法（仓库根目录）:
  .venv/bin/python scripts/compare_double_line_backtest.py
  .venv/bin/python scripts/compare_double_line_backtest.py --days 1461 --tfs 15m,1h,4h
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from analyst.backtest.engine import run_backtest
from analyst.compute.strategies.double_line_reversal import DoubleLineConfig
from analyst.data.fetcher import fetch_candles, fetch_candles_history


def _cfg_variants(base: DoubleLineConfig) -> dict[str, DoubleLineConfig]:
    return {
        "A_基线(无过滤)": replace(
            base,
            require_volume=False,
            require_adx=False,
            use_conditional_edge=False,
        ),
        "B_量能+ADX": replace(
            base,
            require_volume=True,
            require_adx=True,
            use_conditional_edge=False,
        ),
        "C_过滤+条件胜率": replace(
            base,
            require_volume=True,
            require_adx=True,
            use_conditional_edge=True,
        ),
    }


def _summarize(report) -> dict:
    closed = report.closed_trades
    tp_sl = [t for t in closed if t.outcome in ("tp", "sl")]
    loose_wins = sum(
        1
        for t in closed
        if t.outcome == "tp" or (t.outcome == "timeout" and t.pnl_r > 0)
    )
    return {
        "trades": len(closed),
        "win_rate": report.win_rate,
        "loose_win_rate": (loose_wins / len(closed)) if closed else 0.0,
        "total_r": report.total_r,
        "weighted_r": report.total_weighted_r,
        "avg_r": report.avg_r,
        "max_dd_r": report.max_drawdown_r,
        "tp": sum(1 for t in tp_sl if t.outcome == "tp"),
        "sl": sum(1 for t in tp_sl if t.outcome == "sl"),
    }


def _norm_sym(sym: str) -> str:
    s = sym.strip().upper()
    if "/" not in s:
        s = f"{s}/USDT"
    return s


def main() -> int:
    p = argparse.ArgumentParser(description="双线回测三档对比")
    p.add_argument("--symbols", default="BTC,ETH,SOL")
    p.add_argument("--tfs", default="15m,1h,4h")
    p.add_argument(
        "--days",
        type=int,
        default=0,
        help="长历史天数（>0 时用分页拉取，忽略 --bars）",
    )
    p.add_argument("--bars", type=int, default=1500)
    p.add_argument("--market", default="futures")
    p.add_argument("--max-hold", type=int, default=96)
    p.add_argument(
        "--lookback",
        type=int,
        default=320,
        help="策略评估窗口根数（多年回测务必保留，默认 320）",
    )
    args = p.parse_args()

    symbols = [_norm_sym(s) for s in args.symbols.split(",") if s.strip()]
    tfs = [t.strip().lower() for t in args.tfs.split(",") if t.strip()]

    base = DoubleLineConfig(
        require_ema200=True,
        require_ema_slope=False,
        require_volume=True,
        require_adx=True,
        use_conditional_edge=True,
        min_conditional_win_rate=0.42,
    )
    variants = _cfg_variants(base)

    agg: dict[str, dict[str, float]] = {
        name: {
            "trades": 0,
            "wins": 0,
            "resolved": 0,
            "loose_wins": 0,
            "total_r": 0.0,
            "weighted_r": 0.0,
        }
        for name in variants
    }

    t0 = time.time()
    for sym in symbols:
        for tf in tfs:
            label = f"{sym} {tf}"
            if args.days > 0:
                print(f"\n拉取 {label} · 近 {args.days} 天 …", flush=True)
                series = fetch_candles_history(
                    sym,
                    timeframe=tf,
                    days=args.days,
                    market=args.market,
                    use_cache=True,
                )
            else:
                print(f"\n拉取 {label} × {args.bars} …", flush=True)
                series = fetch_candles(
                    sym,
                    timeframe=tf,
                    limit=min(args.bars, 1500),
                    use_cache=False,
                    market=args.market,
                )
            n = len(series.candles)
            if n < 80:
                print(f"  跳过：仅 {n} 根", flush=True)
                continue
            span = ""
            if series.candles:
                a, b = series.candles[0].timestamp, series.candles[-1].timestamp
                span = f"{a:%Y-%m-%d} → {b:%Y-%m-%d} ({n} 根)"
            print(f"  数据：{span}", flush=True)

            for name, cfg in variants.items():
                t1 = time.time()
                rep = run_backtest(
                    series.symbol,
                    tf,
                    series=series,
                    strategy_cfg=cfg,
                    include_rules=False,
                    max_hold=args.max_hold,
                    lookback=args.lookback,
                )
                s = _summarize(rep)
                closed_tp_sl = [
                    t for t in rep.closed_trades if t.outcome in ("tp", "sl")
                ]
                agg[name]["trades"] += s["trades"]
                agg[name]["wins"] += sum(1 for t in closed_tp_sl if t.outcome == "tp")
                agg[name]["resolved"] += len(closed_tp_sl)
                agg[name]["loose_wins"] += s["loose_win_rate"] * s["trades"]
                agg[name]["total_r"] += s["total_r"]
                agg[name]["weighted_r"] += s["weighted_r"]
                print(
                    f"  {name:16s}  n={s['trades']:4d}  "
                    f"TP/SL胜率={s['win_rate']:5.1%}  "
                    f"宽松={s['loose_win_rate']:5.1%}  "
                    f"ΣR={s['total_r']:+7.2f}  "
                    f"加权ΣR={s['weighted_r']:+7.2f}  "
                    f"({time.time() - t1:.1f}s)",
                    flush=True,
                )

    print("\n" + "=" * 78)
    print(f"汇总（全品种×周期）· 耗时 {time.time() - t0:.0f}s")
    print("=" * 78)
    base_name = "A_基线(无过滤)"
    base_wr = (
        agg[base_name]["wins"] / agg[base_name]["resolved"]
        if agg[base_name]["resolved"]
        else 0.0
    )
    base_loose = (
        agg[base_name]["loose_wins"] / agg[base_name]["trades"]
        if agg[base_name]["trades"]
        else 0.0
    )
    base_r = agg[base_name]["total_r"]
    base_w = agg[base_name]["weighted_r"]

    for name, a in agg.items():
        wr = a["wins"] / a["resolved"] if a["resolved"] else 0.0
        loose = a["loose_wins"] / a["trades"] if a["trades"] else 0.0
        print(
            f"{name:16s}  n={int(a['trades']):4d}  "
            f"TP/SL={wr:5.1%} ({(wr - base_wr) * 100:+.1f}pp)  "
            f"宽松={loose:5.1%} ({(loose - base_loose) * 100:+.1f}pp)  "
            f"ΣR={a['total_r']:+8.2f} ({a['total_r'] - base_r:+.2f})  "
            f"加权ΣR={a['weighted_r']:+8.2f} ({a['weighted_r'] - base_w:+.2f})"
        )

    print(
        "\n说明：TP/SL 胜率=TP/(TP+SL)；宽松胜率含 timeout 且 pnl_r>0；"
        "加权ΣR=Σ(pnl_r×risk_scale)。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
