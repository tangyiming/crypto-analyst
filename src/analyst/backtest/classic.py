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
  牛/筑底跑唐奇安只多，熊市只做「反弹冲高做空」（卖强不卖弱）
- buy_hold: 基准
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


STRATEGIES = {
    "buy_hold": positions_buy_hold,
    "donchian": positions_donchian,
    "ema_cross": positions_ema_cross,
    "boll_mr": positions_boll_mr,
    "cycle_switch": positions_cycle_switch,   # 需要 regime 参数（BTC 定牛熊）
}


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
) -> ClassicReport:
    """按目标仓位序列模拟复利权益曲线（第 i 根仓位从 i+1 根生效）。"""
    cost = cost or CostModel()
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

    for i in range(1, n):
        bar_ret = closes[i] / closes[i - 1] - 1.0
        pnl = pos * bar_ret
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
