"""监控页追问：结合当前合约上下文的纯文本问答（非结构化 tool 计划）。"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from analyst.config import get_settings

logger = logging.getLogger(__name__)

CHAT_SYSTEM = """你是加密 U 本位永续合约助手，回答用户关于当前盘面的追问。
约束：
1. 结合提供的上下文（指标/结构/规则基线/AI 计划）作答，勿编造不存在的数据。
2. 给出可核对的价位与逻辑时尽量具体；信息不足要明确说不确定。
3. 这不是投资建议；提醒杠杆与风控，勿鼓动满仓。
4. 用简体中文，简洁分点，一般不超过 350 字。
"""


def _resolve_openai_client(settings):
    from openai import OpenAI

    from analyst.llm.analyst import DEFAULT_BASE_URLS

    groq_key = (getattr(settings, "groq_api_key", "") or "").strip()
    if groq_key and getattr(settings, "llm_try_groq_first", True):
        return (
            OpenAI(
                api_key=groq_key,
                base_url=settings.groq_base_url or "https://api.groq.com/openai/v1",
            ),
            settings.groq_model or "llama-3.3-70b-versatile",
            "groq",
        )

    prov = (settings.llm_provider or "deepseek").lower()
    if prov == "anthropic":
        # 追问走 OpenAI 兼容优先；若仅有 anthropic，退回 deepseek/openai key
        api_key = settings.deepseek_api_key or settings.openai_api_key
        base = settings.llm_base_url or DEFAULT_BASE_URLS.get("deepseek")
        model = settings.llm_model
        if not api_key:
            raise RuntimeError("追问需要 GROQ / DeepSeek / OpenAI 兼容密钥（Anthropic 主线路暂不支持纯文本 chat）")
        return OpenAI(api_key=api_key, base_url=base), model, "deepseek"

    api_key = (
        settings.deepseek_api_key if prov == "deepseek" else settings.openai_api_key
    )
    if not api_key:
        raise RuntimeError(f"{prov.upper()}_API_KEY 未配置")
    base = settings.llm_base_url or DEFAULT_BASE_URLS.get(prov)
    return OpenAI(api_key=api_key, base_url=base), settings.llm_model, prov


def _context_block(symbol: str, timeframe: str, context: dict[str, Any] | None) -> str:
    ctx = context or {}
    lines = [
        f"品种：{symbol}（U 本位永续）",
        f"分析周期：{timeframe}",
        f"现价：{ctx.get('current_price', '—')}",
    ]
    st = ctx.get("structure") or {}
    if st:
        lines.append(
            f"结构：trend={st.get('trend')} 支撑={st.get('supports')} 阻力={st.get('resistances')}"
        )
    fib = ctx.get("fib") or {}
    if fib:
        lines.append(
            f"Fib：0.5={fib.get('retr_500')} 0.618={fib.get('retr_618')} 0.786={fib.get('retr_786')}"
        )
    ind = ctx.get("indicators") or {}
    if ind:
        macd = ind.get("macd") or {}
        ema = ind.get("ema") or {}
        vol = ind.get("volume") or {}
        lines.append(
            f"指标：MACD cross={macd.get('cross_signal')} hist={macd.get('histogram')} "
            f"EMA7/30={ema.get('ema7')}/{ema.get('ema30')} 量能={vol.get('signal')}×{vol.get('ratio')}"
        )
    base = ctx.get("baseline_plan") or {}
    if base:
        lines.append(
            f"规则基线：{base.get('direction')} entry {base.get('entry_low')}-{base.get('entry_high')} "
            f"SL {base.get('stop_loss')} TP {base.get('take_profit_1')} RR {base.get('rr_ratio')}"
        )
    ai = ctx.get("ai_plan") or {}
    if ai:
        lines.append(
            f"AI计划：{ai.get('direction')} entry {ai.get('entry_low')}-{ai.get('entry_high')} "
            f"SL {ai.get('stop_loss')} TP {ai.get('take_profit_1')} RR {ai.get('rr_ratio')}"
        )
        if ai.get("rationale"):
            lines.append(f"AI论述摘要：{str(ai.get('rationale'))[:500]}")
    return "\n".join(lines)


def ask_monitor_question(
    question: str,
    *,
    symbol: str,
    timeframe: str = "4h",
    context: dict[str, Any] | None = None,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """返回 {reply, model, latency_ms}。"""
    q = (question or "").strip()
    if not q:
        raise ValueError("问题不能为空")
    if len(q) > 2000:
        raise ValueError("问题过长（最多 2000 字）")

    settings = get_settings()
    client, model, prov = _resolve_openai_client(settings)

    messages: list[dict[str, str]] = [
        {"role": "system", "content": CHAT_SYSTEM},
        {
            "role": "user",
            "content": "【当前盘面上下文】\n" + _context_block(symbol, timeframe, context),
        },
    ]
    for turn in (history or [])[-8:]:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:2500]})
    messages.append({"role": "user", "content": q})

    max_tokens = min(1024, int(getattr(settings, "llm_max_tokens", 2000) or 2000))
    start = time.time()
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=min(0.5, float(settings.llm_temperature or 0.3)),
        max_tokens=max_tokens,
    )
    latency_ms = int((time.time() - start) * 1000)
    msg = resp.choices[0].message
    reply = (getattr(msg, "content", None) or "").strip()
    if not reply:
        raise RuntimeError(f"{prov} 返回空回复")

    return {
        "reply": reply,
        "model": model,
        "provider": prov,
        "latency_ms": latency_ms,
        "symbol": symbol,
        "timeframe": timeframe,
    }
