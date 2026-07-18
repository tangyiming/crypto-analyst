"""经典策略组合回测（仓位收益口径，含交易成本）。

与 engine.py 的事件式回测（R 口径）互补：
- 策略以「每根收盘后的目标仓位」表达（+1 多 / 0 空仓 / -1 空），
  第 i 根收盘决定的仓位从第 i+1 根开始承担收益——不偷看未来。
- 成本模型：每次换手按名义价值收 (taker 费 + 滑点)，默认单边 0.07%。
- 行情分段：按滚动收益自动标注 bull / bear / chop，
  分别统计策略在三种状态下的收益贡献（不靠人工划日期）。

内置策略均为公开验证多年的经典类：
- donchian: 唐奇安通道突破（海龟）——突破 N 根高点做多、跌破 M 根低点离场，对称做空
- ema_cross: EMA 双均线趋势跟随（always-in 多空互换）
- boll_mr: 布林 z-score 均值回归——超卖接多回归中轨离场，对称做空
- cycle_switch: 牛熊周期切换——减半日历×200日线双确认判熊，
  牛/筑底跑唐奇安只多，熊市两条空腿（反弹冲高空 + 唐奇安破位空）
- buy_hold: 基准

分相位手选腿（自己判断市场阶段后单独执行；自动切换用 cycle_switch）：
- bull_trend: 牛市腿——唐奇安 40/20 只多
- bear_defense: 熊市腿——只空半仓（z 反弹空 + 唐奇安破位空）
- chop_range: 震荡腿——布林均值回归双向半仓（趋势段大亏，勿裸跑）
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from analyst.compute.indicators import ema
from analyst.compute.strategies.cycle_switch import (
    HALVING_DATES,
    CycleSwitchConfig,
    build_cycle_regime,
    halving_phase,
    positions_cycle_switch,
)
from analyst.data.fetcher import Candle

# 各周期一年的根数（夏普年化用）
BARS_PER_YEAR = {
    "15m": 35040, "30m": 17520, "1h": 8760, "2h": 4380,
    "4h": 2190, "6h": 1460, "8h": 1095, "12h": 730, "1d": 365,
}


@dataclass(frozen=True)
class CostModel:
    """单边成本 = taker 手续费 + 滑点（百分比）。"""

    fee_pct: float = 0.05
    slippage_pct: float = 0.02

    @property
    def one_way(self) -> float:
        return (self.fee_pct + self.slippage_pct) / 100.0


@dataclass
class ClassicReport:
    """一次组合回测的汇总。"""

    strategy: str
    symbol: str
    timeframe: str
    bars: int
    start: datetime | None
    end: datetime | None
    total_return_pct: float = 0.0      # 复利总收益
    cagr_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    trades: int = 0                    # 仓位变化次数
    exposure: float = 0.0              # 持仓时间占比
    cost_paid_pct: float = 0.0         # 累计成本（占初始资金，近似）
    funding_pnl_pct: float = 0.0       # 资金费净损益（负=净支付；空头收正费率时为正）
    regime_return_pct: dict[str, float] = field(default_factory=dict)
    regime_bars: dict[str, int] = field(default_factory=dict)
    equity_curve: list[float] = field(default_factory=list, repr=False)

    def to_row(self) -> dict:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "total_return_pct": round(self.total_return_pct, 1),
            "cagr_pct": round(self.cagr_pct, 1),
            "max_drawdown_pct": round(self.max_drawdown_pct, 1),
            "sharpe": round(self.sharpe, 2),
            "trades": self.trades,
            "exposure": round(self.exposure, 2),
            "funding_pnl_pct": round(self.funding_pnl_pct, 2),
            "regimes": {k: round(v, 1) for k, v in self.regime_return_pct.items()},
        }


# ─────────────────────────────────────────────────────────────
# 行情分段：滚动 lookback 根收益 > +thresh → bull；< -thresh → bear；否则 chop
# ─────────────────────────────────────────────────────────────
def label_regimes(
    candles: list[Candle],
    lookback: int = 180,
    thresh: float = 0.15,
) -> list[str]:
    closes = [c.close for c in candles]
    labels: list[str] = []
    for i in range(len(closes)):
        j = max(0, i - lookback)
        base = closes[j]
        chg = (closes[i] - base) / base if base > 0 else 0.0
        if chg > thresh:
            labels.append("bull")
        elif chg < -thresh:
            labels.append("bear")
        else:
            labels.append("chop")
    return labels


# cycle_switch 逻辑见 analyst.compute.strategies.cycle_switch
def positions_buy_hold(candles: list[Candle]) -> list[float]:
    return [1.0] * len(candles)


def positions_donchian(
    candles: list[Candle],
    entry_n: int = 40,
    exit_n: int = 20,
    long_only: bool = False,
) -> list[float]:
    """海龟式通道突破：收盘破前 entry_n 根高点→多，破前 exit_n 根低点→离场；对称做空。

    默认 40/20：5 年 × 4 币参数平原扫描中 entry 30-70 / exit 15-30 一片均为正，
    40/20 居中且最优（4 币平均 CAGR 33%），非孤峰参数。
    """
    n = len(candles)
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    pos = 0.0
    out: list[float] = []
    for i in range(n):
        if i < entry_n:
            out.append(0.0)
            continue
        hh_entry = max(highs[i - entry_n:i])   # 不含当前根
        ll_entry = min(lows[i - entry_n:i])
        hh_exit = max(highs[max(0, i - exit_n):i])
        ll_exit = min(lows[max(0, i - exit_n):i])
        c = closes[i]
        if pos == 0.0:
            if c > hh_entry:
                pos = 1.0
            elif c < ll_entry and not long_only:
                pos = -1.0
        elif pos > 0 and c < ll_exit:
            pos = -1.0 if (c < ll_entry and not long_only) else 0.0
        elif pos < 0 and c > hh_exit:
            pos = 1.0 if c > hh_entry else 0.0
        out.append(pos)
    return out


def positions_ema_cross(
    candles: list[Candle],
    fast: int = 50,
    slow: int = 200,
    long_only: bool = False,
) -> list[float]:
    """EMA 双均线 always-in 趋势跟随。"""
    closes = [c.close for c in candles]
    ef = ema(closes, fast)
    es = ema(closes, slow)
    out: list[float] = []
    for i in range(len(closes)):
        if i < slow:
            out.append(0.0)
        elif ef[i] > es[i]:
            out.append(1.0)
        else:
            out.append(0.0 if long_only else -1.0)
    return out


def positions_boll_mr(
    candles: list[Candle],
    period: int = 20,
    entry_z: float = 2.0,
    long_only: bool = False,
) -> list[float]:
    """布林 z-score 均值回归：z<-entry 接多、回到中轨平；对称做空。"""
    closes = [c.close for c in candles]
    n = len(closes)
    pos = 0.0
    out: list[float] = []
    for i in range(n):
        if i < period:
            out.append(0.0)
            continue
        window = closes[i - period + 1:i + 1]
        mean = sum(window) / period
        var = sum((v - mean) ** 2 for v in window) / period
        std = var ** 0.5
        z = (closes[i] - mean) / std if std > 0 else 0.0
        if pos == 0.0:
            if z < -entry_z:
                pos = 1.0
            elif z > entry_z and not long_only:
                pos = -1.0
        elif pos > 0 and z >= 0:
            pos = 0.0
        elif pos < 0 and z <= 0:
            pos = 0.0
        out.append(pos)
    return out


# ─────────────────────────────────────────────────────────────
# 分相位手选策略：牛市 / 熊市 / 震荡 各一条腿，自己判断阶段后单独执行。
# 自动切换版 = cycle_switch（BTC 减半日历 × 200 日线双确认定相位）。
# ─────────────────────────────────────────────────────────────
def positions_bull_trend(
    candles: list[Candle],
    entry_n: int = 40,
    exit_n: int = 20,
) -> list[float]:
    """牛市腿：唐奇安 40/20 只多。

    确认牛市/筑底阶段手动选择执行；等价于 cycle_switch 的多头腿。
    """
    return positions_donchian(candles, entry_n=entry_n, exit_n=exit_n, long_only=True)


def positions_bear_defense(
    candles: list[Candle],
    entry_n: int = 40,
    exit_n: int = 20,
    fade_z: float = 1.5,
    mr_period: int = 20,
    short_size: float = 0.5,
) -> list[float]:
    """熊市腿：只空、默认半仓，绝不做多。

    两个入场：① 反弹 z-score > fade_z 冲高做空（卖强不卖弱），z 回 0 平仓；
    ② 唐奇安破位空（收盘 < 前 entry_n 根低点），收回 exit_n 根高点回补。
    确认熊市阶段手动选择执行；等价于 cycle_switch 的空头腿。
    """
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    pos = 0.0
    mode: str | None = None
    out: list[float] = []
    for i in range(len(candles)):
        if i < max(entry_n, mr_period):
            out.append(0.0)
            continue
        c = closes[i]
        ll = min(lows[i - entry_n:i])
        hx = max(highs[max(0, i - exit_n):i])
        window = closes[i - mr_period + 1:i + 1]
        mean = sum(window) / mr_period
        var = sum((v - mean) ** 2 for v in window) / mr_period
        std = var ** 0.5
        z = (c - mean) / std if std > 0 else 0.0
        if pos == 0.0 and c < ll:
            pos, mode = -short_size, "tshort"
        elif pos < 0 and mode == "tshort" and c > hx:
            pos, mode = 0.0, None
        if pos == 0.0 and z > fade_z:
            pos, mode = -short_size, "fade"
        elif pos < 0 and mode == "fade" and z <= 0:
            pos, mode = 0.0, None
        out.append(pos)
    return out


def positions_chop_range(
    candles: list[Candle],
    period: int = 20,
    entry_z: float = 2.0,
    size: float = 0.5,
    stop_atr: float = 3.0,
) -> list[float]:
    """震荡腿：布林 z-score 均值回归，双向、默认半仓，带 ATR 硬止损。

    只该在确认震荡（无趋势）阶段手动执行——5 年回测它在趋势段大亏、
    仅震荡段为正，全程裸跑必亏。

    stop_atr：入场价 ± 该倍数×ATR(14) 硬止损，防「震荡中突发单边」的尾部风险。
    无止损时最差单笔 -14%~-34%（半仓）、最差 5 笔合计可达 -129%（SOL），
    一笔尾部亏损吃掉几十笔小赢；3×ATR 止损把尾部近乎砍半，
    代价是震荡段收益让出约 1/4（止损偶尔打掉本会回归的仓位）。
    扫描 2×/3×/趋势保险丝/止损后冷却后 3× 纯止损综合最优。0=禁用。
    """
    closes = [c.close for c in candles]
    n = len(closes)
    # ATR(14) 因果序列
    atrs: list[float] = []
    trs: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c.high - c.low)
        else:
            p = candles[i - 1]
            trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
        w = trs[-14:]
        atrs.append(sum(w) / len(w))
    pos = 0.0
    entry_px = 0.0
    out: list[float] = []
    for i in range(n):
        if i < period:
            out.append(0.0)
            continue
        c = closes[i]
        window = closes[i - period + 1:i + 1]
        mean = sum(window) / period
        var = sum((v - mean) ** 2 for v in window) / period
        std = var ** 0.5
        z = (c - mean) / std if std > 0 else 0.0
        if pos != 0.0 and stop_atr > 0:
            stopped = (
                (pos > 0 and c < entry_px - stop_atr * atrs[i])
                or (pos < 0 and c > entry_px + stop_atr * atrs[i])
            )
            if stopped:
                pos = 0.0
                out.append(pos)
                continue
        if pos == 0.0:
            if z < -entry_z:
                pos, entry_px = size, c
            elif z > entry_z:
                pos, entry_px = -size, c
        elif pos > 0 and z >= 0:
            pos = 0.0
        elif pos < 0 and z <= 0:
            pos = 0.0
        out.append(pos)
    return out


STRATEGIES = {
    "buy_hold": positions_buy_hold,
    "donchian": positions_donchian,
    "ema_cross": positions_ema_cross,
    "boll_mr": positions_boll_mr,
    "cycle_switch": positions_cycle_switch,   # 需要 regime 参数（BTC 定牛熊）
    "bull_trend": positions_bull_trend,       # 牛市腿（手选）
    "bear_defense": positions_bear_defense,   # 熊市腿（手选）
    "chop_range": positions_chop_range,       # 震荡腿（手选）
}


# ─────────────────────────────────────────────────────────────
# 波动率目标化：按已实现波动率反比缩放仓位（因果，只用截至当根的数据）
# ─────────────────────────────────────────────────────────────
def apply_vol_target(
    candles: list[Candle],
    positions: list[float],
    *,
    timeframe: str = "4h",
    target_annual_vol: float = 0.30,
    lookback: int = 42,
    max_scale: float = 1.0,
    min_scale: float = 0.15,
) -> list[float]:
    """仓位 × min(max_scale, 目标年化波动 / 已实现年化波动)。

    高波动段自动减仓、低波动段满仓（默认不加杠杆，封顶 1.0×）。
    lookback=42 根 4h ≈ 一周。波动率不足样本时不缩放。
    """
    closes = [c.close for c in candles]
    bpy = BARS_PER_YEAR.get(timeframe, 2190)
    n = len(closes)
    out = list(positions)
    rets: list[float] = [0.0]
    for i in range(1, n):
        rets.append(closes[i] / closes[i - 1] - 1.0 if closes[i - 1] > 0 else 0.0)
    for i in range(n):
        if i < lookback or positions[i] == 0.0:
            continue
        window = rets[i - lookback + 1 : i + 1]
        mean = sum(window) / len(window)
        var = sum((r - mean) ** 2 for r in window) / len(window)
        vol_annual = (var ** 0.5) * (bpy ** 0.5)
        if vol_annual <= 0:
            continue
        scale = target_annual_vol / vol_annual
        scale = max(min_scale, min(max_scale, scale))
        out[i] = positions[i] * scale
    return out


# ─────────────────────────────────────────────────────────────
# 滚动窗口稳健性：同一仓位序列按窗口切段看每段表现（防整体调参幻觉）
# ─────────────────────────────────────────────────────────────
def rolling_window_report(
    candles: list[Candle],
    positions: list[float],
    *,
    strategy: str,
    symbol: str,
    timeframe: str,
    window_days: int = 180,
    cost: CostModel | None = None,
    funding: list[tuple[int, float]] | None = None,
) -> list[ClassicReport]:
    """把回测区间切成连续 window_days 段，逐段独立模拟。

    一个只有整体数字好看、但一半窗口亏损的策略，大概率是过拟合。
    """
    if not candles:
        return []
    reports: list[ClassicReport] = []
    seg_start = 0
    start_ts = candles[0].timestamp
    for i, c in enumerate(candles):
        if (c.timestamp - start_ts).days >= window_days or i == len(candles) - 1:
            seg_c = candles[seg_start : i + 1]
            seg_p = positions[seg_start : i + 1]
            if len(seg_c) > 30:
                reports.append(
                    simulate(
                        seg_c,
                        seg_p,
                        strategy=strategy,
                        symbol=symbol,
                        timeframe=timeframe,
                        cost=cost,
                        funding=funding,
                    )
                )
            seg_start = i + 1
            start_ts = c.timestamp
    return reports


# ─────────────────────────────────────────────────────────────
# 组合模拟
# ─────────────────────────────────────────────────────────────
def simulate(
    candles: list[Candle],
    positions: list[float],
    *,
    strategy: str,
    symbol: str,
    timeframe: str,
    cost: CostModel | None = None,
    regime_labels: list[str] | None = None,
    funding: list[tuple[int, float]] | None = None,
) -> ClassicReport:
    """按目标仓位序列模拟复利权益曲线（第 i 根仓位从 i+1 根生效）。

    funding: [(结算时间ms, 8h费率), ...]（fetch_funding_history 输出）。
    传入时按持仓方向计费：多头付正费率、空头收正费率（反之亦然）。
    """
    cost = cost or CostModel()

    # 资金费结算映射到 bar：结算时刻落在 (bar[i-1].ts, bar[i].ts] 的费率算在第 i 根
    funding_by_bar: dict[int, float] = {}
    if funding:
        times_ms = [int(c.timestamp.timestamp() * 1000) for c in candles]
        fi = 0
        f_sorted = funding
        for i in range(1, len(times_ms)):
            lo, hi = times_ms[i - 1], times_ms[i]
            while fi < len(f_sorted) and f_sorted[fi][0] <= hi:
                if f_sorted[fi][0] > lo:
                    funding_by_bar[i] = funding_by_bar.get(i, 0.0) + f_sorted[fi][1]
                fi += 1
    n = len(candles)
    report = ClassicReport(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        bars=n,
        start=candles[0].timestamp if candles else None,
        end=candles[-1].timestamp if candles else None,
    )
    if n < 2:
        return report

    closes = [c.close for c in candles]
    labels = regime_labels or label_regimes(candles)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    pos = positions[0]  # 第 0 根收盘决定的仓位，从第 1 根开始生效
    trades = 0
    exposure_bars = 0
    cost_paid = 0.0
    rets: list[float] = []
    regime_ret: dict[str, float] = {"bull": 0.0, "bear": 0.0, "chop": 0.0}
    regime_bars: dict[str, int] = {"bull": 0, "bear": 0, "chop": 0}
    curve: list[float] = [1.0]

    funding_pnl = 0.0
    for i in range(1, n):
        bar_ret = closes[i] / closes[i - 1] - 1.0
        pnl = pos * bar_ret
        # 资金费：多头付正费率、空头收（负号）
        f_rate = funding_by_bar.get(i, 0.0)
        if f_rate and pos != 0.0:
            f_pnl = -pos * f_rate
            pnl += f_pnl
            funding_pnl += f_pnl
        # 换手成本（在第 i 根收盘调仓）
        new_pos = positions[i]
        turnover = abs(new_pos - pos)
        fee = turnover * cost.one_way
        step = (1.0 + pnl) * (1.0 - fee)
        equity *= step
        rets.append(step - 1.0)
        curve.append(equity)
        cost_paid += fee * equity
        lab = labels[i]
        regime_ret[lab] += pnl - fee
        regime_bars[lab] += 1
        if pos != 0.0:
            exposure_bars += 1
        if turnover > 1e-12:
            trades += 1
        pos = new_pos
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)

    years = max((candles[-1].timestamp - candles[0].timestamp).days / 365.25, 1e-9)
    bpy = BARS_PER_YEAR.get(timeframe, 2190)
    mean_r = sum(rets) / len(rets)
    var_r = sum((r - mean_r) ** 2 for r in rets) / len(rets)
    std_r = var_r ** 0.5

    report.total_return_pct = (equity - 1.0) * 100
    report.cagr_pct = ((equity ** (1 / years)) - 1.0) * 100 if equity > 0 else -100.0
    report.max_drawdown_pct = max_dd * 100
    report.sharpe = (mean_r / std_r) * math.sqrt(bpy) if std_r > 0 else 0.0
    report.trades = trades
    report.exposure = exposure_bars / (n - 1)
    report.cost_paid_pct = cost_paid * 100
    report.funding_pnl_pct = funding_pnl * 100
    # 各状态收益以「简单加总的 bar 收益」表达（近似贡献，便于对比）
    report.regime_return_pct = {k: v * 100 for k, v in regime_ret.items()}
    report.regime_bars = regime_bars
    report.equity_curve = curve
    return report


def run_classic_backtest(
    candles: list[Candle],
    *,
    strategy: str,
    symbol: str,
    timeframe: str,
    cost: CostModel | None = None,
    **params,
) -> ClassicReport:
    """便捷入口：按策略名生成仓位并模拟。"""
    fn = STRATEGIES[strategy]
    positions = fn(candles, **params) if params else fn(candles)
    return simulate(
        candles,
        positions,
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        cost=cost,
    )
