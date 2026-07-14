"""实时规则告警（无 LLM）：指标 / 结构 / 量能 / 资金面。

在 K 线收盘评估与 premium 流上触发，供 MonitorHub 推页面 + Telegram。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from analyst.compute.fibonacci import compute_fib
from analyst.compute.indicators import compute_all
from analyst.compute.plan import generate_baseline_plan
from analyst.compute.structure import detect_structure
from analyst.compute.strategies.double_line_reversal import (
    DoubleLineConfig,
    detect_pattern,
    _atr,
)
from analyst.compute.volume import analyze_volume
from analyst.data.fetcher import CandleSeries


@dataclass
class RuleConfig:
    """默认全开；阈值可在 Settings 覆盖。"""

    enable_macd: bool = True
    enable_ema_stack: bool = True
    enable_boll: bool = True
    enable_volume: bool = True
    enable_structure_touch: bool = True
    enable_structure_flip: bool = True
    enable_fib_zone: bool = True
    enable_baseline: bool = True
    enable_break_level: bool = True
    enable_funding: bool = True
    enable_premium: bool = True

    volume_spike_ratio: float = 1.5
    structure_touch_atr_mult: float = 0.35
    funding_extreme_pct: float = 0.05  # |funding|*100 >= 该值（%/8h）
    premium_extreme_pct: float = 0.30  # |mark-index|%


@dataclass
class RuleEvent:
    rule: str
    title: str
    direction: str  # long / short / wait / info
    strength: float
    price: float
    reasons: list[str] = field(default_factory=list)
    break_level: float | None = None
    marker_time: int | None = None
    # 额外展示
    extras: dict[str, Any] = field(default_factory=dict)


def _bar_unix(series: CandleSeries) -> int:
    ts = series.candles[-1].timestamp
    if ts.tzinfo is None:
        return int(ts.replace(tzinfo=timezone.utc).timestamp())
    return int(ts.astimezone(timezone.utc).timestamp())


def _ema_stack(ema7: float, ema30: float, ema52: float) -> str:
    if ema7 > ema30 > ema52:
        return "bull"
    if ema7 < ema30 < ema52:
        return "bear"
    return "mixed"


def evaluate_closed_bar_rules(
    series: CandleSeries,
    state: dict[str, Any],
    cfg: RuleConfig | None = None,
) -> tuple[list[RuleEvent], dict[str, Any]]:
    """对刚收盘的 K 线评估一批规则；返回 (事件, 新状态)。"""
    cfg = cfg or RuleConfig()
    state = dict(state or {})
    events: list[RuleEvent] = []
    if len(series.candles) < 40:
        return events, state

    price = float(series.candles[-1].close)
    prev_close = float(series.candles[-2].close)
    high = float(series.candles[-1].high)
    low = float(series.candles[-1].low)
    t = _bar_unix(series)
    atr = _atr(series.candles, 14) or price * 0.01
    ind = compute_all(series)
    vol = analyze_volume(series)
    structure = detect_structure(series)
    fib = compute_fib(structure.recent_high, structure.recent_low)

    # ── MACD 交叉 ──
    if cfg.enable_macd and ind.macd.cross_signal:
        cross = ind.macd.cross_signal
        if state.get("macd_cross") != f"{t}:{cross}":
            long = cross == "golden"
            events.append(
                RuleEvent(
                    rule="macd_cross",
                    title="MACD 金叉" if long else "MACD 死叉",
                    direction="long" if long else "short",
                    strength=0.7,
                    price=price,
                    reasons=[
                        f"DIF/DEA 交叉={cross}",
                        f"histogram={ind.macd.histogram:.6g}",
                        f"零轴{'之上' if ind.macd.above_zero else '之下'}",
                    ],
                    marker_time=t,
                )
            )
            state["macd_cross"] = f"{t}:{cross}"

    # ── EMA 多空排列翻转 ──
    if cfg.enable_ema_stack:
        stack = _ema_stack(ind.ema.ema7, ind.ema.ema30, ind.ema.ema52)
        prev = state.get("ema_stack")
        if prev and stack in ("bull", "bear") and stack != prev:
            events.append(
                RuleEvent(
                    rule="ema_stack",
                    title="EMA 多头排列" if stack == "bull" else "EMA 空头排列",
                    direction="long" if stack == "bull" else "short",
                    strength=0.65,
                    price=price,
                    reasons=[
                        f"EMA7/30/52 = {ind.ema.ema7:.6g}/{ind.ema.ema30:.6g}/{ind.ema.ema52:.6g}",
                        f"由 {prev} → {stack}",
                    ],
                    marker_time=t,
                )
            )
        if stack != "mixed":
            state["ema_stack"] = stack

    # ── 布林带突破 ──
    if cfg.enable_boll and len(series.candles) >= 2:
        b = ind.boll
        prev = series.candles[-2]
        # 用前收相对中轨粗判「之前在带内」
        prev_inside = b.lower <= prev.close <= b.upper
        now_above = price > b.upper
        now_below = price < b.lower
        key = None
        if prev_inside and now_above:
            key = f"{t}:above"
            events.append(
                RuleEvent(
                    rule="boll_break",
                    title="收盘突破布林上轨",
                    direction="long",
                    strength=0.6,
                    price=price,
                    reasons=[
                        f"上轨 {b.upper:.6g} · 带宽 {b.width:.4g}",
                        f"量比 {vol.volume_ratio:.2f}×",
                    ],
                    break_level=b.upper,
                    marker_time=t,
                )
            )
        elif prev_inside and now_below:
            key = f"{t}:below"
            events.append(
                RuleEvent(
                    rule="boll_break",
                    title="收盘跌破布林下轨",
                    direction="short",
                    strength=0.6,
                    price=price,
                    reasons=[
                        f"下轨 {b.lower:.6g} · 带宽 {b.width:.4g}",
                        f"量比 {vol.volume_ratio:.2f}×",
                    ],
                    break_level=b.lower,
                    marker_time=t,
                )
            )
        if key:
            state["boll_break"] = key

    # ── 量能：放量 / 背离 ──
    if cfg.enable_volume and vol.recent_volume > 0 and vol.avg_volume_20 > 0:
        sig = vol.price_volume_signal or ""
        spike = vol.volume_ratio >= cfg.volume_spike_ratio
        diverge = ("背离" in sig) or ("恐慌" in sig)
        healthy_spike = spike and ("齐升" in sig or "抛售" in sig)
        if spike or diverge or healthy_spike:
            tag = f"{t}:{vol.volume_ratio:.1f}:{sig[:12]}"
            if state.get("volume_tag") != tag:
                direction = "long" if price >= prev_close else "short"
                if "背离" in sig or "出货" in sig:
                    direction = "short" if price >= prev_close else "long"
                events.append(
                    RuleEvent(
                        rule="volume",
                        title="放量异动" if spike else "量价信号",
                        direction=direction,
                        strength=min(0.9, 0.5 + vol.volume_ratio / 10),
                        price=price,
                        reasons=[
                            sig,
                            f"量比 {vol.volume_ratio:.2f}× · OBV {vol.obv_trend}",
                        ],
                        marker_time=t,
                    )
                )
                state["volume_tag"] = tag

    # ── 结构趋势翻转 ──
    if cfg.enable_structure_flip:
        trend = structure.trend
        prev_trend = state.get("structure_trend")
        if prev_trend and trend != prev_trend and trend in ("up", "down"):
            events.append(
                RuleEvent(
                    rule="structure_flip",
                    title="结构转多" if trend == "up" else "结构转空",
                    direction="long" if trend == "up" else "short",
                    strength=0.72,
                    price=price,
                    reasons=[
                        f"trend {prev_trend} → {trend}",
                        f"pivot={structure.key_pivot:.6g}",
                    ],
                    marker_time=t,
                )
            )
        state["structure_trend"] = trend

    # ── 关键位触及（支撑/阻力） ──
    if cfg.enable_structure_touch:
        touch_tol = atr * cfg.structure_touch_atr_mult
        touched = []
        for lvl in structure.supports[:3]:
            if abs(low - lvl) <= touch_tol or abs(price - lvl) <= touch_tol:
                touched.append(("support", lvl))
        for lvl in structure.resistances[:3]:
            if abs(high - lvl) <= touch_tol or abs(price - lvl) <= touch_tol:
                touched.append(("resistance", lvl))
        for kind, lvl in touched[:2]:
            key = f"{t}:{kind}:{round(lvl, 6)}"
            if key in (state.get("structure_touches") or []):
                continue
            events.append(
                RuleEvent(
                    rule="structure_touch",
                    title="触及支撑" if kind == "support" else "触及阻力",
                    direction="long" if kind == "support" else "short",
                    strength=0.68,
                    price=price,
                    reasons=[
                        f"{kind} @ {lvl:.6g}",
                        f"容差 ±{touch_tol:.6g}（{cfg.structure_touch_atr_mult}×ATR）",
                    ],
                    break_level=lvl,
                    marker_time=t,
                )
            )
            recent = list(state.get("structure_touches") or [])
            recent.append(key)
            state["structure_touches"] = recent[-20:]

    # ── Fib 0.5–0.618 回撤区 ──
    if cfg.enable_fib_zone and fib.range > 0:
        lo, hi = sorted((fib.retr_618, fib.retr_500))
        inside = lo <= price <= hi
        was = bool(state.get("in_fib_zone"))
        if inside and not was:
            # 上涨结构里进回撤区偏多接；下跌结构偏空
            direction = "long" if structure.trend != "down" else "short"
            events.append(
                RuleEvent(
                    rule="fib_zone",
                    title="进入 Fib 0.5–0.618 区",
                    direction=direction,
                    strength=0.66,
                    price=price,
                    reasons=[
                        f"区城 {lo:.6g}–{hi:.6g}",
                        f"结构 {structure.trend} · 0.786={fib.retr_786:.6g}",
                    ],
                    marker_time=t,
                )
            )
        state["in_fib_zone"] = inside

    # ── 规则基线计划变向 ──
    if cfg.enable_baseline:
        plan = generate_baseline_plan(price, fib, structure)
        prev_dir = state.get("baseline_dir")
        if prev_dir and plan.direction != prev_dir and plan.direction != "wait":
            events.append(
                RuleEvent(
                    rule="baseline_plan",
                    title=f"规则基线 → {plan.direction.upper()}",
                    direction=plan.direction,
                    strength=0.64,
                    price=price,
                    reasons=[
                        plan.rationale[:120],
                        f"RR={plan.rr_ratio:.2f} entry {plan.entry_low:.6g}-{plan.entry_high:.6g}",
                    ],
                    break_level=plan.entry_high if plan.direction == "long" else plan.entry_low,
                    marker_time=t,
                    extras={
                        "plan": {
                            "direction": plan.direction,
                            "entry_low": plan.entry_low,
                            "entry_high": plan.entry_high,
                            "stop_loss": plan.stop_loss,
                            "take_profit_1": plan.take_profit_1,
                            "take_profit_2": plan.take_profit_2,
                            "rr_ratio": plan.rr_ratio,
                            "rationale": plan.rationale,
                        }
                    },
                )
            )
        if plan.direction != "wait" or prev_dir is None:
            state["baseline_dir"] = plan.direction

    # ── 双线形态突破位触及（早于完整可交易信号） ──
    if cfg.enable_break_level:
        pattern = detect_pattern(series.candles, DoubleLineConfig())
        if pattern and pattern.break_level:
            bl = pattern.break_level
            crossed = False
            if pattern.direction == "long" and high >= bl and prev_close < bl:
                crossed = True
            if pattern.direction == "short" and low <= bl and prev_close > bl:
                crossed = True
            key = f"{t}:break:{round(bl, 6)}:{pattern.direction}"
            if crossed and state.get("break_touch") != key:
                events.append(
                    RuleEvent(
                        rule="break_level",
                        title="触及双线突破位",
                        direction=pattern.direction,
                        strength=0.75,
                        price=price,
                        reasons=[
                            f"突破位 {bl:.6g} · {pattern.direction}",
                            f"重合度 {pattern.overlap_ratio:.2f}",
                        ],
                        break_level=bl,
                        marker_time=t,
                    )
                )
                state["break_touch"] = key

    return events, state


def evaluate_premium_rules(
    premium: dict[str, Any],
    state: dict[str, Any],
    cfg: RuleConfig | None = None,
) -> tuple[list[RuleEvent], dict[str, Any]]:
    """资金费率 / 溢价极端（不一定每秒推，按状态去抖）。"""
    cfg = cfg or RuleConfig()
    state = dict(state or {})
    events: list[RuleEvent] = []
    price = float(premium.get("mark_price") or premium.get("index_price") or 0)
    now = int(datetime.now(timezone.utc).timestamp())
    bucket = now // 3600  # 同一极端每小时最多提醒一次

    if cfg.enable_funding and premium.get("funding_rate") is not None:
        fr = float(premium["funding_rate"])
        fr_pct = fr * 100.0
        if abs(fr_pct) >= cfg.funding_extreme_pct:
            side = "long" if fr_pct < 0 else "short"  # 极端正费率警惕多头拥挤
            key = f"funding:{bucket}:{1 if fr_pct > 0 else -1}"
            if state.get("funding_key") != key:
                events.append(
                    RuleEvent(
                        rule="funding_extreme",
                        title="资金费率极端",
                        direction=side,
                        strength=0.7,
                        price=price,
                        reasons=[
                            f"funding={fr_pct:+.4f}%/8h（阈值 ±{cfg.funding_extreme_pct}%）",
                            "正费率→多头拥挤偏防追多；负费率→空头拥挤偏防追空",
                        ],
                        marker_time=now,
                    )
                )
                state["funding_key"] = key

    if cfg.enable_premium and premium.get("premium_pct") is not None:
        pp = float(premium["premium_pct"])
        if abs(pp) >= cfg.premium_extreme_pct:
            side = "short" if pp > 0 else "long"  # 溢价过高易回归
            key = f"premium:{bucket}:{1 if pp > 0 else -1}"
            if state.get("premium_key") != key:
                events.append(
                    RuleEvent(
                        rule="premium_extreme",
                        title="期现溢价极端",
                        direction=side,
                        strength=0.65,
                        price=price,
                        reasons=[
                            f"溢价 {pp:+.4f}%（阈值 ±{cfg.premium_extreme_pct}%）",
                            f"mark={premium.get('mark_price')} index={premium.get('index_price')}",
                        ],
                        marker_time=now,
                    )
                )
                state["premium_key"] = key

    return events, state


def rule_event_to_alert(
    symbol: str,
    timeframe: str,
    event: RuleEvent,
) -> dict[str, Any]:
    plan = (event.extras or {}).get("plan")
    return {
        "type": "alert",
        "rule": event.rule,
        "title": event.title,
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": event.direction if event.direction in ("long", "short") else "long",
        "strength": event.strength,
        "price": event.price,
        "pattern": event.rule,
        "break_level": event.break_level,
        "reasons": list(event.reasons),
        "filters_passed": [event.rule],
        "marker_time": event.marker_time
        or int(datetime.now(timezone.utc).timestamp()),
        "plan": plan,
        "kelly": None,
        "trail_note": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "demo": False,
    }
