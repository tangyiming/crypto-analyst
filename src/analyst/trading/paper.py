"""纸面交易账本：跟 double_line / cycle_switch，标记价盯盈亏（非真金）。"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from analyst.config import get_settings

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_broker: PaperBroker | None = None

DEFAULT_SOURCES = ("double_line", "cycle_switch")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _cooldown_key(strategy: str, symbol: str, direction: str) -> str:
    return f"{strategy}|{symbol}|{direction}".lower()


def _double_line_tfs_allowed() -> set[str] | None:
    """None=不限制；空集合视为不限制。"""
    raw = (getattr(get_settings(), "monitor_paper_double_line_tfs", "") or "").strip()
    if not raw:
        return None
    out = {x.strip().lower() for x in raw.split(",") if x.strip()}
    return out or None


def _norm_symbol(symbol: str) -> str:
    s = (symbol or "").upper().strip().replace("-", "/")
    if "/" not in s:
        if s.endswith("USDT") and len(s) > 4:
            s = f"{s[:-4]}/USDT"
        else:
            s = f"{s}/USDT"
    return s.split(":")[0]


def _calc_rr(
    direction: str,
    entry: float,
    stop: float | None,
    take: float | None,
) -> float | None:
    """计划盈亏比 R:R = 潜在盈利距离 / 止损距离。"""
    if stop is None or take is None:
        return None
    risk = abs(entry - float(stop))
    if risk <= 0:
        return None
    if direction == "long":
        reward = float(take) - entry
    else:
        reward = entry - float(take)
    if reward <= 0:
        return None
    return round(reward / risk, 4)


def _unrealized_r(
    direction: str,
    entry: float,
    stop: float | None,
    price: float,
) -> float | None:
    """当前浮盈相对 1R 的倍数（负=浮亏）。"""
    if stop is None:
        return None
    risk = abs(entry - float(stop))
    if risk <= 0:
        return None
    if direction == "long":
        return round((price - entry) / risk, 4)
    return round((entry - price) / risk, 4)


def _paper_leverage() -> float:
    lev = float(getattr(get_settings(), "monitor_paper_leverage", 5.0) or 5.0)
    return max(1.0, min(lev, 125.0))


def _sizing_metrics(
    *,
    qty: float,
    entry: float,
    leverage: float | None = None,
    margin: float | None = None,
    notional: float | None = None,
) -> tuple[float, float, float]:
    """返回 (notional, margin, leverage)。旧仓缺字段时按配置杠杆回算。"""
    lev = float(leverage) if leverage and leverage > 0 else _paper_leverage()
    noto = float(notional) if notional and notional > 0 else abs(qty * entry)
    if margin is not None and margin > 0:
        mgn = float(margin)
        # 若历史只存了 margin，反推有效杠杆
        if noto > 0:
            lev = max(1.0, round(noto / mgn, 4))
    else:
        mgn = noto / lev if lev > 0 else noto
    return round(noto, 6), round(mgn, 6), round(lev, 4)


def _paper_sources() -> set[str]:
    raw = (getattr(get_settings(), "monitor_paper_sources", "") or "").strip()
    if not raw:
        return set(DEFAULT_SOURCES)
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


@dataclass
class PaperPosition:
    id: str
    symbol: str
    timeframe: str
    direction: str  # long / short
    qty: float
    entry: float
    stop_loss: float | None
    take_profit: float | None
    opened_at: str
    strategy: str = "ai_plan"
    entry_fee: float = 0.0
    session_id: int | None = None
    model_id: str | None = None
    rationale: str = ""
    target_weight: float | None = None  # cycle_switch 目标仓位
    rr_ratio: float | None = None  # 计划盈亏比
    notional: float | None = None  # 开仓名义价值
    margin: float | None = None  # 开仓保证金 = 名义 / 杠杆
    leverage: float | None = None  # 开仓时使用的杠杆

    def unrealized_pnl(self, price: float) -> float:
        if self.direction == "long":
            return (price - self.entry) * self.qty
        return (self.entry - price) * self.qty


@dataclass
class PaperTrade:
    id: str
    symbol: str
    timeframe: str
    direction: str
    qty: float
    entry: float
    exit: float
    stop_loss: float | None
    take_profit: float | None
    opened_at: str
    closed_at: str
    outcome: str  # tp / sl / signal / manual
    pnl_usd: float
    fees_usd: float
    strategy: str = "ai_plan"
    session_id: int | None = None
    model_id: str | None = None
    rr_ratio: float | None = None
    notional: float | None = None
    margin: float | None = None
    leverage: float | None = None
    margin_roi_pct: float | None = None


@dataclass
class PaperState:
    starting_equity: float = 100.0
    cash: float = 100.0
    equity: float = 100.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    positions: list[PaperPosition] = field(default_factory=list)
    trades: list[PaperTrade] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    # strategy|symbol|direction -> ISO 冷却截止时间
    sl_cooldown_until: dict[str, str] = field(default_factory=dict)
    # ── 风控熔断状态 ──
    day_anchor: str = ""                 # 当日锚（UTC 日期）
    day_start_equity: float = 0.0        # 当日起始权益
    daily_fuse_date: str = ""            # 触发过单日熔断的日期（当日内停开新仓）
    strategy_pnl: dict[str, float] = field(default_factory=dict)       # 累计已实现
    strategy_pnl_peak: dict[str, float] = field(default_factory=dict)  # 已实现峰值
    disabled_strategies: list[str] = field(default_factory=list)       # 回撤停用
    updated_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "starting_equity": self.starting_equity,
            "cash": self.cash,
            "equity": self.equity,
            "realized_pnl": self.realized_pnl,
            "fees_paid": self.fees_paid,
            "positions": [asdict(p) for p in self.positions],
            "trades": [asdict(t) for t in self.trades],
            "equity_curve": list(self.equity_curve[-500:]),
            "sl_cooldown_until": dict(self.sl_cooldown_until),
            "day_anchor": self.day_anchor,
            "day_start_equity": self.day_start_equity,
            "daily_fuse_date": self.daily_fuse_date,
            "strategy_pnl": dict(self.strategy_pnl),
            "strategy_pnl_peak": dict(self.strategy_pnl_peak),
            "disabled_strategies": list(self.disabled_strategies),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperState:
        positions: list[PaperPosition] = []
        for raw in data.get("positions") or []:
            if not isinstance(raw, dict):
                continue
            p = dict(raw)
            p.setdefault("strategy", "ai_plan")
            p.setdefault("target_weight", None)
            p.setdefault("rr_ratio", None)
            p.setdefault("notional", None)
            p.setdefault("margin", None)
            p.setdefault("leverage", None)
            # 兼容旧字段
            if "stop_loss" not in p:
                p["stop_loss"] = None
            if "take_profit" not in p:
                p["take_profit"] = None
            try:
                positions.append(PaperPosition(**{
                    k: p[k] for k in PaperPosition.__dataclass_fields__ if k in p
                }))
            except TypeError:
                continue
        trades: list[PaperTrade] = []
        for raw in data.get("trades") or []:
            if not isinstance(raw, dict):
                continue
            t = dict(raw)
            t.setdefault("strategy", "ai_plan")
            t.setdefault("rr_ratio", None)
            t.setdefault("notional", None)
            t.setdefault("margin", None)
            t.setdefault("leverage", None)
            t.setdefault("margin_roi_pct", None)
            try:
                trades.append(PaperTrade(**{
                    k: t[k] for k in PaperTrade.__dataclass_fields__ if k in t
                }))
            except TypeError:
                continue
        start = float(data.get("starting_equity") or 100.0)
        cool_raw = data.get("sl_cooldown_until") or {}
        cool = (
            {str(k): str(v) for k, v in cool_raw.items()}
            if isinstance(cool_raw, dict)
            else {}
        )
        return cls(
            starting_equity=start,
            cash=float(data.get("cash", start)),
            equity=float(data.get("equity", start)),
            realized_pnl=float(data.get("realized_pnl") or 0.0),
            fees_paid=float(data.get("fees_paid") or 0.0),
            positions=positions,
            trades=trades,
            equity_curve=list(data.get("equity_curve") or []),
            sl_cooldown_until=cool,
            day_anchor=str(data.get("day_anchor") or ""),
            day_start_equity=float(data.get("day_start_equity") or 0.0),
            daily_fuse_date=str(data.get("daily_fuse_date") or ""),
            strategy_pnl={
                str(k): float(v)
                for k, v in (data.get("strategy_pnl") or {}).items()
            },
            strategy_pnl_peak={
                str(k): float(v)
                for k, v in (data.get("strategy_pnl_peak") or {}).items()
            },
            disabled_strategies=[
                str(s) for s in (data.get("disabled_strategies") or [])
            ],
            updated_at=str(data.get("updated_at") or _utc_now_iso()),
        )


class PaperBroker:
    """进程内单例纸面经纪商。"""

    def __init__(self) -> None:
        self.state = PaperState()
        self._marks: dict[str, float] = {}
        self._load()

    def _path(self) -> Path:
        s = get_settings()
        return Path(s.data_cache_dir) / "paper_account.json"

    def _load(self) -> None:
        path = self._path()
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                self.state = PaperState.from_dict(data)
                logger.info(
                    "纸面账本已加载 equity=%.4f positions=%d trades=%d",
                    self.state.equity,
                    len(self.state.positions),
                    len(self.state.trades),
                )
        except Exception as e:
            logger.warning("加载纸面账本失败，使用新账户: %s", e)
            settings = get_settings()
            start = float(getattr(settings, "monitor_paper_equity", 100.0) or 100.0)
            self.state = PaperState(starting_equity=start, cash=start, equity=start)

    def _save(self) -> None:
        self.state.updated_at = _utc_now_iso()
        path = self._path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self.state.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("写入纸面账本失败: %s", e)

    def _mark_equity_point(self) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        curve = self.state.equity_curve
        if curve and curve[-1].get("day") == day:
            curve[-1] = {"day": day, "equity": round(self.state.equity, 6)}
        else:
            curve.append({"day": day, "equity": round(self.state.equity, 6)})
        if len(curve) > 500:
            self.state.equity_curve = curve[-500:]

    def _revalue(self) -> None:
        upnl = 0.0
        for p in self.state.positions:
            px = self._marks.get(p.symbol)
            if px is None:
                px = p.entry
            upnl += p.unrealized_pnl(px)
        self.state.equity = self.state.cash + upnl
        if not self.state.day_anchor:
            self._roll_day_anchor()
        self._mark_equity_point()

    # ── 风控熔断 ─────────────────────────────────────────
    def _roll_day_anchor(self) -> None:
        """跨 UTC 日则重置当日锚（单日亏损熔断随之自动复位）。"""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state.day_anchor != day:
            self.state.day_anchor = day
            self.state.day_start_equity = self.state.equity

    def _daily_fuse_active(self) -> bool:
        """单日亏损熔断：当日权益回撤超限 → 当日停开新仓。"""
        settings = get_settings()
        limit = float(getattr(settings, "paper_daily_loss_limit_pct", 0.0) or 0.0)
        if limit <= 0:
            return False
        self._roll_day_anchor()
        day = self.state.day_anchor
        if self.state.daily_fuse_date == day:
            return True
        base = self.state.day_start_equity
        if base > 0 and self.state.equity <= base * (1.0 - limit / 100.0):
            self.state.daily_fuse_date = day
            logger.warning(
                "🔴 纸面熔断：当日权益 %.2f → %.2f（-%.1f%% ≥ 限额 %.1f%%），今日停开新仓",
                base, self.state.equity,
                (1 - self.state.equity / base) * 100, limit,
            )
            return True
        return False

    def _flatten_all_locked(self, reason: str) -> list[dict[str, Any]]:
        """按最新标记价全平（熔断可选动作）。"""
        events: list[dict[str, Any]] = []
        for pos in list(self.state.positions):
            px = self._marks.get(pos.symbol) or pos.entry
            events.append(self._close_locked(pos, px, reason))
        self.state.positions = []
        self._revalue()
        return events

    def _record_strategy_pnl(self, strategy: str, pnl: float) -> None:
        """更新策略累计已实现盈亏与峰值；回撤超限则停用该策略。"""
        settings = get_settings()
        s = (strategy or "unknown").lower()
        cum = self.state.strategy_pnl.get(s, 0.0) + pnl
        self.state.strategy_pnl[s] = cum
        peak = max(self.state.strategy_pnl_peak.get(s, 0.0), cum)
        self.state.strategy_pnl_peak[s] = peak
        dd_limit = float(
            getattr(settings, "paper_strategy_dd_disable_pct", 0.0) or 0.0
        )
        if dd_limit <= 0 or s in self.state.disabled_strategies:
            return
        start = self.state.starting_equity or 100.0
        dd_pct = (peak - cum) / start * 100.0
        if dd_pct >= dd_limit:
            self.state.disabled_strategies.append(s)
            logger.warning(
                "🔴 纸面熔断：策略 %s 已实现回撤 %.1f%%（峰值 %+.2f → %+.2f，"
                "≥ 限额 %.1f%%），停用其开仓；恢复：analyst paper-fuse clear %s",
                s, dd_pct, peak, cum, dd_limit, s,
            )

    def clear_strategy_fuse(self, strategy: str | None = None) -> list[str]:
        """手动恢复被停用的策略（None=全部）。返回恢复列表。"""
        with _lock:
            if strategy is None:
                cleared = list(self.state.disabled_strategies)
                self.state.disabled_strategies = []
            else:
                s = strategy.lower()
                cleared = [x for x in self.state.disabled_strategies if x == s]
                self.state.disabled_strategies = [
                    x for x in self.state.disabled_strategies if x != s
                ]
            for s in cleared:
                # 回撤基准重置到当前累计，避免恢复后立刻再次触发
                self.state.strategy_pnl_peak[s] = self.state.strategy_pnl.get(s, 0.0)
            self._save()
            return cleared

    def _gross_exposure_blocked(self, new_notional: float) -> bool:
        """组合总名义敞口上限（相关资产敞口叠加保护）。"""
        settings = get_settings()
        cap = float(getattr(settings, "paper_max_gross_exposure", 0.0) or 0.0)
        if cap <= 0 or self.state.equity <= 0:
            return False
        gross = sum(
            (p.notional if p.notional else p.qty * p.entry)
            for p in self.state.positions
        )
        return (gross + max(new_notional, 0.0)) > cap * self.state.equity

    def reset(self, starting_equity: float | None = None) -> PaperState:
        with _lock:
            settings = get_settings()
            start = float(
                starting_equity
                if starting_equity is not None
                else (getattr(settings, "monitor_paper_equity", 100.0) or 100.0)
            )
            self.state = PaperState(starting_equity=start, cash=start, equity=start)
            self._roll_day_anchor()
            self._marks.clear()
            self._save()
            return self.state

    def status(self) -> dict[str, Any]:
        with _lock:
            self._revalue()
            closed = self.state.trades
            wins = sum(1 for t in closed if t.pnl_usd > 0)
            losses = sum(1 for t in closed if t.pnl_usd < 0)
            decided = wins + losses
            pos_rows = []
            by_strategy: dict[str, dict[str, Any]] = {}
            used_margin = 0.0
            total_notional = 0.0
            for p in self.state.positions:
                px = self._marks.get(p.symbol, p.entry)
                upnl = p.unrealized_pnl(px)
                rr = p.rr_ratio
                if rr is None:
                    rr = _calc_rr(p.direction, p.entry, p.stop_loss, p.take_profit)
                noto, mgn, lev = _sizing_metrics(
                    qty=p.qty,
                    entry=p.entry,
                    leverage=p.leverage,
                    margin=p.margin,
                    notional=p.notional,
                )
                mark_notional = abs(p.qty * px)
                margin_roi = (upnl / mgn * 100.0) if mgn > 0 else None
                if p.direction == "long":
                    price_chg = (px / p.entry - 1.0) * 100.0 if p.entry else 0.0
                else:
                    price_chg = (p.entry / px - 1.0) * 100.0 if px else 0.0
                used_margin += mgn
                total_notional += mark_notional
                row = asdict(p)
                row["mark"] = px
                row["unrealized_pnl"] = round(upnl, 6)
                row["rr_ratio"] = rr
                row["unrealized_r"] = _unrealized_r(
                    p.direction, p.entry, p.stop_loss, px
                )
                row["notional"] = noto
                row["mark_notional"] = round(mark_notional, 6)
                row["margin"] = mgn
                row["leverage"] = lev
                row["margin_roi_pct"] = (
                    round(margin_roi, 2) if margin_roi is not None else None
                )
                row["price_chg_pct"] = round(price_chg, 4)
                pos_rows.append(row)
                bucket = by_strategy.setdefault(
                    p.strategy,
                    {
                        "strategy": p.strategy,
                        "open": 0,
                        "unrealized_pnl": 0.0,
                        "realized_pnl": 0.0,
                        "trades": 0,
                        "wins": 0,
                        "losses": 0,
                    },
                )
                bucket["open"] += 1
                bucket["unrealized_pnl"] += upnl
            for t in closed:
                bucket = by_strategy.setdefault(
                    t.strategy,
                    {
                        "strategy": t.strategy,
                        "open": 0,
                        "unrealized_pnl": 0.0,
                        "realized_pnl": 0.0,
                        "trades": 0,
                        "wins": 0,
                        "losses": 0,
                    },
                )
                bucket["trades"] += 1
                bucket["realized_pnl"] += t.pnl_usd
                if t.pnl_usd > 0:
                    bucket["wins"] += 1
                elif t.pnl_usd < 0:
                    bucket["losses"] += 1
            for b in by_strategy.values():
                b["unrealized_pnl"] = round(b["unrealized_pnl"], 6)
                b["realized_pnl"] = round(b["realized_pnl"], 6)
                d = b["wins"] + b["losses"]
                b["win_rate"] = round(b["wins"] / d, 4) if d else None

            acct_lev = (
                round(total_notional / self.state.equity, 4)
                if self.state.equity > 0
                else 0.0
            )
            free_margin = round(self.state.equity - used_margin, 6)

            def _trade_row(t: PaperTrade) -> dict[str, Any]:
                noto, mgn, lev = _sizing_metrics(
                    qty=t.qty,
                    entry=t.entry,
                    leverage=t.leverage,
                    margin=t.margin,
                    notional=t.notional,
                )
                roi = t.margin_roi_pct
                if roi is None and mgn > 0:
                    roi = round(t.pnl_usd / mgn * 100.0, 2)
                return {
                    **asdict(t),
                    "rr_ratio": t.rr_ratio
                    if t.rr_ratio is not None
                    else _calc_rr(t.direction, t.entry, t.stop_loss, t.take_profit),
                    "notional": noto,
                    "margin": mgn,
                    "leverage": lev,
                    "margin_roi_pct": roi,
                }

            return {
                "enabled": bool(getattr(get_settings(), "monitor_paper_enabled", False)),
                "sources": sorted(_paper_sources()),
                "starting_equity": self.state.starting_equity,
                "cash": round(self.state.cash, 6),
                "equity": round(self.state.equity, 6),
                "realized_pnl": round(self.state.realized_pnl, 6),
                "unrealized_pnl": round(
                    sum(r["unrealized_pnl"] for r in pos_rows), 6
                ),
                "fees_paid": round(self.state.fees_paid, 6),
                "return_pct": round(
                    (self.state.equity / self.state.starting_equity - 1.0) * 100
                    if self.state.starting_equity
                    else 0.0,
                    2,
                ),
                "leverage": _paper_leverage(),
                "used_margin": round(used_margin, 6),
                "free_margin": free_margin,
                "total_notional": round(total_notional, 6),
                "account_leverage": acct_lev,
                "open_positions": len(self.state.positions),
                "closed_trades": len(closed),
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / decided, 4) if decided else None,
                "positions": pos_rows,
                "by_strategy": list(by_strategy.values()),
                "recent_trades": [_trade_row(t) for t in closed[-30:][::-1]],
                "equity_curve": list(self.state.equity_curve[-90:]),
                "updated_at": self.state.updated_at,
                "marks": dict(self._marks),
                "risk_fuse": {
                    "daily_fuse_active": self._daily_fuse_active(),
                    "daily_loss_limit_pct": float(
                        getattr(get_settings(), "paper_daily_loss_limit_pct", 0.0)
                        or 0.0
                    ),
                    "day_start_equity": round(self.state.day_start_equity, 6),
                    "disabled_strategies": list(self.state.disabled_strategies),
                    "max_gross_exposure": float(
                        getattr(get_settings(), "paper_max_gross_exposure", 0.0)
                        or 0.0
                    ),
                },
            }

    def try_open_from_plan(
        self,
        *,
        symbol: str,
        timeframe: str,
        direction: str,
        price: float,
        plan: dict[str, Any],
        strategy: str = "ai_plan",
        session_id: int | None = None,
        model_id: str | None = None,
        risk_scale: float = 1.0,
    ) -> dict[str, Any] | None:
        """按计划开纸面仓（ai_plan / double_line）。成功返回事件 dict。"""
        settings = get_settings()
        if not getattr(settings, "monitor_paper_enabled", False):
            return None
        strategy = (strategy or "ai_plan").strip().lower()
        if strategy not in _paper_sources():
            return None
        if strategy == "double_line":
            allowed = _double_line_tfs_allowed()
            tf_l = (timeframe or "").strip().lower()
            if allowed is not None and tf_l not in allowed:
                logger.info(
                    "纸面跳过：double_line 周期 %s 不在白名单 %s",
                    tf_l or "?",
                    ",".join(sorted(allowed)),
                )
                return None
        direction = (direction or "").lower().strip()
        if direction not in ("long", "short"):
            return None
        sym = _norm_symbol(symbol)
        entry = float(price)
        if entry <= 0:
            return None
        sl = plan.get("stop_loss")
        tp = plan.get("take_profit_1")
        if sl is None or tp is None:
            return None
        stop = float(sl)
        take = float(tp)
        risk_dist = abs(entry - stop)
        if risk_dist <= 0:
            return None
        if direction == "long" and not (stop < entry < take):
            logger.info(
                "纸面跳过：long 价位不合理 %s/%s e=%s sl=%s tp=%s",
                strategy,
                sym,
                entry,
                stop,
                take,
            )
            return None
        if direction == "short" and not (take < entry < stop):
            logger.info(
                "纸面跳过：short 价位不合理 %s/%s e=%s sl=%s tp=%s",
                strategy,
                sym,
                entry,
                stop,
                take,
            )
            return None

        plan_rr = plan.get("rr_ratio")
        try:
            rr = float(plan_rr) if plan_rr is not None else None
        except (TypeError, ValueError):
            rr = None
        if rr is None or rr <= 0:
            rr = _calc_rr(direction, entry, stop, take)

        try:
            rs = float(risk_scale)
        except (TypeError, ValueError):
            rs = 1.0
        if rs <= 0:
            logger.info("纸面跳过：risk_scale<=0 %s/%s", strategy, sym)
            return None

        with _lock:
            return self._open_locked(
                symbol=sym,
                timeframe=timeframe,
                direction=direction,
                entry=entry,
                stop=stop,
                take=take,
                strategy=strategy,
                session_id=session_id,
                model_id=model_id,
                rationale=str(plan.get("rationale") or "")[:200],
                risk_scale=rs,
                rr_ratio=rr,
            )

    def sync_cycle_target(
        self,
        *,
        symbol: str,
        timeframe: str,
        target_position: float,
        price: float,
        regime: str | None = None,
    ) -> list[dict[str, Any]]:
        """按 cycle_switch 目标仓位同步纸面持仓（无固定 TP/SL，信号平仓）。"""
        settings = get_settings()
        if not getattr(settings, "monitor_paper_enabled", False):
            return []
        if "cycle_switch" not in _paper_sources():
            return []
        sym = _norm_symbol(symbol)
        px = float(price)
        if px <= 0:
            return []
        target = float(target_position)
        events: list[dict[str, Any]] = []
        with _lock:
            self._marks[sym] = px
            existing = [
                p
                for p in self.state.positions
                if p.symbol == sym and p.strategy == "cycle_switch"
            ]
            others = [
                p
                for p in self.state.positions
                if not (p.symbol == sym and p.strategy == "cycle_switch")
            ]

            if abs(target) < 1e-9:
                for p in existing:
                    events.append(self._close_locked(p, px, "signal"))
                self.state.positions = others
                self._revalue()
                if events:
                    for ev in events:
                        ev["equity"] = round(self.state.equity, 6)
                    self._save()
                return events

            want = "long" if target > 0 else "short"
            keep: list[PaperPosition] = []
            for p in existing:
                if p.direction != want:
                    events.append(self._close_locked(p, px, "signal"))
                else:
                    p.target_weight = target
                    keep.append(p)
            self.state.positions = others + keep

            if not keep:
                opened = self._open_locked(
                    symbol=sym,
                    timeframe=timeframe,
                    direction=want,
                    entry=px,
                    stop=None,
                    take=None,
                    strategy="cycle_switch",
                    rationale=f"cycle_switch target={target:.2f} regime={regime or '-'}",
                    risk_scale=abs(target),
                    target_weight=target,
                )
                if opened:
                    events.append(opened)

            self._revalue()
            if events:
                for ev in events:
                    ev["equity"] = round(self.state.equity, 6)
                self._save()
        return events

    def _open_locked(
        self,
        *,
        symbol: str,
        timeframe: str,
        direction: str,
        entry: float,
        stop: float | None,
        take: float | None,
        strategy: str,
        session_id: int | None = None,
        model_id: str | None = None,
        rationale: str = "",
        risk_scale: float = 1.0,
        target_weight: float | None = None,
        rr_ratio: float | None = None,
    ) -> dict[str, Any] | None:
        settings = get_settings()
        max_open = max(1, int(getattr(settings, "monitor_paper_max_positions", 12) or 12))
        if any(p.symbol == symbol and p.strategy == strategy for p in self.state.positions):
            logger.info("纸面跳过：已有 %s/%s 持仓", strategy, symbol)
            return None
        if len(self.state.positions) >= max_open:
            logger.info("纸面跳过：已达最大持仓数 %d", max_open)
            return None
        if self._daily_fuse_active():
            logger.info("纸面跳过：单日亏损熔断生效中（今日停开新仓）")
            return None
        if (strategy or "").lower() in self.state.disabled_strategies:
            logger.info("纸面跳过：策略 %s 因回撤熔断已停用", strategy)
            return None

        if strategy == "double_line":
            cool_min = int(
                getattr(settings, "monitor_paper_sl_cooldown_minutes", 0) or 0
            )
            if cool_min > 0:
                ck = _cooldown_key(strategy, symbol, direction)
                until = _parse_iso(self.state.sl_cooldown_until.get(ck))
                if until is not None and _utc_now() < until:
                    logger.info(
                        "纸面跳过：%s/%s/%s 止损冷却至 %s",
                        strategy,
                        symbol,
                        direction,
                        until.isoformat(),
                    )
                    return None

        risk_pct = float(getattr(settings, "monitor_paper_risk_pct", 0.01) or 0.01)
        risk_pct = max(0.001, min(risk_pct, 0.05))
        risk_usd = self.state.equity * risk_pct * max(0.05, min(abs(risk_scale), 1.0))
        if risk_usd <= 0:
            return None
        if stop is not None:
            risk_dist = abs(entry - float(stop))
        else:
            # cycle 等无止损：用 2% 名义距离估仓
            risk_dist = entry * 0.02
        if risk_dist <= 0:
            return None
        qty = risk_usd / risk_dist
        notional = qty * entry
        if qty <= 0 or notional < 0.01:
            logger.info("纸面跳过：仓位过小 qty=%s notional=%s", qty, notional)
            return None

        lev = _paper_leverage()
        margin = notional / lev if lev > 0 else notional
        max_mgn_pct = float(
            getattr(settings, "monitor_paper_max_margin_pct", 0.15) or 0.0
        )
        # 仅 double_line：止损极窄时名义仓会爆炸
        if (
            strategy == "double_line"
            and max_mgn_pct > 0
            and self.state.equity > 0
        ):
            max_mgn = self.state.equity * max(0.01, min(max_mgn_pct, 1.0))
            if margin > max_mgn and margin > 0:
                scale = max_mgn / margin
                qty *= scale
                notional = qty * entry
                margin = notional / lev if lev > 0 else notional
                logger.info(
                    "纸面缩仓：保证金封顶 equity×%.0f%% → margin=%.4f",
                    max_mgn_pct * 100,
                    margin,
                )
        if qty <= 0 or notional < 0.01:
            logger.info("纸面跳过：缩仓后过小")
            return None

        if self._gross_exposure_blocked(notional):
            logger.info(
                "纸面跳过：组合名义敞口将超上限（equity×%.1f），%s/%s 不开",
                float(getattr(settings, "paper_max_gross_exposure", 0.0) or 0.0),
                strategy,
                symbol,
            )
            return None

        fee_bps = float(getattr(settings, "monitor_paper_fee_bps", 4.0) or 0.0)
        fee = notional * (fee_bps / 10_000.0)
        if self.state.cash < fee:
            return None

        if rr_ratio is None:
            rr_ratio = _calc_rr(direction, entry, stop, take)

        noto, mgn, lev = _sizing_metrics(
            qty=qty, entry=entry, leverage=lev, notional=notional
        )

        self.state.cash -= fee
        self.state.fees_paid += fee
        pos = PaperPosition(
            id=uuid.uuid4().hex[:12],
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            qty=qty,
            entry=entry,
            stop_loss=stop,
            take_profit=take,
            opened_at=_utc_now_iso(),
            strategy=strategy,
            entry_fee=fee,
            session_id=session_id,
            model_id=model_id,
            rationale=rationale,
            target_weight=target_weight,
            rr_ratio=rr_ratio,
            notional=noto,
            margin=mgn,
            leverage=lev,
        )
        self.state.positions.append(pos)
        self._marks[symbol] = entry
        self._revalue()
        self._save()
        event = {
            "type": "paper_open",
            "position": asdict(pos),
            "equity": round(self.state.equity, 6),
            "cash": round(self.state.cash, 6),
        }
        logger.info(
            "纸面开仓 [%s] %s %s qty=%.6g entry=%.6g margin=%.4f lev=%.1fx sl=%s tp=%s equity=%.4f",
            strategy,
            direction,
            symbol,
            qty,
            entry,
            mgn,
            lev,
            stop,
            take,
            self.state.equity,
        )
        return event

    def on_mark(self, symbol: str, price: float) -> list[dict[str, Any]]:
        """标记价更新：有 SL/TP 的仓检查平仓；全体刷新浮盈。"""
        settings = get_settings()
        if not getattr(settings, "monitor_paper_enabled", False):
            return []
        if price is None or float(price) <= 0:
            return []
        sym = _norm_symbol(symbol)
        px = float(price)
        events: list[dict[str, Any]] = []
        with _lock:
            self._marks[sym] = px
            still: list[PaperPosition] = []
            for pos in self.state.positions:
                if pos.symbol != sym:
                    still.append(pos)
                    continue
                outcome = None
                if pos.stop_loss is not None and pos.take_profit is not None:
                    if pos.direction == "long":
                        if px <= pos.stop_loss:
                            outcome = "sl"
                        elif px >= pos.take_profit:
                            outcome = "tp"
                    else:
                        if px >= pos.stop_loss:
                            outcome = "sl"
                        elif px <= pos.take_profit:
                            outcome = "tp"
                if outcome is None:
                    still.append(pos)
                    continue
                ev = self._close_locked(pos, px, outcome)
                events.append(ev)
            self.state.positions = still
            self._revalue()
            # 单日亏损熔断：可选全平（默认只停开新仓，见 _open_locked）
            if self._daily_fuse_active() and getattr(
                settings, "paper_flatten_on_daily_fuse", False
            ) and self.state.positions:
                events.extend(self._flatten_all_locked("fuse"))
            if events:
                for ev in events:
                    ev["equity"] = round(self.state.equity, 6)
                self._save()
            else:
                # 浮盈变化也落盘，便于页面刷新看到实时权益
                self._save()
        return events

    def _close_locked(
        self, pos: PaperPosition, exit_price: float, outcome: str
    ) -> dict[str, Any]:
        settings = get_settings()
        fee_bps = float(getattr(settings, "monitor_paper_fee_bps", 4.0) or 0.0)
        notional = pos.qty * exit_price
        exit_fee = notional * (fee_bps / 10_000.0)
        raw_pnl = pos.unrealized_pnl(exit_price)
        pnl = raw_pnl - exit_fee
        self.state.cash += raw_pnl - exit_fee
        self.state.fees_paid += exit_fee
        self.state.realized_pnl += pnl
        noto, mgn, lev = _sizing_metrics(
            qty=pos.qty,
            entry=pos.entry,
            leverage=pos.leverage,
            margin=pos.margin,
            notional=pos.notional,
        )
        margin_roi = round(pnl / mgn * 100.0, 2) if mgn > 0 else None
        trade = PaperTrade(
            id=pos.id,
            symbol=pos.symbol,
            timeframe=pos.timeframe,
            direction=pos.direction,
            qty=pos.qty,
            entry=pos.entry,
            exit=exit_price,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            opened_at=pos.opened_at,
            closed_at=_utc_now_iso(),
            outcome=outcome,
            pnl_usd=round(pnl, 6),
            fees_usd=round(pos.entry_fee + exit_fee, 6),
            strategy=pos.strategy,
            session_id=pos.session_id,
            model_id=pos.model_id,
            rr_ratio=pos.rr_ratio
            if pos.rr_ratio is not None
            else _calc_rr(pos.direction, pos.entry, pos.stop_loss, pos.take_profit),
            notional=noto,
            margin=mgn,
            leverage=lev,
            margin_roi_pct=margin_roi,
        )
        self.state.trades.append(trade)
        if len(self.state.trades) > 500:
            self.state.trades = self.state.trades[-500:]
        self._record_strategy_pnl(pos.strategy, pnl)

        if outcome == "sl" and (pos.strategy or "").lower() == "double_line":
            cool_min = int(
                getattr(settings, "monitor_paper_sl_cooldown_minutes", 0) or 0
            )
            if cool_min > 0:
                ck = _cooldown_key(pos.strategy, pos.symbol, pos.direction)
                until = _utc_now() + timedelta(minutes=cool_min)
                self.state.sl_cooldown_until[ck] = until.isoformat()
                # 清理过期冷却键
                now = _utc_now()
                expired = [
                    k
                    for k, v in self.state.sl_cooldown_until.items()
                    if (t := _parse_iso(v)) is not None and t <= now
                ]
                for k in expired:
                    if k != ck:
                        self.state.sl_cooldown_until.pop(k, None)

        logger.info(
            "纸面平仓 [%s] %s %s outcome=%s pnl=%.4f cash=%.4f",
            pos.strategy,
            pos.direction,
            pos.symbol,
            outcome,
            pnl,
            self.state.cash,
        )
        return {
            "type": "paper_close",
            "trade": asdict(trade),
            "equity": round(self.state.cash, 6),
        }


def get_paper_broker() -> PaperBroker:
    global _broker
    with _lock:
        if _broker is None:
            _broker = PaperBroker()
        return _broker
