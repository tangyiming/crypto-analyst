"""零下二度风格「波段锁点」预计算。

把可复现的点位公式放在代码里，再以短字段注入 LLM user 模板，
避免把整套方法论 thrash 进 system prompt。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from analyst.compute.fibonacci import FibLevels, compute_fib
from analyst.compute.structure import Structure
from analyst.data.fetcher import CandleSeries


@dataclass
class JackLevels:
    """预计算锁点（供基线计划 / AI 模板 / Web 展示）。"""

    swing_high: float
    swing_low: float
    rebound_382: float
    rebound_500: float
    rebound_618: float
    retr_382: float
    retr_618: float
    boll_mid: float | None
    confluence_382: bool
    confluence_618: bool
    daily_bias: str  # up / down / range
    defense_level: float
    htf_ready: bool
    horizon: str  # swing / short
    touch_level: float | None
    touch_count: int
    rs_note: str
    summary_line: str

    def to_dict(self) -> dict:
        return asdict(self)

    def prompt_block(self, compact: bool = False) -> str:
        """注入 user 模板的短文本。"""
        conf = []
        if self.confluence_382:
            conf.append("0.382↔BOLL中轨")
        if self.confluence_618:
            conf.append("0.618↔BOLL中轨")
        conf_s = "、".join(conf) if conf else "无"
        touch = (
            f"{_fmt(self.touch_level)}×{self.touch_count}"
            if self.touch_level is not None and self.touch_count > 0
            else "无"
        )
        if compact:
            return (
                f"锁点 H/L={_fmt(self.swing_high)}/{_fmt(self.swing_low)} "
                f"反抽0.382={_fmt(self.rebound_382)} 目标0.618={_fmt(self.rebound_618)} "
                f"BOLL中={_fmt(self.boll_mid) if self.boll_mid is not None else 'N/A'} "
                f"共振={conf_s} 日线={self.daily_bias} 防守={_fmt(self.defense_level)} "
                f"高周期成熟={self.htf_ready}/{self.horizon} 触及={touch} RS={self.rs_note}"
            )
        return (
            f"- 波段高/低：{_fmt(self.swing_high)} / {_fmt(self.swing_low)}\n"
            f"- 反抽阻力 0.382：{_fmt(self.rebound_382)}（近压）\n"
            f"- 反弹目标 0.618：{_fmt(self.rebound_618)}（主目标）\n"
            f"- 回撤支撑 0.382/0.618：{_fmt(self.retr_382)} / {_fmt(self.retr_618)}\n"
            f"- BOLL 中轨：{_fmt(self.boll_mid) if self.boll_mid is not None else 'N/A'} · 共振={conf_s}\n"
            f"- 日线定调：{self.daily_bias} · 防守失效：{_fmt(self.defense_level)}\n"
            f"- 高周期成熟：{self.htf_ready} · 建议视野：{self.horizon}\n"
            f"- 关键位触及：{touch}（二破/回踩站稳优先）\n"
            f"- vs BTC：{self.rs_note}\n"
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


def _near(a: float, b: float | None, pct: float = 0.012) -> bool:
    if b is None or a <= 0 or b <= 0:
        return False
    return abs(a - b) / b <= pct


def _daily_bias_from_indicators(daily: dict | None, structure: Structure) -> str:
    if structure.trend in ("up", "down"):
        # 结构优先，指标辅助
        if not daily:
            return structure.trend
    if not daily:
        return structure.trend if structure.trend != "range" else "range"
    ema = daily.get("ema") or {}
    macd = daily.get("macd") or {}
    e7 = ema.get("ema7")
    e30 = ema.get("ema30")
    hist = macd.get("histogram")
    above = macd.get("above_zero")
    score = 0
    if e7 is not None and e30 is not None:
        score += 1 if e7 > e30 else -1
    if hist is not None:
        score += 1 if hist > 0 else -1
    if above is True:
        score += 1
    elif above is False:
        score -= 1
    if score >= 2:
        return "up"
    if score <= -2:
        return "down"
    return structure.trend if structure.trend != "range" else "range"


def _htf_ready(daily: dict | None) -> bool:
    """用日线 MACD 近似「周线级动能是否成熟」：零轴附近金叉/柱翻红。"""
    if not daily:
        return False
    macd = daily.get("macd") or {}
    sig = str(macd.get("cross_signal") or "")
    hist = macd.get("histogram")
    above = macd.get("above_zero")
    if "golden" in sig.lower() or "金叉" in sig:
        return True
    if hist is not None and hist > 0 and above is not False:
        return True
    # 柱由负转正途中也算接近成熟
    if hist is not None and abs(float(hist)) > 0 and above is False and hist > 0:
        return True
    return False


def _count_level_touches(
    series: CandleSeries | None,
    level: float,
    *,
    lookback: int = 40,
    tol_pct: float = 0.004,
) -> int:
    """统计近期高点刺破/触及某阻力的次数（二次突破用）。"""
    if not series or not series.candles or level <= 0:
        return 0
    candles = series.candles[-lookback:]
    count = 0
    prev_above = False
    for c in candles:
        pierced = c.high >= level * (1 - tol_pct)
        closed_above = c.close >= level * (1 - tol_pct)
        # 一次「触及」：刺破或收上；连续多根只计一次进入
        above = pierced or closed_above
        if above and not prev_above:
            count += 1
        prev_above = above
    return count


def _rs_note(
    symbol: str,
    series: CandleSeries | None,
    btc_series: CandleSeries | None,
) -> str:
    if "BTC" in (symbol or "").upper().replace("/", ""):
        return "标的即 BTC"
    if not series or not btc_series or len(series.candles) < 10 or len(btc_series.candles) < 10:
        return "暂无对比"
    n = min(20, len(series.candles), len(btc_series.candles))
    a = series.candles[-n:]
    b = btc_series.candles[-n:]
    a_low = min(c.low for c in a)
    b_low = min(c.low for c in b)
    a_ret = (a[-1].close / a[0].close - 1.0) * 100
    b_ret = (b[-1].close / b[0].close - 1.0) * 100
    # 低点是否抬升：用相对窗口起点
    a_first_low = min(c.low for c in a[: max(3, n // 3)])
    b_first_low = min(c.low for c in b[: max(3, n // 3)])
    a_hl = a_low > a_first_low * 0.998
    b_hl = b_low > b_first_low * 0.998
    if a_ret > b_ret + 1 and (a_hl or not b_hl):
        return f"强于BTC（近窗收益 {a_ret:+.1f}% vs {b_ret:+.1f}%）"
    if a_ret < b_ret - 1:
        return f"弱于BTC（近窗收益 {a_ret:+.1f}% vs {b_ret:+.1f}%）"
    return f"与BTC同步（近窗 {a_ret:+.1f}% vs {b_ret:+.1f}%）"


def compute_jack_levels(
    *,
    current_price: float,
    structure: Structure,
    fib: FibLevels | None = None,
    daily_indicators: dict | None = None,
    primary_series: CandleSeries | None = None,
    btc_series: CandleSeries | None = None,
    symbol: str = "",
    confluence_pct: float = 0.012,
) -> JackLevels:
    """从结构/斐波/日线指标计算锁点。"""
    fib = fib or compute_fib(structure.recent_high, structure.recent_low)
    boll_mid = None
    if daily_indicators:
        boll = daily_indicators.get("boll") or {}
        mid = boll.get("middle")
        if mid is not None:
            boll_mid = float(mid)

    daily_bias = _daily_bias_from_indicators(daily_indicators, structure)
    htf_ready = _htf_ready(daily_indicators)
    horizon = "swing" if htf_ready else "short"

    # 防守位：上涨用最近支撑/回撤 0.786；下跌用最近阻力
    if daily_bias == "up":
        defense = structure.supports[0] if structure.supports else fib.retr_786
    elif daily_bias == "down":
        defense = structure.resistances[0] if structure.resistances else fib.rebound_786
    else:
        defense = structure.key_pivot or current_price

    # 关键阻力：优先结构阻力，否则反弹 0.382
    touch_level = structure.resistances[0] if structure.resistances else fib.rebound_382
    touch_count = _count_level_touches(primary_series, touch_level)

    conf_382 = _near(fib.rebound_382, boll_mid, confluence_pct)
    conf_618 = _near(fib.rebound_618, boll_mid, confluence_pct)

    rs = _rs_note(symbol, primary_series, btc_series)

    if daily_bias == "up":
        summary = (
            f"日线偏多，策略偏「低多」；近压 {_fmt(fib.rebound_382)}，"
            f"主目标 {_fmt(fib.rebound_618)}；破 {_fmt(defense)} 失效"
        )
    elif daily_bias == "down":
        summary = (
            f"日线偏空，策略偏「高空」；反弹区关注 {_fmt(fib.rebound_500)}-{_fmt(fib.rebound_618)}；"
            f"破 {_fmt(defense)} 失效"
        )
    else:
        summary = (
            f"日线震荡；反抽看 {_fmt(fib.rebound_382)}，大反弹看 {_fmt(fib.rebound_618)}；"
            f"等边界再动手"
        )
    if not htf_ready:
        summary += "；高周期未成熟→只做短线反抽"
    if touch_count >= 2:
        summary += f"；关键位 {_fmt(touch_level)} 已第 {touch_count} 次触及"

    return JackLevels(
        swing_high=fib.high,
        swing_low=fib.low,
        rebound_382=fib.rebound_382,
        rebound_500=fib.rebound_500,
        rebound_618=fib.rebound_618,
        retr_382=fib.retr_382,
        retr_618=fib.retr_618,
        boll_mid=boll_mid,
        confluence_382=conf_382,
        confluence_618=conf_618,
        daily_bias=daily_bias,
        defense_level=float(defense),
        htf_ready=htf_ready,
        horizon=horizon,
        touch_level=float(touch_level) if touch_level else None,
        touch_count=touch_count,
        rs_note=rs,
        summary_line=summary,
    )
