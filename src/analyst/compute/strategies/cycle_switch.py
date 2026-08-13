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
  · bear         → 清多；两条空腿（均默认半仓）：
                   ① 反弹 z-score > 1.5 做空，z 回 0 平仓（卖强不卖弱）
                   ② 唐奇安破位空：跌破 40 根低点进，收回 20 根高点出
                     （bear_trend_short，5 年回测 BTC/ETH/SOL 熊市段收益
                      由 ≈0 提升至 +37%~+47%，可配置关闭）
  · 保险丝       → 相位翻出 bear 即强平空单（防「周期不重演」）

  回测与监控
  ─────────────────────────────────────
  · 组合回测：analyst backtest-classic BTC -s cycle_switch --days 1825
  · 实时相位：analyst cycle-status BTC,ETH,SOL
  · 周期展望：analyst cycle-outlook（Wolfy 刻舟求剑 + 狼波提醒）
  · 各盯盘币对在配置周期（默认 4h）收盘评估仓位；相位用 BTC 定调
  · 仓位相对上一根 K 线变化 → 页面告警 + AI 候选（不直推 TG；可交易由 AI→ai_plan）
  · 周期位置日更见 cycle_outlook（每天 1 条）
  · 评估品种可用 MONITOR_CYCLE_SYMBOLS 白名单（空=跟随 DEFAULT_SYMBOLS）

  注意：减半日历边界（牛 550 天 / 熊 400 天）仅拟合 2 个完整周期，
  必须与均线双确认一起用；回测≠未来，上线前请小仓位验证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from analyst.compute.indicators import compute_adx, ema
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
    bear_trend_short: bool = True  # 熊市加唐奇安破位空腿
    bull_days: int = 550
    bear_days: int = 400
    ma_period: int = 1200  # 4h × 1200 ≈ 200 日
    ma_band: float = 0.03
    min_adx: float = 20.0  # 新开仓 ADX 门槛；0=关闭（回测仓位不受影响）
    # 波动率目标化（建议仓位提示用）：5 年回测 MDD -45.7%→-28.0%、
    # 最差 180 天窗口 -42.4%→-19.0%，CAGR 仅让 2pp（scripts/optimize_experiments.py）
    vol_target: float = 0.30   # 目标年化波动；0=关闭
    vol_lookback: int = 42     # 已实现波动率窗口（根）


@dataclass
class CycleSwitchSignal:
    """实时监控：目标仓位 + 牛熊相位。"""

    market_regime: str  # bull / bear / accum
    calendar_phase: str
    target_position: float  # 1.0 / 0.0 / -short_size
    prev_position: float
    changed: bool
    price: float
    reasons: list[str] = field(default_factory=list)
    donchian_entry: float | None = None
    donchian_exit: float | None = None
    z_score: float | None = None
    days_since_halving: int = 0
    adx: float = 0.0
    chop_blocked: bool = False  # 新开仓但 ADX 过低：页面仍提示，不当交易候选
    vol_scale: float = 1.0      # 波动率目标化缩放系数（0.15–1.0）
    suggested_size: float = 0.0  # target_position × vol_scale（建议仓位）


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
    bear_trend_short: bool = True,
    **_ignored,
) -> list[float]:
    """生成每根收盘后的目标仓位序列。

    ``**_ignored`` 兼容旧调用里多余的 keyword（如 symbol / trail_dd_pct），一律忽略。
    """
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    pos = 0.0
    mode: str | None = None  # 持仓归属：fade（z 反弹空）/ tshort（唐奇安空）/ None
    out: list[float] = []
    for i in range(len(candles)):
        if i < max(entry_n, mr_period):
            out.append(0.0)
            continue
        reg = regime.get(candles[i].timestamp, "accum")
        c = closes[i]
        if reg == "bear":
            if pos > 0:
                pos, mode = 0.0, None
            if bear_trend_short:
                ll = min(lows[i - entry_n : i])
                hx = max(highs[max(0, i - exit_n) : i])
                if pos == 0.0 and c < ll:
                    pos, mode = -short_size, "tshort"
                elif pos < 0 and mode == "tshort" and c > hx:
                    pos, mode = 0.0, None
            window = closes[i - mr_period + 1 : i + 1]
            mean = sum(window) / mr_period
            var = sum((v - mean) ** 2 for v in window) / mr_period
            std = var ** 0.5
            z = (c - mean) / std if std > 0 else 0.0
            if pos == 0.0 and z > fade_z:
                pos, mode = -short_size, "fade"
            elif pos < 0 and mode == "fade" and z <= 0:
                pos, mode = 0.0, None
        else:
            if pos < 0:
                pos, mode = 0.0, None
            hh = max(highs[i - entry_n : i])
            lx = min(lows[max(0, i - exit_n) : i])
            if pos == 0.0 and c > hh:
                pos, mode = 1.0, "trend"
            elif pos > 0 and c < lx:
                pos, mode = 0.0, None
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
        bear_trend_short=cfg.bear_trend_short,
    )
    target = positions[-1]
    # 用上一根收盘仓位判断变化，避免进程重启时 prev=0 误报「空仓→持仓」
    prev_bar = positions[-2] if len(positions) >= 2 else 0.0
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
    hh = max(highs[i - cfg.entry_n : i]) if i >= cfg.entry_n else None
    lx = min(lows[max(0, i - cfg.exit_n) : i]) if i >= cfg.exit_n else None
    w = closes[-cfg.mr_period :]
    mean = sum(w) / len(w)
    std = (sum((v - mean) ** 2 for v in w) / len(w)) ** 0.5
    z = (closes[-1] - mean) / std if std > 0 else 0.0

    zh = {"bull": "牛市", "bear": "熊市", "accum": "筑底"}
    reasons = [
        f"双确认相位={zh.get(reg, reg)}（日历={zh.get(cal, cal)}）",
        f"目标仓位={_position_label(target)}",
    ]
    if reg == "bear":
        ll = min(lows[i - cfg.entry_n : i]) if i >= cfg.entry_n else None
        hx2 = max(highs[max(0, i - cfg.exit_n) : i]) if i >= 1 else None
        broke_down = (
            cfg.bear_trend_short and ll is not None and closes[-1] < ll
        )
        if cfg.bear_trend_short and ll is not None:
            reasons.append(f"熊市唐奇安：破位空={ll:.6g} / 回补={hx2:.6g}")
        if target < 0 and prev_bar >= 0:
            cause = (
                f"唐奇安破位（收盘 < {ll:.6g}）"
                if broke_down
                else f"z-score={z:+.2f} > {cfg.fade_z}"
            )
            reasons.append(f"新开空：{cause}")
        elif target < 0:
            reasons.append(f"持有空仓：z-score={z:+.2f}")
        elif target == 0 and prev_bar < 0:
            reasons.append(f"平空：z-score={z:+.2f} ≤ 0 或收回 {cfg.exit_n} 根高点")
        else:
            cond = f"需 z>{cfg.fade_z}"
            if cfg.bear_trend_short and ll is not None:
                cond += f" 或收盘破 {ll:.6g}"
            reasons.append(f"观望未开空：z-score={z:+.2f}（{cond}）")
    else:
        if hh is not None:
            reasons.append(f"唐奇安入场={hh:.6g} / 离场={lx:.6g}")
        if target > 0 and prev_bar <= 0:
            reasons.append("唐奇安突破开多")
        elif target == 0 and prev_bar > 0:
            reasons.append("唐奇安跌破离场")
        elif target > 0:
            reasons.append("持有多仓（唐奇安）")
        else:
            reasons.append("观望：未突破唐奇安入场位")

    adx = compute_adx(highs[:-1], lows[:-1], closes[:-1]) if len(closes) > 2 else 0.0
    adx_ready = len(closes) >= 32
    opening = abs(target) > 1e-9 and abs(prev_bar) < 1e-9
    chop_blocked = (
        opening
        and cfg.min_adx > 0
        and adx_ready
        and adx < cfg.min_adx
    )
    reasons.append(f"ADX={adx:.1f}" + (
        f"（<{cfg.min_adx:.0f} 震荡，新开仓不作为交易候选）" if chop_blocked else ""
    ))

    # 波动率目标化建议仓位（不改变信号节奏，只提示按波动缩放的大小）
    vol_scale = 1.0
    if cfg.vol_target > 0 and len(closes) > cfg.vol_lookback:
        rets = [
            closes[k] / closes[k - 1] - 1.0
            for k in range(len(closes) - cfg.vol_lookback, len(closes))
            if closes[k - 1] > 0
        ]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        span = (candles[-1].timestamp - candles[-2].timestamp).total_seconds() or 14400
        bpy = 365 * 24 * 3600 / span
        vol_annual = (var ** 0.5) * (bpy ** 0.5)
        if vol_annual > 0:
            vol_scale = max(0.15, min(1.0, cfg.vol_target / vol_annual))
    suggested = target * vol_scale
    if abs(target) > 1e-9:
        reasons.append(
            f"波动率目标化：建议仓位 {abs(suggested):.0%}"
            f"（{_position_label(target)} × {vol_scale:.2f}）"
        )

    return CycleSwitchSignal(
        market_regime=reg,
        calendar_phase=cal,
        target_position=target,
        prev_position=prev_bar,
        changed=abs(target - prev_bar) > 1e-9,
        price=last.close,
        reasons=reasons,
        donchian_entry=hh,
        donchian_exit=lx,
        z_score=z,
        days_since_halving=days_since,
        adx=adx,
        chop_blocked=chop_blocked,
        vol_scale=vol_scale,
        suggested_size=suggested,
    )
