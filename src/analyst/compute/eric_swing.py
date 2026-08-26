"""Eric 风格「周线超卖波段」交易计划状态机（提醒开仓 / 第一半止盈 / 第二半止盈 / 止损）。

规则来自对 CycleStudies 标注的逆向 + 2015–2026 BTC/ETH 回测（见 memory: eric-band-filter-reverse）：

1. 观察（watch）：周线波段过滤器进入超卖状态（Stoch(HLC3,14,5)−50 ≤ −40，滞回 −25 退出）。
2. 开仓（entry）：仍在超卖状态且周线读数**拐头向上**（本周 > 上周），或本周退出超卖区。
   直接在首根超卖进场会早 1–3 周、45% 先被止损打掉，回测证伪。
   止损 = 本段超卖以来最低价 × (1 − 3%)，但不超过入场价 −20%；不要用固定 −12%（提前入场 + 固定止损是最差组合）。
3. 第一半止盈（tp1，卖 1/2，止损上移到成本）：以下任一
   - 日线最高 ≥ 入场 × 1.25
   - 日线收盘 ≥ 周线 EMA21 且已盈利 ≥ 8%
   - 日线波段过滤器进入超买状态 且已盈利 ≥ 5%（Eric：「触发日线超买先止盈一部分」）
4. 第二半止盈（tp2）：日线收盘从入场后最高价回撤 20%；或保本止损被打；或持有满 52 周。

回测（拐头进场 + 段低止损(≤20%) + 上述止盈，日线成交）：BTC 2015– 11 笔 复利 +405% 胜率 91%；
ETH 2018– 11 笔 +212% 胜率 91%；最差单笔 −20%。止损距离均值 ~20%，仓位按风险定：每笔风险 2% 权益 ≈ 10% 仓位。
**只对 BTC/ETH 成立**：BNB 2018– 复利 +5% 胜率 50%，SOL 2020– 复利 −48% 胜率 29%（山寨周线超卖后常再跌 30–50%），
默认只对白名单品种运行（MONITOR_ERIC_SWING_SYMBOLS）。

用法：每根日线/周线收盘后调用 `advance(state, weekly, daily)`，传入**已收盘**的周线与日线序列，
返回 (事件列表, 新状态)。状态是纯 dict，可 JSON 持久化；用 last_weekly_ts / last_daily_ts 去重。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone
from typing import Any

from analyst.compute.band_filter import band_filter_series, compute_band_filter, hysteresis_states
from analyst.compute.indicators import ema
from analyst.data.fetcher import CandleSeries

STOP_PAD = 0.03          # 止损：段最低价下方 3%
MAX_STOP_DIST = 0.20     # 止损距离上限：拐头时若已反弹很多，段低太远，改为入场 −20%（BTC/ETH 回测更好）
TP1_GAIN = 0.25          # 第一半止盈目标
TP1_EMA21_MIN_GAIN = 0.08
TP1_DAILY_OB_MIN_GAIN = 0.05
TP2_TRAIL = 0.20         # 余仓从最高价回撤 20% 离场
MAX_HOLD_WEEKS = 52
DEFAULT_RISK_PCT = 2.0   # 每笔风险占权益百分比（仓位建议用）
NEAR_PCT = 0.03          # 距止损/跟踪线 3% 以内 → 提前预警
BUFF_HALF_MIN = 5        # 周线超卖首根起，Buff（结构位/背离/EMA/月线共振）≥5 即先进半仓，拐头再加满
# 2026-08-27 回测证伪：开启后 BTC 复利 +160%→+127%（胜率 85%→73%）、ETH +494%→+268%（100%→71%），
# 提前进的半仓多数在拐头前被止损。保留代码路径，默认关闭；除非有新证据不要打开。
ENABLE_BUFF_HALF = False


def weekly_buff(weekly: CandleSeries, monthly: CandleSeries | None, *, stop: float | None = None) -> tuple[int, tuple[str, ...]]:
    """周线做多 Buff 评分（复用 eric_signals.score_buff）：超卖 + 前低/支撑 + 底背离 + EMA 回踩 + 月线共振 + 盈亏比。"""
    try:
        from analyst.compute.eric_signals import (
            _ema_touch,
            _near_level,
            detect_macd_divergence,
            detect_rsi_divergence,
            ema_ladder,
            rsi_series,
            score_buff,
        )
        from analyst.compute.indicators import compute_macd
        from analyst.compute.structure import detect_structure

        c = weekly.candles
        closes = [float(x.close) for x in c]
        price = closes[-1]
        trs = [max(c[i].high - c[i].low, abs(c[i].high - c[i - 1].close), abs(c[i].low - c[i - 1].close)) for i in range(1, len(c))]
        atr = sum(trs[-14:]) / max(1, len(trs[-14:]))
        structure = detect_structure(weekly)
        support = structure.supports[0] if getattr(structure, "supports", None) else None
        bf = compute_band_filter(weekly)
        htf_bf = compute_band_filter(monthly) if monthly is not None and len(monthly.candles) >= 25 else None
        rsi = rsi_series(closes, 14)
        macd = compute_macd(weekly)
        div = detect_rsi_divergence(closes, rsi) == "bull" or detect_macd_divergence(closes, macd.series_dif or []) == "bull"
        ladder = ema_ladder(closes)
        ema_sup = _ema_touch(price, float(c[-1].high), float(c[-1].low), ladder, atr, side="support")
        near = _near_level(price, float(support) if support is not None else None, atr, 0.85)
        tgt = None
        if ladder:
            ups = sorted(x for x in (ladder.ema21, ladder.ema50, ladder.ema55, ladder.ema200) if x > price)
            tgt = ups[0] if ups else None
        rr = None
        if tgt is not None and stop is not None and price > stop:
            rr = (tgt - price) / (price - stop)
        b = score_buff(
            side="long", bf=bf, near_structure=near, div=div, ema_lvl=ema_sup,
            htf_bf=htf_bf, htf_bias="mixed", rr=rr,
        )
        return int(b.score), tuple(b.tags)
    except Exception:
        return 0, ()


@dataclass
class SwingEvent:
    kind: str            # watch / entry / tp1 / tp2 / stop
    title: str
    direction: str       # long / short(卖出动作) / info
    price: float
    reasons: list[str] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)
    marker_time: int | None = None


def _ts(c) -> int:
    t = c.timestamp
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return int(t.timestamp())


def _fmt(x: float | None) -> str:
    return "—" if x is None else f"{x:.6g}"


def band_readings(series_by_tf: dict[str, CandleSeries | None]) -> list[str]:
    """日/周/月 波段过滤器读数一行一条，供告警文案/面板。"""
    out: list[str] = []
    name = {"1d": "日线", "1w": "周线", "1M": "月线"}
    for tf in ("1d", "1w", "1M"):
        s = series_by_tf.get(tf)
        if not s or len(s.candles) < 25:
            continue
        bf = compute_band_filter(s)
        if not bf:
            continue
        st = {"oversold": "超卖", "overbought": "超买", "neutral": "中性"}[bf.state]
        extra = ""
        if tf == "1w" and bf.ob_gate:
            extra = f" · Z26={bf.channel_z:.2f}" + ("（加速区）" if bf.state == "overbought" else "")
        if bf.turned_up:
            extra += " · 拐头↑"
        if bf.turned_down:
            extra += " · 拐头↓"
        if bf.streak >= 2:
            extra += f" · 第{bf.streak}次"
        out.append(f"{name.get(tf, tf)} BF {bf.value:+.1f}（{st}）{extra}")
    return out


def _position_hint(entry: float, stop: float, risk_pct: float) -> str:
    risk = entry / stop - 1.0 if stop > 0 else 0.0
    if risk <= 0:
        return ""
    size = risk_pct / 100.0 / risk * 100.0
    return f"止损距离 {risk*100:.1f}% → 每笔风险 {risk_pct:g}% 权益 ≈ 仓位 {size:.0f}% 权益"


def advance(
    state: dict[str, Any] | None,
    weekly: CandleSeries,
    daily: CandleSeries | None,
    *,
    monthly: CandleSeries | None = None,
    risk_pct: float = DEFAULT_RISK_PCT,
) -> tuple[list[SwingEvent], dict[str, Any]]:
    """推进状态机。weekly/daily 必须只含已收盘 K。"""
    st: dict[str, Any] = dict(state or {})
    st.setdefault("phase", "flat")
    events: list[SwingEvent] = []
    wc = weekly.candles
    if len(wc) < 60:
        return events, st

    highs = [float(c.high) for c in wc]
    lows = [float(c.low) for c in wc]
    closes = [float(c.close) for c in wc]
    X = band_filter_series(highs, lows, closes)
    states = hysteresis_states(X, os_level=-40, os_exit=-25, ob_level=40, ob_exit=25)
    e21 = ema(closes, 21)
    w_ts = _ts(wc[-1])
    readings = band_readings({"1d": daily, "1w": weekly, "1M": monthly})

    # ── 周线收盘：观察 / 开仓 ──
    if st.get("last_weekly_ts") != w_ts:
        st["last_weekly_ts"] = w_ts
        cur_os = states[-1] == -1
        if st["phase"] == "flat" and cur_os:
            st["phase"] = "armed"
            st["signal_ts"] = w_ts
            st["episode_low"] = lows[-1]
            # 段起点：往前找本段首根
            k = len(states) - 1
            while k > 0 and states[k - 1] == -1:
                k -= 1
            st["episode_low"] = min(lows[k:])
            events.append(
                SwingEvent(
                    kind="watch",
                    title="Eric 周线超卖 · 进入观察",
                    direction="info",
                    price=closes[-1],
                    reasons=[
                        f"周线波段过滤器 {X[-1]:+.1f} ≤ −40，进入超卖状态",
                        "不在首根进场（回测：早 1–3 周、45% 先被止损）",
                        f"等周线读数拐头向上再开仓；预备止损 ≈ 段最低 {_fmt(st['episode_low'])} × 0.97",
                        *readings,
                    ],
                    plan={"stage": "watch", "episode_low": st["episode_low"]},
                    marker_time=w_ts,
                )
            )
        elif st["phase"] == "armed" or (
            st["phase"] == "long" and float(st.get("size") or 1.0) < 1.0 and not st.get("tp1_done")
        ):
            half_in = st["phase"] == "long"
            st["episode_low"] = min(float(st.get("episode_low") or lows[-1]), lows[-1])
            turned = cur_os and X[-1] > X[-2]
            exited = not cur_os and states[-2] == -1
            if turned or exited:
                px = closes[-1]
                if half_in:
                    # 加满另一半：均价重算，止损沿用（不放宽），目标按均价
                    entry = (float(st["entry"]) + px) / 2.0
                    stop = float(st["stop"])
                    st.update(entry=entry, size=1.0, tp1_level=entry * (1 + TP1_GAIN), peak=max(float(st.get("peak") or px), px))
                    title = "Eric 周线超卖 · 拐头确认，加满另一半"
                    first = f"半仓成本 {_fmt(st.get('entry1'))} + 本周 {_fmt(px)} → 均价 {_fmt(entry)}"
                else:
                    entry = px
                    stop = min(float(st["episode_low"]) * (1 - STOP_PAD), entry * 0.995)
                    stop = max(stop, entry * (1 - MAX_STOP_DIST))
                    st.update(
                        phase="long", entry_ts=w_ts, entry=entry, entry1=entry, size=1.0, stop=stop, init_stop=stop,
                        tp1_level=entry * (1 + TP1_GAIN), peak=entry, tp1_done=False,
                    )
                    title = "Eric 周线超卖 · 拐头确认，建议开多"
                    first = f"止损 {_fmt(stop)}（段最低 {_fmt(st['episode_low'])} − 3%，上限入场 −20%）"
                events.append(
                    SwingEvent(
                        kind="entry",
                        title=title,
                        direction="long",
                        price=px,
                        reasons=[
                            (f"周线读数拐头 {X[-2]:+.1f} → {X[-1]:+.1f}（仍在超卖区）" if turned else f"周线读数退出超卖区（{X[-2]:+.1f} → {X[-1]:+.1f}）"),
                            first,
                            f"第一半止盈：{_fmt(st['tp1_level'])}（均价 +25%）/ 周线 EMA21 且 ≥+8% / 日线超买 且 ≥+5%",
                            "第二半：止损上移成本后，从最高价回撤 20% 离场；最长持有 52 周",
                            _position_hint(entry, stop, risk_pct),
                            "定性：做反弹，不赌反转（Eric）",
                            *readings,
                        ],
                        plan={
                            "stage": "entry", "entry": entry, "stop": stop, "tp1": st["tp1_level"],
                            "trail_pct": TP2_TRAIL, "risk_pct": risk_pct, "size": 1.0,
                        },
                        marker_time=w_ts,
                    )
                )
            elif ENABLE_BUFF_HALF and not half_in and cur_os:
                # 未拐头，但 Buff 叠够（前低/支撑 + 背离 + EMA + 月线共振）→ 先进半仓（Eric 的组合进场）
                pre_stop = max(float(st["episode_low"]) * (1 - STOP_PAD), closes[-1] * (1 - MAX_STOP_DIST))
                buff, tags = weekly_buff(weekly, monthly, stop=pre_stop)
                if buff >= BUFF_HALF_MIN:
                    entry = closes[-1]
                    stop = min(pre_stop, entry * 0.995)
                    st.update(
                        phase="long", entry_ts=w_ts, entry=entry, entry1=entry, size=0.5, stop=stop, init_stop=stop,
                        tp1_level=entry * (1 + TP1_GAIN), peak=entry, tp1_done=False, buff=buff,
                    )
                    events.append(
                        SwingEvent(
                            kind="entry_half",
                            title="Eric 周线超卖 · Buff 达标，先进半仓",
                            direction="long",
                            price=entry,
                            reasons=[
                                f"Buff={buff}≥{BUFF_HALF_MIN}：{' + '.join(tags) or '—'}",
                                f"周线读数 {X[-1]:+.1f} 仍在超卖区、未拐头 → 半仓；拐头后加满另一半",
                                f"止损 {_fmt(stop)}（段最低 −3%，上限 −20%）",
                                _position_hint(entry, stop, risk_pct) + "（本次先用一半）",
                                *readings,
                            ],
                            plan={"stage": "entry_half", "entry": entry, "stop": stop, "tp1": st["tp1_level"], "size": 0.5, "buff": buff},
                            marker_time=w_ts,
                        )
                    )
            elif not half_in and not cur_os:
                st["phase"] = "flat"
        elif st["phase"] in ("long", "runner") and cur_os and states[-2] != -1:
            # 持仓中再次进入周线超卖：只提示，不加仓（由止损处理）
            events.append(
                SwingEvent(
                    kind="watch",
                    title="Eric 周线再次超卖（持仓中）",
                    direction="info",
                    price=closes[-1],
                    reasons=[f"周线读数 {X[-1]:+.1f}，当前止损 {_fmt(st.get('stop'))}", *readings],
                    plan={"stage": st["phase"]},
                    marker_time=w_ts,
                )
            )

    # ── 日线收盘：止损 / 第一半 / 第二半 ──
    if daily is None or len(daily.candles) < 30 or st["phase"] not in ("long", "runner"):
        return events, st
    dc = daily.candles
    d_ts = _ts(dc[-1])
    if st.get("last_daily_ts") == d_ts or d_ts <= int(st.get("entry_ts") or 0):
        return events, st
    st["last_daily_ts"] = d_ts
    d_hi, d_lo, d_close = float(dc[-1].high), float(dc[-1].low), float(dc[-1].close)
    entry = float(st["entry"])
    stop = float(st["stop"])
    weeks_held = max(0, (d_ts - int(st["entry_ts"])) // (7 * 86400))

    if d_lo <= stop:
        kind = "stop"
        title = "Eric 波段 · 止损离场" if not st.get("tp1_done") else "Eric 波段 · 余仓保本离场"
        pnl = (stop / entry - 1) * 100
        events.append(
            SwingEvent(
                kind=kind,
                title=title,
                direction="short",
                price=stop,
                reasons=[f"日线最低 {_fmt(d_lo)} ≤ 止损 {_fmt(stop)}", f"本笔 {'余仓' if st.get('tp1_done') else '全仓'} {pnl:+.1f}%", *readings],
                plan={"stage": "closed", "reason": "stop", "pnl_pct": pnl},
                marker_time=d_ts,
            )
        )
        st = {"phase": "flat", "last_weekly_ts": st.get("last_weekly_ts"), "last_daily_ts": d_ts}
        return events, st

    st["peak"] = max(float(st.get("peak") or entry), d_hi)
    if not st.get("tp1_done"):
        reasons_hit: list[str] = []
        if d_hi >= float(st["tp1_level"]):
            reasons_hit.append(f"日线最高 {_fmt(d_hi)} ≥ 目标 {_fmt(st['tp1_level'])}（+25%）")
        if d_close >= e21[-1] and d_close >= entry * (1 + TP1_EMA21_MIN_GAIN):
            reasons_hit.append(f"日线收盘 {_fmt(d_close)} ≥ 周线 EMA21 {_fmt(e21[-1])} 且盈利 ≥8%")
        dbf = compute_band_filter(daily)
        if dbf and dbf.entered_overbought and d_close >= entry * (1 + TP1_DAILY_OB_MIN_GAIN):
            reasons_hit.append(f"日线波段过滤器进入超买 {dbf.value:+.1f} 且盈利 ≥5%")
        if reasons_hit:
            px = float(st["tp1_level"]) if d_hi >= float(st["tp1_level"]) else d_close
            st["tp1_done"] = True
            st["tp1_price"] = px
            st["stop"] = max(stop, entry)
            st["phase"] = "runner"
            events.append(
                SwingEvent(
                    kind="tp1",
                    title="Eric 波段 · 建议卖出一半（第一止盈）",
                    direction="short",
                    price=px,
                    reasons=[
                        *reasons_hit,
                        f"卖出 1/2，盈利 {(px/entry-1)*100:+.1f}%；止损上移到成本 {_fmt(entry)}",
                        f"余仓：从最高价回撤 {int(TP2_TRAIL*100)}% 离场（当前最高 {_fmt(st['peak'])}）",
                        *readings,
                    ],
                    plan={"stage": "runner", "tp1_price": px, "stop": st["stop"], "trail_from": st["peak"]},
                    marker_time=d_ts,
                )
            )
        elif weeks_held >= MAX_HOLD_WEEKS:
            events.append(_timeout(st, d_close, d_ts, readings))
            st = {"phase": "flat", "last_weekly_ts": st.get("last_weekly_ts"), "last_daily_ts": d_ts}
        if st.get("phase") in ("long", "runner"):
            events.append(_status_event(st, d_close, d_ts, readings))
        return events, st

    # runner
    trail_level = float(st["peak"]) * (1 - TP2_TRAIL)
    if d_close <= trail_level:
        pnl = (d_close / entry - 1) * 100
        events.append(
            SwingEvent(
                kind="tp2",
                title="Eric 波段 · 余仓离场（第二止盈）",
                direction="short",
                price=d_close,
                reasons=[
                    f"日线收盘 {_fmt(d_close)} 从最高 {_fmt(st['peak'])} 回撤 ≥{int(TP2_TRAIL*100)}%",
                    f"余仓 {pnl:+.1f}%；第一半 {(float(st['tp1_price'])/entry-1)*100:+.1f}%",
                    *readings,
                ],
                plan={"stage": "closed", "reason": "trail", "pnl_pct": pnl},
                marker_time=d_ts,
            )
        )
        st = {"phase": "flat", "last_weekly_ts": st.get("last_weekly_ts"), "last_daily_ts": d_ts}
    elif weeks_held >= MAX_HOLD_WEEKS:
        events.append(_timeout(st, d_close, d_ts, readings))
        st = {"phase": "flat", "last_weekly_ts": st.get("last_weekly_ts"), "last_daily_ts": d_ts}
    else:
        st["trail_level"] = trail_level
        events.append(_status_event(st, d_close, d_ts, readings))
    return events, st


def _status_event(st: dict[str, Any], price: float, ts: int, readings: list[str]) -> SwingEvent:
    """每日持仓心跳；距止损/跟踪线 ≤3% 时升级为 near 预警。"""
    entry = float(st["entry"])
    stop = float(st["stop"])
    pnl = (price / entry - 1) * 100
    lines = [f"浮盈 {pnl:+.1f}%（均价 {_fmt(entry)}，仓位 {float(st.get('size') or 1.0):.0%}）"]
    dist = price / stop - 1
    lines.append(f"距{'保本' if st.get('tp1_done') else ''}止损 {_fmt(stop)}：{dist*100:.1f}%")
    near = dist <= NEAR_PCT
    if st.get("tp1_done"):
        trail = float(st.get("trail_level") or float(st["peak"]) * (1 - TP2_TRAIL))
        d2 = price / trail - 1
        lines.append(f"距跟踪离场线 {_fmt(trail)}：{d2*100:.1f}%（最高 {_fmt(st['peak'])}）")
        near = near or d2 <= NEAR_PCT
    else:
        lines.append(f"距第一止盈 {_fmt(st['tp1_level'])}：{(float(st['tp1_level'])/price-1)*100:.1f}%")
    kind = "near" if near else "status"
    title = "Eric 波段 · ⚠️ 临近止损/离场线" if near else "Eric 波段 · 持仓状态"
    return SwingEvent(
        kind=kind, title=title, direction="info", price=price,
        reasons=[*lines, *readings],
        plan={"stage": st.get("phase"), "pnl_pct": pnl, "stop": stop, "trail_level": st.get("trail_level")},
        marker_time=ts,
    )


def check_mark_stop(state: dict[str, Any] | None, mark: float, now_ts: int) -> tuple[list[SwingEvent], dict[str, Any]]:
    """实时标记价止损：持仓中且 mark ≤ 止损 → 立即出 stop 事件（不等日线收盘）。"""
    st: dict[str, Any] = dict(state or {})
    if st.get("phase") not in ("long", "runner") or mark is None:
        return [], st
    stop = float(st["stop"])
    if float(mark) > stop:
        return [], st
    entry = float(st["entry"])
    pnl = (stop / entry - 1) * 100
    ev = SwingEvent(
        kind="stop",
        title="Eric 波段 · 止损离场（实时标记价）" if not st.get("tp1_done") else "Eric 波段 · 余仓保本离场（实时标记价）",
        direction="short",
        price=float(mark),
        reasons=[f"标记价 {_fmt(float(mark))} ≤ 止损 {_fmt(stop)}", f"本笔 {'余仓' if st.get('tp1_done') else '全仓'} {pnl:+.1f}%（按止损价）", "实时触发，不等日线收盘"],
        plan={"stage": "closed", "reason": "stop_mark", "pnl_pct": pnl},
        marker_time=now_ts,
    )
    return [ev], {"phase": "flat", "last_weekly_ts": st.get("last_weekly_ts"), "last_daily_ts": st.get("last_daily_ts"), "closed_ts": now_ts}


def _timeout(st: dict[str, Any], price: float, ts: int, readings: list[str]) -> SwingEvent:
    entry = float(st["entry"])
    pnl = (price / entry - 1) * 100
    return SwingEvent(
        kind="tp2",
        title="Eric 波段 · 持有满 52 周，建议离场",
        direction="short",
        price=price,
        reasons=[f"超过最长持有期；当前 {pnl:+.1f}%", *readings],
        plan={"stage": "closed", "reason": "timeout", "pnl_pct": pnl},
        marker_time=ts,
    )


def replay(
    weekly: CandleSeries,
    daily: CandleSeries | None,
    *,
    lookback_weeks: int = 160,
    risk_pct: float = DEFAULT_RISK_PCT,
) -> dict[str, Any]:
    """用历史 K 线回放状态机，得到「当前应处阶段」（冷启动/状态文件丢失时用；不产生事件）。

    第 i 根周线收盘后的那一周里，日线逐根用 weekly=wc[:i+1] 推进。
    """
    from datetime import timedelta

    wc = weekly.candles
    if len(wc) < 61:
        return {"phase": "flat"}
    start = max(60, len(wc) - lookback_weeks)
    dc = daily.candles if daily else []
    st: dict[str, Any] = {}
    for i in range(start, len(wc)):
        wk_i = CandleSeries(weekly.symbol, weekly.timeframe, list(wc[: i + 1]))
        _, st = advance(st, wk_i, None, risk_pct=risk_pct)
        if not dc:
            continue
        lo = wc[i].timestamp + timedelta(days=7)
        hi = lo + timedelta(days=7)
        for j, c in enumerate(dc):
            if c.timestamp < lo:
                continue
            if c.timestamp >= hi:
                break
            _, st = advance(
                st, wk_i, CandleSeries(daily.symbol, daily.timeframe, list(dc[: j + 1])), risk_pct=risk_pct
            )
    return st
