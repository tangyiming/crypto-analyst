"""双线反转策略（K 线形态，非均线交叉）。

来源：加密大漂亮《AI 交易机器人实战》
https://www.youtube.com/watch?v=fqK-3LK_kF0
（yt-dlp + Whisper 转写核验）

形态定义（视频口述）：
- 看涨：阴线后接阳线；看跌：阳线后接阴线
- 三要件：①突变（跳出整理）②两根各自方向都强势 ③实体/区间大面积重合
- 入场：看跌跌破两线最低点；看涨突破两线最高点
- 止损：双线外侧 + 缓冲（视频默认约 2%）
- 止盈：按 R（入场到止损距离）；视频先定 2R，另有 8R+移动止损方案
- 趋势过滤：EMA200 之上只做多、之下只做空
- Kelly：盈利后按凯利思路加仓（本模块给出建议仓位，不自动下单）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from analyst.compute.indicators import ema
from analyst.compute.kelly import KellySize, suggest_position
from analyst.compute.plan import TradePlan, calculate_rr, _wait_plan
from analyst.compute.volume import analyze_volume
from analyst.data.fetcher import Candle, CandleSeries


@dataclass(frozen=True)
class DoubleLineConfig:
    """双线反转参数（对齐视频默认，可 .env / CLI 覆盖）。"""

    # 形态强度
    min_body_ratio: float = 0.55       # |close-open| / (high-low)
    min_overlap_ratio: float = 0.50    # 两根 K 区间重合 / 较短那根区间
    min_sudden_atr_mult: float = 1.2   # 两根合并振幅 ≥ ATR(14) * 该倍数 → 突变
    atr_period: int = 14

    # 执行
    stop_buffer_pct: float = 2.0       # 视频：缓冲约 2%
    take_profit_r: float = 2.0         # 视频回测性价比最优档
    trail_to_8r: bool = False          # True 时给出 8R 移动止损说明
    ema_trend_period: int = 200
    require_ema200: bool = True

    # 可选过滤器（她人工盯盘时会看）
    require_volume: bool = False
    require_fib_zone: bool = False     # 兼容旧 CLI；默认关，形态自带入场位

    assumed_win_rate: float = 0.47     # 视频含趋势过滤后回测约 47%
    kelly_scale: float = 0.25


@dataclass
class PatternBars:
    first: Candle
    second: Candle
    direction: str                     # long / short
    overlap_ratio: float
    body_ratio_avg: float
    sudden_score: float
    break_level: float                 # 突破入场触发价
    pattern_high: float
    pattern_low: float


@dataclass
class DoubleLineSignal:
    """实时监控可用的结构化信号。"""

    direction: str                     # long / short / wait
    strength: float
    price: float
    pattern: str | None                # bullish_engulf_pair / bearish_...
    break_level: float | None
    reasons: list[str] = field(default_factory=list)
    filters_passed: list[str] = field(default_factory=list)
    filters_failed: list[str] = field(default_factory=list)
    plan: TradePlan | None = None
    kelly: KellySize | None = None
    trail_note: str | None = None
    bar_ts: object | None = None
    # 兼容旧字段名（CLI / notifier 若引用）
    ema_cross: str | None = None
    macd_cross: str | None = None


def _body_ratio(c: Candle) -> float:
    rng = c.high - c.low
    if rng <= 0:
        return 0.0
    return abs(c.close - c.open) / rng


def _is_bull(c: Candle) -> bool:
    return c.close > c.open


def _is_bear(c: Candle) -> bool:
    return c.close < c.open


def _overlap_ratio(a: Candle, b: Candle) -> float:
    lo = max(a.low, b.low)
    hi = min(a.high, b.high)
    overlap = max(0.0, hi - lo)
    short = min(a.high - a.low, b.high - b.low)
    if short <= 0:
        return 0.0
    return overlap / short


def _atr(candles: list[Candle], period: int) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        tr = max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        trs.append(tr)
    window = trs[-period:]
    return sum(window) / len(window) if window else 0.0


def detect_pattern(
    candles: list[Candle],
    cfg: DoubleLineConfig,
) -> PatternBars | None:
    """在最近两根**已收盘** K 上检测双线反转形态。"""
    if len(candles) < cfg.atr_period + 2:
        return None
    c1, c2 = candles[-2], candles[-1]
    atr = _atr(candles, cfg.atr_period)

    if _is_bear(c1) and _is_bull(c2):
        direction = "long"
    elif _is_bull(c1) and _is_bear(c2):
        direction = "short"
    else:
        return None

    b1, b2 = _body_ratio(c1), _body_ratio(c2)
    if b1 < cfg.min_body_ratio or b2 < cfg.min_body_ratio:
        return None

    ov = _overlap_ratio(c1, c2)
    if ov < cfg.min_overlap_ratio:
        return None

    span = max(c1.high, c2.high) - min(c1.low, c2.low)
    sudden = span / atr if atr > 0 else 0.0
    if sudden < cfg.min_sudden_atr_mult:
        return None

    ph, pl = max(c1.high, c2.high), min(c1.low, c2.low)
    break_level = ph if direction == "long" else pl
    return PatternBars(
        first=c1,
        second=c2,
        direction=direction,
        overlap_ratio=ov,
        body_ratio_avg=(b1 + b2) / 2,
        sudden_score=sudden,
        break_level=break_level,
        pattern_high=ph,
        pattern_low=pl,
    )


def _trail_note(entry: float, stop: float, direction: str, enabled: bool) -> str | None:
    if not enabled:
        return None
    r = abs(entry - stop)
    if r <= 0:
        return None
    if direction == "long":
        return (
            f"8R 移动止损方案：浮盈 2R→止损移到入场+1R；"
            f"4R→+2R；8R→+4R（R={r:.2f}）"
        )
    return (
        f"8R 移动止损方案：浮盈 2R→止损移到入场-1R；"
        f"4R→-2R；8R→-4R（R={r:.2f}）"
    )


def _build_plan(
    price: float,
    pattern: PatternBars,
    cfg: DoubleLineConfig,
    reasons: list[str],
) -> TradePlan:
    buf = cfg.stop_buffer_pct / 100.0
    if pattern.direction == "long":
        # 突破入场：用形态高点（或现价若已突破）
        entry = max(price, pattern.break_level)
        stop = pattern.pattern_low * (1.0 - buf)
        r = abs(entry - stop)
        tp1 = entry + cfg.take_profit_r * r
        tp2 = entry + 8.0 * r if cfg.trail_to_8r else entry + 3.0 * r
        rr = calculate_rr(entry, stop, tp1, "long")
        return TradePlan(
            direction="long",
            entry_low=pattern.break_level,
            entry_high=entry,
            stop_loss=stop,
            take_profit_1=tp1,
            take_profit_2=tp2,
            rr_ratio=rr,
            rationale=" | ".join(reasons),
        )

    entry = min(price, pattern.break_level)
    stop = pattern.pattern_high * (1.0 + buf)
    r = abs(entry - stop)
    tp1 = entry - cfg.take_profit_r * r
    tp2 = entry - 8.0 * r if cfg.trail_to_8r else entry - 3.0 * r
    rr = calculate_rr(entry, stop, tp1, "short")
    return TradePlan(
        direction="short",
        entry_low=entry,
        entry_high=pattern.break_level,
        stop_loss=stop,
        take_profit_1=tp1,
        take_profit_2=tp2,
        rr_ratio=rr,
        rationale=" | ".join(reasons),
    )


def evaluate_double_line(
    series: CandleSeries,
    cfg: DoubleLineConfig | None = None,
    **_ignored: object,
) -> DoubleLineSignal:
    """评估最近收盘 K 是否形成双线反转，以及是否触发/接近突破入场。"""
    cfg = cfg or DoubleLineConfig()
    candles = series.candles
    price = series.latest_close if candles else 0.0
    bar_ts = series.latest.timestamp if candles else None

    if len(candles) < cfg.atr_period + 2:
        return DoubleLineSignal(
            direction="wait",
            strength=0.0,
            price=price,
            pattern=None,
            break_level=None,
            reasons=["数据不足"],
            bar_ts=bar_ts,
        )

    # 用「倒数第二、三根」当作已确认形态，最新一根用于判断是否突破
    # （实时：最新根可能未收盘；引擎在 closed 时再评）
    if len(candles) < cfg.atr_period + 3:
        hist = candles
        live = candles[-1]
        pattern = detect_pattern(hist, cfg)
    else:
        hist = candles[:-1]
        live = candles[-1]
        pattern = detect_pattern(hist, cfg)

    if pattern is None:
        # 也可能形态就在最新两根（刚收盘那根）
        pattern = detect_pattern(candles, cfg)
        live = candles[-1]
        if pattern is None:
            return DoubleLineSignal(
                direction="wait",
                strength=0.0,
                price=price,
                pattern=None,
                break_level=None,
                reasons=["未识别到符合三要件的双线反转"],
                bar_ts=bar_ts,
            )

    name = "bullish_double_line" if pattern.direction == "long" else "bearish_double_line"
    reasons = [
        f"{'看涨' if pattern.direction == 'long' else '看跌'}双线反转",
        f"重合={pattern.overlap_ratio:.0%} 实体均值={pattern.body_ratio_avg:.0%} "
        f"突变={pattern.sudden_score:.1f}×ATR",
        f"突破位={pattern.break_level:.2f}",
    ]
    passed = ["pattern"]
    failed: list[str] = []

    if cfg.require_ema200:
        closes = [c.close for c in candles]
        e200 = ema(closes, cfg.ema_trend_period)[-1]
        if pattern.direction == "long" and price < e200:
            failed.append("ema200")
            return DoubleLineSignal(
                direction="wait",
                strength=0.35,
                price=price,
                pattern=name,
                break_level=pattern.break_level,
                reasons=reasons + [f"价格 {price:.2f} < EMA{cfg.ema_trend_period} {e200:.2f}，只做多过滤"],
                filters_passed=passed,
                filters_failed=failed,
                bar_ts=bar_ts,
            )
        if pattern.direction == "short" and price > e200:
            failed.append("ema200")
            return DoubleLineSignal(
                direction="wait",
                strength=0.35,
                price=price,
                pattern=name,
                break_level=pattern.break_level,
                reasons=reasons + [f"价格 {price:.2f} > EMA{cfg.ema_trend_period} {e200:.2f}，只做空过滤"],
                filters_passed=passed,
                filters_failed=failed,
                bar_ts=bar_ts,
            )
        passed.append("ema200")
        reasons.append(f"EMA{cfg.ema_trend_period} 顺势")

    if cfg.require_volume:
        vol = analyze_volume(series)
        sig = vol.price_volume_signal
        bad = (
            (pattern.direction == "long" and ("顶背离" in sig or "价跌量增" in sig))
            or (pattern.direction == "short" and "量价齐升" in sig)
        )
        if bad:
            failed.append("volume")
            return DoubleLineSignal(
                direction="wait",
                strength=0.4,
                price=price,
                pattern=name,
                break_level=pattern.break_level,
                reasons=reasons + [f"量能冲突：{sig}"],
                filters_passed=passed,
                filters_failed=failed,
                bar_ts=bar_ts,
            )
        passed.append("volume")

    # 突破判定：现价需穿越 break_level（允许刚收盘刺破）
    triggered = (
        (pattern.direction == "long" and live.close >= pattern.break_level)
        or (pattern.direction == "short" and live.close <= pattern.break_level)
    )
    if not triggered:
        return DoubleLineSignal(
            direction="wait",
            strength=0.55,
            price=price,
            pattern=name,
            break_level=pattern.break_level,
            reasons=reasons + ["形态成立，等待价格突破入场位"],
            filters_passed=passed,
            filters_failed=failed + ["breakout"],
            bar_ts=bar_ts,
        )
    passed.append("breakout")

    plan = _build_plan(price, pattern, cfg, reasons)
    if plan.rr_ratio < cfg.take_profit_r * 0.99:
        # 理论上 rr ≈ take_profit_r；若止损过近异常则观望
        return DoubleLineSignal(
            direction="wait",
            strength=0.5,
            price=price,
            pattern=name,
            break_level=pattern.break_level,
            reasons=reasons + [f"R:R 异常 {plan.rr_ratio:.2f}"],
            filters_passed=passed,
            filters_failed=failed + ["rr"],
            plan=plan,
            bar_ts=bar_ts,
        )

    kelly = suggest_position(
        cfg.assumed_win_rate,
        max(plan.rr_ratio, 0.01),
        kelly_scale=cfg.kelly_scale,
    )
    # 覆盖 note：强调视频里 Kelly 是盈利加仓
    kelly = KellySize(
        win_rate=kelly.win_rate,
        payoff_ratio=kelly.payoff_ratio,
        full_kelly=kelly.full_kelly,
        fraction=kelly.fraction,
        suggested_fraction=kelly.suggested_fraction,
        risk_budget_pct=kelly.risk_budget_pct,
        note=kelly.note + " 视频方案：浮盈后可按凯利思路加仓（需人工确认）。",
    )

    strength = min(
        1.0,
        0.5
        + 0.15 * min(pattern.overlap_ratio, 1.0)
        + 0.1 * min(pattern.body_ratio_avg, 1.0)
        + 0.05 * min(pattern.sudden_score / 3.0, 1.0),
    )

    return DoubleLineSignal(
        direction=pattern.direction,
        strength=strength,
        price=price,
        pattern=name,
        break_level=pattern.break_level,
        reasons=reasons + ["已触发突破入场"],
        filters_passed=passed,
        filters_failed=failed,
        plan=plan,
        kelly=kelly,
        trail_note=_trail_note(
            (plan.entry_low + plan.entry_high) / 2,
            plan.stop_loss,
            pattern.direction,
            cfg.trail_to_8r,
        ),
        bar_ts=bar_ts,
        ema_cross="golden" if pattern.direction == "long" else "death",
        macd_cross=None,
    )
