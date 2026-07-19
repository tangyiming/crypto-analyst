"""告警通道：终端 + 可选 Telegram。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from rich.console import Console
from rich.panel import Panel

logger = logging.getLogger("uvicorn.error")
console = Console()


@dataclass
class ConsoleNotifier:
    def send_text(self, text: str) -> None:
        console.print(Panel(text, title="📢 规则提醒", border_style="yellow"))


@dataclass
class TelegramNotifier:
    bot_token: str
    chat_id: str

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
    notifiers: list[ConsoleNotifier | TelegramNotifier]

    def send_text(self, text: str) -> None:
        for n in self.notifiers:
            try:
                n.send_text(text)
            except Exception as e:
                logger.exception("notifier text error: %s", e)


def build_default_notifier(
    *,
    telegram_bot_token: str = "",
    telegram_chat_id: str = "",
) -> MultiNotifier:
    items: list[ConsoleNotifier | TelegramNotifier] = [ConsoleNotifier()]
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
    rule = str(alert.get("rule") or "")
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
    if rule == "ai_plan":
        lines.append("AI 盯盘点评 · 仅提醒 · 不开仓")
    else:
        lines.append("规则提醒 only，不下单 / 非开仓信号")
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
