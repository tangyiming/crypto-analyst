"""CycleStudies（百萬Eric）技术方案的可计算近似。

主信号：波段过滤器近似 Stoch(HLC3,14,5)−50（有界 ±50、阈值 ±40，见 band_filter.py）；并内化三套可数字化技巧：
1) Buff 评分卡（位置+过滤器+背离+EMA+HTF）
2) EMA21/55 回踩做多 / 拒绝做空或止盈
3) 多周期共振（本周期 + 更高周期过滤器同向极值）

数据源与 Jack 隔离：.cache/cyclestudies_tweets/
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from analyst.compute.band_filter import BandFilterSnapshot, compute_band_filter
from analyst.compute.indicators import compute_macd, ema
from analyst.data.fetcher import CandleSeries


def rsi_series(closes: list[float], period: int = 14) -> list[float]:
    n = len(closes)
    out = [50.0] * n
    if n < period + 1:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains[i] = max(d, 0.0)
        losses[i] = max(-d, 0.0)
    avg_g = sum(gains[1 : period + 1]) / period
    avg_l = sum(losses[1 : period + 1]) / period
    if avg_l == 0:
        out[period] = 100.0
    else:
        out[period] = 100.0 - (100.0 / (1.0 + avg_g / avg_l))
    for i in range(period + 1, n):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        if avg_l == 0:
            out[i] = 100.0
        else:
            out[i] = 100.0 - (100.0 / (1.0 + avg_g / avg_l))
    return out


@dataclass(frozen=True)
class EricEmaLadder:
    ema21: float
    ema50: float
    ema55: float
    ema200: float


@dataclass(frozen=True)
class BuffScore:
    """叠 Buff：分数越高越接近他「值得做」的组合。"""

    score: int
    tags: tuple[str, ...] = ()
    side: Literal["long", "short", "flat"] = "flat"


@dataclass(frozen=True)
class EricSignal:
    kind: Literal[
        "oversold",
        "overbought",
        "bull_div",
        "bear_div",
        "rebound_long",
        "fade_short",
        "ema_pullback",
        "ema_reject",
        "mtf_align",
    ]
    rsi: float
    strength: float
    reasons: list[str]
    target: float | None = None
    stop_hint: float | None = None
    filter_value: float | None = None
    buff: int = 0
    buff_tags: tuple[str, ...] = ()


def ema_ladder(closes: list[float]) -> EricEmaLadder | None:
    if len(closes) < 55:
        return None
    e21 = ema(closes, 21)
    e50 = ema(closes, 50)
    e55 = ema(closes, 55)
    e200 = ema(closes, 200) if len(closes) >= 200 else e55
    return EricEmaLadder(
        ema21=e21[-1],
        ema50=e50[-1],
        ema55=e55[-1],
        ema200=e200[-1],
    )


def _pivot_indices(values: list[float], *, left: int = 3, right: int = 3) -> tuple[list[int], list[int]]:
    highs: list[int] = []
    lows: list[int] = []
    n = len(values)
    for i in range(left, n - right):
        w = values[i - left : i + right + 1]
        if values[i] == max(w) and w.count(values[i]) == 1:
            highs.append(i)
        if values[i] == min(w) and w.count(values[i]) == 1:
            lows.append(i)
    return highs, lows


def detect_rsi_divergence(
    closes: list[float],
    rsi: list[float],
    *,
    lookback: int = 60,
) -> Literal["bull", "bear"] | None:
    if len(closes) < lookback + 10:
        return None
    c = closes[-lookback:]
    r = rsi[-lookback:]
    hi_idx, lo_idx = _pivot_indices(c)
    if len(lo_idx) >= 2:
        a, b = lo_idx[-2], lo_idx[-1]
        if c[b] < c[a] and r[b] > r[a] and r[b] < 45:
            return "bull"
    if len(hi_idx) >= 2:
        a, b = hi_idx[-2], hi_idx[-1]
        if c[b] > c[a] and r[b] < r[a] and r[b] > 55:
            return "bear"
    return None


def detect_macd_divergence(
    closes: list[float],
    dif: list[float],
    *,
    lookback: int = 60,
) -> Literal["bull", "bear"] | None:
    if len(closes) < lookback + 10 or len(dif) < lookback + 10:
        return None
    c = closes[-lookback:]
    d = dif[-lookback:]
    hi_idx, lo_idx = _pivot_indices(c)
    if len(lo_idx) >= 2:
        a, b = lo_idx[-2], lo_idx[-1]
        if c[b] < c[a] and d[b] > d[a] and d[b] < 0:
            return "bull"
    if len(hi_idx) >= 2:
        a, b = hi_idx[-2], hi_idx[-1]
        if c[b] > c[a] and d[b] < d[a] and d[b] > 0:
            return "bear"
    return None


def _near_level(price: float, level: float | None, atr: float, mult: float = 0.8) -> bool:
    if level is None or atr <= 0:
        return False
    return abs(price - level) <= mult * atr


def _ema_touch(
    price: float,
    high: float,
    low: float,
    ladder: EricEmaLadder | None,
    atr: float,
    *,
    side: Literal["support", "resist"],
    mult: float = 0.55,
) -> float | None:
    """返回触及的 EMA 价；多看支撑回踩，空看压力拒绝。"""
    if ladder is None or atr <= 0:
        return None
    levels = (ladder.ema21, ladder.ema55, ladder.ema50)
    for lvl in levels:
        if side == "support":
            # 回踩：本根低点碰均线，收盘仍在均线附近上方或略破
            if abs(low - lvl) <= mult * atr or abs(price - lvl) <= mult * atr:
                if price >= lvl - 0.25 * atr:
                    return lvl
        else:
            if abs(high - lvl) <= mult * atr or abs(price - lvl) <= mult * atr:
                if price <= lvl + 0.25 * atr:
                    return lvl
    return None


def score_buff(
    *,
    side: Literal["long", "short"],
    bf: BandFilterSnapshot | None,
    near_structure: bool,
    div: bool,
    ema_lvl: float | None,
    htf_bf: BandFilterSnapshot | None,
    htf_bias: str,
    rr: float | None,
) -> BuffScore:
    score = 0
    tags: list[str] = []
    if bf:
        if side == "long":
            if bf.zone == "deep_os" or bf.entered_deep_os:
                score += 3
                tags.append("深超卖")
            elif bf.zone == "oversold" or bf.entered_oversold:
                score += 2
                tags.append("超卖")
        else:
            if bf.zone == "deep_ob" or bf.entered_deep_ob:
                score += 3
                tags.append("深超买")
            elif bf.zone == "overbought" or bf.entered_overbought:
                score += 2
                tags.append("超买")
    if near_structure:
        score += 2
        tags.append("前低/支撑" if side == "long" else "结构压力")
    if div:
        score += 2
        tags.append("底背离" if side == "long" else "顶背离")
    if ema_lvl is not None:
        score += 1
        tags.append(f"EMA@{ema_lvl:.6g}")
    if htf_bf:
        if side == "long" and htf_bf.zone in ("oversold", "deep_os"):
            score += 2
            tags.append("HTF超卖共振")
        elif side == "short" and htf_bf.zone in ("overbought", "deep_ob"):
            score += 2
            tags.append("HTF超买共振")
    bias = (htf_bias or "mixed").lower()
    if side == "long" and bias == "bear":
        score -= 1
        tags.append("HTF偏空降权")
    elif side == "short" and bias == "bull":
        score -= 1
        tags.append("HTF偏多降权")
    elif side == "long" and bias == "bull":
        score += 1
        tags.append("HTF顺势")
    elif side == "short" and bias == "bear":
        score += 1
        tags.append("HTF顺势")
    if rr is not None and rr >= 1.5:
        score += 1
        tags.append(f"RR≥1.5({rr:.1f})")
    return BuffScore(score=score, tags=tuple(tags), side=side)


def evaluate_eric(
    series: CandleSeries,
    *,
    oversold: float = 30.0,
    overbought: float = 70.0,
    structure_resist: float | None = None,
    structure_support: float | None = None,
    atr: float = 0.0,
    htf_series: CandleSeries | None = None,
    htf_bias: str = "mixed",
    min_buff: int = 4,
) -> list[EricSignal]:
    """对最新收盘根评估 Eric 风格信号（边沿触发由调用方用 state 去重）。"""
    candles = series.candles
    closes = [float(c.close) for c in candles]
    if len(closes) < 40:
        return []
    high = float(candles[-1].high)
    low = float(candles[-1].low)
    rsi = rsi_series(closes, 14)
    cur_r = rsi[-1]
    price = closes[-1]
    ladder = ema_ladder(closes)
    out: list[EricSignal] = []

    bf = compute_band_filter(series)
    htf_bf = compute_band_filter(htf_series) if htf_series is not None else None

    # 事件（刚进入）与状态（仍在区内）严格分开；只认波段过滤器，不用 RSI 兜底——
    # 否则无法区分信号来自过滤器还是 RSI 在补数（RSI 仅作展示/背离用）。
    entered_os = bool(bf and (bf.entered_oversold or bf.entered_deep_os))
    entered_ob = bool(bf and (bf.entered_overbought or bf.entered_deep_ob))
    in_os_zone = bool(bf and bf.state == "oversold")
    in_ob_zone = bool(bf and bf.state == "overbought")

    rsi_div = detect_rsi_divergence(closes, rsi)
    macd = compute_macd(series)
    macd_div = detect_macd_divergence(closes, macd.series_dif or [])
    bull_div = rsi_div == "bull" or macd_div == "bull"
    bear_div = rsi_div == "bear" or macd_div == "bear"
    near_support = _near_level(price, structure_support, atr, 0.85)
    near_resist = _near_level(price, structure_resist, atr, 0.85)
    ema_sup = _ema_touch(price, high, low, ladder, atr, side="support")
    ema_res = _ema_touch(price, high, low, ladder, atr, side="resist")

    long_targets: list[float] = []
    short_targets: list[float] = []
    if ladder:
        long_targets = sorted(
            {x for x in (ladder.ema21, ladder.ema50, ladder.ema55, ladder.ema200) if x > price}
        )
        short_targets = sorted(
            {x for x in (ladder.ema21, ladder.ema50, ladder.ema55, ladder.ema200) if x < price},
            reverse=True,
        )
    if structure_resist and structure_resist > price:
        long_targets = sorted(set(long_targets + [structure_resist]))
    if structure_support and structure_support < price:
        short_targets = sorted(set(short_targets + [structure_support]), reverse=True)

    fv = bf.value if bf else None

    def _rr(tgt: float | None, stop: float | None, side: str) -> float | None:
        if tgt is None or stop is None or price <= 0:
            return None
        risk = abs(price - stop)
        reward = abs(tgt - price)
        if risk <= 0:
            return None
        return reward / risk

    # ── 超卖 / Buff 反弹 ──
    if entered_os:
        tgt = long_targets[0] if long_targets else None
        stop = structure_support if structure_support and structure_support < price else None
        if stop is None and atr > 0:
            stop = price - 1.2 * atr
        deep = bool(bf and (bf.entered_deep_os or bf.zone == "deep_os"))
        buff = score_buff(
            side="long",
            bf=bf,
            near_structure=near_support,
            div=bull_div,
            ema_lvl=ema_sup,
            htf_bf=htf_bf,
            htf_bias=htf_bias,
            rr=_rr(tgt, stop, "long"),
        )
        tag = f"波段过滤器(近似) {bf.value:.1f}" + (" 深超卖" if deep else " 超卖")
        if bf.turned_up:
            tag += "·拐头"
        if bf.streak >= 2:
            tag += f"·多{bf.streak}"
        reasons = [tag, f"Buff={buff.score}（{'+'.join(buff.tags) or '无'}）", "定性：技术性反弹，不赌趋势反转"]
        if tgt:
            reasons.append(f"反弹目标参考 {tgt:.6g}")
        out.append(
            EricSignal(
                kind="oversold",
                rsi=cur_r,
                strength=min(0.9, 0.55 + 0.05 * buff.score),
                reasons=reasons,
                target=tgt,
                stop_hint=stop,
                filter_value=fv,
                buff=buff.score,
                buff_tags=buff.tags,
            )
        )
        if buff.score >= min_buff:
            out.append(
                EricSignal(
                    kind="rebound_long",
                    rsi=cur_r,
                    strength=min(0.95, 0.7 + 0.04 * buff.score),
                    reasons=[
                        f"Buff达标 {buff.score}≥{min_buff}：{' + '.join(buff.tags)}",
                        *reasons[2:],
                    ],
                    target=tgt,
                    stop_hint=stop,
                    filter_value=fv,
                    buff=buff.score,
                    buff_tags=buff.tags,
                )
            )

    # ── 超买 / Buff 做空或止盈 ──
    if entered_ob:
        tgt = short_targets[0] if short_targets else None
        stop = structure_resist if structure_resist and structure_resist > price else None
        if stop is None and atr > 0:
            stop = price + 1.2 * atr
        deep = bool(bf and (bf.entered_deep_ob or bf.zone == "deep_ob"))
        buff = score_buff(
            side="short",
            bf=bf,
            near_structure=near_resist,
            div=bear_div,
            ema_lvl=ema_res,
            htf_bf=htf_bf,
            htf_bias=htf_bias,
            rr=_rr(tgt, stop, "short"),
        )
        tag = f"波段过滤器(近似) {bf.value:.1f}" + (" 深超买" if deep else " 超买")
        if bf.turned_down:
            tag += "·拐头"
        if bf.streak >= 2:
            tag += f"·空{bf.streak}（二级/三级顶部超买）"
        reasons = [tag, f"Buff={buff.score}（{'+'.join(buff.tags) or '无'}）", "宜在压力处机械止盈或布局回落"]
        if tgt:
            reasons.append(f"回落参考 {tgt:.6g}")
        out.append(
            EricSignal(
                kind="overbought",
                rsi=cur_r,
                strength=min(0.88, 0.52 + 0.05 * buff.score),
                reasons=reasons,
                target=tgt,
                stop_hint=stop,
                filter_value=fv,
                buff=buff.score,
                buff_tags=buff.tags,
            )
        )
        if buff.score >= min_buff:
            out.append(
                EricSignal(
                    kind="fade_short",
                    rsi=cur_r,
                    strength=min(0.95, 0.7 + 0.04 * buff.score),
                    reasons=[
                        f"Buff达标 {buff.score}≥{min_buff}：{' + '.join(buff.tags)}",
                        *reasons[2:],
                    ],
                    target=tgt,
                    stop_hint=stop,
                    filter_value=fv,
                    buff=buff.score,
                    buff_tags=buff.tags,
                )
            )

    # ── EMA 回踩做多（超卖区或刚触发）──
    if ema_sup is not None and in_os_zone:
        tgt = long_targets[0] if long_targets else (ladder.ema200 if ladder else None)
        stop = ema_sup - 1.0 * atr if atr > 0 else None
        buff = score_buff(
            side="long", bf=bf, near_structure=near_support, div=bull_div,
            ema_lvl=ema_sup, htf_bf=htf_bf, htf_bias=htf_bias, rr=_rr(tgt, stop, "long"),
        )
        if buff.score >= max(3, min_buff - 1):
            out.append(
                EricSignal(
                    kind="ema_pullback",
                    rsi=cur_r,
                    strength=min(0.9, 0.68 + 0.03 * buff.score),
                    reasons=[
                        f"回踩 EMA {ema_sup:.6g} + 超卖区",
                        f"Buff={buff.score}：{'+'.join(buff.tags)}",
                        "止损好设，仓位好推（CycleStudies 回踩逻辑）",
                    ],
                    target=tgt,
                    stop_hint=stop,
                    filter_value=fv,
                    buff=buff.score,
                    buff_tags=buff.tags,
                )
            )

    # ── EMA 拒绝 / 压力止盈空 ──
    if ema_res is not None and in_ob_zone:
        tgt = short_targets[0] if short_targets else None
        stop = ema_res + 1.0 * atr if atr > 0 else None
        buff = score_buff(
            side="short", bf=bf, near_structure=near_resist, div=bear_div,
            ema_lvl=ema_res, htf_bf=htf_bf, htf_bias=htf_bias, rr=_rr(tgt, stop, "short"),
        )
        if buff.score >= max(3, min_buff - 1):
            out.append(
                EricSignal(
                    kind="ema_reject",
                    rsi=cur_r,
                    strength=min(0.9, 0.68 + 0.03 * buff.score),
                    reasons=[
                        f"均线压力 EMA {ema_res:.6g} + 超买区",
                        f"Buff={buff.score}：{'+'.join(buff.tags)}",
                        "机械止盈/布局回落，不追空杀跌",
                    ],
                    target=tgt,
                    stop_hint=stop,
                    filter_value=fv,
                    buff=buff.score,
                    buff_tags=buff.tags,
                )
            )

    # ── 多周期共振 ──
    if htf_bf is not None and bf is not None:
        if entered_os and htf_bf.zone in ("oversold", "deep_os"):
            out.append(
                EricSignal(
                    kind="mtf_align",
                    rsi=cur_r,
                    strength=0.88,
                    reasons=[
                        f"多周期超卖共振：本周期 BF={bf.value:.1f} · HTF BF={htf_bf.value:.1f}（{htf_bf.zone}）",
                        "大小周期同向极值，优先进场窗口",
                    ],
                    target=long_targets[0] if long_targets else None,
                    stop_hint=structure_support,
                    filter_value=fv,
                    buff=5,
                    buff_tags=("MTF超卖",),
                )
            )
        if entered_ob and htf_bf.zone in ("overbought", "deep_ob"):
            out.append(
                EricSignal(
                    kind="mtf_align",
                    rsi=cur_r,
                    strength=0.88,
                    reasons=[
                        f"多周期超买共振：本周期 BF={bf.value:.1f} · HTF BF={htf_bf.value:.1f}（{htf_bf.zone}）",
                        "大小周期同向极值，优先止盈/做空窗口",
                    ],
                    target=short_targets[0] if short_targets else None,
                    stop_hint=structure_resist,
                    filter_value=fv,
                    buff=5,
                    buff_tags=("MTF超买",),
                )
            )

    if bull_div and not entered_os and cur_r <= oversold + 5:
        src = "MACD" if macd_div == "bull" else "RSI"
        out.append(
            EricSignal(
                kind="bull_div",
                rsi=cur_r,
                strength=0.68 if macd_div == "bull" else 0.64,
                reasons=[f"价创新低但 {src} 抬高（底背离）", f"RSI={cur_r:.1f}"],
                target=long_targets[0] if long_targets else None,
                filter_value=fv,
            )
        )
    if bear_div and not entered_ob and cur_r >= overbought - 5:
        src = "MACD" if macd_div == "bear" else "RSI"
        out.append(
            EricSignal(
                kind="bear_div",
                rsi=cur_r,
                strength=0.68 if macd_div == "bear" else 0.64,
                reasons=[f"价创新高但 {src} 走弱（顶背离）", f"RSI={cur_r:.1f}"],
                target=short_targets[0] if short_targets else None,
                filter_value=fv,
            )
        )

    return out
