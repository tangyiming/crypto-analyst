"""WolfyXBT 四年周期理论 + 狼波动能（刻舟求剑 / 狼波周期指数）。

图 1 — 日历模型（刻舟求剑）
  锚点：历次熊市底部（非减半日）；牛市固定 1064 天 → 预计见顶；
  熊市固定 364 天 → 预计见底 → 下一轮牛市开始。减半日常落在牛市中段。

图 2 — 狼波动能（本模块用 RSI + 短期动量近似，非 TradingView 原指标）
  红色区 RSI≥80：过热，牛市末端预警
  蓝色/紫色 RSI≤30：超卖，熊市末端抄底参考
  与日历信号叠加可提高可信度，单独使用易误判。

仅供周期位置提醒，不自动下单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from analyst.compute.strategies.cycle_switch import HALVING_DATES
from analyst.data.fetcher import Candle, CandleSeries

# Wolfy 图 1：历次熊市底部（= 下一轮牛市起点）
WOLFY_BEAR_BOTTOMS = [
    datetime(2015, 1, 14),
    datetime(2018, 12, 15),
    datetime(2022, 11, 21),
]

WOLFY_BULL_DAYS = 1064
WOLFY_BEAR_DAYS = 364

# 图 1 历史验证：2014 熊市 413 天，之后模型统一 364 天
WOLFY_ALERT_WINDOW_DAYS = 90   # 距里程碑 ≤90 天开始提醒
WOLFY_ALERT_URGENT_DAYS = 30   # ≤30 天升级为「临近」


@dataclass(frozen=True)
class WolfyMilestone:
    kind: str          # bull_top | bear_bottom | bull_start
    date: datetime
    label: str


@dataclass
class WolfyCalendarState:
    """日历相位：当前处于牛/熊第几天、下一个里程碑。"""

    phase: str                     # bull | bear
    phase_day: int                 # 本相位第几天（1-based）
    phase_total_days: int          # 本相位总天数
    days_to_milestone: int
    next_milestone: WolfyMilestone
    cycle_bull_start: datetime
    alerts: list[str] = field(default_factory=list)


@dataclass
class WolfyWaveState:
    """狼波动能近似（RSI 热度分区）。"""

    rsi: float
    heat: str          # extreme_hot | hot | neutral | cool | extreme_cold
    heat_label: str
    roc_20_pct: float  # 20 根收益率 %
    alerts: list[str] = field(default_factory=list)


@dataclass
class CycleOutlook:
    """日历 + 动能综合展望。"""

    as_of: datetime
    price: float
    calendar: WolfyCalendarState
    wave: WolfyWaveState | None
    summary: str
    alerts: list[str] = field(default_factory=list)


def _rsi_series(values: list[float], period: int = 14) -> list[float]:
    if len(values) < 2:
        return [50.0] * len(values)
    out: list[float] = [50.0] * len(values)
    for i in range(period, len(values)):
        gains, losses = 0.0, 0.0
        for j in range(i - period + 1, i + 1):
            chg = values[j] - values[j - 1]
            if chg >= 0:
                gains += chg
            else:
                losses -= chg
        avg_gain = gains / period
        avg_loss = losses / period
        if avg_loss <= 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def compute_wolfy_wave(closes: list[float]) -> WolfyWaveState:
    """狼波动能近似：RSI 分区 + 20 根动量。"""
    rsi_vals = _rsi_series(closes, 14)
    rsi = rsi_vals[-1] if rsi_vals else 50.0
    roc = 0.0
    if len(closes) > 20 and closes[-21] > 0:
        roc = (closes[-1] / closes[-21] - 1.0) * 100.0

    if rsi >= 80:
        heat, label = "extreme_hot", "过热（红）"
    elif rsi >= 65:
        heat, label = "hot", "偏热（橙）"
    elif rsi >= 40:
        heat, label = "neutral", "中性（黄绿）"
    elif rsi >= 30:
        heat, label = "cool", "偏冷（浅蓝）"
    else:
        heat, label = "extreme_cold", "超卖（深蓝）"

    alerts: list[str] = []
    if heat == "extreme_hot":
        alerts.append("狼波过热：动能极端偏高，警惕牛市末端 / 假突破")
    elif heat == "hot" and roc > 15:
        alerts.append("狼波偏热：短期涨速较快，不宜追高")
    elif heat == "extreme_cold":
        alerts.append("狼波超卖：动能极端偏低，熊市末端可关注抄底窗口")
    elif heat == "cool" and roc < -15:
        alerts.append("狼波偏冷：短期跌速较快，勿盲目追空")

    return WolfyWaveState(
        rsi=rsi,
        heat=heat,
        heat_label=label,
        roc_20_pct=roc,
        alerts=alerts,
    )


def format_milestone_countdown(cal: WolfyCalendarState) -> str:
    """转折点预估倒计时（周期提醒主文案）。"""
    ms = cal.next_milestone
    phase_zh = "牛市" if cal.phase == "bull" else "熊市"
    urgent = cal.days_to_milestone <= WOLFY_ALERT_URGENT_DAYS
    icon = "🔴" if urgent else "⏳"
    return (
        f"{icon} {phase_zh} · 距转折点「{ms.label}」预估还有 "
        f"{cal.days_to_milestone} 天（{ms.date:%Y-%m-%d}）"
    )


def calendar_countdown_dict(cal: WolfyCalendarState) -> dict:
    """前端 / 告警 payload 用的结构化倒计时。"""
    ms = cal.next_milestone
    return {
        "days": cal.days_to_milestone,
        "label": ms.label,
        "date": ms.date.isoformat(),
        "phase": cal.phase,
        "phase_day": cal.phase_day,
        "phase_total_days": cal.phase_total_days,
        "urgent": cal.days_to_milestone <= WOLFY_ALERT_URGENT_DAYS,
    }


def _calendar_alerts(phase: str, days_to: int, milestone_kind: str, milestone_label: str) -> list[str]:
    alerts: list[str] = []
    if days_to > WOLFY_ALERT_WINDOW_DAYS:
        return alerts

    urgent = days_to <= WOLFY_ALERT_URGENT_DAYS
    if phase == "bull" and milestone_kind == "bull_top":
        if urgent:
            alerts.append(
                f"🔴 转折点预警：距「{milestone_label}」约 {days_to} 天，临近顶部区间"
            )
        else:
            alerts.append(
                f"⚠️ 转折点关注：距「{milestone_label}」约 {days_to} 天，注意减仓/止盈"
            )
    elif phase == "bear" and milestone_kind == "bear_bottom":
        if urgent:
            alerts.append(
                f"🟢 转折点预警：距「{milestone_label}」约 {days_to} 天，临近抄底窗口"
            )
        else:
            alerts.append(
                f"📉 转折点关注：距「{milestone_label}」约 {days_to} 天，可逐步观察建仓"
            )
    elif phase == "bear" and milestone_kind == "bull_start":
        if urgent:
            alerts.append(f"🚀 转折点临近：距「{milestone_label}」约 {days_to} 天")
        else:
            alerts.append(
                f"📈 转折点关注：距「{milestone_label}」约 {days_to} 天，熊市尾声"
            )
    return alerts


def wolfy_calendar_phase(
    ts: datetime,
    *,
    bull_days: int = WOLFY_BULL_DAYS,
    bear_days: int = WOLFY_BEAR_DAYS,
) -> WolfyCalendarState:
    """从最近熊市底部向前推进，定位当前日历相位。"""
    anchor = max(b for b in WOLFY_BEAR_BOTTOMS if b <= ts)
    # 沿里程碑链前进，直到 ts 落在某个牛/熊段内
    while True:
        bull_top = anchor + timedelta(days=bull_days)
        bear_bottom = bull_top + timedelta(days=bear_days)
        if ts < bull_top:
            days_in = (ts - anchor).days + 1
            days_to = (bull_top - ts).days
            ms = WolfyMilestone("bull_top", bull_top, "预计牛市见顶")
            alerts = _calendar_alerts("bull", days_to, "bull_top", ms.label)
            if days_in <= 14:
                alerts.insert(0, "🐂 日历：处于牛市初期（熊市底后两周内）")
            return WolfyCalendarState(
                phase="bull",
                phase_day=days_in,
                phase_total_days=bull_days,
                days_to_milestone=days_to,
                next_milestone=ms,
                cycle_bull_start=anchor,
                alerts=alerts,
            )
        if ts < bear_bottom:
            days_in = (ts - bull_top).days + 1
            days_to = (bear_bottom - ts).days
            ms = WolfyMilestone("bear_bottom", bear_bottom, "预计熊市见底")
            alerts = _calendar_alerts("bear", days_to, "bear_bottom", ms.label)
            if days_in <= 14:
                alerts.insert(0, "🐻 日历：已进入预计熊市区间（见顶后两周内）")
            return WolfyCalendarState(
                phase="bear",
                phase_day=days_in,
                phase_total_days=bear_days,
                days_to_milestone=days_to,
                next_milestone=ms,
                cycle_bull_start=anchor,
                alerts=alerts,
            )
        # 越过本周期熊市底 → 下一周期牛市从 bear_bottom 开始
        anchor = bear_bottom


def build_wolfy_timeline(
    ts: datetime,
    *,
    past_cycles: int = 1,
    future_cycles: int = 1,
) -> dict:
    """生成牛熊分段 + 里程碑，供前端时间轴绘图。"""
    anchor = max(b for b in WOLFY_BEAR_BOTTOMS if b <= ts)
    for _ in range(past_cycles):
        anchor -= timedelta(days=WOLFY_BULL_DAYS + WOLFY_BEAR_DAYS)

    segments: list[dict] = []
    markers: list[dict] = []
    cur = anchor
    horizon = ts + timedelta(days=(WOLFY_BULL_DAYS + WOLFY_BEAR_DAYS) * future_cycles)
    cycle_idx = 0
    while cur < horizon:
        bull_top = cur + timedelta(days=WOLFY_BULL_DAYS)
        bear_bottom = bull_top + timedelta(days=WOLFY_BEAR_DAYS)
        segments.append({
            "phase": "bull",
            "start": cur.isoformat(),
            "end": bull_top.isoformat(),
            "label": "牛市",
            "days": WOLFY_BULL_DAYS,
        })
        segments.append({
            "phase": "bear",
            "start": bull_top.isoformat(),
            "end": bear_bottom.isoformat(),
            "label": "熊市",
            "days": WOLFY_BEAR_DAYS,
        })
        markers.extend([
            {"kind": "bull_start", "date": cur.isoformat(), "label": "牛起"},
            {"kind": "bull_top", "date": bull_top.isoformat(), "label": "预计牛顶"},
            {"kind": "bear_bottom", "date": bear_bottom.isoformat(), "label": "预计熊底"},
        ])
        for h in HALVING_DATES:
            if cur <= h < bear_bottom:
                markers.append({
                    "kind": "halving",
                    "date": h.isoformat(),
                    "label": "减半",
                })
        cur = bear_bottom
        cycle_idx += 1

    t0 = datetime.fromisoformat(segments[0]["start"])
    t1 = datetime.fromisoformat(segments[-1]["end"])
    span = max((t1 - t0).total_seconds(), 1.0)
    now_pct = min(100.0, max(0.0, (ts - t0).total_seconds() / span * 100.0))

    return {
        "range_start": t0.isoformat(),
        "range_end": t1.isoformat(),
        "now": ts.isoformat(),
        "now_pct": round(now_pct, 2),
        "segments": segments,
        "markers": markers,
    }


def outlook_to_api_dict(outlook: CycleOutlook, timeline: dict | None = None) -> dict:
    """序列化为 JSON 友好结构。"""
    cal = outlook.calendar
    wave = outlook.wave
    return {
        "as_of": outlook.as_of.isoformat(),
        "price": outlook.price,
        "summary": outlook.summary,
        "alerts": outlook.alerts,
        "countdown": calendar_countdown_dict(cal),
        "calendar": {
            "phase": cal.phase,
            "phase_day": cal.phase_day,
            "phase_total_days": cal.phase_total_days,
            "phase_pct": round(cal.phase_day / cal.phase_total_days * 100, 1),
            "days_to_milestone": cal.days_to_milestone,
            "next_milestone": {
                "kind": cal.next_milestone.kind,
                "date": cal.next_milestone.date.isoformat(),
                "label": cal.next_milestone.label,
            },
            "cycle_bull_start": cal.cycle_bull_start.isoformat(),
        },
        "wave": (
            {
                "rsi": round(wave.rsi, 1),
                "heat": wave.heat,
                "heat_label": wave.heat_label,
                "roc_20_pct": round(wave.roc_20_pct, 2),
            }
            if wave
            else None
        ),
        "timeline": timeline,
    }


def evaluate_cycle_outlook(
    series: CandleSeries,
    *,
    as_of: datetime | None = None,
) -> CycleOutlook:
    """综合日历 + 狼波动能，生成提醒列表。"""
    candles = series.candles
    ts = as_of or (candles[-1].timestamp if candles else datetime.utcnow())
    price = candles[-1].close if candles else 0.0
    cal = wolfy_calendar_phase(ts)

    wave = None
    if len(candles) >= 30:
        wave = compute_wolfy_wave([c.close for c in candles])

    alerts = [format_milestone_countdown(cal)]
    alerts.extend(cal.alerts)
    if wave:
        alerts.extend(wave.alerts)
        # 日历 × 动能交叉确认
        if cal.phase == "bull" and cal.days_to_milestone <= WOLFY_ALERT_WINDOW_DAYS:
            if wave.heat in ("extreme_hot", "hot"):
                alerts.append(
                    f"✅ 交叉确认：距「{cal.next_milestone.label}」还有 "
                    f"{cal.days_to_milestone} 天 + 狼波偏热，顶部风险上升"
                )
        if cal.phase == "bear" and cal.days_to_milestone <= WOLFY_ALERT_WINDOW_DAYS:
            if wave.heat in ("extreme_cold", "cool"):
                alerts.append(
                    f"✅ 交叉确认：距「{cal.next_milestone.label}」还有 "
                    f"{cal.days_to_milestone} 天 + 狼波超卖，底部概率上升"
                )

    zh_phase = "牛市" if cal.phase == "bull" else "熊市"
    pct = cal.phase_day / cal.phase_total_days * 100
    summary = (
        f"日历{zh_phase}第 {cal.phase_day}/{cal.phase_total_days} 天（{pct:.0f}%），"
        f"距{cal.next_milestone.label}还有 {cal.days_to_milestone} 天"
        f"（{cal.next_milestone.date:%Y-%m-%d}）"
    )
    if wave:
        summary += f"；狼波 RSI={wave.rsi:.0f}（{wave.heat_label}）"

    return CycleOutlook(
        as_of=ts,
        price=price,
        calendar=cal,
        wave=wave,
        summary=summary,
        alerts=alerts,
    )


def format_outlook_text(outlook: CycleOutlook, symbol: str = "BTC/USDT") -> str:
    """Telegram / 终端文案。"""
    cal = outlook.calendar
    lines = [
        f"🧭 周期展望 · {symbol}",
        format_milestone_countdown(cal),
        outlook.summary,
        f"现价 {outlook.price:.6g} · 截至 {outlook.as_of:%Y-%m-%d %H:%M} UTC",
        f"本周期牛市起点 {cal.cycle_bull_start:%Y-%m-%d}",
    ]
    if outlook.alerts:
        lines.append("—— 提醒 ——")
        lines.extend(outlook.alerts[:6])
    lines.append("刻舟求剑日历 + 狼波近似，仅供参考")
    return "\n".join(lines)
