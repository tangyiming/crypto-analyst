"""实时价格告警。

跟着标记价逐笔判断，不看 K 线收盘。触发方式对齐常见交易终端：

- 价位只在「穿越」时响一次，价格本来就在线另一侧不会立刻响
- 百分比 / 绝对变化看滚动窗口（或从设定时的锚定价起算）
- 条件持续成立时不连发；重复告警要等条件解除，或冷却结束后从新锚点再量
"""

from __future__ import annotations

import json
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

KINDS = ("cross", "above", "below", "pct_change", "abs_change")
DIRECTIONS = ("up", "down", "any")
# 1s 标记价，窗口最长记 6 小时
_TAPE_MAX = 6 * 60 * 60


def normalize_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper().replace("-", "/")
    if "/" not in s:
        if s.endswith("USDT") and len(s) > 4:
            s = f"{s[:-4]}/USDT"
        else:
            s = f"{s}/USDT"
    return s.split(":")[0]


def _fmt_price(price: float) -> str:
    p = float(price)
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:,.4f}".rstrip("0").rstrip(".")
    return f"{p:.6f}".rstrip("0").rstrip(".")


@dataclass
class PriceAlert:
    id: str
    symbol: str
    kind: str
    direction: str = "any"
    price: float | None = None
    threshold: float | None = None
    window_sec: int = 0
    repeat: bool = False
    cooldown_sec: int = 300
    enabled: bool = True
    note: str = ""
    created_at: float = 0.0
    anchor_price: float | None = None
    anchor_at: float | None = None
    last_fired_at: float | None = None
    fire_count: int = 0
    # 运行时：上一笔价、条件是否已锁存（不落盘也行，落盘可避免重启后连发）
    last_price: float | None = None
    latched: bool = False

    def to_public(self) -> dict[str, Any]:
        data = asdict(self)
        data["label"] = describe_alert(self)
        return data


@dataclass
class PriceAlertBook:
    path: Path
    alerts: dict[str, PriceAlert] = field(default_factory=dict)
    _tapes: dict[str, deque[tuple[float, float]]] = field(default_factory=dict)

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        items = raw.get("alerts") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return
        for row in items:
            alert = _alert_from_row(row)
            if alert:
                self.alerts[alert.id] = alert

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "alerts": [asdict(a) for a in self.alerts.values()],
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_alerts(self) -> list[PriceAlert]:
        return sorted(self.alerts.values(), key=lambda a: a.created_at, reverse=True)

    def symbols_wanted(self) -> set[str]:
        return {a.symbol for a in self.alerts.values() if a.enabled}

    def add(self, spec: dict[str, Any], *, now: float | None = None) -> PriceAlert:
        now = time.time() if now is None else now
        alert = _build_alert(spec, now=now)
        self.alerts[alert.id] = alert
        self.save()
        return alert

    def update(self, alert_id: str, patch: dict[str, Any]) -> PriceAlert | None:
        alert = self.alerts.get(alert_id)
        if not alert:
            return None
        if "enabled" in patch:
            alert.enabled = bool(patch["enabled"])
            if alert.enabled:
                alert.latched = False
                alert.last_price = None
        if "repeat" in patch:
            alert.repeat = bool(patch["repeat"])
        if "note" in patch:
            alert.note = str(patch["note"] or "")[:80]
        if "cooldown_sec" in patch:
            alert.cooldown_sec = max(0, int(patch["cooldown_sec"]))
        self.save()
        return alert

    def delete(self, alert_id: str) -> bool:
        if alert_id not in self.alerts:
            return False
        del self.alerts[alert_id]
        self.save()
        return True

    def on_tick(
        self, symbol: str, price: float, *, now: float | None = None
    ) -> list[tuple[PriceAlert, dict[str, Any]]]:
        """喂入一笔标记价。返回本次新触发的 (alert, event)。"""
        symbol = normalize_symbol(symbol)
        price = float(price)
        if price <= 0:
            return []
        now = time.time() if now is None else float(now)
        tape = self._tapes.setdefault(symbol, deque(maxlen=_TAPE_MAX))
        tape.append((now, price))

        fired: list[tuple[PriceAlert, dict[str, Any]]] = []
        for alert in list(self.alerts.values()):
            if alert.symbol != symbol or not alert.enabled:
                continue
            event = self._eval_one(alert, price, now, tape)
            if event is None:
                continue
            fired.append((alert, event))
        if fired:
            self.save()
        return fired

    def _eval_one(
        self,
        alert: PriceAlert,
        price: float,
        now: float,
        tape: deque[tuple[float, float]],
    ) -> dict[str, Any] | None:
        prev = alert.last_price
        alert.last_price = price
        if alert.anchor_price is None:
            alert.anchor_price = price
            alert.anchor_at = now

        if alert.kind in ("cross", "above", "below"):
            hit = _level_hit(alert, prev, price)
        else:
            hit = _move_hit(alert, price, now, tape)
        if hit is None:
            return None
        if alert.latched:
            return None
        if alert.last_fired_at and now - alert.last_fired_at < alert.cooldown_sec:
            alert.latched = True
            return None

        alert.last_fired_at = now
        alert.fire_count += 1
        alert.latched = True
        if alert.kind in ("pct_change", "abs_change") and alert.window_sec <= 0:
            alert.anchor_price = price
            alert.anchor_at = now
        if not alert.repeat:
            alert.enabled = False
        return _event(alert, price, now, hit)

    def note_retraced(self) -> None:
        """条件不再成立时解开锁存，重复告警才能再响。由 on_tick 内联处理。"""


def _level_hit(alert: PriceAlert, prev: float | None, price: float) -> dict[str, Any] | None:
    level = alert.price
    if level is None or prev is None:
        return None
    up = prev < level <= price
    down = prev > level >= price
    kind = alert.kind
    direction = alert.direction
    if kind == "above":
        crossed, side = up, "up"
        away = price < level
    elif kind == "below":
        crossed, side = down, "down"
        away = price > level
    elif direction == "up":
        crossed, side, away = up, "up", price < level
    elif direction == "down":
        crossed, side, away = down, "down", price > level
    elif up:
        crossed, side, away = True, "up", False
    elif down:
        crossed, side, away = True, "down", False
    else:
        crossed, side, away = False, "", price != level
    if away:
        alert.latched = False
    if not crossed:
        return None
    return {"side": side, "level": level, "ref": prev}


def _move_hit(
    alert: PriceAlert,
    price: float,
    now: float,
    tape: deque[tuple[float, float]],
) -> dict[str, Any] | None:
    ref, ref_at = _reference(alert, now, tape)
    if ref is None or ref <= 0:
        return None
    delta = price - ref
    pct = delta / ref * 100.0
    metric = pct if alert.kind == "pct_change" else delta
    threshold = float(alert.threshold or 0)
    if threshold <= 0:
        return None
    direction = alert.direction
    if direction == "up":
        ok = metric >= threshold
    elif direction == "down":
        ok = metric <= -threshold
    else:
        ok = abs(metric) >= threshold
    if not ok:
        alert.latched = False
        return None
    side = "up" if delta >= 0 else "down"
    return {
        "side": side,
        "ref": ref,
        "ref_at": ref_at,
        "delta": delta,
        "pct": pct,
    }


def _reference(
    alert: PriceAlert,
    now: float,
    tape: deque[tuple[float, float]],
) -> tuple[float | None, float | None]:
    if alert.window_sec <= 0:
        return alert.anchor_price, alert.anchor_at
    cutoff = now - alert.window_sec
    # 窗口还没攒够：最早一笔必须早于 cutoff
    if not tape or tape[0][0] > cutoff:
        return None, None
    chosen: tuple[float, float] | None = None
    for ts, px in tape:
        if ts <= cutoff:
            chosen = (ts, px)
        else:
            break
    if chosen is None:
        return None, None
    return chosen[1], chosen[0]


def _event(alert: PriceAlert, price: float, now: float, hit: dict[str, Any]) -> dict[str, Any]:
    side = hit.get("side") or ""
    direction = "long" if side == "up" else "short" if side == "down" else "wait"
    return {
        "type": "alert",
        "rule": "price_alert",
        "title": describe_alert(alert),
        "symbol": alert.symbol,
        "timeframe": "tick",
        "direction": direction,
        "price": price,
        "marker_time": int(now),
        "reasons": [_reason(alert, price, hit)],
        "price_alert_id": alert.id,
        "note": alert.note,
    }


def describe_alert(alert: PriceAlert) -> str:
    base = alert.symbol.split("/")[0]
    if alert.kind == "above" and alert.price is not None:
        text = f"{base} 上穿 {_fmt_price(alert.price)}"
    elif alert.kind == "below" and alert.price is not None:
        text = f"{base} 下穿 {_fmt_price(alert.price)}"
    elif alert.kind == "cross" and alert.price is not None:
        arrow = {"up": "上穿", "down": "下穿"}.get(alert.direction, "达到")
        text = f"{base} {arrow} {_fmt_price(alert.price)}"
    elif alert.kind == "pct_change":
        text = f"{base} {_dir_word(alert.direction)} {alert.threshold:g}%/{_window_word(alert.window_sec)}"
    elif alert.kind == "abs_change":
        text = f"{base} {_dir_word(alert.direction)} {_fmt_price(float(alert.threshold or 0))}/{_window_word(alert.window_sec)}"
    else:
        text = f"{base} 价格告警"
    if alert.note:
        text = f"{text} · {alert.note}"
    return text


def telegram_text(alert: PriceAlert, event: dict[str, Any]) -> str:
    price = event.get("price")
    reasons = event.get("reasons") or []
    lines = [
        f"🔔 {describe_alert(alert)}",
        f"现价 {_fmt_price(float(price))}" if price is not None else "",
    ]
    if reasons:
        lines.append(str(reasons[0]))
    if not alert.repeat:
        lines.append("已触发，告警已关闭")
    return "\n".join(x for x in lines if x)


def _reason(alert: PriceAlert, price: float, hit: dict[str, Any]) -> str:
    if alert.kind in ("cross", "above", "below"):
        return f"现价 {_fmt_price(price)}，价位 {_fmt_price(float(hit['level']))}"
    window = _window_word(alert.window_sec)
    pct = hit.get("pct")
    delta = hit.get("delta")
    ref = hit.get("ref")
    pct_s = f"{pct:+.2f}%" if isinstance(pct, float) else ""
    delta_s = f"{delta:+.4g}" if isinstance(delta, float) else ""
    ref_s = _fmt_price(float(ref)) if isinstance(ref, float) else ""
    if alert.kind == "pct_change":
        return f"{window} {pct_s}（自 {ref_s} → {_fmt_price(price)}）"
    return f"{window} {delta_s}（{pct_s}，自 {ref_s}）"


def _dir_word(direction: str) -> str:
    return {"up": "上涨", "down": "下跌"}.get(direction, "波动")


def _window_word(window_sec: int) -> str:
    if window_sec <= 0:
        return "自设定起"
    if window_sec % 3600 == 0:
        return f"{window_sec // 3600}小时"
    if window_sec % 60 == 0:
        return f"{window_sec // 60}分钟"
    return f"{window_sec}秒"


def _build_alert(spec: dict[str, Any], *, now: float) -> PriceAlert:
    kind = str(spec.get("kind") or "").strip()
    if kind not in KINDS:
        raise ValueError(f"不支持的告警类型: {kind}")
    symbol = normalize_symbol(str(spec.get("symbol") or ""))
    direction = str(spec.get("direction") or "any").strip().lower()
    if kind == "above":
        direction = "up"
    elif kind == "below":
        direction = "down"
    if direction not in DIRECTIONS:
        raise ValueError("direction 须为 up / down / any")
    price = _opt_float(spec.get("price"))
    threshold = _opt_float(spec.get("threshold"))
    if kind in ("cross", "above", "below"):
        if price is None or price <= 0:
            raise ValueError("价位告警需要大于 0 的价格")
    else:
        if threshold is None or threshold <= 0:
            raise ValueError("涨跌告警需要大于 0 的阈值")
    window_sec = int(spec.get("window_sec") or 0)
    if window_sec < 0 or window_sec > 6 * 3600:
        raise ValueError("窗口需在 0 到 6 小时之间")
    cooldown = spec.get("cooldown_sec")
    cooldown_sec = 300 if cooldown is None else max(0, int(cooldown))
    return PriceAlert(
        id=uuid.uuid4().hex[:12],
        symbol=symbol,
        kind=kind,
        direction=direction,
        price=price,
        threshold=threshold,
        window_sec=window_sec,
        repeat=bool(spec.get("repeat")),
        cooldown_sec=cooldown_sec,
        enabled=True,
        note=str(spec.get("note") or "")[:80],
        created_at=now,
    )


def _alert_from_row(row: Any) -> PriceAlert | None:
    if not isinstance(row, dict) or not row.get("id"):
        return None
    try:
        return PriceAlert(
            id=str(row["id"]),
            symbol=normalize_symbol(str(row.get("symbol") or "")),
            kind=str(row.get("kind") or ""),
            direction=str(row.get("direction") or "any"),
            price=_opt_float(row.get("price")),
            threshold=_opt_float(row.get("threshold")),
            window_sec=int(row.get("window_sec") or 0),
            repeat=bool(row.get("repeat")),
            cooldown_sec=int(row.get("cooldown_sec") or 0),
            enabled=bool(row.get("enabled", True)),
            note=str(row.get("note") or ""),
            created_at=float(row.get("created_at") or 0),
            anchor_price=_opt_float(row.get("anchor_price")),
            anchor_at=_opt_float(row.get("anchor_at")),
            last_fired_at=_opt_float(row.get("last_fired_at")),
            fire_count=int(row.get("fire_count") or 0),
            last_price=None,
            latched=False,
        )
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num:  # NaN
        return None
    return num
