"""向前回放回测引擎：规则告警前瞻命中率。

在 K 线收盘逐根回放 evaluate_closed_bar_rules，对每个带方向的告警
用「ATR 屏障」前瞻验证：未来 horizon 根内先走出 +1×ATR（顺方向）算命中，
先走出 -1×ATR（逆方向）算打脸，都没到算 flat。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from analyst.data.fetcher import Candle, CandleSeries, fetch_candles
from analyst.monitor.rules import RuleConfig, evaluate_closed_bar_rules

# 指标窗口最少需要的历史根数
WARMUP_BARS = 60


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


@dataclass
class RuleStat:
    """单条规则的前瞻统计。"""

    rule: str
    n: int = 0
    wins: int = 0
    losses: int = 0
    flat: int = 0
    sum_fwd_ret_pct: float = 0.0   # 按方向符号化的前瞻收益合计

    @property
    def win_rate(self) -> float:
        resolved = self.wins + self.losses
        return self.wins / resolved if resolved else 0.0

    @property
    def avg_fwd_ret_pct(self) -> float:
        return self.sum_fwd_ret_pct / self.n if self.n else 0.0


@dataclass
class BacktestReport:
    symbol: str
    timeframe: str
    bars: int
    start: datetime | None
    end: datetime | None
    rule_stats: dict[str, RuleStat] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars": self.bars,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "rules": {
                name: {
                    "n": st.n,
                    "wins": st.wins,
                    "losses": st.losses,
                    "flat": st.flat,
                    "win_rate": round(st.win_rate, 4),
                    "avg_fwd_ret_pct": round(st.avg_fwd_ret_pct, 4),
                }
                for name, st in sorted(self.rule_stats.items())
            },
        }


def _rule_forward_outcome(
    candles: list,
    idx: int,
    direction: str,
    horizon: int,
    atr: float,
) -> tuple[str, float]:
    """ATR 屏障前瞻：先 +1×ATR 顺向 → win；先 1×ATR 逆向 → loss；否则 flat。

    返回 (outcome, 按方向符号化的前瞻收益%)。
    """
    price = candles[idx].close
    if price <= 0 or atr <= 0:
        return "flat", 0.0
    up_barrier = price + atr
    dn_barrier = price - atr
    end = min(len(candles), idx + 1 + horizon)
    outcome = "flat"
    for i in range(idx + 1, end):
        c = candles[i]
        if direction == "long":
            if c.low <= dn_barrier:   # 保守：同根先算逆向
                outcome = "loss"
                break
            if c.high >= up_barrier:
                outcome = "win"
                break
        else:
            if c.high >= up_barrier:
                outcome = "loss"
                break
            if c.low <= dn_barrier:
                outcome = "win"
                break

    last = candles[end - 1].close if end - 1 > idx else price
    ret_pct = (last - price) / price * 100.0
    if direction == "short":
        ret_pct = -ret_pct
    return outcome, ret_pct


def run_backtest(
    symbol: str,
    timeframe: str = "15m",
    *,
    bars: int = 1000,
    market: str = "futures",
    rule_cfg: RuleConfig | None = None,
    include_rules: bool = True,
    rule_horizon: int = 12,
    series: CandleSeries | None = None,
) -> BacktestReport:
    """对单品种单周期做规则告警前瞻回放。

    Args:
        bars: 拉取历史根数（binance 单次上限 1500）
        rule_horizon: 规则前瞻窗口（根）
        series: 传入现成 K 线（测试用）；否则 REST 拉取
    """
    rule_cfg = rule_cfg or RuleConfig()
    if series is None:
        series = fetch_candles(
            symbol,
            timeframe=timeframe,
            limit=min(bars, 1500),
            use_cache=False,
            market=market,
        )
    candles = series.candles
    report = BacktestReport(
        symbol=series.symbol,
        timeframe=series.timeframe,
        bars=len(candles),
        start=candles[0].timestamp if candles else None,
        end=candles[-1].timestamp if candles else None,
    )
    if len(candles) <= WARMUP_BARS or not include_rules:
        return report

    rule_state: dict = {}

    for i in range(WARMUP_BARS, len(candles)):
        full_window = CandleSeries(
            symbol=series.symbol,
            timeframe=series.timeframe,
            candles=candles[: i + 1],
        )
        events, rule_state = evaluate_closed_bar_rules(
            full_window, rule_state, rule_cfg
        )
        if events:
            atr = _atr(candles[max(0, i - 50) : i + 1], 14)
            for ev in events:
                if ev.direction not in ("long", "short"):
                    continue
                outcome, ret_pct = _rule_forward_outcome(
                    candles, i, ev.direction, rule_horizon, atr
                )
                st = report.rule_stats.setdefault(ev.rule, RuleStat(rule=ev.rule))
                st.n += 1
                st.sum_fwd_ret_pct += ret_pct
                if outcome == "win":
                    st.wins += 1
                elif outcome == "loss":
                    st.losses += 1
                else:
                    st.flat += 1

    return report
