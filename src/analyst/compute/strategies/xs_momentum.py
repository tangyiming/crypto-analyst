"""横截面动量（cross-sectional momentum）。

不问「这个币会不会涨」，问「哪个币最强/最弱」：
  · 每 rebalance 根 K 线，把观察池按近 lookback 根收益排序
  · 相位 bull/accum（BTC 定调）→ 等权做多最强 top_n
  · 相位 bear → 做空最弱 top_n（合计 short_size，默认半仓），或空仓
  · 组合总敞口恒 ≤ 1（多头合计 1.0 / 空头合计 short_size）

为什么值得加：与单币时序策略（cycle_switch/双线）相关性低——
时序问方向，横截面问相对强弱；两类信号来源不同，组合后收益更平滑。
学术与实盘验证：加密市场 30 天动量横截面效应是文献里最稳健的因子之一。

回测：analyst backtest-xs --days 1825
上市晚的币（SUI/ASTER 等）自动在有足够历史后才进入排序池。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from analyst.backtest.classic import BARS_PER_YEAR, CostModel
from analyst.data.fetcher import Candle


@dataclass
class XsMomentumConfig:
    # 5 年 8 币扫描：12~25 天一片平原（Sharpe 1.10-1.23），30 天反而弱；
    # 取 14 天居中，非孤峰参数（7 天 1.03 / 30 天 0.85 两端衰减平滑）。
    lookback: int = 84         # 动量窗口（4h×84 = 14 天）
    rebalance: int = 42        # 调仓间隔（4h×42 = 7 天）
    top_n: int = 2
    short_size: float = 0.5    # 熊市做空总敞口
    short_in_bear: bool = True


@dataclass
class XsPortfolioReport:
    """横截面组合回测汇总。"""

    symbols: list[str]
    timeframe: str
    bars: int
    start: datetime | None
    end: datetime | None
    total_return_pct: float = 0.0
    cagr_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    rebalances: int = 0
    exposure: float = 0.0
    funding_pnl_pct: float = 0.0
    equity_curve: list[float] = field(default_factory=list, repr=False)
    # 每次调仓的持仓快照（时间, {symbol: weight}）
    holdings_log: list[tuple[datetime, dict[str, float]]] = field(default_factory=list)

    def to_row(self) -> dict:
        return {
            "strategy": "xs_momentum",
            "symbols": self.symbols,
            "timeframe": self.timeframe,
            "total_return_pct": round(self.total_return_pct, 1),
            "cagr_pct": round(self.cagr_pct, 1),
            "max_drawdown_pct": round(self.max_drawdown_pct, 1),
            "sharpe": round(self.sharpe, 2),
            "rebalances": self.rebalances,
            "exposure": round(self.exposure, 2),
            "funding_pnl_pct": round(self.funding_pnl_pct, 2),
        }


def _align(series_map: dict[str, list[Candle]]) -> tuple[list[datetime], dict[str, dict[datetime, Candle]]]:
    """时间轴 = 各币时间戳并集（升序）；各币缺的时段视为「未上市/无数据」。"""
    by_sym: dict[str, dict[datetime, Candle]] = {
        s: {c.timestamp: c for c in candles} for s, candles in series_map.items()
    }
    all_ts: set[datetime] = set()
    for m in by_sym.values():
        all_ts.update(m.keys())
    return sorted(all_ts), by_sym


def weights_xs_momentum(
    series_map: dict[str, list[Candle]],
    regime: dict[datetime, str] | None = None,
    cfg: XsMomentumConfig | None = None,
) -> tuple[list[datetime], dict[str, list[float]]]:
    """生成对齐时间轴上的各币目标权重序列（因果：只用截至当根的数据）。"""
    cfg = cfg or XsMomentumConfig()
    timeline, by_sym = _align(series_map)
    n = len(timeline)
    weights: dict[str, list[float]] = {s: [0.0] * n for s in series_map}
    if n == 0:
        return timeline, weights

    # 每币按自身历史建立 ts→(idx, close) 以取 lookback 前的价
    closes_seq: dict[str, list[tuple[datetime, float]]] = {
        s: sorted(((c.timestamp, c.close) for c in candles))
        for s, candles in series_map.items()
    }
    idx_of: dict[str, dict[datetime, int]] = {
        s: {ts: i for i, (ts, _) in enumerate(seq)} for s, seq in closes_seq.items()
    }

    current: dict[str, float] = {s: 0.0 for s in series_map}
    for i, ts in enumerate(timeline):
        if i % cfg.rebalance == 0:
            reg = (regime or {}).get(ts, "accum")
            scores: list[tuple[float, str]] = []
            for s, seq in closes_seq.items():
                j = idx_of[s].get(ts)
                if j is None or j < cfg.lookback:
                    continue
                past = seq[j - cfg.lookback][1]
                if past <= 0:
                    continue
                scores.append((seq[j][1] / past - 1.0, s))
            current = {s: 0.0 for s in series_map}
            if len(scores) >= max(cfg.top_n, 2):
                scores.sort(reverse=True)
                if reg in ("bull", "accum"):
                    for _, s in scores[: cfg.top_n]:
                        current[s] = 1.0 / cfg.top_n
                elif reg == "bear" and cfg.short_in_bear:
                    for _, s in scores[-cfg.top_n :]:
                        current[s] = -cfg.short_size / cfg.top_n
        for s in series_map:
            # 该币此刻无数据（未上市）则权重强制 0
            weights[s][i] = current[s] if ts in by_sym[s] else 0.0
    return timeline, weights


def backtest_xs_momentum(
    series_map: dict[str, list[Candle]],
    regime: dict[datetime, str] | None = None,
    cfg: XsMomentumConfig | None = None,
    *,
    timeframe: str = "4h",
    cost: CostModel | None = None,
    funding_map: dict[str, list[tuple[int, float]]] | None = None,
) -> XsPortfolioReport:
    """组合级回测：多币权重共用一条复利权益曲线（含换手成本与资金费）。"""
    cfg = cfg or XsMomentumConfig()
    cost = cost or CostModel()
    timeline, weights = weights_xs_momentum(series_map, regime, cfg)
    n = len(timeline)
    _, by_sym = _align(series_map)

    report = XsPortfolioReport(
        symbols=sorted(series_map),
        timeframe=timeframe,
        bars=n,
        start=timeline[0] if timeline else None,
        end=timeline[-1] if timeline else None,
    )
    if n < 2:
        return report

    # 资金费映射：ms → rate，逐币
    fund_sorted: dict[str, list[tuple[int, float]]] = {
        s: sorted(v) for s, v in (funding_map or {}).items()
    }
    fund_idx: dict[str, int] = {s: 0 for s in fund_sorted}

    equity, peak, max_dd = 1.0, 1.0, 0.0
    rets: list[float] = []
    exposure_bars = 0
    rebalances = 0
    funding_pnl_total = 0.0
    prev_w = {s: weights[s][0] for s in series_map}
    curve = [1.0]

    for i in range(1, n):
        ts_prev, ts = timeline[i - 1], timeline[i]
        lo_ms = int(ts_prev.timestamp() * 1000)
        hi_ms = int(ts.timestamp() * 1000)
        pnl = 0.0
        for s, w in prev_w.items():
            if w == 0.0:
                continue
            c_prev = by_sym[s].get(ts_prev)
            c_now = by_sym[s].get(ts)
            if c_prev is None or c_now is None or c_prev.close <= 0:
                continue
            pnl += w * (c_now.close / c_prev.close - 1.0)
            # 资金费
            seq = fund_sorted.get(s)
            if seq:
                k = fund_idx[s]
                while k < len(seq) and seq[k][0] <= hi_ms:
                    if seq[k][0] > lo_ms:
                        f = -w * seq[k][1]
                        pnl += f
                        funding_pnl_total += f
                    k += 1
                fund_idx[s] = k
        new_w = {s: weights[s][i] for s in series_map}
        turnover = sum(abs(new_w[s] - prev_w[s]) for s in series_map)
        fee = turnover * cost.one_way
        step = (1.0 + pnl) * (1.0 - fee)
        equity *= step
        rets.append(step - 1.0)
        curve.append(equity)
        if turnover > 1e-12:
            rebalances += 1
        if any(abs(w) > 1e-12 for w in prev_w.values()):
            exposure_bars += 1
        prev_w = new_w
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)

    years = max((timeline[-1] - timeline[0]).days / 365.25, 1e-9)
    bpy = BARS_PER_YEAR.get(timeframe, 2190)
    mean_r = sum(rets) / len(rets)
    var_r = sum((r - mean_r) ** 2 for r in rets) / len(rets)
    std_r = var_r ** 0.5

    report.total_return_pct = (equity - 1.0) * 100
    report.cagr_pct = ((equity ** (1 / years)) - 1.0) * 100 if equity > 0 else -100.0
    report.max_drawdown_pct = max_dd * 100
    report.sharpe = (mean_r / std_r) * math.sqrt(bpy) if std_r > 0 else 0.0
    report.rebalances = rebalances
    report.exposure = exposure_bars / (n - 1)
    report.funding_pnl_pct = funding_pnl_total * 100
    report.equity_curve = curve
    return report


def current_xs_ranking(
    series_map: dict[str, list[Candle]],
    cfg: XsMomentumConfig | None = None,
) -> list[tuple[str, float]]:
    """当前时点的动量排名（监控/CLI 展示用），降序。"""
    cfg = cfg or XsMomentumConfig()
    out: list[tuple[str, float]] = []
    for s, candles in series_map.items():
        if len(candles) <= cfg.lookback:
            continue
        past = candles[-1 - cfg.lookback].close
        if past > 0:
            out.append((s, candles[-1].close / past - 1.0))
    out.sort(key=lambda x: -x[1])
    return out
