"""纸面交易引擎（Paper Broker）。

消费策略给出的 OrderPlan（限价入场 / 止损 / 一半止盈），用实时标记价撮合，管理持仓、
保本损、余仓离场、日内熔断，状态持久化到 .cache/data/paper_state.json。

- `submit(plan)`：登记挂单（同品种同时只允许一张挂单或一仓；熔断暂停期拒单）
- `on_mark(symbol, price, ts)`：每个标记价 tick 调用——限价成交、止损、第一止盈（并上移止损到成本）
- `on_bar_close(symbol, price, reg, ts)`：4h 收盘调用——TP1 后余仓按规则离场、挂单过期、超时离场
- 日内熔断：权益较当日起点回撤 ≥ daily_fuse_pct → 全平并暂停到次日 00:00 UTC

所有撮合都产出 PaperEvent，由 hub 转成页面/TG 告警（规则名 paper_*）。
纸面成交按「触价即成交」加固定滑点，比真实略乐观；用于验证信号实时性与规则，不用于夸大收益。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from analyst.compute.strategies.jack_pullback import (
    MAKER_FEE,
    SLIPPAGE,
    TAKER_FEE,
    JackPullbackConfig,
    OrderPlan,
    position_size,
)

logger = logging.getLogger(__name__)


@dataclass
class PaperEvent:
    kind: str            # submit / fill / tp1 / stop / exit / expire / fuse / reject
    symbol: str
    price: float
    qty: float = 0.0
    pnl_usd: float | None = None
    text: str = ""
    ts: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso(ts: datetime | None = None) -> str:
    return (ts or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


class PaperBroker:
    def __init__(
        self,
        state_path: Path,
        *,
        equity0: float = 10_000.0,
        cfg: JackPullbackConfig | None = None,
        daily_fuse_pct: float = 3.0,
        max_positions: int = 2,
    ) -> None:
        self.path = Path(state_path)
        self.cfg = cfg or JackPullbackConfig()
        self.daily_fuse_pct = daily_fuse_pct
        self.max_positions = max_positions
        self.state: dict[str, Any] = {
            "equity": equity0,
            "equity0": equity0,
            "positions": {},
            "pending": {},
            "closed": [],
            "day": None,
            "day_start_equity": equity0,
            "paused_until": None,
            "journal": [],
        }
        self.load()

    # ── 持久化 ──
    def load(self) -> None:
        try:
            if self.path.is_file():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and "equity" in raw:
                    self.state.update(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning("paper state load failed: %s", e)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            st = dict(self.state)
            st["journal"] = st["journal"][-500:]
            st["closed"] = st["closed"][-300:]
            self.path.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning("paper state save failed: %s", e)

    # ── 工具 ──
    @property
    def equity(self) -> float:
        return float(self.state["equity"])

    def _paused(self, ts: datetime) -> bool:
        pu = self.state.get("paused_until")
        return bool(pu) and ts < datetime.fromisoformat(pu)

    def _roll_day(self, ts: datetime) -> None:
        day = ts.astimezone(timezone.utc).strftime("%Y-%m-%d")
        if self.state.get("day") != day:
            self.state["day"] = day
            self.state["day_start_equity"] = self.equity

    def _log(self, ev: PaperEvent) -> PaperEvent:
        self.state["journal"].append(ev.to_dict())
        return ev

    # ── 挂单 ──
    def submit(self, plan: OrderPlan, ts: datetime | None = None) -> PaperEvent | None:
        ts = ts or datetime.now(timezone.utc)
        self._roll_day(ts)
        sym = plan.symbol
        if self._paused(ts):
            return self._log(PaperEvent("reject", sym, plan.entry, text="熔断暂停期，拒绝新挂单", ts=_now_iso(ts)))
        if sym in self.state["positions"]:
            return None
        if len(self.state["positions"]) >= self.max_positions:
            return self._log(PaperEvent("reject", sym, plan.entry, text=f"已达最大持仓数 {self.max_positions}", ts=_now_iso(ts)))
        prev = self.state["pending"].get(sym)
        if prev and abs(prev["entry"] / plan.entry - 1) < 0.002:
            return None  # 同一计划重复提交
        qty = position_size(self.equity, plan, self.cfg)
        if qty <= 0:
            return None
        d = plan.to_dict()
        d["qty"] = qty
        self.state["pending"][sym] = d
        self.save()
        return self._log(
            PaperEvent(
                "submit", sym, plan.entry, qty,
                text=f"挂限价多 {plan.entry:.6g} × {qty:.4g}（名义 {qty*plan.entry:,.0f}U）· 止损 {plan.stop:.6g} · 一半止盈 {plan.tp1:.6g} · RR {plan.rr:.1f} · 有效至 {plan.expires_at:%m-%d %H:%M}",
                ts=_now_iso(ts), extra={"plan": d},
            )
        )

    # ── 标记价撮合 ──
    def on_mark(self, symbol: str, price: float, ts: datetime | None = None) -> list[PaperEvent]:
        ts = ts or datetime.now(timezone.utc)
        self._roll_day(ts)
        events: list[PaperEvent] = []
        pend = self.state["pending"].get(symbol)
        pos = self.state["positions"].get(symbol)
        if pend and not pos:
            if ts >= datetime.fromisoformat(pend["expires_at"]):
                del self.state["pending"][symbol]
                events.append(self._log(PaperEvent("expire", symbol, price, text=f"挂单 {pend['entry']:.6g} 过期未成交", ts=_now_iso(ts))))
            elif price <= pend["entry"]:
                qty = float(pend["qty"])
                fee = qty * pend["entry"] * MAKER_FEE
                self.state["equity"] -= fee
                pos = {
                    "symbol": symbol, "side": "long", "qty": qty, "qty0": qty, "entry": pend["entry"],
                    "stop": pend["stop"], "tp1": pend["tp1"], "tp1_done": False, "opened_at": _now_iso(ts),
                    "fees": fee, "realized": 0.0, "peak": price, "plan": pend,
                }
                self.state["positions"][symbol] = pos
                del self.state["pending"][symbol]
                events.append(self._log(PaperEvent("fill", symbol, pend["entry"], qty, text=f"限价成交 {pend['entry']:.6g} × {qty:.4g}；止损 {pend['stop']:.6g} · 一半止盈 {pend['tp1']:.6g}", ts=_now_iso(ts))))
        if pos:
            pos["peak"] = max(float(pos.get("peak") or price), price)
            if price <= pos["stop"]:
                px = pos["stop"] * (1 - SLIPPAGE)
                events.append(self._close(symbol, pos, pos["qty"], px, "be_stop" if pos["tp1_done"] else "stop", ts))
            elif not pos["tp1_done"] and price >= pos["tp1"]:
                q = pos["qty"] * self.cfg.tp1_frac
                px = pos["tp1"] * (1 - SLIPPAGE)
                pnl = q * (px - pos["entry"]) - q * px * TAKER_FEE
                pos["qty"] -= q
                pos["realized"] += pnl
                pos["fees"] += q * px * TAKER_FEE
                pos["tp1_done"] = True
                if self.cfg.be_after_tp1:
                    pos["stop"] = max(pos["stop"], pos["entry"])
                self.state["equity"] += pnl
                events.append(self._log(PaperEvent("tp1", symbol, px, q, pnl, text=f"一半止盈 {px:.6g} × {q:.4g}，{pnl:+.1f}U；止损上移到成本 {pos['stop']:.6g}", ts=_now_iso(ts))))
        events.extend(self._check_fuse(price_by={symbol: price}, ts=ts))
        if events:
            self.save()
        return events

    # ── 4h 收盘管理 ──
    def on_bar_close(self, symbol: str, price: float, reg: Any, ts: datetime | None = None) -> list[PaperEvent]:
        ts = ts or datetime.now(timezone.utc)
        events: list[PaperEvent] = []
        pos = self.state["positions"].get(symbol)
        if not pos:
            return events
        opened = datetime.fromisoformat(pos["opened_at"])
        held_bars = int((ts - opened).total_seconds() // (4 * 3600))
        reason = None
        if pos["tp1_done"]:
            r = self.cfg.exit_rule
            if r == "boll_mid" and getattr(reg, "boll_4h_mid", None) and price < reg.boll_4h_mid:
                reason = "trail_boll_mid"
            elif r == "ema12h" and getattr(reg, "ema12h_6", None) and price < reg.ema12h_6:
                reason = "trail_ema12h"
        if reason is None and held_bars >= self.cfg.max_hold_bars:
            reason = "timeout"
        if reason:
            events.append(self._close(symbol, pos, pos["qty"], price * (1 - SLIPPAGE), reason, ts))
            self.save()
        return events

    def _close(self, symbol: str, pos: dict[str, Any], qty: float, px: float, reason: str, ts: datetime) -> PaperEvent:
        fee = qty * px * TAKER_FEE
        pnl = qty * (px - pos["entry"]) - fee
        pos["realized"] += pnl
        pos["fees"] += fee
        self.state["equity"] += pnl
        total = pos["realized"]
        rec = {**{k: v for k, v in pos.items() if k != "plan"}, "closed_at": _now_iso(ts), "exit": px, "reason": reason, "net": total}
        self.state["closed"].append(rec)
        self.state["positions"].pop(symbol, None)
        kind = "stop" if reason in ("stop", "be_stop") else "exit"
        return self._log(PaperEvent(kind, symbol, px, qty, pnl, text=f"{reason} 平仓 {px:.6g} × {qty:.4g}，本腿 {pnl:+.1f}U，本笔合计 {total:+.1f}U · 权益 {self.equity:,.0f}U", ts=_now_iso(ts), extra={"reason": reason}))

    def _check_fuse(self, *, price_by: dict[str, float], ts: datetime) -> list[PaperEvent]:
        if self.daily_fuse_pct <= 0 or self._paused(ts):
            return []
        start = float(self.state.get("day_start_equity") or self.equity)
        if start <= 0:
            return []
        # 按标记价算浮动权益（否则浮亏再大也不会熔断）
        unrealized = sum(
            p["qty"] * (price_by.get(sym, p["entry"]) - p["entry"]) for sym, p in self.state["positions"].items()
        )
        dd = (self.equity + unrealized) / start - 1
        if dd > -self.daily_fuse_pct / 100:
            return []
        events: list[PaperEvent] = []
        for sym, pos in list(self.state["positions"].items()):
            px = price_by.get(sym) or pos["entry"]
            events.append(self._close(sym, pos, pos["qty"], px * (1 - SLIPPAGE), "fuse", ts))
        self.state["pending"].clear()
        nxt = (ts.astimezone(timezone.utc) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        self.state["paused_until"] = nxt.isoformat()
        events.append(self._log(PaperEvent("fuse", "*", 0.0, text=f"日内回撤 {dd*100:.1f}% ≥ {self.daily_fuse_pct}%：全平并暂停至 {nxt:%m-%d %H:%M} UTC", ts=_now_iso(ts))))
        return events

    # ── 汇总 ──
    def summary(self) -> dict[str, Any]:
        closed = self.state["closed"]
        wins = [c for c in closed if c["net"] > 0]
        return {
            "equity": self.equity,
            "return_pct": (self.equity / float(self.state["equity0"]) - 1) * 100,
            "positions": {k: {kk: v[kk] for kk in ("qty", "entry", "stop", "tp1", "tp1_done", "opened_at")} for k, v in self.state["positions"].items()},
            "pending": {k: {kk: v[kk] for kk in ("entry", "stop", "tp1", "qty", "expires_at")} for k, v in self.state["pending"].items()},
            "closed_n": len(closed),
            "win_rate": len(wins) / len(closed) if closed else 0.0,
            "paused_until": self.state.get("paused_until"),
            "day_start_equity": self.state.get("day_start_equity"),
        }
