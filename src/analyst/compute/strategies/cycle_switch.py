"""牛熊周期切换策略（cycle_switch）。

按市场相位切换打法，而非全年只做多或只做空：

  相位判定（BTC 定全市场，山寨跟随 beta）
  ─────────────────────────────────────
  · bear：减半日历处于熊市区间 且 BTC 收盘跌破 200 日 EMA（双确认）
  · bull：BTC 站上 200 日 EMA（带 3% 缓冲防抖）
  · accum：其余时间（筑底/过渡，允许做多、不做空）

  各相位执行
  ─────────────────────────────────────
  · bull / accum → 唐奇安 40/20 只多（突破 40 根高点进，跌破 20 根低点出）
  · bear         → 清多；等反弹 z-score > 1.5 才做空（默认半仓），z 回 0 平仓
                   卖强不卖弱——追跌做空在加密市场已被回测证伪
  · 保险丝       → 相位翻出 bear 即强平空单（防「周期不重演」）

  回测与监控
  ─────────────────────────────────────
  · 组合回测：analyst backtest-classic BTC -s cycle_switch --days 1825
  · 实时相位：analyst cycle-status BTC,ETH,SOL
  · 周期展望：analyst cycle-outlook（Wolfy 刻舟求剑 + 狼波提醒）
  · 各盯盘币对在配置周期（默认 4h）收盘评估仓位；相位用 BTC 定调
  · 仓位变化 → 该币页面 + TG + AI 候选；周期位置日更见 cycle_outlook（每天 1 条）

  注意：减半日历边界（牛 550 天 / 熊 400 天）仅拟合 2 个完整周期，
  必须与均线双确认一起用；回测≠未来，上线先 paper trading。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from analyst.compute.indicators import ema
from analyst.data.fetcher import Candle, CandleSeries

# BTC 减半日期（未来为预估）
HALVING_DATES = [
    datetime(2012, 11, 28),
    datetime(2016, 7, 9),
    datetime(2020, 5, 11),
    datetime(2024, 4, 19),
    datetime(2028, 4, 1),
]


@dataclass
class CycleSwitchConfig:
    """cycle_switch 参数（与回测默认一致）。"""

    entry_n: int = 40
    exit_n: int = 20
    fade_z: float = 1.5
    mr_period: int = 20
    short_size: float = 0.5
    bull_days: int = 550
    bear_days: int = 400
    ma_period: int = 1200      # 4h × 1200 ≈ 200 日
    ma_band: float = 0.03


@dataclass
class CycleSwitchSignal:
    """实时监控：目标仓位 + 牛熊相位。"""

    market_regime: str         # bull / bear / accum
    calendar_phase: str
    target_position: float     # 1.0 / 0.0 / -short_size
    prev_position: float
    changed: bool
    price: float
    reasons: list[str] = field(default_factory=list)
    donchian_entry: float | None = None
    donchian_exit: float | None = None
    z_score: float | None = None
    days_since_halving: int = 0


def halving_phase(
    ts: datetime,
    bull_days: int = 550,
    bear_days: int = 400,
) -> str:
    """减半日历相位：减半后 0-bull_days 牛；再 bear_days 熊；之后筑底。"""
    past = [h for h in HALVING_DATES if h <= ts]
    if not past:
        return "accum"
    d = (ts - past[-1]).days
    if d < bull_days:
        return "bull"
    if d < bull_days + bear_days:
        return "bear"
    return "accum"


def build_cycle_regime(
    btc_candles: list[Candle],
    *,
    bull_days: int = 550,
    bear_days: int = 400,
    ma_period: int = 1200,
    band: float = 0.03,
) -> dict[datetime, str]:
    """用 BTC 定全市场牛熊（山寨跟随 BTC beta）。

    双确认：日历说熊 且 价格跌破 200 日 EMA → bear；站上 EMA → bull；其余 accum。
    """
    closes = [c.close for c in btc_candles]
    e_ma = ema(closes, ma_period)
    regime: dict[datetime, str] = {}
    ma_state = "bull"
    for i, c in enumerate(btc_candles):
        if c.close > e_ma[i] * (1 + band):
            ma_state = "bull"
        elif c.close < e_ma[i] * (1 - band):
            ma_state = "bear"
        cal = halving_phase(c.timestamp, bull_days, bear_days)
        if ma_state == "bear" and cal == "bear":
            regime[c.timestamp] = "bear"
        elif ma_state == "bull":
            regime[c.timestamp] = "bull"
        else:
            regime[c.timestamp] = "accum"
    return regime


def positions_cycle_switch(
    candles: list[Candle],
    regime: dict[datetime, str],
    entry_n: int = 40,
    exit_n: int = 20,
    fade_z: float = 1.5,
    mr_period: int = 20,
    short_size: float = 0.5,
) -> list[float]:
    """生成每根收盘后的目标仓位序列。"""
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    pos = 0.0
    out: list[float] = []
    for i in range(len(candles)):
        if i < max(entry_n, mr_period):
            out.append(0.0)
            continue
        reg = regime.get(candles[i].timestamp, "accum")
        c = closes[i]
        if reg == "bear":
            if pos > 0:
                pos = 0.0
            window = closes[i - mr_period + 1:i + 1]
            mean = sum(window) / mr_period
            var = sum((v - mean) ** 2 for v in window) / mr_period
            std = var ** 0.5
            z = (c - mean) / std if std > 0 else 0.0
            if pos == 0.0 and z > fade_z:
                pos = -short_size
            elif pos < 0 and z <= 0:
                pos = 0.0
        else:
            if pos < 0:
                pos = 0.0
            hh = max(highs[i - entry_n:i])
            lx = min(lows[max(0, i - exit_n):i])
            if pos == 0.0 and c > hh:
                pos = 1.0
            elif pos > 0 and c < lx:
                pos = 0.0
        out.append(pos)
    return out


def _position_label(pos: float) -> str:
    if pos > 0:
        return "做多 100%"
    if pos < 0:
        return f"做空 {abs(pos):.0%}"
    return "空仓"


def evaluate_cycle_switch(
    series: CandleSeries,
    regime: dict[datetime, str],
    *,
    prev_position: float = 0.0,
    cfg: CycleSwitchConfig | None = None,
) -> CycleSwitchSignal:
    """评估单品种当前目标仓位（用于监控告警）。"""
    cfg = cfg or CycleSwitchConfig()
    candles = series.candles
    if len(candles) < max(cfg.entry_n, cfg.mr_period) + 1:
        return CycleSwitchSignal(
            market_regime="accum",
            calendar_phase="accum",
            target_position=0.0,
            prev_position=prev_position,
            changed=False,
            price=series.latest_close if candles else 0.0,
            reasons=["数据不足"],
        )

    positions = positions_cycle_switch(
        candles,
        regime,
        entry_n=cfg.entry_n,
        exit_n=cfg.exit_n,
        fade_z=cfg.fade_z,
        mr_period=cfg.mr_period,
        short_size=cfg.short_size,
    )
    target = positions[-1]
    last = candles[-1]
    ts = last.timestamp
    reg = regime.get(ts, "accum")
    cal = halving_phase(ts, cfg.bull_days, cfg.bear_days)
    past = [h for h in HALVING_DATES if h <= ts]
    days_since = (ts - past[-1]).days if past else 0

    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    i = len(candles) - 1
    hh = max(highs[i - cfg.entry_n:i]) if i >= cfg.entry_n else None
    lx = min(lows[max(0, i - cfg.exit_n):i]) if i >= cfg.exit_n else None
    w = closes[-cfg.mr_period:]
    mean = sum(w) / len(w)
    std = (sum((v - mean) ** 2 for v in w) / len(w)) ** 0.5
    z = (closes[-1] - mean) / std if std > 0 else 0.0

    zh = {"bull": "牛市", "bear": "熊市", "accum": "筑底"}
    reasons = [
        f"双确认相位={zh.get(reg, reg)}（日历={zh.get(cal, cal)}）",
        f"目标仓位={_position_label(target)}",
    ]
    if reg == "bear":
        reasons.append(f"z-score={z:+.2f}（做空需 >{cfg.fade_z}）")
    else:
        if hh is not None:
            reasons.append(f"唐奇安入场位={hh:.6g} 离场位={lx:.6g}")

    return CycleSwitchSignal(
        market_regime=reg,
        calendar_phase=cal,
        target_position=target,
        prev_position=prev_position,
        changed=abs(target - prev_position) > 1e-9,
        price=last.close,
        reasons=reasons,
        donchian_entry=hh,
        donchian_exit=lx,
        z_score=z,
        days_since_halving=days_since,
    )
