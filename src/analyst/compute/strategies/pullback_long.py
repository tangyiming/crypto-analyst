"""「强势盘低多」打法的可执行规则 + 事件式回测。

把点位引擎（swing_levels / market_regime）的候选位变成一张**可下单的计划**（OrderPlan），
回测与纸面/实盘共用同一个 `build_plan()`，保证回测的就是将来跑的。

规则 v1（只做多；空单镜像另测）：
- 触发：4h 收盘时 regime == strong_trend 且 trade_side == long（日线/周线定调多 + 小时走强），防守位未破。
- 挂单：限价多 = 回踩 0.618；没有则用近支撑。要求在现价下方 0.4%–4%。
- 止损：min(近枢轴低, 4h BOLL 下轨) 再下 0.3%；距离限定 0.8%–4%（太窄扫针，太宽盈亏比差）。
- 第一止盈（一半）：近阻力；若盈亏比 < 1.2 用本波 1.5 延伸；成交后止损上移到成本（保本损）。
- 余仓：4h 收盘跌破 4h BOLL 中轨离场，或持有满 MAX_HOLD 根。
- 挂单有效期 6 根 4h（24h）；同一品种同时只持一仓。
- 仓位：每笔风险 = 权益 × risk_pct；名义 ≤ 权益 × max_leverage。

成本：限价入场按 maker 0.02%；止损/止盈/离场按 taker 0.05% + 滑点 0.02%；资金费按历史费率逐次结算。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from analyst.compute.swing_levels import SwingLevels
from analyst.compute.market_regime import MarketRegime
from analyst.data.fetcher import Candle, CandleSeries

MAKER_FEE = 0.0002
TAKER_FEE = 0.0005
SLIPPAGE = 0.0002


@dataclass
class PullbackLongConfig:
    risk_pct: float = 1.0          # 每笔风险占权益 %
    max_leverage: float = 3.0      # 名义敞口上限（倍权益）
    entry_min_below: float = 0.004  # 挂单至少低于现价 0.4%
    entry_max_below: float = 0.04
    stop_pad: float = 0.003
    min_stop_pct: float = 0.008
    max_stop_pct: float = 0.04
    min_rr: float = 1.2
    tp1_frac: float = 0.5
    order_ttl_bars: int = 6        # 4h 根
    max_hold_bars: int = 60        # 4h 根 = 10 天
    allow_short: bool = False
    entry_pref: str = "618"        # 618 | 50 | support：挂单位优先级
    exit_rule: str = "boll_mid"    # boll_mid | ema12h | prev_low：TP1 后余仓离场规则
    be_after_tp1: bool = True      # TP1 后止损上移成本


@dataclass
class OrderPlan:
    symbol: str
    side: str                 # long / short
    entry: float
    stop: float
    tp1: float
    created_at: datetime
    expires_at: datetime
    reasons: list[str] = field(default_factory=list)
    regime: str = ""
    ref_price: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["expires_at"] = self.expires_at.isoformat()
        return d

    @property
    def risk_pct(self) -> float:
        return abs(self.entry - self.stop) / self.entry

    @property
    def rr(self) -> float:
        return abs(self.tp1 - self.entry) / max(1e-9, abs(self.entry - self.stop))


def build_plan(
    symbol: str,
    price: float,
    swing: SwingLevels,
    reg: MarketRegime,
    now: datetime,
    cfg: PullbackLongConfig | None = None,
    *,
    bar_seconds: int = 4 * 3600,
) -> OrderPlan | None:
    """从当前点位/盘面生成一张计划；不满足条件返回 None。回测与实盘共用。"""
    cfg = cfg or PullbackLongConfig()
    if reg.regime != "strong_trend" or reg.trade_side != "long" or reg.defense_broken:
        return None
    # 入场位：按 entry_pref 排优先级（默认回踩 0.618 → 近支撑）
    order = {
        "618": (reg.pullback_618, reg.nearest_support),
        "50": (reg.pullback_50, reg.pullback_618, reg.nearest_support),
        "support": (reg.nearest_support, reg.pullback_618),
    }.get(cfg.entry_pref, (reg.pullback_618, reg.nearest_support))
    cands = [x for x in order if x]
    entry = None
    for x in cands:
        below = 1 - x / price
        if cfg.entry_min_below <= below <= cfg.entry_max_below:
            entry = x
            break
    if entry is None:
        return None
    # 止损：近枢轴低 / 4h BOLL 下轨中较高者（离入场更近，贴在下方强支撑），再下 pad
    stop_cands = [x for x in (list(reg.pivot_supports)[:2] + [reg.boll_4h_lower]) if x and x < entry]
    stop = (max(stop_cands) if stop_cands else entry * (1 - cfg.max_stop_pct)) * (1 - cfg.stop_pad)
    dist = 1 - stop / entry
    if dist < cfg.min_stop_pct:
        stop = entry * (1 - cfg.min_stop_pct)
    if dist > cfg.max_stop_pct:
        stop = entry * (1 - cfg.max_stop_pct)
    # 第一止盈：近阻力；盈亏比不够则用 1.5 延伸 / 24h 高
    tp_cands = [x for x in (reg.nearest_resistance, reg.ext_150, reg.boll_4h_upper) if x and x > entry]
    tp1 = None
    for x in tp_cands:
        if (x - entry) / (entry - stop) >= cfg.min_rr:
            tp1 = x
            break
    if tp1 is None:
        return None
    return OrderPlan(
        symbol=symbol,
        side="long",
        entry=float(entry),
        stop=float(stop),
        tp1=float(tp1),
        created_at=now,
        expires_at=now + timedelta(seconds=bar_seconds * cfg.order_ttl_bars),
        reasons=[
            f"{reg.regime_zh}·{reg.trade_side}；回踩位 {entry:.6g}（{'回踩0.618' if entry == reg.pullback_618 else '近支撑'}）",
            f"止损 {stop:.6g}（{dist*100:.1f}%）· 一半止盈 {tp1:.6g}（RR {(tp1-entry)/(entry-stop):.1f}）",
            "成交后止损上移成本；余仓 4h 收盘破 BOLL 中轨离场",
        ],
        regime=reg.regime,
        ref_price=price,
    )


def position_size(equity: float, plan: OrderPlan, cfg: PullbackLongConfig) -> float:
    """按风险定仓位（基础货币数量），并受名义杠杆上限约束。"""
    risk_usd = equity * cfg.risk_pct / 100.0
    per_unit = abs(plan.entry - plan.stop)
    qty = risk_usd / per_unit if per_unit > 0 else 0.0
    max_qty = equity * cfg.max_leverage / plan.entry
    return max(0.0, min(qty, max_qty))


# ───────────────────────── 回测 ─────────────────────────


@dataclass
class Trade:
    symbol: str
    entry_time: datetime
    entry: float
    stop: float
    tp1: float
    qty: float
    exit_time: datetime | None = None
    exit_reason: str = ""
    pnl_usd: float = 0.0
    fees_usd: float = 0.0
    funding_usd: float = 0.0
    tp1_done: bool = False
    legs: list[tuple[str, float, float]] = field(default_factory=list)  # (reason, qty, price)


def _slice(series: CandleSeries, t: datetime, n: int) -> CandleSeries:
    cs = series.candles
    # 二分找 <= t 的最后一根
    lo, hi = 0, len(cs)
    while lo < hi:
        mid = (lo + hi) // 2
        if cs[mid].timestamp <= t:
            lo = mid + 1
        else:
            hi = mid
    return CandleSeries(series.symbol, series.timeframe, cs[max(0, lo - n) : lo])


def run_backtest(
    symbol: str,
    hourly: CandleSeries,
    h4: CandleSeries,
    daily: CandleSeries,
    *,
    cfg: PullbackLongConfig | None = None,
    equity0: float = 10_000.0,
    start: datetime | None = None,
    funding: list[tuple[int, float]] | None = None,
) -> dict[str, Any]:
    """事件式回测：4h 收盘出计划 → 1h 撮合限价/止损/止盈 → 4h 收盘管理余仓。"""
    from analyst.monitor.regime_live import compute_monitor_regime

    cfg = cfg or PullbackLongConfig()
    equity = equity0
    peak = equity
    max_dd = 0.0
    trades: list[Trade] = []
    open_trade: Trade | None = None
    pending: OrderPlan | None = None
    curve: list[tuple[datetime, float]] = []
    fund = sorted(funding or [])
    fi = 0

    h1 = hourly.candles
    bars4 = [c for c in h4.candles if (start is None or c.timestamp >= start)]
    # 1h 指针
    j = 0
    while j < len(h1) and h1[j].timestamp <= (bars4[0].timestamp if bars4 else h1[-1].timestamp):
        j += 1

    for i, bar in enumerate(bars4):
        t_close = bar.timestamp + timedelta(hours=4)
        # ── 撮合本根 4h 内的 1h K（限价入场 / 止损 / 止盈）──
        while j < len(h1) and h1[j].timestamp < t_close:
            c = h1[j]
            j += 1
            # 资金费结算
            while fi < len(fund) and fund[fi][0] <= int(c.timestamp.timestamp() * 1000) + 3_600_000:
                if open_trade and fund[fi][0] > int(c.timestamp.timestamp() * 1000):
                    f = -fund[fi][1] * open_trade.qty * c.close  # 多头付正费率
                    open_trade.funding_usd += f
                    equity += f
                fi += 1
            if pending and open_trade is None:
                if c.timestamp >= pending.expires_at:
                    pending = None
                elif c.low <= pending.entry:
                    qty = position_size(equity, pending, cfg)
                    if qty > 0:
                        fee = qty * pending.entry * MAKER_FEE
                        equity -= fee
                        open_trade = Trade(symbol, c.timestamp, pending.entry, pending.stop, pending.tp1, qty, fees_usd=fee)
                        # 同一根 K 内如果直接扫到止损，按止损处理（保守）
                    pending = None
            if open_trade:
                tr = open_trade
                if c.low <= tr.stop:
                    px = tr.stop * (1 - SLIPPAGE)
                    equity += _exit_leg(tr, tr.qty, px, "stop" if not tr.tp1_done else "be_stop", c.timestamp)
                    trades.append(tr)
                    open_trade = None
                elif not tr.tp1_done and c.high >= tr.tp1:
                    q = tr.qty * cfg.tp1_frac
                    px = tr.tp1 * (1 - SLIPPAGE)
                    equity += _exit_leg(tr, q, px, "tp1", None)
                    tr.tp1_done = True
                    if cfg.be_after_tp1:
                        tr.stop = max(tr.stop, tr.entry)  # 保本损
            peak = max(peak, equity)
            max_dd = min(max_dd, equity / peak - 1)
        # ── 4h 收盘：管理余仓 / 出新计划 ──
        w = _slice(h4, bar.timestamp, 300)
        d = _slice(daily, bar.timestamp - timedelta(hours=bar.timestamp.hour), 400)  # 只用已收盘日线
        hh = _slice(hourly, bar.timestamp + timedelta(hours=3, minutes=59), 240)
        if len(w.candles) < 60 or len(hh.candles) < 30:
            curve.append((t_close, equity))
            continue
        price = bar.close
        swing, reg = compute_monitor_regime(
            symbol=symbol, current_price=price, worker_series=w, daily_series=d, hourly_series=hh, h4_series=w,
            m5_series=None, btc_series=None,
            high_24h=max(x.high for x in hh.candles[-24:]), low_24h=min(x.low for x in hh.candles[-24:]),
        )
        if open_trade:
            tr = open_trade
            held = int((bar.timestamp - tr.entry_time).total_seconds() // (4 * 3600))
            exit_now = None
            if tr.tp1_done:
                if cfg.exit_rule == "boll_mid" and reg.boll_4h_mid and price < reg.boll_4h_mid:
                    exit_now = "trail_boll_mid"
                elif cfg.exit_rule == "ema12h" and reg.ema12h_6 and price < reg.ema12h_6:
                    exit_now = "trail_ema12h"
                elif cfg.exit_rule == "prev_low" and i >= 1 and price < bars4[i - 1].low:
                    exit_now = "trail_prev_low"
            if exit_now is None and held >= cfg.max_hold_bars:
                exit_now = "timeout"
            if exit_now:
                px = price * (1 - SLIPPAGE)
                equity += _exit_leg(tr, tr.qty, px, exit_now, t_close)
                trades.append(tr)
                open_trade = None
        if open_trade is None and pending is None:
            pending = build_plan(symbol, price, swing, reg, t_close, cfg)
        curve.append((t_close, equity))
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1)

    if open_trade:
        tr = open_trade
        equity += _exit_leg(tr, tr.qty, h1[-1].close, "open_end", h1[-1].timestamp)
        trades.append(tr)

    wins = [t for t in trades if _net(t) > 0]
    rets = [_net(t) for t in trades]
    return {
        "symbol": symbol,
        "trades": trades,
        "n": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "total_return_pct": (equity / equity0 - 1) * 100,
        "max_dd_pct": max_dd * 100,
        "avg_trade_usd": sum(rets) / len(rets) if rets else 0.0,
        "profit_factor": (sum(r for r in rets if r > 0) / abs(sum(r for r in rets if r < 0))) if any(r < 0 for r in rets) else float("inf"),
        "equity": equity,
        "curve": curve,
        "cfg": asdict(cfg),
    }


def _exit_leg(tr: Trade, qty: float, px: float, reason: str, t: datetime | None) -> float:
    """按 qty 在 px 平掉一部分：记录腿、累计毛利与出场费；返回本腿对权益的净影响。"""
    gross = qty * (px - tr.entry)
    fee = qty * px * TAKER_FEE
    tr.legs.append((reason, qty, px))
    tr.pnl_usd += gross
    tr.fees_usd += fee
    tr.qty -= qty
    if t is not None:
        tr.exit_time = t
        tr.exit_reason = reason
    return gross - fee


def _net(tr: Trade) -> float:
    return tr.pnl_usd - tr.fees_usd + tr.funding_usd

