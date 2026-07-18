"""向前回放回测引擎。

两条评估线（与实时盯盘同一套代码路径，保证回测≈实盘逻辑）：

1. 双线反转策略（evaluate_double_line）：
   逐根收盘回放 → 出现可交易信号即按 plan 开仓 →
   之后逐根判定 止损 / TP1 / 超时（同根先到止损，保守口径）。
   输出胜率、平均 R、累计 R、盈亏比、最大回撤（R 口径）。

2. 规则告警（evaluate_closed_bar_rules）：
   每个带方向的告警用「ATR 屏障」前瞻验证：
   未来 horizon 根内先走出 +1×ATR（顺方向）算命中，
   先走出 -1×ATR（逆方向）算打脸，都没到算 flat。
   输出每条规则的样本数 / 命中率 / 平均前瞻收益。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from analyst.compute.strategies.double_line_reversal import (
    DoubleLineConfig,
    _atr,
    evaluate_double_line,
)
from analyst.data.fetcher import CandleSeries, fetch_candles
from analyst.monitor.rules import RuleConfig, evaluate_closed_bar_rules

# 指标窗口最少需要的历史根数
WARMUP_BARS = 60


@dataclass
class Trade:
    """一笔模拟交易（双线反转策略）。"""

    direction: str
    entry_time: datetime
    entry: float
    stop_loss: float
    take_profit: float
    exit_time: datetime | None = None
    exit_price: float | None = None
    outcome: str = "open"          # tp / sl / timeout / open
    pnl_r: float = 0.0             # 未加权 R（每笔名义风险=1R）
    weighted_pnl_r: float = 0.0    # pnl_r × risk_scale（近似资金曲线）
    risk_scale: float = 1.0
    bars_held: int = 0


@dataclass
class RuleStat:
    """单条规则的前瞻统计。"""

    rule: str
    n: int = 0
    wins: int = 0
    losses: int = 0
    flat: int = 0
    sum_fwd_ret_pct: float = 0.0   # 按方向符号化的前瞻收益合计

    @property
    def win_rate(self) -> float:
        resolved = self.wins + self.losses
        return self.wins / resolved if resolved else 0.0

    @property
    def avg_fwd_ret_pct(self) -> float:
        return self.sum_fwd_ret_pct / self.n if self.n else 0.0


@dataclass
class BacktestReport:
    symbol: str
    timeframe: str
    bars: int
    start: datetime | None
    end: datetime | None
    trades: list[Trade] = field(default_factory=list)
    rule_stats: dict[str, RuleStat] = field(default_factory=dict)

    # ── 策略汇总 ──
    @property
    def closed_trades(self) -> list[Trade]:
        return [t for t in self.trades if t.outcome in ("tp", "sl", "timeout")]

    @property
    def win_rate(self) -> float:
        closed = [t for t in self.closed_trades if t.outcome in ("tp", "sl")]
        if not closed:
            return 0.0
        return sum(1 for t in closed if t.outcome == "tp") / len(closed)

    @property
    def total_r(self) -> float:
        return sum(t.pnl_r for t in self.closed_trades)

    @property
    def total_weighted_r(self) -> float:
        return sum(t.weighted_pnl_r for t in self.closed_trades)

    @property
    def avg_r(self) -> float:
        closed = self.closed_trades
        return self.total_r / len(closed) if closed else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl_r for t in self.closed_trades if t.pnl_r > 0)
        losses = -sum(t.pnl_r for t in self.closed_trades if t.pnl_r < 0)
        if losses <= 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    @property
    def max_drawdown_r(self) -> float:
        """按交易顺序累计 R 的最大回撤。"""
        peak = 0.0
        cum = 0.0
        dd = 0.0
        for t in self.closed_trades:
            cum += t.pnl_r
            peak = max(peak, cum)
            dd = min(dd, cum - peak)
        return dd

    @property
    def max_drawdown_weighted_r(self) -> float:
        peak = 0.0
        cum = 0.0
        dd = 0.0
        for t in self.closed_trades:
            cum += t.weighted_pnl_r
            peak = max(peak, cum)
            dd = min(dd, cum - peak)
        return dd

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars": self.bars,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "strategy": {
                "trades": len(self.closed_trades),
                "win_rate": round(self.win_rate, 4),
                "total_r": round(self.total_r, 2),
                "total_weighted_r": round(self.total_weighted_r, 2),
                "avg_r": round(self.avg_r, 3),
                "profit_factor": (
                    round(self.profit_factor, 2)
                    if self.profit_factor != float("inf")
                    else None
                ),
                "max_drawdown_r": round(self.max_drawdown_r, 2),
                "max_drawdown_weighted_r": round(self.max_drawdown_weighted_r, 2),
                "detail": [
                    {
                        "direction": t.direction,
                        "entry_time": t.entry_time.isoformat(),
                        "entry": t.entry,
                        "stop_loss": t.stop_loss,
                        "take_profit": t.take_profit,
                        "outcome": t.outcome,
                        "pnl_r": round(t.pnl_r, 2),
                        "weighted_pnl_r": round(t.weighted_pnl_r, 2),
                        "risk_scale": round(t.risk_scale, 3),
                        "bars_held": t.bars_held,
                    }
                    for t in self.trades
                ],
            },
            "rules": {
                name: {
                    "n": st.n,
                    "wins": st.wins,
                    "losses": st.losses,
                    "flat": st.flat,
                    "win_rate": round(st.win_rate, 4),
                    "avg_fwd_ret_pct": round(st.avg_fwd_ret_pct, 4),
                }
                for name, st in sorted(self.rule_stats.items())
            },
        }


def _simulate_trade(
    candles: list,
    signal_idx: int,
    direction: str,
    entry: float,
    stop: float,
    tp: float,
    max_hold: int,
    risk_scale: float = 1.0,
) -> Trade:
    """从 signal_idx 的下一根开始判定 SL / TP / 超时（同根先算止损）。"""
    rs = max(0.0, float(risk_scale or 1.0))
    trade = Trade(
        direction=direction,
        entry_time=candles[signal_idx].timestamp,
        entry=entry,
        stop_loss=stop,
        take_profit=tp,
        risk_scale=rs,
    )

    def _apply_weight() -> None:
        trade.weighted_pnl_r = trade.pnl_r * rs

    risk = abs(entry - stop)
    if risk <= 0:
        trade.outcome = "timeout"
        return trade

    end = min(len(candles), signal_idx + 1 + max_hold)
    for i in range(signal_idx + 1, end):
        c = candles[i]
        if direction == "long":
            hit_sl = c.low <= stop
            hit_tp = c.high >= tp
        else:
            hit_sl = c.high >= stop
            hit_tp = c.low <= tp
        if hit_sl:  # 保守：同根双触先算止损
            trade.outcome = "sl"
            trade.pnl_r = -1.0
            trade.exit_price = stop
            trade.exit_time = c.timestamp
            trade.bars_held = i - signal_idx
            _apply_weight()
            return trade
        if hit_tp:
            trade.outcome = "tp"
            trade.pnl_r = abs(tp - entry) / risk
            trade.exit_price = tp
            trade.exit_time = c.timestamp
            trade.bars_held = i - signal_idx
            _apply_weight()
            return trade

    # 超时：以窗口末根收盘平仓
    if end - 1 > signal_idx:
        c = candles[end - 1]
        trade.outcome = "timeout" if end < len(candles) else "open"
        move = (c.close - entry) if direction == "long" else (entry - c.close)
        trade.pnl_r = move / risk
        trade.exit_price = c.close
        trade.exit_time = c.timestamp
        trade.bars_held = end - 1 - signal_idx
        # 数据末尾未平仓的单，不计入 closed 统计
        if trade.outcome == "open":
            trade.pnl_r = 0.0
        _apply_weight()
    return trade


def _rule_forward_outcome(
    candles: list,
    idx: int,
    direction: str,
    horizon: int,
    atr: float,
) -> tuple[str, float]:
    """ATR 屏障前瞻：先 +1×ATR 顺向 → win；先 1×ATR 逆向 → loss；否则 flat。

    返回 (outcome, 按方向符号化的前瞻收益%)。
    """
    price = candles[idx].close
    if price <= 0 or atr <= 0:
        return "flat", 0.0
    up_barrier = price + atr
    dn_barrier = price - atr
    end = min(len(candles), idx + 1 + horizon)
    outcome = "flat"
    for i in range(idx + 1, end):
        c = candles[i]
        if direction == "long":
            if c.low <= dn_barrier:   # 保守：同根先算逆向
                outcome = "loss"
                break
            if c.high >= up_barrier:
                outcome = "win"
                break
        else:
            if c.high >= up_barrier:
                outcome = "loss"
                break
            if c.low <= dn_barrier:
                outcome = "win"
                break

    last = candles[end - 1].close if end - 1 > idx else price
    ret_pct = (last - price) / price * 100.0
    if direction == "short":
        ret_pct = -ret_pct
    return outcome, ret_pct


def run_backtest(
    symbol: str,
    timeframe: str = "15m",
    *,
    bars: int = 1000,
    market: str = "futures",
    strategy_cfg: DoubleLineConfig | None = None,
    rule_cfg: RuleConfig | None = None,
    include_rules: bool = True,
    rule_horizon: int = 12,
    max_hold: int = 96,
    series: CandleSeries | None = None,
    lookback: int | None = 320,
) -> BacktestReport:
    """对单品种单周期做向前回放回测。

    Args:
        bars: 拉取历史根数（binance 单次上限 1500）
        rule_horizon: 规则前瞻窗口（根）
        max_hold: 策略单笔最长持仓（根），超时按收盘平仓
        series: 传入现成 K 线（测试用）；否则 REST 拉取
        lookback: 策略评估只用最近 N 根（EMA200 约需 220+）；
                  None=用全部历史（短样本可，多年回测会极慢）
    """
    strategy_cfg = strategy_cfg or DoubleLineConfig()
    rule_cfg = rule_cfg or RuleConfig()
    if series is None:
        series = fetch_candles(
            symbol,
            timeframe=timeframe,
            limit=min(bars, 1500),
            use_cache=False,
            market=market,
        )
    candles = series.candles
    report = BacktestReport(
        symbol=series.symbol,
        timeframe=series.timeframe,
        bars=len(candles),
        start=candles[0].timestamp if candles else None,
        end=candles[-1].timestamp if candles else None,
    )
    if len(candles) <= WARMUP_BARS:
        return report

    rule_state: dict = {}
    in_position_until = -1          # 持仓期间不重复开仓（bar index）
    last_entry_bar = -1
    # 规则状态机需要连续全历史；策略评估可截断
    lb = None if lookback is None else max(int(lookback), WARMUP_BARS + 20)

    for i in range(WARMUP_BARS, len(candles)):
        if lb is None:
            win_candles = candles[: i + 1]
        else:
            start = max(0, i + 1 - lb)
            win_candles = candles[start : i + 1]
        window = CandleSeries(
            symbol=series.symbol,
            timeframe=series.timeframe,
            candles=win_candles,
        )

        # ── 1. 双线反转策略 ──
        if i > in_position_until and i != last_entry_bar:
            sig = evaluate_double_line(window, strategy_cfg)
            if sig.direction in ("long", "short") and sig.plan is not None:
                plan = sig.plan
                entry = sig.price
                trade = _simulate_trade(
                    candles,
                    i,
                    sig.direction,
                    entry,
                    plan.stop_loss,
                    plan.take_profit_1,
                    max_hold,
                    risk_scale=getattr(sig, "risk_scale", 1.0) or 1.0,
                )
                report.trades.append(trade)
                last_entry_bar = i
                in_position_until = i + max(trade.bars_held, 1)

        # ── 2. 规则告警前瞻 ──
        if include_rules:
            # 规则依赖跨 bar 状态，仍喂全量前缀
            full_window = CandleSeries(
                symbol=series.symbol,
                timeframe=series.timeframe,
                candles=candles[: i + 1],
            )
            events, rule_state = evaluate_closed_bar_rules(
                full_window, rule_state, rule_cfg
            )
            if events:
                atr = _atr(candles[max(0, i - 50) : i + 1], 14)
                for ev in events:
                    if ev.direction not in ("long", "short"):
                        continue
                    outcome, ret_pct = _rule_forward_outcome(
                        candles, i, ev.direction, rule_horizon, atr
                    )
                    st = report.rule_stats.setdefault(ev.rule, RuleStat(rule=ev.rule))
                    st.n += 1
                    st.sum_fwd_ret_pct += ret_pct
                    if outcome == "win":
                        st.wins += 1
                    elif outcome == "loss":
                        st.losses += 1
                    else:
                        st.flat += 1

    return report
