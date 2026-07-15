"""告警通道：终端 + 可选 Telegram。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx
from rich.console import Console
from rich.panel import Panel

from analyst.compute.strategies.double_line_reversal import DoubleLineSignal

logger = logging.getLogger("uvicorn.error")
console = Console()


class Notifier(Protocol):
    def notify(self, symbol: str, timeframe: str, signal: DoubleLineSignal) -> None: ...


@dataclass
class ConsoleNotifier:
    def notify(self, symbol: str, timeframe: str, signal: DoubleLineSignal) -> None:
        plan = signal.plan
        kelly = signal.kelly
        body = (
            f"[bold]{signal.direction.upper()}[/bold]  strength={signal.strength:.2f}\n"
            f"价格：{signal.price:.4f}\n"
            f"形态：{signal.pattern or '-'}  突破位：{signal.break_level or '-'}\n"
            f"原因：{'；'.join(signal.reasons)}\n"
        )
        if plan and plan.direction != "wait":
            body += (
                f"入场：{plan.entry_low:.2f}-{plan.entry_high:.2f}  "
                f"止损：{plan.stop_loss:.2f}  "
                f"止盈：{plan.take_profit_1:.2f}  R:R={plan.rr_ratio:.2f}\n"
            )
        if kelly:
            body += (
                f"Kelly：建议仓位 {kelly.suggested_fraction:.2%} "
                f"(风险≈{kelly.risk_budget_pct:.2f}%) — {kelly.note}\n"
            )
        if signal.trail_note:
            body += f"{signal.trail_note}\n"
        body += "[dim]仅提醒，不自动下单[/dim]"
        console.print(
            Panel(
                body,
                title=f"🚨 可交易提醒 · {symbol} · {timeframe}",
                border_style="red" if signal.direction == "short" else "green",
            )
        )


@dataclass
class TelegramNotifier:
    bot_token: str
    chat_id: str

    def notify(self, symbol: str, timeframe: str, signal: DoubleLineSignal) -> None:
        plan = signal.plan
        kelly = signal.kelly
        lines = [
            f"🚨 {symbol} {timeframe} · {signal.direction.upper()}",
            f"price={signal.price:.4f} strength={signal.strength:.2f}",
            f"pattern={signal.pattern} break={signal.break_level}",
            "；".join(signal.reasons),
        ]
        if plan and plan.direction != "wait":
            lines.append(
                f"entry {plan.entry_low:.2f}-{plan.entry_high:.2f} "
                f"SL {plan.stop_loss:.2f} TP {plan.take_profit_1:.2f} "
                f"RR {plan.rr_ratio:.2f}"
            )
        if kelly:
            lines.append(
                f"Kelly size {kelly.suggested_fraction:.2%} "
                f"risk≈{kelly.risk_budget_pct:.2f}%"
            )
        lines.append("提醒 only，不下单")
        self.send_text("\n".join(lines))

    def send_text(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        preview = text.replace("\n", " / ")[:120]
        try:
            r = httpx.post(
                url,
                json={"chat_id": self.chat_id, "text": text},
                timeout=15.0,
            )
            if r.status_code >= 400:
                logger.error(
                    "Telegram HTTP %s chat_id=%s body=%s preview=%s",
                    r.status_code,
                    self.chat_id,
                    (r.text or "")[:300],
                    preview,
                )
                r.raise_for_status()
            logger.info(
                "Telegram ok chat_id=***%s preview=%s",
                str(self.chat_id)[-4:],
                preview,
            )
        except Exception as e:
            logger.error("Telegram notify failed: %s preview=%s", e, preview)


@dataclass
class MultiNotifier:
    notifiers: list[Notifier]

    def notify(self, symbol: str, timeframe: str, signal: DoubleLineSignal) -> None:
        for n in self.notifiers:
            try:
                n.notify(symbol, timeframe, signal)
            except Exception as e:
                logger.exception("notifier error: %s", e)

    def send_text(self, text: str) -> None:
        for n in self.notifiers:
            try:
                if isinstance(n, TelegramNotifier):
                    n.send_text(text)
                elif isinstance(n, ConsoleNotifier):
                    console.print(Panel(text, title="📢 规则提醒", border_style="yellow"))
            except Exception as e:
                logger.exception("notifier text error: %s", e)


def build_default_notifier(
    *,
    telegram_bot_token: str = "",
    telegram_chat_id: str = "",
) -> MultiNotifier:
    items: list[Notifier] = [ConsoleNotifier()]
    tok = (telegram_bot_token or "").strip()
    chat = (telegram_chat_id or "").strip()
    if tok and chat:
        items.append(TelegramNotifier(bot_token=tok, chat_id=chat))
    else:
        logger.debug(
            "Telegram 未启用：token=%s chat_id=%s",
            bool(tok),
            bool(chat),
        )
    return MultiNotifier(items)


def format_rule_alert_text(symbol: str, timeframe: str, alert: dict) -> str:
    title = alert.get("title") or alert.get("rule") or "规则"
    dir_ = (alert.get("direction") or "").upper()
    lines = [
        f"📡 {title} · {symbol} {timeframe} · {dir_}",
        f"price={alert.get('price')}",
    ]
    reasons = alert.get("reasons") or []
    if reasons:
        lines.append("；".join(str(x) for x in reasons[:4]))
    plan = alert.get("plan") or {}
    if plan.get("stop_loss") is not None:
        lines.append(
            f"entry {plan.get('entry_low')}-{plan.get('entry_high')} "
            f"SL {plan.get('stop_loss')} TP {plan.get('take_profit_1')}"
        )
    lines.append("规则提醒 only，不下单 / 非 AI")
    return "\n".join(lines)


def format_cycle_alert_text(
    symbol: str,
    timeframe: str,
    signal: object,
    calendar: object | None = None,
) -> str:
    """cycle_switch 仓位变化 Telegram 文案。"""
    from analyst.compute.cycle_theory import (
        WolfyCalendarState,
        format_milestone_countdown,
    )
    from analyst.compute.strategies.cycle_switch import CycleSwitchSignal

    if not isinstance(signal, CycleSwitchSignal):
        return f"cycle_switch · {symbol} {timeframe}"
    zh = {"bull": "牛市", "bear": "熊市", "accum": "筑底"}
    prev = (
        "做多" if signal.prev_position > 0
        else ("做空" if signal.prev_position < 0 else "空仓")
    )
    now = (
        "做多 100%" if signal.target_position > 0
        else (
            f"做空 {abs(signal.target_position):.0%}"
            if signal.target_position < 0
            else "空仓"
        )
    )
    lines = [
        f"🧭 cycle_switch · {symbol} {timeframe}",
    ]
    if isinstance(calendar, WolfyCalendarState):
        lines.append(format_milestone_countdown(calendar))
    lines.extend([
        f"相位 {zh.get(signal.market_regime, signal.market_regime)} "
        f"(日历 {zh.get(signal.calendar_phase, signal.calendar_phase)})",
        f"仓位 {prev} → {now} · price={signal.price:.6g}",
        "；".join(signal.reasons[:3]),
        "周期策略提醒 only，不下单",
    ])
    return "\n".join(lines)
