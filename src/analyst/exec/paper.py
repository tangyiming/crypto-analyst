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
    def report(self, marks: dict[str, float] | None = None, *, journal_n: int = 100, closed_n: int = 100, now: datetime | None = None) -> dict[str, Any]:
        """页面用完整视图：权益/当日盈亏/熔断、持仓（含浮盈）、挂单（含距离）、已平仓、统计、日志。"""
        marks = marks or {}
        now = now or datetime.now(timezone.utc)
        equity0 = float(self.state["equity0"])
        day_start = float(self.state.get("day_start_equity") or equity0)

        positions = []
        unrealized_total = 0.0
        for sym, p in self.state["positions"].items():
            mark = marks.get(sym)
            qty = float(p["qty"])
            entry = float(p["entry"])
            unreal = qty * (mark - entry) if mark is not None else None
            if unreal is not None:
                unrealized_total += unreal
            try:
                held_h = (now - datetime.fromisoformat(p["opened_at"])).total_seconds() / 3600
            except Exception:  # noqa: BLE001
                held_h = None
            positions.append({
                "symbol": sym,
                "side": p.get("side", "long"),
                "qty": qty,
                "qty0": float(p.get("qty0") or qty),
                "entry": entry,
                "stop": float(p["stop"]),
                "tp1": float(p["tp1"]),
                "tp1_done": bool(p.get("tp1_done")),
                "mark": mark,
                "unrealized": unreal,
                "unrealized_pct": ((mark / entry - 1) * 100) if mark else None,
                "realized": float(p.get("realized") or 0.0),
                "fees": float(p.get("fees") or 0.0),
                "notional": qty * (mark or entry),
                "stop_dist_pct": ((mark / float(p["stop"]) - 1) * 100) if mark else None,
                "tp1_dist_pct": ((float(p["tp1"]) / mark - 1) * 100) if mark else None,
                "peak": p.get("peak"),
                "opened_at": p.get("opened_at"),
                "held_hours": held_h,
                "max_hold_hours": int(self.cfg.max_hold_bars) * 4,
                "reasons": list((p.get("plan") or {}).get("reasons") or []),
            })

        pending = []
        for sym, d in self.state["pending"].items():
            mark = marks.get(sym)
            entry = float(d["entry"])
            pending.append({
                "symbol": sym,
                "side": d.get("side", "long"),
                "entry": entry,
                "stop": float(d["stop"]),
                "tp1": float(d["tp1"]),
                "qty": float(d.get("qty") or 0.0),
                "notional": float(d.get("qty") or 0.0) * entry,
                "mark": mark,
                "dist_pct": ((mark / entry - 1) * 100) if mark else None,
                "stop_pct": abs(entry - float(d["stop"])) / entry * 100,
                "rr": (float(d["tp1"]) - entry) / max(entry - float(d["stop"]), 1e-9),
                "created_at": d.get("created_at"),
                "expires_at": d.get("expires_at"),
                "regime": d.get("regime"),
                "ref_price": d.get("ref_price"),
                "reasons": list(d.get("reasons") or []),
            })

        closed = list(self.state["closed"])
        nets = [float(c.get("net") or 0.0) for c in closed]
        wins = [n for n in nets if n > 0]
        losses = [-n for n in nets if n <= 0]
        gross_win = sum(wins)
        gross_loss = sum(losses)
        stats = {
            "closed_n": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(closed)) if closed else None,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (None if not wins else float("inf")),
            "net_total": sum(nets),
            "avg_net": (sum(nets) / len(nets)) if nets else None,
            "avg_win": (gross_win / len(wins)) if wins else None,
            "avg_loss": (-gross_loss / len(losses)) if losses else None,
            "fees_total": sum(float(c.get("fees") or 0.0) for c in closed),
            "by_reason": {},
        }
        for c in closed:
            r = str(c.get("reason") or "?")
            stats["by_reason"][r] = stats["by_reason"].get(r, 0) + 1
        if stats["profit_factor"] == float("inf"):
            stats["profit_factor"] = None

        closed_view = []
        for c in closed[-closed_n:][::-1]:
            entry = float(c.get("entry") or 0.0)
            closed_view.append({
                "symbol": c.get("symbol"),
                "side": c.get("side", "long"),
                "qty0": float(c.get("qty0") or c.get("qty") or 0.0),
                "entry": entry,
                "exit": float(c.get("exit") or 0.0),
                "exit_pct": ((float(c.get("exit") or 0.0) / entry - 1) * 100) if entry else None,
                "net": float(c.get("net") or 0.0),
                "fees": float(c.get("fees") or 0.0),
                "reason": c.get("reason"),
                "tp1_done": bool(c.get("tp1_done")),
                "opened_at": c.get("opened_at"),
                "closed_at": c.get("closed_at"),
            })

        equity = self.equity
        paused_until = self.state.get("paused_until")
        paused = bool(paused_until) and now < datetime.fromisoformat(paused_until)
        return {
            "generated_at": _now_iso(now),
            "equity": equity,
            "equity0": equity0,
            "return_pct": (equity / equity0 - 1) * 100,
            "unrealized": unrealized_total,
            "equity_mtm": equity + unrealized_total,
            "day": self.state.get("day"),
            "day_start_equity": day_start,
            "day_pnl": equity + unrealized_total - day_start,
            "day_pnl_pct": ((equity + unrealized_total) / day_start - 1) * 100 if day_start else 0.0,
            "fuse": {"daily_pct": self.daily_fuse_pct, "paused": paused, "paused_until": paused_until},
            "limits": {"max_positions": self.max_positions, "open": len(positions), "pending": len(pending)},
            "cfg": {
                "risk_pct": self.cfg.risk_pct,
                "max_leverage": self.cfg.max_leverage,
                "entry_pref": self.cfg.entry_pref,
                "exit_rule": self.cfg.exit_rule,
                "min_rr": self.cfg.min_rr,
                "min_stop_pct": self.cfg.min_stop_pct,
                "tp1_frac": self.cfg.tp1_frac,
                "order_ttl_bars": self.cfg.order_ttl_bars,
                "max_hold_bars": self.cfg.max_hold_bars,
            },
            "positions": positions,
            "pending": pending,
            "closed": closed_view,
            "stats": stats,
            "journal": list(self.state["journal"])[-journal_n:][::-1],
        }

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
