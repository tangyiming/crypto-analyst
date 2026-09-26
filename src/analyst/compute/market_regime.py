"""「三盘分类」与执行 playbook（可代码化部分）。

参考公开推文中可复现的执行框架：
- 强势盘：市价小头仓 + 突破近阻力加仓，新高/前高止盈
- 震荡盘：扎针损头仓后转低多；回踩支撑不破补仓；当日振幅 0.50–0.618 止盈
- 弱势盘：日线转空，反弹近阻力头仓空 + 跌破支撑加仓

与 swing_levels（锁点）互补：锁点给价位，本模块给「当前该用哪套打法」。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from analyst.compute.indicators import compute_boll, compute_macd, ema
from analyst.compute.swing_levels import SwingLevels
from analyst.compute.structure import Structure
from analyst.data.fetcher import Candle, CandleSeries


@dataclass
class MarketRegime:
    """当前盘面分类 + 可执行 playbook 摘要。"""

    regime: str  # strong_trend | range | weak_trend
    regime_zh: str
    trade_side: str  # long | short | wait
    seed_style: str  # market | pullback | resistance_short
    add_mode: str  # breakout | pullback | breakdown | none
    tp_style: str  # new_high | intraday_618 | structure_target
    defense_broken: bool
    continuation_intact: bool
    nearest_support: float | None
    nearest_resistance: float | None
    prev_day_high: float | None
    prev_day_low: float | None
    intraday_high: float | None
    intraday_low: float | None
    tp_intraday_50: float | None
    tp_intraday_618: float | None
    ema12h_6: float | None
    spike_stop_recent: bool
    waist_line: float | None = None
    below_waist: bool = False
    weekly_boll_mid: float | None = None
    wick_hold: bool = False
    htf_top_div: bool = False
    sub4h_pullback_fake_short: bool = False
    accel_2d: bool = False
    accel_2d_note: str = ""
    golden_3d: bool = False
    golden_5d: bool = False
    golden_note: str = ""
    hollow_daily: bool = False
    boll_mid_3d: float | None = None
    boll_mid_5d: float | None = None
    htf_ltf_resonance: bool = False
    near_rebound_618: bool = False
    macd_8h_decel: bool = False
    macd_12h_decel: bool = False
    ema5d_6: float | None = None
    second_break: bool = False
    weekly_macd_zero: bool = False
    # ── 2026-08 对照公开标注补的点位 ──
    # 日内回踩做多位：24h高 − (24h高 − 低)×0.5/0.618；振幅 >6% 时「低」改用最后一波冲高的起点（日内回踩低）
    pullback_50: float | None = None
    pullback_618: float | None = None
    pullback_low_used: float | None = None
    pullback_note: str = ""
    weekly_boll_upper: float | None = None      # 周线 BOLL 上轨（反弹阻力）
    monthly_boll_mid: float | None = None       # 月线 BOLL 中轨（自然月，含当月）：突破即大方向反转
    # 大周期：周期高点 → 之后的低点；低 + (高−低)×0.382/0.5/0.618 = 反转确认梯子
    cycle_high: float | None = None
    cycle_low: float | None = None
    cycle_382: float | None = None
    cycle_500: float | None = None
    cycle_618: float | None = None
    # 熊市底部参考：周期高 − (周期高 − 上一轮熊底)×0.618（BTC 126208/15443 → 57755）
    bear_bottom_618: float | None = None
    # 整数关口与「屏障」：关口下方 2–4% 是止跌屏障，上方 4–6% 是首个压力
    round_level: float | None = None
    barrier_below: tuple[float, float] | None = None
    barrier_above: tuple[float, float] | None = None
    # 4h BOLL 三轨：短线防守/低点/短阻常落在这里（SOL 8/26 防守 92=下轨，8/27 低点 97=中轨、短阻 102.75=上轨）
    boll_4h_lower: float | None = None
    boll_4h_mid: float | None = None
    boll_4h_upper: float | None = None
    # 币安 App 默认 BOLL(20,2)，15m/1h/4h/12h 共用。SOL 8/22 突破 97.56 可追 = 1h 上轨；BTC 8/15 支撑 62500 ≈ 12h 下轨 62698
    boll_1h_lower: float | None = None
    boll_1h_mid: float | None = None
    boll_1h_upper: float | None = None
    boll_12h_lower: float | None = None
    boll_12h_mid: float | None = None
    boll_12h_upper: float | None = None
    # 4h 最近枢轴（3 根确认）：比结构识别更贴近现价的近支/近阻来源
    pivot_supports: tuple[float, ...] = ()
    pivot_resistances: tuple[float, ...] = ()
    # 最近一波（24h 基准低 → 24h 高）的延伸目标：高 + (高−低)×0.5 / 0.618（SOL 8/25：87→103.26 ×1.5 = 111）
    ext_150: float | None = None
    ext_1618: float | None = None
    playbook_line: str = ""
    summary_line: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def _fmt_pair(p: tuple[float, float] | None) -> str:
        return "N/A" if not p else f"{_fmt(p[0])}-{_fmt(p[1])}"

    def prompt_block(self, *, compact: bool = False) -> str:
        if compact:
            return (
                f"盘面={self.regime_zh} 方向={self.trade_side} "
                f"头仓={self.seed_style} 补仓={self.add_mode} "
                f"止盈={self.tp_style} 延续={self.continuation_intact} "
                f"防守破={self.defense_broken} "
                f"近支={_fmt(self.nearest_support)} 近压={_fmt(self.nearest_resistance)} "
                f"昨高={_fmt(self.prev_day_high)} 日内618={_fmt(self.tp_intraday_618)} "
                f"12hEMA6={_fmt(self.ema12h_6)} 腰斩={_fmt(self.waist_line)} "
                f"周BOLL中={_fmt(self.weekly_boll_mid)} "
                f"诱空={self.sub4h_pullback_fake_short} 2d加速={self.accel_2d} "
                f"3d金叉={self.golden_3d} 5d金叉={self.golden_5d} "
                f"共振市价冲={self.htf_ltf_resonance} "
                f"二次突破={self.second_break} "
                f"8h/12h减速={self.macd_8h_decel}/{self.macd_12h_decel}"
            )
        return (
            f"- 盘面分类：{self.regime_zh}（{self.regime}）\n"
            f"- 建议方向：{self.trade_side} · 头仓={self.seed_style} · 补仓={self.add_mode}\n"
            f"- 止盈风格：{self.tp_style}\n"
            f"- 延续上涨/下跌：{self.continuation_intact} · 防守位已破：{self.defense_broken}\n"
            f"- 最近支撑：{_fmt(self.nearest_support)} · 最近阻力：{_fmt(self.nearest_resistance)}\n"
            f"- 前一日高/低：{_fmt(self.prev_day_high)} / {_fmt(self.prev_day_low)}\n"
            f"- 当日振幅 TP：0.50={_fmt(self.tp_intraday_50)} · 0.618={_fmt(self.tp_intraday_618)}\n"
            f"- 12h EMA6（扎针参考）：{_fmt(self.ema12h_6)}\n"
            f"- 近根扎针止损：{self.spike_stop_recent} · 虚破仍站稳：{self.wick_hold}\n"
            f"- 腰斩线（周期高点×0.5）：{_fmt(self.waist_line)} · 已跌破：{self.below_waist}\n"
            f"- 周线 BOLL 中轨（强势回调极限，非空目标）：{_fmt(self.weekly_boll_mid)}\n"
            f"- 1h/4h 顶背离：{self.htf_top_div} · 4h以下回踩诱空：{self.sub4h_pullback_fake_short}\n"
            f"- 2日线加速：{self.accel_2d} {self.accel_2d_note}\n"
            f"- 3日/5日金叉：{self.golden_3d}/{self.golden_5d} {self.golden_note}\n"
            f"- 日线空心阳加速：{self.hollow_daily} · 大小周期共振：{self.htf_ltf_resonance}\n"
            f"- 3日/5日 BOLL 中轨（强势减仓防守/上行阻力）：{_fmt(self.boll_mid_3d)} / {_fmt(self.boll_mid_5d)}\n"
            f"- 5日 EMA6（回踩防守）：{_fmt(self.ema5d_6)}\n"
            f"- 近反弹 0.618（鱼尾不加仓）：{self.near_rebound_618}\n"
            f"- 二次突破（日周托底下无假突破）：{self.second_break}\n"
            f"- 8h/12h MACD 归零下跌减速：{self.macd_8h_decel}/{self.macd_12h_decel}\n"
            f"- 周线 MACD 归零（大回调将尽）：{self.weekly_macd_zero}\n"
            f"- 日内回踩做多位 0.5/0.618：{_fmt(self.pullback_50)} / {_fmt(self.pullback_618)}"
            f"（振幅基准低点 {_fmt(self.pullback_low_used)}{'，' + self.pullback_note if self.pullback_note else ''}）\n"
            f"- 周线 BOLL 上轨（反弹阻力）：{_fmt(self.weekly_boll_upper)} · 月线 BOLL 中轨（破则大反转）：{_fmt(self.monthly_boll_mid)}\n"
            f"- 大周期 {_fmt(self.cycle_low)}→{_fmt(self.cycle_high)} 反转梯子 0.382/0.5/0.618："
            f"{_fmt(self.cycle_382)} / {_fmt(self.cycle_500)} / {_fmt(self.cycle_618)} · 熊底参考 {_fmt(self.bear_bottom_618)}\n"
            f"- 整数关口 {_fmt(self.round_level)}：屏障支撑 {self._fmt_pair(self.barrier_below)} · 首压 {self._fmt_pair(self.barrier_above)}\n"
            f"- 4h BOLL 下/中/上（短线防守/低点/短阻）：{_fmt(self.boll_4h_lower)} / {_fmt(self.boll_4h_mid)} / {_fmt(self.boll_4h_upper)}\n"
            f"- 1h BOLL 下/中/上（追涨/短止盈）：{_fmt(self.boll_1h_lower)} / {_fmt(self.boll_1h_mid)} / {_fmt(self.boll_1h_upper)}"
            f" · 12h BOLL 下/中/上：{_fmt(self.boll_12h_lower)} / {_fmt(self.boll_12h_mid)} / {_fmt(self.boll_12h_upper)}\n"
            f"- 4h 枢轴支撑：{', '.join(_fmt(x) for x in self.pivot_supports) or 'N/A'} · 枢轴阻力：{', '.join(_fmt(x) for x in self.pivot_resistances) or 'N/A'}\n"
            f"- 本波延伸目标 1.5/1.618：{_fmt(self.ext_150)} / {_fmt(self.ext_1618)}\n"
            f"- Playbook：{self.playbook_line}\n"
            f"- 摘要：{self.summary_line}"
        )


def _fmt(x: float | None) -> str:
    if x is None:
        return "N/A"
    ax = abs(x)
    if ax >= 1000:
        return f"{x:.2f}"
    if ax >= 1:
        return f"{x:.4f}"
    return f"{x:.6f}"


def _prev_day_hl(daily: CandleSeries | None) -> tuple[float | None, float | None]:
    if not daily or len(daily.candles) < 2:
        return None, None
    prev = daily.candles[-2]
    return float(prev.high), float(prev.low)


def _intraday_range(
    daily: CandleSeries | None,
    *,
    fallback_high: float | None = None,
    fallback_low: float | None = None,
) -> tuple[float | None, float | None]:
    if daily and daily.candles:
        last = daily.candles[-1]
        return float(last.high), float(last.low)
    if fallback_high is not None and fallback_low is not None:
        return float(fallback_high), float(fallback_low)
    return None, None


def _resample_12h_closes(hourly: CandleSeries) -> list[float]:
    candles = hourly.candles
    if len(candles) < 12:
        return []
    closes: list[float] = []
    # 从最近完整 12 根 1h 往回聚合
    n = len(candles) - (len(candles) % 12)
    for i in range(11, n, 12):
        closes.append(candles[i].close)
    if not closes and len(candles) >= 12:
        closes.append(candles[-1].close)
    return closes


def _ema12h_6(hourly: CandleSeries | None) -> float | None:
    if not hourly or len(hourly.candles) < 24:
        return None
    closes = _resample_12h_closes(hourly)
    if len(closes) < 3:
        return None
    return ema(closes, 6)[-1]


def _defense_broken(
    current_price: float,
    defense: float,
    primary: CandleSeries | None,
    *,
    side: str,
    tol: float = 0.003,
) -> bool:
    if defense <= 0:
        return False
    if side == "long":
        if current_price < defense * (1 - tol):
            return True
        if primary and primary.candles:
            last = primary.candles[-1]
            if last.close < defense * (1 - tol):
                return True
        return False
    if side == "short":
        if current_price > defense * (1 + tol):
            return True
        if primary and primary.candles:
            last = primary.candles[-1]
            if last.close > defense * (1 + tol):
                return True
        return False
    return False


def _spike_stop_recent(
    primary: CandleSeries | None,
    defense: float,
    *,
    side: str,
    lookback: int = 4,
    wick_tol: float = 0.004,
) -> bool:
    """近几根是否出现「扎针穿防守但收回」——震荡盘典型损头仓场景。"""
    if not primary or not primary.candles or defense <= 0:
        return False
    for c in primary.candles[-lookback:]:
        if side == "long":
            pierced = c.low < defense * (1 - wick_tol)
            recovered = c.close >= defense * (1 - wick_tol * 0.5)
            if pierced and recovered:
                return True
        elif side == "short":
            pierced = c.high > defense * (1 + wick_tol)
            recovered = c.close <= defense * (1 + wick_tol * 0.5)
            if pierced and recovered:
                return True
    return False


def _resample_every_n(daily: CandleSeries, n: int, tf: str) -> CandleSeries | None:
    cs = daily.candles
    if len(cs) < n:
        return None
    start = len(cs) % n
    out: list[Candle] = []
    for i in range(start, len(cs), n):
        chunk = cs[i : i + n]
        if len(chunk) < n:
            break
        out.append(
            Candle(
                timestamp=chunk[-1].timestamp,
                open=chunk[0].open,
                high=max(c.high for c in chunk),
                low=min(c.low for c in chunk),
                close=chunk[-1].close,
                volume=sum(c.volume for c in chunk),
            )
        )
    if not out:
        return None
    return CandleSeries(symbol=daily.symbol, timeframe=tf, candles=out)


def _resample_weekly(daily: CandleSeries | None) -> CandleSeries | None:
    if not daily or len(daily.candles) < 10:
        return None
    groups: dict[tuple[int, int], list[Candle]] = {}
    for c in daily.candles:
        key = (c.timestamp.isocalendar()[0], c.timestamp.isocalendar()[1])
        groups.setdefault(key, []).append(c)
    out: list[Candle] = []
    for key in sorted(groups):
        chunk = groups[key]
        out.append(
            Candle(
                timestamp=chunk[-1].timestamp,
                open=chunk[0].open,
                high=max(x.high for x in chunk),
                low=min(x.low for x in chunk),
                close=chunk[-1].close,
                volume=sum(x.volume for x in chunk),
            )
        )
    if len(out) < 8:
        return None
    return CandleSeries(symbol=daily.symbol, timeframe="1w", candles=out)


def _weekly_boll_mid(daily: CandleSeries | None) -> float | None:
    weekly = _resample_weekly(daily)
    if not weekly:
        return None
    period = min(20, len(weekly.candles))
    return compute_boll(weekly, period=period).middle


def _weekly_boll_upper(daily: CandleSeries | None) -> float | None:
    weekly = _resample_weekly(daily)
    if not weekly:
        return None
    period = min(20, len(weekly.candles))
    return compute_boll(weekly, period=period).upper


def _resample_calendar_month(daily: CandleSeries | None) -> CandleSeries | None:
    """自然月重采样（含当月未收盘 K）。月线 BOLL 中轨按此算：ETH 2026-08-22 = 2646 精确复现。"""
    if not daily or len(daily.candles) < 60:
        return None
    groups: dict[tuple[int, int], list[Candle]] = {}
    for c in daily.candles:
        groups.setdefault((c.timestamp.year, c.timestamp.month), []).append(c)
    out: list[Candle] = []
    for key in sorted(groups):
        chunk = groups[key]
        out.append(
            Candle(
                timestamp=chunk[-1].timestamp,
                open=chunk[0].open,
                high=max(x.high for x in chunk),
                low=min(x.low for x in chunk),
                close=chunk[-1].close,
                volume=sum(x.volume for x in chunk),
            )
        )
    if len(out) < 6:
        return None
    return CandleSeries(symbol=daily.symbol, timeframe="1M", candles=out)


def _monthly_boll_mid(daily: CandleSeries | None) -> float | None:
    monthly = _resample_calendar_month(daily)
    if not monthly:
        return None
    period = min(20, len(monthly.candles))
    return compute_boll(monthly, period=period).middle


def _pullback_levels(
    hourly: CandleSeries | None,
    *,
    bars: int = 24,
    wide_pct: float = 0.06,
) -> tuple[float | None, float | None, float | None, str]:
    """日内回踩做多位：24h高 − (24h高 − 低)×0.5/0.618。

    24h 振幅 > 6% 且 24h 低点在高点之前（先跌后拉）时，「低」改用 24h 低→高之间最后一个局部低点
    （最后一波冲高的起点）。BTC 2026-08-21：24h 79556/72280 → 75059（通常回踩不到）；
    改用 74214 → 76255（对照标注 76267，实际低 76237）。
    """
    if not hourly or len(hourly.candles) < bars:
        return None, None, None, ""
    c = hourly.candles[-bars:]
    hi_i = max(range(len(c)), key=lambda i: c[i].high)
    lo_i = min(range(len(c)), key=lambda i: c[i].low)
    hi, lo = c[hi_i].high, c[lo_i].low
    if hi <= 0 or hi <= lo:
        return None, None, None, ""
    used, note = lo, ""
    if (hi - lo) / hi > wide_pct and lo_i < hi_i:
        piv = [
            c[i].low
            for i in range(lo_i + 1, hi_i)
            if c[i].low <= c[i - 1].low and c[i].low <= c[i + 1].low
        ]
        if piv:
            used = piv[-1]
            note = f"24h振幅{(hi - lo) / hi * 100:.1f}%过大，改用日内回踩低 {used:.6g}"
    rng = hi - used
    return hi - rng * 0.5, hi - rng * 0.618, used, note


def _cycle_fib(
    daily: CandleSeries | None,
    *,
    min_bars: int = 300,
) -> tuple[float | None, float | None, float | None, float | None, float | None, float | None]:
    """周期高点（日线最高 high）→ 其后的最低 low；低 + (高−低)×0.382/0.5/0.618 = 反转确认梯子。

    ETH 2026-08：4957.67/1503.6 → 2823/3231/3638（对照标注 2822/3230/3637）。
    熊底参考：高 − (高 − 上一轮熊底)×0.618，上一轮熊底 = 周期高点之前的最低 low（需足够长历史）。
    """
    if not daily or len(daily.candles) < min_bars:
        return (None,) * 6
    cs = daily.candles
    hi_i = max(range(len(cs)), key=lambda i: cs[i].high)
    after = cs[hi_i + 1 :]
    if len(after) < 5:
        return (None,) * 6
    lo = min(x.low for x in after)
    hi = cs[hi_i].high
    if hi <= lo:
        return (None,) * 6
    rng = hi - lo
    before = cs[:hi_i]
    bear_bottom = None
    if len(before) >= 200:
        prev_low = min(x.low for x in before)
        if prev_low < lo:
            bear_bottom = hi - (hi - prev_low) * 0.618
    return hi, lo, lo + rng * 0.382, lo + rng * 0.5, lo + rng * 0.618, bear_bottom


def _boll_4h(h4: CandleSeries | None) -> tuple[float | None, float | None, float | None]:
    if not h4 or len(h4.candles) < 21:
        return None, None, None
    b = compute_boll(h4)
    return b.lower, b.middle, b.upper


def _pivot_levels(
    series: CandleSeries | None,
    price: float,
    *,
    k: int = 3,
    lookback: int = 120,
    n: int = 3,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """4h 枢轴高/低（左右各 k 根确认）：现价下方最近 n 个 = 支撑，上方最近 n 个 = 阻力。"""
    if not series or len(series.candles) < 2 * k + 5 or price <= 0:
        return (), ()
    cs = series.candles[-lookback:]
    highs: list[float] = []
    lows: list[float] = []
    for i in range(k, len(cs) - k):
        w = cs[i - k : i + k + 1]
        if cs[i].high >= max(x.high for x in w):
            highs.append(cs[i].high)
        if cs[i].low <= min(x.low for x in w):
            lows.append(cs[i].low)
    sup = sorted({x for x in lows + highs if x < price * 0.998}, reverse=True)[:n]
    res = sorted({x for x in highs + lows if x > price * 1.002})[:n]
    return tuple(sup), tuple(res)


def _extension_targets(hourly: CandleSeries | None, used_low: float | None, *, bars: int = 24) -> tuple[float | None, float | None]:
    """最近一波（基准低 → 24h 高）的 1.5 / 1.618 延伸：高 + (高−低)×0.5 / 0.618。"""
    if not hourly or len(hourly.candles) < bars or used_low is None:
        return None, None
    hi = max(x.high for x in hourly.candles[-bars:])
    if hi <= used_low:
        return None, None
    rng = hi - used_low
    return hi + rng * 0.5, hi + rng * 0.618


def _round_barriers(price: float) -> tuple[float | None, tuple[float, float] | None, tuple[float, float] | None]:
    """最近的整数关口（高于现价的第一个）及其屏障区：下方 2–4%，上方 4–6%。

    网格 = 半个数量级：ETH 2420 → 2500（站稳 2500），BTC 78k → 80000（8w），SOL 97 → 100。
    """
    if price <= 0:
        return None, None, None
    import math

    mag = 10 ** math.floor(math.log10(price))
    step = mag / 2  # 半个数量级：63088 → 5000 → 65000；78746 → 80000；2420 → 2500；97 → 100；101 → 150
    level = math.floor(price / step) * step + step
    if level <= price * 1.001:
        level += step
    return level, (level * 0.96, level * 0.98), (level * 1.04, level * 1.06)


def _accel_2d(daily: CandleSeries | None) -> tuple[bool, str]:
    """2 日线：图上先标金叉（可在零下），文字说快线随后触零轴才加速冲。"""
    if not daily:
        return False, ""
    series = _resample_every_n(daily, 2, "2d")
    if not series or len(series.candles) < 35:
        return False, ""
    macd = compute_macd(series)
    if len(macd.series_dif) < 3 or len(macd.series_dea) < 3:
        return False, ""
    prev, now = macd.series_dif[-2], macd.series_dif[-1]
    prev_dea, now_dea = macd.series_dea[-2], macd.series_dea[-1]
    rising = now > prev
    golden = macd.cross_signal == "golden" or (prev <= prev_dea and now > now_dea)
    touch_zero = prev <= 0 and rising and (
        now >= 0 or (now < 0 and abs(now) <= abs(prev) * 0.55)
    )
    hit = golden or touch_zero
    if touch_zero:
        note = f"2d快线触零轴 DIF {prev:.4f}→{now:.4f}"
    elif golden:
        note = f"2d金叉{'零下' if now < 0 else ''} DIF {now:.4f}"
    else:
        note = ""
    return hit, note


def _hist_series(series: CandleSeries) -> list[float]:
    macd = compute_macd(series)
    return [
        (d - e) * 2
        for d, e in zip(macd.series_dif, macd.series_dea, strict=False)
    ]


def _top_div(series: CandleSeries | None, *, lookback: int = 36) -> bool:
    """价创新高但 MACD 柱峰值走低。"""
    if not series or len(series.candles) < lookback:
        return False
    hists = _hist_series(series)
    if len(hists) < lookback:
        return False
    highs = [c.high for c in series.candles[-lookback:]]
    h = hists[-lookback:]
    split = max(8, lookback // 3)
    recent_high = max(highs[-split:])
    prior_high = max(highs[:-split])
    recent_hist = max(h[-split:])
    prior_hist = max(h[:-split])
    return recent_high > prior_high * 1.001 and recent_hist < prior_hist * 0.98


def _wick_hold(
    series: CandleSeries | None,
    support: float | None,
    *,
    lookback: int = 3,
    extend_pct: float = 0.008,
) -> bool:
    """虚破：刺穿支撑但收盘仍站上（允许下延一点）。"""
    if not series or not series.candles or not support or support <= 0:
        return False
    floor = support * (1 - extend_pct)
    for c in series.candles[-lookback:]:
        if c.low <= floor and c.close >= support * 0.997:
            return True
    return False


def _nd_boll_mid(daily: CandleSeries | None, n: int) -> float | None:
    series = _resample_every_n(daily, n, f"{n}d") if daily else None
    if not series or len(series.candles) < 8:
        return None
    period = min(20, len(series.candles))
    return compute_boll(series, period=period).middle


def _macd_golden_or_brewing(series: CandleSeries | None) -> tuple[bool, str]:
    """金叉或 DIF 从下方抬向 DEA（酝酿）。"""
    if not series or len(series.candles) < 35:
        return False, ""
    macd = compute_macd(series)
    if len(macd.series_dif) < 3 or len(macd.series_dea) < 3:
        return False, ""
    prev_d, now_d = macd.series_dif[-2], macd.series_dif[-1]
    prev_e, now_e = macd.series_dea[-2], macd.series_dea[-1]
    if macd.cross_signal == "golden" or (prev_d <= prev_e and now_d > now_e):
        return True, "金叉"
    brewing = now_d > prev_d and now_d <= now_e and (now_e - now_d) <= max(abs(now_e) * 0.45, 1e-9)
    if brewing:
        return True, "金叉酝酿"
    return False, ""


def _nd_golden(daily: CandleSeries | None, n: int) -> tuple[bool, str]:
    series = _resample_every_n(daily, n, f"{n}d") if daily else None
    hit, kind = _macd_golden_or_brewing(series)
    return hit, f"{n}d{kind}" if hit else ""


def _hollow_daily_accel(daily: CandleSeries | None) -> bool:
    """空心阳/方块：实体大，或收盘穿日线 BOLL 上轨（美光图：阳线 + 上轨外）。"""
    if not daily or not daily.candles:
        return False
    c = daily.candles[-1]
    rng = c.high - c.low
    yang_block = False
    if rng > 0:
        body = c.close - c.open
        yang_block = body > 0 and body / rng >= 0.55 and c.close >= c.low + rng * 0.68
    boll = compute_boll(daily)
    above_upper = boll.upper > 0 and c.close >= boll.upper
    return yang_block or above_upper


def _hourly_strong(hourly: CandleSeries | None) -> bool:
    """小时走强：图上 EMA6>24>52 多头排列，且 MACD DIF≥DEA、柱为正。"""
    if not hourly or len(hourly.candles) < 52:
        return False
    macd = compute_macd(hourly)
    macd_ok = macd.histogram > 0 and macd.dif >= macd.dea
    closes = hourly.closes
    e6, e24, e52 = ema(closes, 6)[-1], ema(closes, 24)[-1], ema(closes, 52)[-1]
    ema_stack = e6 >= e24 >= e52
    return macd_ok and ema_stack


def _ltf_macd_above_zero(m5: CandleSeries | None) -> bool:
    """1/3/5 分钟 MACD 都在零上 ≈ 只能现价冲（用 5m 代理）。"""
    if not m5 or len(m5.candles) < 35:
        return False
    macd = compute_macd(m5)
    return macd.dif > 0 and macd.dea > 0


def _daily_momentum_fade(daily: CandleSeries | None) -> bool:
    """日线下跌动能减弱：柱线仍在零下但抬升。"""
    if not daily or len(daily.candles) < 35:
        return False
    hists = _hist_series(daily)
    if len(hists) < 3:
        return False
    return hists[-1] > hists[-2] and hists[-1] <= 0


def _macd_decel_to_zero(series: CandleSeries | None) -> bool:
    """「MACD 归零、下跌减速」= MACD 柱在零下开始缩短（向零轴收敛）。

    看柱线而不是 DIF 拐头：2026-08 BTC 对照，柱缩短法在 8h(8/13)、12h(8/15)、
    日线(8/17) 与公开标注时间吻合；DIF 抬升法要晚 1–2 天。
    """
    if not series or len(series.candles) < 35:
        return False
    hists = _hist_series(series)
    if len(hists) < 3:
        return False
    prev, now = hists[-2], hists[-1]
    return now < 0 and now > prev


def _nd_ema6(daily: CandleSeries | None, n: int) -> float | None:
    series = _resample_every_n(daily, n, f"{n}d") if daily else None
    if not series or len(series.candles) < 6:
        return None
    return ema(series.closes, 6)[-1]


def compute_market_regime(
    *,
    current_price: float,
    swing: SwingLevels,
    structure: Structure,
    primary_series: CandleSeries | None = None,
    daily_series: CandleSeries | None = None,
    hourly_series: CandleSeries | None = None,
    h4_series: CandleSeries | None = None,
    m5_series: CandleSeries | None = None,
    high_24h: float | None = None,
    low_24h: float | None = None,
) -> MarketRegime:
    """根据锁点 + 结构 + 近端 K 线，输出三盘分类与 playbook。"""
    nearest_support = structure.supports[0] if structure.supports else swing.retr_618
    nearest_resistance = (
        structure.resistances[0] if structure.resistances else swing.rebound_382
    )
    prev_high, prev_low = _prev_day_hl(daily_series)
    intra_high, intra_low = _intraday_range(
        daily_series, fallback_high=high_24h, fallback_low=low_24h
    )
    tp50 = tp618 = None
    if intra_high is not None and intra_low is not None and intra_high > intra_low:
        rng = intra_high - intra_low
        tp50 = intra_low + rng * 0.50
        tp618 = intra_low + rng * 0.618

    ema12 = _ema12h_6(hourly_series)
    # 腰斩线 = 周期最高点 × 0.5（63000 是 BTC 腰斩位，这之下不追空只低吸，126k/2）。
    # 用手头日线里的最高 high（需 ≥200 根，hub 拉 400 根）；不够长才退回最近波段高点。
    cycle_high = (
        max(c.high for c in daily_series.candles)
        if daily_series and len(daily_series.candles) >= 200
        else None
    )
    waist_src = cycle_high if cycle_high else (swing.swing_high if swing.swing_high > 0 else None)
    waist = waist_src * 0.5 if waist_src else None
    below_waist = bool(waist and current_price <= waist * 1.02)
    weekly_mid = _weekly_boll_mid(daily_series)
    boll_3d = _nd_boll_mid(daily_series, 3)
    boll_5d = _nd_boll_mid(daily_series, 5)
    accel, accel_note = _accel_2d(daily_series)
    g3, n3 = _nd_golden(daily_series, 3)
    g5, n5 = _nd_golden(daily_series, 5)
    golden_note = " · ".join(x for x in (n3, n5) if x)
    hollow = _hollow_daily_accel(daily_series)
    hourly_ok = _hourly_strong(hourly_series)
    ltf_zero = _ltf_macd_above_zero(m5_series)
    daily_fade = _daily_momentum_fade(daily_series)
    htf_div = _top_div(hourly_series) or _top_div(h4_series)
    wick = _wick_hold(primary_series or hourly_series, nearest_support)
    near_618 = current_price >= swing.rebound_618 * 0.992 if swing.rebound_618 > 0 else False
    resonance = swing.daily_bias == "up" and (hourly_ok or ltf_zero)
    h8 = _resample_every_n(h4_series, 2, "8h") if h4_series else None
    h12 = _resample_every_n(h4_series, 3, "12h") if h4_series else None
    dec8 = _macd_decel_to_zero(h8)
    dec12 = _macd_decel_to_zero(h12)
    ema5d6 = _nd_ema6(daily_series, 5)
    pb50, pb618, pb_low, pb_note = _pullback_levels(hourly_series)
    weekly_upper = _weekly_boll_upper(daily_series)
    monthly_mid = _monthly_boll_mid(daily_series)
    cyc_hi, cyc_lo, cyc_382, cyc_500, cyc_618, bear_618 = _cycle_fib(daily_series)
    round_level, barrier_below, barrier_above = _round_barriers(current_price)
    b4_lo, b4_mid, b4_up = _boll_4h(h4_series)
    b1_lo, b1_mid, b1_up = _boll_4h(hourly_series)
    b12_lo, b12_mid, b12_up = _boll_4h(h12)
    piv_sup, piv_res = _pivot_levels(h4_series or primary_series, current_price)
    # 多月级结构位（BTC 5 月高 82800、7 月初低 60455/60666、SOL 87）：日线枢轴 250 天，与 4h 枢轴合并
    d_sup, d_res = _pivot_levels(daily_series, current_price, k=3, lookback=250, n=4)
    piv_sup = tuple(sorted(set(piv_sup) | set(d_sup), reverse=True)[:5])
    piv_res = tuple(sorted(set(piv_res) | set(d_res))[:5])
    ext150, ext1618 = _extension_targets(hourly_series, pb_low)
    # 结构识别常滞后（SOL 8/22 价格 99 时近支仍 76.6、近阻 79.6 在现价之下）：
    # 近支/近阻必须夹住现价；优先 4h 枢轴，其次回踩位/4h BOLL/24h 高/周 BOLL 上轨/整数关口。
    def _pick(cands: list[float | None], *, below: bool, min_gap: float = 0.015) -> float | None:
        """按优先级取第一个与现价至少相隔 min_gap 的候选；都太近则退回离现价最远的合法候选。"""
        valid = [x for x in cands if x is not None and ((x < current_price) if below else (x > current_price))]
        for x in valid:
            if abs(x / current_price - 1) >= min_gap:
                return x
        return (min(valid) if below else max(valid)) if valid else None

    hi24 = max(x.high for x in hourly_series.candles[-24:]) if hourly_series and len(hourly_series.candles) >= 24 else None
    # 支撑优先级：回踩 0.618（如 96–94）→ 4h 枢轴低 → 结构支撑 → 4h BOLL 中/下
    sup_cands = [pb618, piv_sup[0] if piv_sup else None, nearest_support, b4_mid, b12_lo, b4_lo]
    # 阻力优先级：4h 枢轴高 → 24h 高 → 结构阻力 → 1h/4h BOLL 上 → 12h/周 BOLL 上 → 整数关口首压 → 本波 1.5 延伸
    res_cands = [piv_res[0] if piv_res else None, hi24, nearest_resistance, b1_up, b4_up, b12_up, weekly_upper,
                 barrier_above[0] if barrier_above else None, ext150]
    nearest_support = _pick(sup_cands, below=True) or nearest_support
    nearest_resistance = _pick(res_cands, below=False) or nearest_resistance
    # 回踩位只在现价上方时有意义（价格已经跌破就不是「回踩」了）
    if pb618 is not None and pb618 >= current_price:
        pb50 = pb618 = None
        pb_note = (pb_note + "；" if pb_note else "") + "现价已低于回踩位"
    second_break = swing.touch_count >= 2 and swing.daily_bias == "up"
    weekly_zero = _macd_decel_to_zero(_resample_weekly(daily_series))
    bias = swing.daily_bias
    ladder = (
        "每突破一个近阻力可加仓一点（略高于阻力更稳），新均价附近设「基本止盈防守」，"
        "加仓那部分先止盈；二次突破已破阻力比回踩补更稳。开仓后先设止损再抓止盈。"
        "只在浮盈后才加重仓；头仓轻仓。"
    )
    boll_def = ""
    if boll_5d or boll_3d:
        boll_def = (
            f"上行阻力/减仓防守看 5日 BOLL 中 {_fmt(boll_5d)}、"
            f"3日 BOLL 中 {_fmt(boll_3d)}"
            + (f"、5日 EMA6 {_fmt(ema5d6)}" if ema5d6 else "")
            + "。"
        )

    if bias == "down":
        side = "short"
        defense_broken = _defense_broken(
            current_price, swing.defense_level, primary_series, side="short"
        )
        spike = _spike_stop_recent(
            primary_series, swing.defense_level, side="short"
        )
        regime = "weak_trend"
        regime_zh = "弱势盘"
        seed_style = "resistance_short"
        add_mode = "none" if below_waist else "breakdown"
        tp_style = "structure_target"
        continuation = not defense_broken
        fake_short = False
        if below_waist:
            side = "wait"
            playbook = (
                f"已近/跌破腰斩线 {_fmt(waist)}：穷寇莫追，不再加空；"
                "等反弹到近阻力再考虑头仓空。"
            )
            summary = f"弱势但触及腰斩线 {_fmt(waist)}，停止追空。"
            if daily_fade and hourly_ok:
                side = "long"
                seed_style = "market"
                add_mode = "none"
                playbook = (
                    f"腰斩区 {_fmt(waist)}：日线下跌动能减弱且小时走强，"
                    "可市价小头仓低吸，仍不追空。"
                )
                summary = "腰斩区止跌+小时走强，只做低吸头仓。"
        else:
            if weekly_zero or dec8 or dec12:
                add_mode = "none"
            playbook = (
                "日线转空：反弹近阻力或突破后的新高点小头仓空，不空小回踩；"
                "跌破最近支撑可加仓；止损放在阻力上方；"
                "目标先看近支撑，不追到腰斩线以下。"
            )
            if weekly_zero:
                playbook += "周线 MACD 归零，大回调将尽，不追空。"
            elif dec8 or dec12:
                playbook += "8h/12h MACD 归零下跌减速，最多轻仓高空，优先等支撑低吸。"
            if defense_broken:
                summary = (
                    f"弱势延续，现价 {_fmt(current_price)} 已压过防守 {_fmt(swing.defense_level)}；"
                    f"反弹 {_fmt(nearest_resistance)} 附近仍偏空。"
                )
            else:
                summary = (
                    f"弱势但尚未有效突破防守 {_fmt(swing.defense_level)}；"
                    f"等反弹 {_fmt(nearest_resistance)} 附近再试空。"
                )
    elif bias == "up":
        side = "long"
        defense_broken = _defense_broken(
            current_price, swing.defense_level, primary_series, side="long"
        )
        spike = _spike_stop_recent(
            primary_series, swing.defense_level, side="long"
        )
        continuation = not defense_broken
        fake_short = continuation and not htf_div
        above_mid = current_price >= (swing.swing_high + swing.swing_low) / 2
        strong = (
            continuation
            and (swing.htf_ready or resonance)
            and above_mid
            and not spike
        )
        if resonance and continuation and not spike:
            strong = True
        if strong:
            regime = "strong_trend"
            regime_zh = "强势盘"
            seed_style = "market"
            add_mode = "none" if near_618 else "breakout"
            tp_style = "new_high"
            playbook = (
                "强势盘：日周定调+小时走强则市价小头仓，不等低多。"
                + ladder
                + boll_def
                + "4h 以下回踩若无 1h/4h 顶背离视为诱空，不追空。"
                + (
                    f"周线 BOLL 中轨 {_fmt(weekly_mid)} 是回调极限参考，不是空目标。"
                    if weekly_mid
                    else ""
                )
                + ("已近反弹 0.618，鱼尾不加仓、不追多。" if near_618 else "")
            )
            bits = [
                f"延续完好，防守 {_fmt(swing.defense_level)} 未破",
                f"突破 {_fmt(nearest_resistance)} 或昨高 {_fmt(prev_high)} 可加仓"
                if not near_618
                else "鱼尾区不加仓",
            ]
            if accel:
                bits.append(f"2日线加速（{accel_note}）")
            if golden_note:
                bits.append(golden_note)
            if hollow:
                bits.append("日线空心阳加速")
            if resonance:
                bits.append("大小周期共振市价冲")
            if ltf_zero:
                bits.append("5m MACD零上现价冲")
            if fake_short:
                bits.append("4h以下回踩诱空")
            if second_break:
                bits.append("二次突破同一阻力，日周托底下无假突破")
            if weekly_zero:
                bits.append("周线MACD归零")
            summary = "；".join(bits) + "。"
        else:
            regime = "range"
            regime_zh = "震荡盘"
            seed_style = "market" if resonance else "pullback"
            add_mode = "none" if near_618 else "pullback"
            tp_style = "intraday_618"
            playbook = (
                (
                    "大小周期共振：小时已走强，市价小头仓，等低多容易踏空。"
                    if resonance
                    else "震荡/延续中断：优先低多；虚破下延一点仍可头仓；"
                )
                + "回踩支撑不破可补仓；止盈看当日振幅 0.50–0.618；"
                "突破上一高点后再切回强势打法。" + ladder + boll_def
            )
            parts = []
            if defense_broken:
                parts.append(
                    f"跌破防守 {_fmt(swing.defense_level)}，延续上涨中断；"
                    f"强压参考 {_fmt(swing.rebound_618)}"
                )
            if spike:
                parts.append("近根出现扎针止损，宜等支撑站稳再进")
            if wick:
                parts.append("支撑虚破后收回，仍可试头仓")
            if not parts:
                parts.append(
                    f"高周期未成熟或处于波段上半区，宜 {_fmt(nearest_support)} 附近低多"
                )
            summary = "；".join(parts)
    else:
        side = "wait"
        defense_broken = False
        spike = False
        continuation = False
        fake_short = False
        regime = "range"
        regime_zh = "震荡盘"
        seed_style = "pullback"
        add_mode = "none"
        tp_style = "intraday_618"
        playbook = (
            "震荡定调：不在中间位追；靠近支撑小仓试多/靠近阻力试空；"
            "日内反弹 TP 看 0.50–0.618；突破边界再切换趋势打法。"
        )
        summary = (
            f"日线震荡；上 {_fmt(nearest_resistance)} / 下 {_fmt(nearest_support)} "
            f"边界外再动手。"
        )

    return MarketRegime(
        regime=regime,
        regime_zh=regime_zh,
        trade_side=side,
        seed_style=seed_style,
        add_mode=add_mode,
        tp_style=tp_style,
        defense_broken=defense_broken,
        continuation_intact=continuation,
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        prev_day_high=prev_high,
        prev_day_low=prev_low,
        intraday_high=intra_high,
        intraday_low=intra_low,
        tp_intraday_50=tp50,
        tp_intraday_618=tp618,
        ema12h_6=ema12,
        spike_stop_recent=spike,
        waist_line=waist,
        below_waist=below_waist,
        weekly_boll_mid=weekly_mid,
        wick_hold=wick,
        htf_top_div=htf_div,
        sub4h_pullback_fake_short=fake_short,
        accel_2d=accel,
        accel_2d_note=accel_note,
        golden_3d=g3,
        golden_5d=g5,
        golden_note=golden_note,
        hollow_daily=hollow,
        boll_mid_3d=boll_3d,
        boll_mid_5d=boll_5d,
        htf_ltf_resonance=resonance,
        near_rebound_618=near_618,
        macd_8h_decel=dec8,
        macd_12h_decel=dec12,
        ema5d_6=ema5d6,
        second_break=second_break,
        weekly_macd_zero=weekly_zero,
        pullback_50=pb50,
        pullback_618=pb618,
        pullback_low_used=pb_low,
        pullback_note=pb_note,
        weekly_boll_upper=weekly_upper,
        monthly_boll_mid=monthly_mid,
        cycle_high=cyc_hi,
        cycle_low=cyc_lo,
        cycle_382=cyc_382,
        cycle_500=cyc_500,
        cycle_618=cyc_618,
        bear_bottom_618=bear_618,
        round_level=round_level,
        barrier_below=barrier_below,
        barrier_above=barrier_above,
        boll_4h_lower=b4_lo,
        boll_4h_mid=b4_mid,
        boll_4h_upper=b4_up,
        boll_1h_lower=b1_lo,
        boll_1h_mid=b1_mid,
        boll_1h_upper=b1_up,
        boll_12h_lower=b12_lo,
        boll_12h_mid=b12_mid,
        boll_12h_upper=b12_up,
        pivot_supports=piv_sup,
        pivot_resistances=piv_res,
        ext_150=ext150,
        ext_1618=ext1618,
        playbook_line=playbook,
        summary_line=summary,
    )
