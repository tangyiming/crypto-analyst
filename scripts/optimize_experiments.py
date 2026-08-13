"""在保留的策略上做变体实验：5 年整体 + 180 天滚动窗口双重验证。

用法：.venv/bin/python scripts/optimize_experiments.py
"""
from __future__ import annotations

import math
from datetime import datetime

from analyst.backtest.classic import (
    CostModel,
    apply_vol_target,
    rolling_window_report,
    simulate,
)
from analyst.compute.strategies.cycle_switch import (
    build_cycle_regime,
    positions_cycle_switch,
)
from analyst.data.fetcher import Candle, fetch_candles_history

SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "UNI/USDT"]
TF = "4h"
BPY = 2190
cost = CostModel()

series_map = {
    s: fetch_candles_history(s, TF, days=1825, market="futures").candles
    for s in SYMBOLS
}
regime = build_cycle_regime(series_map["BTC/USDT"])


# ────────────────────── 组合模拟（带滚动窗口） ──────────────────────
def align(series_map):
    by_sym = {s: {c.timestamp: c for c in cs} for s, cs in series_map.items()}
    all_ts = sorted({ts for m in by_sym.values() for ts in m})
    return all_ts, by_sym


def xs_weights(
    series_map,
    regime,
    *,
    lookback=84,
    rebalance=42,
    top_n=2,
    risk_adjusted=False,
    inverse_vol=False,
    short_in_bear=True,
    short_size=0.5,
):
    timeline, by_sym = align(series_map)
    n = len(timeline)
    weights = {s: [0.0] * n for s in series_map}
    closes_seq = {
        s: sorted(((c.timestamp, c.close) for c in cs)) for s, cs in series_map.items()
    }
    idx_of = {s: {ts: i for i, (ts, _) in enumerate(seq)} for s, seq in closes_seq.items()}

    current = {s: 0.0 for s in series_map}
    for i, ts in enumerate(timeline):
        if i % rebalance == 0:
            reg = regime.get(ts, "accum")
            scored: list[tuple[float, float, str]] = []  # (score, vol, sym)
            for s, seq in closes_seq.items():
                j = idx_of[s].get(ts)
                if j is None or j < lookback:
                    continue
                past = seq[j - lookback][1]
                if past <= 0:
                    continue
                ret = seq[j][1] / past - 1.0
                rets = [
                    seq[k][1] / seq[k - 1][1] - 1.0
                    for k in range(j - lookback + 1, j + 1)
                    if seq[k - 1][1] > 0
                ]
                mean = sum(rets) / len(rets)
                vol = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5
                vol = max(vol, 1e-6)
                score = ret / (vol * math.sqrt(len(rets))) if risk_adjusted else ret
                scored.append((score, vol, s))
            current = {s: 0.0 for s in series_map}
            if len(scored) >= max(top_n, 2):
                scored.sort(reverse=True)
                if reg in ("bull", "accum"):
                    chosen = scored[:top_n]
                    total = 1.0
                elif reg == "bear" and short_in_bear:
                    chosen = [(sc, v, s) for sc, v, s in scored[-top_n:]]
                    total = -short_size
                else:
                    chosen = []
                    total = 0.0
                if chosen:
                    if inverse_vol:
                        inv = [1.0 / v for _, v, _ in chosen]
                        ssum = sum(inv)
                        for (sc, v, s), w in zip(chosen, inv):
                            current[s] = total * (w / ssum)
                    else:
                        for _, _, s in chosen:
                            current[s] = total / len(chosen)
        for s in series_map:
            weights[s][i] = current[s] if ts in by_sym[s] else 0.0
    return timeline, weights, by_sym


def portfolio_run(series_map, regime, **kw):
    timeline, weights, by_sym = xs_weights(series_map, regime, **kw)
    n = len(timeline)
    equity, peak, mdd = 1.0, 1.0, 0.0
    prev_w = {s: weights[s][0] for s in series_map}
    bar_rets: list[tuple[datetime, float]] = []
    for i in range(1, n):
        ts_p, ts = timeline[i - 1], timeline[i]
        pnl = 0.0
        for s, w in prev_w.items():
            if w == 0.0:
                continue
            cp, cn = by_sym[s].get(ts_p), by_sym[s].get(ts)
            if cp is None or cn is None or cp.close <= 0:
                continue
            pnl += w * (cn.close / cp.close - 1.0)
        new_w = {s: weights[s][i] for s in series_map}
        fee = sum(abs(new_w[s] - prev_w[s]) for s in series_map) * cost.one_way
        step = (1.0 + pnl) * (1.0 - fee)
        equity *= step
        bar_rets.append((ts, step - 1.0))
        prev_w = new_w
        peak = max(peak, equity)
        mdd = min(mdd, equity / peak - 1.0)
    years = max((timeline[-1] - timeline[0]).days / 365.25, 1e-9)
    rets = [r for _, r in bar_rets]
    mean_r = sum(rets) / len(rets)
    std_r = (sum((r - mean_r) ** 2 for r in rets) / len(rets)) ** 0.5
    sharpe = mean_r / std_r * math.sqrt(BPY) if std_r > 0 else 0.0
    cagr = (equity ** (1 / years) - 1.0) * 100

    # 180 天滚动窗口
    win, seg_eq, seg_start = [], 1.0, bar_rets[0][0]
    for ts, r in bar_rets:
        seg_eq *= 1.0 + r
        if (ts - seg_start).days >= 180:
            win.append((seg_eq - 1.0) * 100)
            seg_eq, seg_start = 1.0, ts
    win.sort()
    pos_share = sum(1 for w in win if w > 0) / len(win) if win else 0.0
    return {
        "cagr": round(cagr, 1),
        "sharpe": round(sharpe, 2),
        "mdd": round(mdd * 100, 1),
        "win_pos": f"{pos_share:.0%}",
        "win_med": round(win[len(win) // 2], 1) if win else 0,
        "win_worst": round(win[0], 1) if win else 0,
    }


print("── xs_momentum 变体（组合级，4 币池）──")
variants = [
    ("基线：纯收益排名+等权", {}),
    ("风险调整排名", {"risk_adjusted": True}),
    ("等权→倒数波动配权", {"inverse_vol": True}),
    ("风险调整+倒数波动", {"risk_adjusted": True, "inverse_vol": True}),
    ("熊市空仓（不做空）", {"short_in_bear": False}),
    ("风险调整+熊市空仓", {"risk_adjusted": True, "short_in_bear": False}),
]
for name, kw in variants:
    r = portfolio_run(series_map, regime, **kw)
    print(f"{name:24s} {r}")


# ────────────────────── cycle_switch 变体（单币平均） ──────────────────────
def adx_series(candles: list[Candle], period: int = 14) -> list[float]:
    n = len(candles)
    out = [0.0] * n
    if n < period * 2 + 1:
        return out
    trs, pdm, mdm = [], [], []
    for i in range(1, n):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up = h - candles[i - 1].high
        dn = candles[i - 1].low - l
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)

    def wilder(vals):
        sm = [sum(vals[:period]) / period]
        for v in vals[period:]:
            sm.append((sm[-1] * (period - 1) + v) / period)
        return sm

    atr_s, p_s, m_s = wilder(trs), wilder(pdm), wilder(mdm)
    dx = []
    for a, p, m in zip(atr_s, p_s, m_s):
        if a <= 0:
            dx.append(0.0)
            continue
        pdi, mdi = 100 * p / a, 100 * m / a
        dx.append(0.0 if pdi + mdi <= 0 else 100 * abs(pdi - mdi) / (pdi + mdi))
    adx_s = wilder(dx)
    # 对齐：adx_s[k] 对应 candle index k + 2*period
    for k, v in enumerate(adx_s):
        idx = k + 2 * period
        if idx < n:
            out[idx] = v
    return out


def gate_entries_by_adx(candles, positions, min_adx=20.0):
    """新开仓时 ADX(前一根)<门槛 → 跳过整笔交易。"""
    adx = adx_series(candles)
    out = list(positions)
    i, n = 0, len(positions)
    prev = 0.0
    while i < n:
        cur = out[i]
        if abs(prev) < 1e-9 and abs(cur) > 1e-9 and adx[i - 1] if i else 0:
            pass
        if abs(prev) < 1e-9 and abs(cur) > 1e-9 and i > 0 and 0 < adx[i - 1] < min_adx:
            j = i
            while j < n and abs(out[j] - cur) < 1e-9:
                out[j] = 0.0
                j += 1
            i = j
            prev = 0.0
            continue
        prev = out[i]
        i += 1
    return out


def cycle_stats(variant):
    full, windows = [], []
    for sym, candles in series_map.items():
        pos = positions_cycle_switch(candles, regime)
        if variant == "vol_target":
            pos = apply_vol_target(candles, pos, timeframe=TF, target_annual_vol=0.30)
        elif variant == "adx_gate":
            pos = gate_entries_by_adx(candles, pos, min_adx=20.0)
        elif variant == "adx_gate+vt":
            pos = gate_entries_by_adx(candles, pos, min_adx=20.0)
            pos = apply_vol_target(candles, pos, timeframe=TF, target_annual_vol=0.30)
        rep = simulate(candles, pos, strategy="cycle", symbol=sym, timeframe=TF, cost=cost)
        full.append((rep.cagr_pct, rep.sharpe, rep.max_drawdown_pct))
        for r in rolling_window_report(
            candles, pos, strategy="cycle", symbol=sym, timeframe=TF,
            window_days=180, cost=cost,
        ):
            windows.append(r.total_return_pct)
    windows.sort()
    n = len(windows)
    return {
        "cagr": round(sum(x[0] for x in full) / 4, 1),
        "sharpe": round(sum(x[1] for x in full) / 4, 2),
        "mdd": round(sum(x[2] for x in full) / 4, 1),
        "win_pos": f"{sum(1 for w in windows if w > 0) / n:.0%}",
        "win_med": round(windows[n // 2], 1),
        "win_worst": round(windows[0], 1),
    }


print("\n── cycle_switch 变体（4 币平均）──")
for v in ("baseline", "vol_target", "adx_gate", "adx_gate+vt"):
    print(f"{v:24s} {cycle_stats(v)}")
