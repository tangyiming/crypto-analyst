"""CycleStudies「波段过滤器 | 百万Eric」的可计算近似。

闭源脚本没有公开源码；本模块的参数来自 2026-08 对其截图/推文标注做的逆向拟合
（见 memory: eric-band-filter-reverse），已确认的性质：

- 读数**严格有界 [-50, +50]**，阈值 ±40 出红/绿柱；-50.00 精确出现在收盘价创新低的 K
- 不是 LazyBear WT(10,21) 的同标度版本（2025-04-07 周线超卖时 WT(10,21) 只有 -14），
  也不是 RSI / CCI÷2.5（ChatGPT V1）——后者周线 7 个标注只中 2 个且经常越过 ±50
- 阶梯线只是 TradingView step-line 样式，每档一根 K，不是状态机

对约 600 个候选参数组合的筛选里，同时满足「有界 ±50、创新低精确 -50、大部分时间在中间区」
这些形状性质，并对他 日线 7/7、周线 7/7、4h 13/16 个标注超卖事件召回的近似是：

    value = Stochastic(HLC3, length=14, smooth=5) − 50

即 HLC3 在 14 根内的相对位置（0..100）做 5 根 SMA 平滑再居中。事件密度约为他的 1.3~1.5 倍。
（另一个候选 clamp(2.6·WT(21,5)) 事件召回 100%，但截断后读数近似方波、中间区占比仅 11%，
形状失真，不采用。）它仍**不等于**原脚本 1:1 复刻；把它当「形状与触发时点接近」的工程近似使用。

滞回（对他标注段的实验，见 memory）：读数 ≤ -40 进入超卖状态、回到 ≥ -25 才退出（超买对称）。
一段极值只报一次事件；不加滞回时周线超卖会多报 3 段、拖尾长，加了以后 7/7 且零多报。
把进入阈值收紧到 ±45 或把平滑缩到 3 都会漏掉他「刚碰 -40」的信号，已证伪，不要再调。

周线超买门（ob_gate，2026-08 实验）：他的周线超买不是位置型——2024-02→04、2025-05→07 冲新高时读数只有
17–38。唯一在 2021 起做到 2/2 命中、0 误报且结束时间吻合的是通道偏离 Z26 = (close − SMA26)/σ26：
进入 z ≥ 2.93、退出 z ≤ 1.86（由截图拟合 Eric ≈ 14.1·z − 1.2 反推，冻结不调）。该标度不能迁移到
日线/4h（那两个周期 Stoch +40/+25 已 6/6、0.74），所以只在 timeframe == 1w 时启用；样本 n=2，
定位是「周线高风险区/阶段顶部过滤器」，不是逃顶指令。

信号语义（来自推文）：
- 「触发超卖」= 收盘读数 ≤ -40（柱出现）；同向连续第 n 次触发标注「多n」（二级/三级）
- 「反转超卖」= 处于超卖区且读数从低点拐头向上（他画蓝柱）
- 4h 超卖单独**没有**统计优势（中位收益≈0），他主要用它止盈/管空单；
  周线/日线 + 结构位 + 背离叠加才是他真正的进场组合
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from analyst.compute.indicators import ema
from analyst.data.fetcher import CandleSeries

# Eric 过滤器/波段计划只在这两个品种上完成了逆向标注对照与 2015–2026 回测；
# 其他品种（BNB +5%/50%、SOL -48%/29%）证伪或未验证，信号只作参考。
ERIC_VALIDATED_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT"})


def eric_symbol_validated(symbol: str | None) -> bool:
    """BTC/USDT、BTCUSDT、BTC/USDT:USDT 等写法统一后判断是否在验证名单。"""
    if not symbol:
        return False
    base = symbol.split(":")[0].replace("/", "").replace("-", "").upper()
    return base in ERIC_VALIDATED_SYMBOLS


ERIC_UNVALIDATED_NOTE = "⚠️ Eric 过滤器仅在 BTC/ETH 上验证有效（BNB/SOL 回测证伪），本品种信号仅供参考"
# Eric 分析只在日线、周线上运行（4h 超卖无统计优势；1h 及以下只是噪音）
ERIC_TIMEFRAMES = frozenset({"1d", "1w"})


def eric_scope_ok(symbol: str | None, timeframe: str | None) -> bool:
    """Eric 规则/波段计划的适用范围：BTC/ETH × 日线/周线。"""
    return eric_symbol_validated(symbol) and str(timeframe or "").lower() in ERIC_TIMEFRAMES

# 拟合得到的默认参数（channel_len = 随机指标回看根数，average_len = SMA 平滑根数）
DEFAULT_CHANNEL_LEN = 14
DEFAULT_AVERAGE_LEN = 5
CLIP = 50.0
# 周线超买门（Z26 通道偏离）默认参数，冻结
OB_GATE_LEN = 26
OB_GATE_ENTER_Z = 2.93
OB_GATE_EXIT_Z = 1.86
WEEKLY_TIMEFRAMES = ("1w", "w", "1week", "weekly")


def wavetrend_series(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    *,
    channel_len: int = 10,
    average_len: int = 21,
) -> list[float]:
    """LazyBear WaveTrend 主线（TCI），未缩放。"""
    n = len(closes)
    if n == 0:
        return []
    hlc3 = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes, strict=True)]
    esa = ema(hlc3, channel_len)
    d = [abs(hlc3[i] - esa[i]) for i in range(n)]
    de = ema(d, channel_len)
    ci = [
        0.0 if de[i] == 0 else (hlc3[i] - esa[i]) / (0.015 * de[i])
        for i in range(n)
    ]
    return ema(ci, average_len)


def band_filter_series(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    *,
    channel_len: int = DEFAULT_CHANNEL_LEN,
    average_len: int = DEFAULT_AVERAGE_LEN,
) -> list[float]:
    """波段过滤器近似值序列：SMA(Stoch(HLC3, channel_len), average_len) − 50，范围 [-50, 50]。

    前 channel_len + average_len − 2 根不足回看，用 0（中性）填充。
    """
    n = len(closes)
    if n == 0:
        return []
    hlc3 = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes, strict=True)]
    raw = [50.0] * n
    for i in range(channel_len - 1, n):
        window = hlc3[i - channel_len + 1 : i + 1]
        hh, ll = max(window), min(window)
        raw[i] = 50.0 if hh == ll else 100.0 * (hlc3[i] - ll) / (hh - ll)
    out = [0.0] * n
    if average_len <= 1:
        return [max(-CLIP, min(CLIP, r - 50.0)) for r in raw]
    for i in range(average_len - 1, n):
        v = sum(raw[i - average_len + 1 : i + 1]) / average_len - 50.0
        out[i] = max(-CLIP, min(CLIP, v))
    return out


def channel_z_series(closes: list[float], length: int = OB_GATE_LEN) -> list[float]:
    """Z_len = (close − SMA_len) / σ_len（总体标准差）；前 length−1 根为 0。"""
    n = len(closes)
    out = [0.0] * n
    for i in range(length - 1, n):
        w = closes[i - length + 1 : i + 1]
        m = sum(w) / length
        var = sum((x - m) ** 2 for x in w) / length
        sd = var ** 0.5
        out[i] = 0.0 if sd == 0 else (closes[i] - m) / sd
    return out


Zone = Literal["deep_os", "oversold", "neutral", "overbought", "deep_ob"]


@dataclass(frozen=True)
class BandFilterSnapshot:
    """单根收盘的过滤器读数。"""

    value: float
    prev: float
    zone: Zone
    entered_oversold: bool
    entered_deep_os: bool
    entered_overbought: bool
    entered_deep_ob: bool
    channel_len: int
    average_len: int
    # 同向连续触发计数：本根在超卖/超买区时，这是自上次相反信号以来的第几个同向事件段
    # （对应他图上的「多1/多2」「二级/三级超买」）；不在区内为 0
    streak: int = 0
    # 「反转超卖/超买」：仍在极值区，但读数已从低/高点拐头
    turned_up: bool = False
    turned_down: bool = False
    # 带滞回的状态：读数 ≤ os_level 进入 oversold，回到 ≥ os_exit 才退出（超买对称）。
    # entered_* 事件按该状态机的翻转判定，一段极值只报一次；zone 仍是无滞回的即时读数分区。
    state: Literal["oversold", "neutral", "overbought"] = "neutral"
    # 周线超买门：Z26 读数与是否启用（启用时 overbought 状态由 Z26 决定，而非 Stoch 读数）
    channel_z: float = 0.0
    ob_gate: bool = False


def classify_zone(
    value: float,
    *,
    os_level: float = -40.0,
    deep_os: float = -48.0,
    ob_level: float = 40.0,
    deep_ob: float = 48.0,
) -> Zone:
    if value <= deep_os:
        return "deep_os"
    if value <= os_level:
        return "oversold"
    if value >= deep_ob:
        return "deep_ob"
    if value >= ob_level:
        return "overbought"
    return "neutral"


def hysteresis_states(
    values: list[float],
    *,
    os_level: float,
    os_exit: float,
    ob_level: float,
    ob_exit: float,
    ob_series: list[float] | None = None,
) -> list[int]:
    """逐根状态：-1 超卖 / 0 中性 / +1 超买；进入用 os_level/ob_level，退出用 os_exit/ob_exit。

    ob_series 非空时，超买端改用该序列（如 Z26）判定 ob_level/ob_exit，超卖端仍用 values。
    """
    out: list[int] = []
    st = 0
    obs = ob_series if ob_series is not None else values
    for v, ov in zip(values, obs, strict=True):
        if st == -1:
            if v >= os_exit:
                st = 1 if ov >= ob_level else 0
        elif st == 1:
            if ov <= ob_exit:
                st = -1 if v <= os_level else 0
        else:
            if v <= os_level:
                st = -1
            elif ov >= ob_level:
                st = 1
        out.append(st)
    return out


def _streak(states: list[int]) -> int:
    """自最近一次相反方向状态以来，同向状态段的序号（当前不在极值状态则为 0）。"""
    side = states[-1]
    if side == 0:
        return 0
    count = 0
    prev = 0
    for st in states:
        if st == -side:
            count = 0
        elif st == side and prev != side:
            count += 1
        prev = st
    return count


def compute_band_filter(
    series: CandleSeries,
    *,
    channel_len: int = DEFAULT_CHANNEL_LEN,
    average_len: int = DEFAULT_AVERAGE_LEN,
    os_level: float = -40.0,
    deep_os: float = -48.0,
    ob_level: float = 40.0,
    deep_ob: float = 48.0,
    os_exit: float = -25.0,
    ob_exit: float = 25.0,
    ob_gate: bool | None = None,
) -> BandFilterSnapshot | None:
    """对最新收盘计算波段过滤器近似（含滞回状态机）。

    ob_gate: None = 按 series.timeframe 自动（周线启用 Z26 超买门），True/False 强制。
    """
    candles = series.candles
    if len(candles) < max(channel_len, average_len) + 5:
        return None
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    closes = [float(c.close) for c in candles]
    bf = band_filter_series(
        highs, lows, closes, channel_len=channel_len, average_len=average_len
    )
    cur, prev = bf[-1], bf[-2]
    zone = classify_zone(
        cur, os_level=os_level, deep_os=deep_os, ob_level=ob_level, deep_ob=deep_ob
    )
    if ob_gate is None:
        ob_gate = str(getattr(series, "timeframe", "")).lower() in WEEKLY_TIMEFRAMES
    zs = channel_z_series(closes, OB_GATE_LEN) if ob_gate else None
    win = 400
    states = hysteresis_states(
        bf[-win:],
        os_level=os_level,
        os_exit=os_exit,
        ob_level=OB_GATE_ENTER_Z if ob_gate else ob_level,
        ob_exit=OB_GATE_EXIT_Z if ob_gate else ob_exit,
        ob_series=zs[-win:] if zs is not None else None,
    )
    st, st_prev = states[-1], states[-2]
    state = {-1: "oversold", 0: "neutral", 1: "overbought"}[st]
    # 拐头：在极值状态内且当前值离开本段极值（用最近 3 根判断，避免单根抖动）
    turned_up = st == -1 and cur > prev and prev <= bf[-3]
    turned_down = st == 1 and cur < prev and prev >= bf[-3]
    if ob_gate:
        entered_deep_ob = st == 1 and zs[-2] < OB_GATE_ENTER_Z + 0.5 and zs[-1] >= OB_GATE_ENTER_Z + 0.5
    else:
        entered_deep_ob = st == 1 and prev < deep_ob and cur >= deep_ob
    return BandFilterSnapshot(
        value=cur,
        prev=prev,
        zone=zone,
        entered_oversold=st == -1 and st_prev != -1,
        entered_deep_os=st == -1 and prev > deep_os and cur <= deep_os,
        entered_overbought=st == 1 and st_prev != 1,
        entered_deep_ob=entered_deep_ob,
        channel_len=channel_len,
        average_len=average_len,
        streak=_streak(states),
        turned_up=turned_up,
        turned_down=turned_down,
        state=state,  # type: ignore[arg-type]
        channel_z=zs[-1] if zs is not None else 0.0,
        ob_gate=ob_gate,
    )
