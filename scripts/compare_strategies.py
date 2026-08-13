"""策略排位赛：全部单币策略 + xs_momentum + funding_carry，5 年与近 1 年对比。

用法：.venv/bin/python scripts/compare_strategies.py
"""
from __future__ import annotations

import json

from analyst.backtest.classic import (
    STRATEGIES,
    CostModel,
    label_regimes,
    simulate,
)
from analyst.compute.strategies.cycle_switch import build_cycle_regime
from analyst.compute.strategies.funding_carry import (
    FundingCarryConfig,
    backtest_funding_carry,
)
from analyst.compute.strategies.xs_momentum import (
    XsMomentumConfig,
    backtest_xs_momentum,
)
from analyst.data.derivatives import fetch_funding_history
from analyst.data.fetcher import fetch_candles_history

SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "UNI/USDT"]
DAYS = 1825
TF = "4h"
BARS_365 = 6 * 365  # 4h × 6/天

series_map = {}
for s in SYMBOLS:
    sr = fetch_candles_history(s, TF, days=DAYS, market="futures")
    series_map[s] = sr.candles
    print(f"fetched {s}: {len(sr.candles)} bars")

regime_full = build_cycle_regime(series_map["BTC/USDT"])
cost = CostModel()

single = [s for s in STRATEGIES if s != "cycle_switch"] + ["cycle_switch"]

rows = []
for strat in single:
    fn = STRATEGIES[strat]
    full_stats, recent_stats = [], []
    for sym, candles in series_map.items():
        kwargs = {}
        if strat == "cycle_switch":
            kwargs = {"regime": regime_full}
        pos = fn(candles, **kwargs) if kwargs else fn(candles)
        rep = simulate(
            candles, pos, strategy=strat, symbol=sym, timeframe=TF,
            cost=cost, regime_labels=label_regimes(candles),
        )
        full_stats.append((rep.cagr_pct, rep.sharpe, rep.max_drawdown_pct))
        # 近 365 天：直接截尾重跑（仓位函数因果，重算即可）
        tail = candles[-BARS_365:]
        kwargs2 = {"regime": regime_full} if strat == "cycle_switch" else {}
        pos2 = fn(tail, **kwargs2) if kwargs2 else fn(tail)
        rep2 = simulate(
            tail, pos2, strategy=strat, symbol=sym, timeframe=TF, cost=cost,
        )
        recent_stats.append((rep2.total_return_pct, rep2.sharpe, rep2.max_drawdown_pct))

    def avg(xs, k):
        return sum(x[k] for x in xs) / len(xs)

    rows.append({
        "strategy": strat,
        "cagr5y": round(avg(full_stats, 0), 1),
        "sharpe5y": round(avg(full_stats, 1), 2),
        "mdd5y": round(avg(full_stats, 2), 1),
        "ret1y": round(avg(recent_stats, 0), 1),
        "sharpe1y": round(avg(recent_stats, 1), 2),
        "mdd1y": round(avg(recent_stats, 2), 1),
    })

# xs_momentum（组合级）
xs_cfg = XsMomentumConfig()
xs_full = backtest_xs_momentum(series_map, regime_full, xs_cfg, timeframe=TF)
tail_map = {s: c[-BARS_365:] for s, c in series_map.items()}
xs_recent = backtest_xs_momentum(tail_map, regime_full, xs_cfg, timeframe=TF)
rows.append({
    "strategy": "xs_momentum",
    "cagr5y": round(xs_full.cagr_pct, 1),
    "sharpe5y": round(xs_full.sharpe, 2),
    "mdd5y": round(xs_full.max_drawdown_pct, 1),
    "ret1y": round(xs_recent.total_return_pct, 1),
    "sharpe1y": round(xs_recent.sharpe, 2),
    "mdd1y": round(xs_recent.max_drawdown_pct, 1),
})

# funding_carry（逐币平均，需资金费历史）
carry_full, carry_recent = [], []
for s in SYMBOLS:
    funding = fetch_funding_history(s, days=DAYS)
    if len(funding) < 200:
        print(f"skip carry {s}: funding={len(funding)}")
        continue
    cfg = FundingCarryConfig()
    r1 = backtest_funding_carry(s, funding, cfg)
    r2 = backtest_funding_carry(s, funding[-3 * 365:], cfg)
    carry_full.append((r1.apr_pct, r1.max_drawdown_pct))
    carry_recent.append((r2.apr_pct, r2.max_drawdown_pct))
if carry_full:
    rows.append({
        "strategy": "funding_carry",
        "cagr5y": round(sum(x[0] for x in carry_full) / len(carry_full), 1),
        "sharpe5y": None,
        "mdd5y": round(sum(x[1] for x in carry_full) / len(carry_full), 2),
        "ret1y": round(sum(x[0] for x in carry_recent) / len(carry_recent), 1),
        "sharpe1y": None,
        "mdd1y": round(sum(x[1] for x in carry_recent) / len(carry_recent), 2),
    })

print(json.dumps(rows, ensure_ascii=False, indent=1))
