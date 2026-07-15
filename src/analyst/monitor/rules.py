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
    """默认全开；阈值可在 Settings 覆盖。

    降噪参数依据回测校准（BTC 15m 1000 根）：
    volume 46% 命中 / structure_touch 50% 且样本占八成 → 提高门槛、要求确认。
    """

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

    volume_spike_ratio: float = 2.0        # 放量阈值（曾 1.5×，噪音过多）
    volume_min_body_atr: float = 0.3       # 放量还需实体 ≥ 0.3×ATR 才算有方向
    structure_touch_atr_mult: float = 0.35
    touch_require_hold: bool = True        # 触及后收盘需守住（支撑上方/阻力下方）
    touch_cooldown_bars: int = 12          # 同一价位冷却根数，防重复刷屏
    boll_min_vol_ratio: float = 1.2        # 布林突破需量比确认
    boll_atr_margin: float = 0.1           # 收盘需越过轨道 0.1×ATR，滤刺破
    macd_require_context: bool = True      # 金叉需零轴上或 EMA 短多头（死叉反之）
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

    # ── MACD 交叉（需趋势语境：金叉在零轴上或短均线多头，死叉反之） ──
    if cfg.enable_macd and ind.macd.cross_signal:
        cross = ind.macd.cross_signal
        long = cross == "golden"
        context_ok = True
        if cfg.macd_require_context:
            if long:
                context_ok = ind.macd.above_zero or ind.ema.ema7 > ind.ema.ema30
            else:
                context_ok = (not ind.macd.above_zero) or ind.ema.ema7 < ind.ema.ema30
        if context_ok and state.get("macd_cross") != f"{t}:{cross}":
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

    # ── 布林带突破（需量比确认 + 越轨 0.1×ATR，滤影线刺破） ──
    if cfg.enable_boll and len(series.candles) >= 2:
        b = ind.boll
        prev = series.candles[-2]
        margin = atr * cfg.boll_atr_margin
        vol_ok = vol.volume_ratio >= cfg.boll_min_vol_ratio
        # 用前收相对中轨粗判「之前在带内」
        prev_inside = b.lower <= prev.close <= b.upper
        now_above = price > b.upper + margin
        now_below = price < b.lower - margin
        key = None
        if prev_inside and now_above and vol_ok:
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
                        f"量比 {vol.volume_ratio:.2f}×（≥{cfg.boll_min_vol_ratio}）",
                    ],
                    break_level=b.upper,
                    marker_time=t,
                )
            )
        elif prev_inside and now_below and vol_ok:
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
                        f"量比 {vol.volume_ratio:.2f}×（≥{cfg.boll_min_vol_ratio}）",
                    ],
                    break_level=b.lower,
                    marker_time=t,
                )
            )
        if key:
            state["boll_break"] = key

    # ── 量能：放量 + 实体确认（回测 46% 命中 → 只报「放量且有真实方向」） ──
    if cfg.enable_volume and vol.recent_volume > 0 and vol.avg_volume_20 > 0:
        sig = vol.price_volume_signal or ""
        last = series.candles[-1]
        body = abs(last.close - last.open)
        spike = vol.volume_ratio >= cfg.volume_spike_ratio
        big_body = body >= atr * cfg.volume_min_body_atr
        diverge = ("背离" in sig) or ("恐慌" in sig)
        # 必须放量；纯背离但无量不再报（回测确认为噪音）
        if spike and (big_body or diverge):
            tag = f"{t}:{vol.volume_ratio:.1f}:{sig[:12]}"
            if state.get("volume_tag") != tag:
                # 方向看本根实体，而非相邻收盘差（震荡里后者频繁翻面）
                direction = "long" if last.close >= last.open else "short"
                if "背离" in sig or "出货" in sig:
                    direction = "short" if last.close >= last.open else "long"
                events.append(
                    RuleEvent(
                        rule="volume",
                        title="放量异动",
                        direction=direction,
                        strength=min(0.9, 0.5 + vol.volume_ratio / 10),
                        price=price,
                        reasons=[
                            sig,
                            f"量比 {vol.volume_ratio:.2f}× · 实体 {body / atr:.2f}×ATR"
                            f" · OBV {vol.obv_trend}",
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

    # ── 关键位触及（需收盘守住 + 同价位冷却，回测 50% 且刷屏 → 降噪） ──
    if cfg.enable_structure_touch:
        bar_no = int(state.get("bar_no", 0)) + 1
        state["bar_no"] = bar_no
        cooldown: dict[str, int] = dict(state.get("touch_cooldown") or {})
        touch_tol = atr * cfg.structure_touch_atr_mult
        touched = []
        for lvl in structure.supports[:3]:
            hit = abs(low - lvl) <= touch_tol or abs(price - lvl) <= touch_tol
            # 守住 = 收盘回到支撑上方（触及后被买起来，才有做多参考价值）
            held = (not cfg.touch_require_hold) or price >= lvl
            if hit and held:
                touched.append(("support", lvl))
        for lvl in structure.resistances[:3]:
            hit = abs(high - lvl) <= touch_tol or abs(price - lvl) <= touch_tol
            held = (not cfg.touch_require_hold) or price <= lvl
            if hit and held:
                touched.append(("resistance", lvl))
        for kind, lvl in touched[:2]:
            lvl_key = f"{kind}:{round(lvl, 6)}"
            last_bar = cooldown.get(lvl_key)
            if last_bar is not None and bar_no - last_bar < cfg.touch_cooldown_bars:
                continue
            cooldown[lvl_key] = bar_no
            events.append(
                RuleEvent(
                    rule="structure_touch",
                    title="触及支撑" if kind == "support" else "触及阻力",
                    direction="long" if kind == "support" else "short",
                    strength=0.68,
                    price=price,
                    reasons=[
                        f"{kind} @ {lvl:.6g} · 收盘守住",
                        f"容差 ±{touch_tol:.6g}（{cfg.structure_touch_atr_mult}×ATR）"
                        f" · 冷却 {cfg.touch_cooldown_bars} 根",
                    ],
                    break_level=lvl,
                    marker_time=t,
                )
            )
        # 只保留最近的冷却记录，防状态膨胀
        if len(cooldown) > 30:
            cooldown = dict(
                sorted(cooldown.items(), key=lambda kv: kv[1])[-20:]
            )
        state["touch_cooldown"] = cooldown

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
        # 告警用宽松突变阈值（1.2）：作为提醒回测命中率 62–77%，样本宝贵；
        # 策略入场则用更严的默认 2.0（见 DoubleLineConfig）。
        pattern = detect_pattern(
            series.candles, DoubleLineConfig(min_sudden_atr_mult=1.2)
        )
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
