"""LLM 调用器 - 多 provider 支持。

支持的 provider:
- deepseek (默认，OpenAI 兼容)
- openai (GPT / 兼容网关)
- anthropic (Claude 系列)
- 可选链路：**Groq**（短 prompt）→ **b.ai**（BAI_* 完整 v1 prompt）→ **LLM_PROVIDER**（常为 DeepSeek）
  - 不配 Groq 时：b.ai → LLM_PROVIDER

通过 settings.llm_provider 确定主线路；Groq / b.ai 为附加前置层。所有线路均走 tool calling 输出
结构化结果，避免解析失败。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from analyst.compute.plan import TradePlan
from analyst.config import get_settings
from analyst.llm.prompts import load_system_prompt, load_user_template

_log = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    plan: TradePlan
    raw_text: str
    model: str
    prompt_version: str
    cost_usd: float
    latency_ms: int


# ═══════════════════════════════════════════════════════════════
# Tool Schema - 各 provider 通用
# ═══════════════════════════════════════════════════════════════
ANALYSIS_PARAMETERS = {
    "type": "object",
    "properties": {
        "direction": {
            "type": "string",
            "enum": ["long", "short", "wait"],
            "description": "主推方向",
        },
        "confidence": {
            "type": "integer",
            "description": "信号强度 1-5",
        },
        "entry_low": {"type": "number"},
        "entry_high": {"type": "number"},
        "stop_loss": {"type": "number"},
        "take_profit_1": {"type": "number"},
        "take_profit_2": {
            "type": ["number", "null"],
            "description": "可选第二目标",
        },
        "rr_ratio": {
            "type": "number",
            "description": "盈亏比；< 2 时 direction 必须为 wait",
        },
        "key_supports": {
            "type": "array",
            "items": {"type": "number"},
            "description": "主要支撑位（最多 3 个）",
        },
        "key_resistances": {
            "type": "array",
            "items": {"type": "number"},
            "description": "主要阻力位（最多 3 个）",
        },
        "pivot_level": {"type": "number", "description": "多空分界位"},
        "rationale": {"type": "string", "description": "完整论述（200-500 字）"},
        "invalidation": {"type": "string", "description": "失效条件，一句话"},
    },
    "required": [
        "direction", "confidence", "entry_low", "entry_high",
        "stop_loss", "take_profit_1", "rr_ratio",
        "key_supports", "key_resistances", "pivot_level",
        "rationale", "invalidation",
    ],
}

# Anthropic 格式
ANTHROPIC_TOOL = {
    "name": "submit_analysis",
    "description": "提交结构化的市场分析与交易计划",
    "input_schema": ANALYSIS_PARAMETERS,
}

# OpenAI / DeepSeek 格式
OPENAI_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_analysis",
        "description": "提交结构化的市场分析与交易计划",
        "parameters": ANALYSIS_PARAMETERS,
    },
}


# ═══════════════════════════════════════════════════════════════
# 价格表 (USD per million tokens)
# ═══════════════════════════════════════════════════════════════
PRICING: dict[str, dict[str, float]] = {
    # Groq 免费层（全部记为 0）
    "llama-3.3-70b-versatile": {"input": 0.0, "output": 0.0},
    "llama-3.1-8b-instant": {"input": 0.0, "output": 0.0},
    "qwen-2.5-72b-instruct": {"input": 0.0, "output": 0.0},
    "deepseek-r1-distill-llama-70b": {"input": 0.0, "output": 0.0},
    # DeepSeek 官方
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    # b.ai 聚合网关（注意 model id 用横杠）
    "deepseek-v4-pro": {"input": 0.435, "output": 0.87},
    "deepseek-v4-flash": {"input": 0.14, "output": 0.28},
    "minimax-m2.5": {"input": 0.30, "output": 1.20},
    "kimi-k2.5": {"input": 0.23, "output": 3.00},
    "glm-5": {"input": 0.30, "output": 2.55},
    "gpt-5.5": {"input": 5.0, "output": 30.0},
    "gpt-5.4": {"input": 2.5, "output": 15.0},
    "claude-opus-4.7": {"input": 5.0, "output": 25.0},
    "claude-opus-4.6": {"input": 5.0, "output": 25.0},
    "claude-opus-4.5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-4.6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4.5": {"input": 3.0, "output": 15.0},
    "claude-haiku-4.5": {"input": 1.0, "output": 5.0},
    "gemini-3.1-pro": {"input": 2.0, "output": 12.0},
    "gemini-3-flash": {"input": 0.5, "output": 3.0},
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4-turbo": {"input": 10.0, "output": 30.0},
    # Anthropic
    "claude-3-5-sonnet-20241022": {"input": 3.0, "output": 15.0},
    "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.0},
    "claude-3-opus-20240229": {"input": 15.0, "output": 75.0},
    "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
}


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """根据模型估算成本（USD）。"""
    p = PRICING.get(model, {"input": 1.0, "output": 3.0})
    return input_tokens / 1_000_000 * p["input"] + output_tokens / 1_000_000 * p["output"]


# ═══════════════════════════════════════════════════════════════
# 主入口：根据 provider 路由
# ═══════════════════════════════════════════════════════════════
def analyze_market(
    market_snapshot: dict,
    indicators_snapshot: dict,
    user_account: dict | None = None,
) -> LLMResponse:
    """让 AI 分析市场并给出交易计划。"""
    settings = get_settings()
    provider = settings.llm_provider.lower()

    system = load_system_prompt(settings.llm_prompt_version)
    template = load_user_template(settings.llm_prompt_version)
    user_msg = _build_user_message(template, market_snapshot, indicators_snapshot, user_account)

    def _fallback() -> LLMResponse:
        if provider == "anthropic":
            return _call_anthropic(system, user_msg, settings)
        if provider in ("deepseek", "openai"):
            return _call_openai_compatible(system, user_msg, settings)
        raise ValueError(
            f"不支持的 LLM_PROVIDER: {provider}（可选: deepseek / openai / anthropic）"
        )

    groq_key = (getattr(settings, "groq_api_key", "") or "").strip()
    if groq_key and getattr(settings, "llm_try_groq_first", True):
        try:
            sys_g = load_system_prompt("groq")
            tpl_g = load_user_template("groq")
            user_g = _build_user_message(
                tpl_g,
                market_snapshot,
                indicators_snapshot,
                user_account,
                recent_lessons_max_items=3,
            )
            g_cap = int(getattr(settings, "groq_max_tokens", 4096) or 4096)
            g_cap = max(1024, min(g_cap, 8192))
            user_cap = max(1024, int(settings.llm_max_tokens or 4000))
            eff_max = min(g_cap, user_cap)
            return _call_openai_compatible(
                sys_g,
                user_g,
                settings,
                override_api_key=groq_key,
                override_base_url=settings.groq_base_url or "https://api.groq.com/openai/v1",
                override_model=settings.groq_model,
                override_max_tokens=eff_max,
                routing_provider="groq",
                skip_deepseek_extras=True,
                prompt_version_for_response="groq",
            )
        except Exception as exc:
            _log.warning("Groq 请求失败，尝试 b.ai 或主线路: %s", exc)

    bai_key = (getattr(settings, "bai_api_key", "") or "").strip()
    bai_model = (getattr(settings, "bai_model", "") or "").strip()
    if (
        bai_key
        and bai_model
        and getattr(settings, "llm_try_bai_after_groq", True)
    ):
        try:
            b_max = max(1024, min(int(settings.llm_max_tokens or 4000), 8192))
            return _call_openai_compatible(
                system,
                user_msg,
                settings,
                override_api_key=bai_key,
                override_base_url=settings.bai_base_url or "https://api.b.ai/v1",
                override_model=bai_model,
                override_max_tokens=b_max,
                routing_provider="openai",
                skip_deepseek_extras=True,
                prompt_version_for_response=settings.llm_prompt_version,
            )
        except Exception as exc:
            _log.warning("b.ai 请求失败，采用主线路 %s: %s", provider, exc)

    return _fallback()


# ═══════════════════════════════════════════════════════════════
# Anthropic (Claude)
# ═══════════════════════════════════════════════════════════════
def _call_anthropic(system: str, user_msg: str, settings) -> LLMResponse:
    from anthropic import Anthropic

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 未配置")

    client = Anthropic(api_key=settings.anthropic_api_key)

    start = time.time()
    response = client.messages.create(
        model=settings.llm_model,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        system=system,
        tools=[ANTHROPIC_TOOL],
        tool_choice={"type": "tool", "name": "submit_analysis"},
        messages=[{"role": "user", "content": user_msg}],
    )
    latency_ms = int((time.time() - start) * 1000)

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        raise RuntimeError(f"Anthropic 未返回 tool_use。响应: {response.content}")

    payload = tool_use.input
    cost = _estimate_cost(
        settings.llm_model,
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    return LLMResponse(
        plan=_payload_to_plan(payload),
        raw_text=json.dumps(payload, ensure_ascii=False, indent=2),
        model=settings.llm_model,
        prompt_version=settings.llm_prompt_version,
        cost_usd=cost,
        latency_ms=latency_ms,
    )


# ═══════════════════════════════════════════════════════════════
# OpenAI / DeepSeek（共用 OpenAI SDK + 兼容协议）
# ═══════════════════════════════════════════════════════════════
DEFAULT_BASE_URLS = {
    # 官方文档：https://api-docs.deepseek.com/zh-cn/ （勿加 /v1，与 OpenAI SDK 组合后路径正确）
    "deepseek": "https://api.deepseek.com",
    "openai": None,                     # OpenAI SDK 默认
}


def _is_bai_gateway(base_url: str | None) -> bool:
    url = (base_url or "").lower()
    return "api.b.ai" in url or url.endswith("b.ai/v1")


def _openai_tool_choice(base_url: str | None, provider: str):
    """b.ai 与 DeepSeek 官方 V4 / reasoner 等不支持强制指名 function，只能用 auto。"""
    if _is_bai_gateway(base_url):
        return "auto"
    if provider == "deepseek":
        return "auto"
    return {"type": "function", "function": {"name": "submit_analysis"}}


def _message_as_dict(msg: Any) -> dict[str, Any]:
    """将 SDK message 转为 dict，包含 reasoning_content 等扩展字段。"""
    try:
        d = msg.model_dump(mode="python")
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {}


def _assistant_message_combined_text(msg: Any) -> str:
    """合并 content / reasoning_content（DeepSeek thinking 下 content 常为空但 reasoning 有文）。"""
    parts: list[str] = []
    d = _message_as_dict(msg)
    for key in ("content", "reasoning_content", "reasoning"):
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    extra = getattr(msg, "model_extra", None)
    if isinstance(extra, dict):
        for key in ("reasoning_content", "reasoning"):
            v = extra.get(key)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return "\n\n".join(out)


def _tool_calls_from_message(message: Any) -> list[Any]:
    """优先用 SDK 解析的 tool_calls；为空时从 model_dump / function_call 兜底。"""
    tc = getattr(message, "tool_calls", None)
    if isinstance(tc, list) and len(tc) > 0:
        return tc
    d = _message_as_dict(message)
    raw = d.get("tool_calls")
    if isinstance(raw, list) and len(raw) > 0:
        return raw
    fc = d.get("function_call")
    if isinstance(fc, dict) and fc.get("name"):
        return [{"type": "function", "id": "function_call", "function": fc}]
    return []


def _tool_call_name_and_arguments(tc: Any) -> tuple[str | None, Any]:
    if tc is None:
        return None, None
    fn = getattr(tc, "function", None)
    if fn is not None:
        return getattr(fn, "name", None), getattr(fn, "arguments", None)
    if isinstance(tc, dict):
        fobj = tc.get("function")
        if isinstance(fobj, dict):
            return fobj.get("name"), fobj.get("arguments")
    return None, None


def _parse_submit_analysis_payload_from_tool_calls(
    tool_calls: list[Any],
) -> dict[str, Any] | None:
    for tc in tool_calls:
        name, args_raw = _tool_call_name_and_arguments(tc)
        if name and name != "submit_analysis":
            continue
        if args_raw is None:
            continue
        if isinstance(args_raw, dict):
            obj = args_raw
        else:
            s = str(args_raw).strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
        if isinstance(obj, dict) and "direction" in obj:
            return obj
    return None


def _effective_openai_max_tokens(settings) -> int:
    """DeepSeek V4 + thinking 会消耗大量输出 token，避免 length 截断导致无 content / 无 tool_calls。"""
    mt = int(getattr(settings, "llm_max_tokens", 4000) or 4000)
    if settings.llm_provider.lower() != "deepseek":
        return mt
    model = (settings.llm_model or "").lower()
    if model.startswith("deepseek-v4") and getattr(settings, "deepseek_thinking_enabled", True):
        return max(mt, 8192)
    return mt


def _extract_tool_payload_from_content(content: str | None) -> dict[str, Any] | None:
    """无 tool_calls 时，从 assistant 纯文本里抠 JSON（b.ai / 部分模型）。"""
    if not content:
        return None
    text = content.strip()
    for candidate in (text, ):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and "direction" in obj:
                return obj
        except json.JSONDecodeError:
            pass
    if "```" in text:
        start = text.find("```json")
        if start >= 0:
            start = text.find("\n", start) + 1
            end = text.find("```", start)
            if end > start:
                try:
                    obj = json.loads(text[start:end].strip())
                    if isinstance(obj, dict) and "direction" in obj:
                        return obj
                except json.JSONDecodeError:
                    pass
        start = text.find("```")
        if start >= 0:
            start = text.find("\n", start) + 1
            end = text.find("```", start)
            if end > start:
                try:
                    obj = json.loads(text[start:end].strip())
                    if isinstance(obj, dict) and "direction" in obj:
                        return obj
                except json.JSONDecodeError:
                    pass
    # 取首个完整对象
    i = text.find("{")
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[i : j + 1])
                    if isinstance(obj, dict) and "direction" in obj:
                        return obj
                except json.JSONDecodeError:
                    pass
                break
    return None


def _deepseek_v4_request_extras(settings) -> dict[str, Any]:
    """DeepSeek V4 官方推荐参数（与文档示例一致）。非 deepseek-v4 模型不注入。"""
    model = (settings.llm_model or "").lower()
    if not model.startswith("deepseek-v4"):
        return {}
    out: dict[str, Any] = {}
    eff = (getattr(settings, "deepseek_reasoning_effort", None) or "").strip()
    if eff:
        out["reasoning_effort"] = eff
    if getattr(settings, "deepseek_thinking_enabled", True):
        out["extra_body"] = {"thinking": {"type": "enabled"}}
    return out


def _call_openai_compatible(
    system: str,
    user_msg: str,
    settings,
    *,
    override_api_key: str | None = None,
    override_base_url: str | None = None,
    override_model: str | None = None,
    override_max_tokens: int | None = None,
    routing_provider: str | None = None,
    skip_deepseek_extras: bool = False,
    prompt_version_for_response: str | None = None,
) -> LLMResponse:
    from openai import OpenAI

    prov = (routing_provider or settings.llm_provider).lower()

    if override_api_key is not None:
        api_key = override_api_key
    else:
        api_key = (
            settings.deepseek_api_key
            if prov == "deepseek"
            else settings.openai_api_key
        )
    if not api_key:
        raise RuntimeError(
            f"{prov.upper()}_API_KEY 未配置，请检查 .env 文件"
        )

    if override_base_url is not None:
        base_url = override_base_url
    else:
        base_url = settings.llm_base_url or DEFAULT_BASE_URLS.get(prov)

    model = override_model or settings.llm_model

    client = OpenAI(api_key=api_key, base_url=base_url)

    start = time.time()
    max_tokens = (
        override_max_tokens
        if override_max_tokens is not None
        else _effective_openai_max_tokens(settings)
    )
    req: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        "tools": [OPENAI_TOOL],
        "tool_choice": _openai_tool_choice(base_url, prov),
        "temperature": settings.llm_temperature,
        "max_tokens": max_tokens,
    }
    if not skip_deepseek_extras:
        req.update(_deepseek_v4_request_extras(settings))
    response = client.chat.completions.create(**req)
    latency_ms = int((time.time() - start) * 1000)

    choice = response.choices[0]
    msg = choice.message
    tool_calls = _tool_calls_from_message(msg)
    payload = _parse_submit_analysis_payload_from_tool_calls(tool_calls)
    if payload is None:
        payload = _extract_tool_payload_from_content(_assistant_message_combined_text(msg))
    if payload is None:
        fr = getattr(choice, "finish_reason", None)
        combined = _assistant_message_combined_text(msg)
        raise RuntimeError(
            f"{prov} 未返回 submit_analysis 的有效 tool_calls，且无法从文本解析 JSON。"
            f" finish_reason={fr!r}。"
            f" 合并文本节选: {combined[:500]!r}"
        )

    usage = response.usage
    input_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
    output_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
    cost = _estimate_cost(model, input_tokens, output_tokens)
    pv = prompt_version_for_response or settings.llm_prompt_version

    return LLMResponse(
        plan=_payload_to_plan(payload),
        raw_text=json.dumps(payload, ensure_ascii=False, indent=2),
        model=model,
        prompt_version=pv,
        cost_usd=cost,
        latency_ms=latency_ms,
    )


# ═══════════════════════════════════════════════════════════════
# 共用辅助
# ═══════════════════════════════════════════════════════════════
def _safe(d: dict, *path: str, default: str = "N/A") -> str:
    """安全提取嵌套字段，并格式化为字符串。"""
    cur: object = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p, default)
    if isinstance(cur, float):
        return f"{cur:.4f}"
    if cur is None:
        return "N/A"
    return str(cur)


def _format_derivatives_block(d: dict | None) -> str:
    """格式化衍生品区块。无数据时返回提示。"""
    if not d:
        return "（无永续合约数据，可能是该币种没有 USDT 永续）"
    return (
        f"- Funding: {d['funding_rate_pct']:+.4f}%/8h · {d['funding_sentiment']}\n"
        f"- Open Interest: {d['oi_signal']}（4h {d['oi_change_pct_4h']:+.2f}%）\n"
        f"- 大户多空比: {d['long_short_ratio']:.2f}（>1 多>空）\n"
        f"- Mark/Index: {d['mark_price']:.4f} / {d['index_price']:.4f}（基差 {d['basis_pct']:+.3f}%）"
    )


def _format_macro_block(m: dict | None) -> str:
    if not m:
        return "（宏观数据获取失败，分析时跳过）"
    return (
        f"- BTC 主导率: {m['btc_dominance']:.2f}% · ETH 主导率: {m['eth_dominance']:.2f}%\n"
        f"- 加密总市值: ${m['total_market_cap_usd']/1e12:.2f}T（24h {m['market_cap_change_24h']:+.2f}%）\n"
        f"- Fear & Greed: {m['fear_greed_index']}/100 · {m['fear_greed_emoji']}"
    )


def _recent_ai_lessons_markdown(max_items: int = 10, period_days: int = 90) -> str:
    """从已验证会话提炼简短复盘，供模型自我校准（不含用户主观计划）。"""
    from analyst.storage import repo

    try:
        rows = repo.list_verified_sessions(period_days)
    except Exception:
        return "（暂无法读取历史验证记录。）"
    if not rows:
        return "（尚无已验证记录；本次为纯盘面分析。）"
    lines: list[str] = []
    for s, _up, a, v in rows[:max_items]:
        sym = (s.symbol or "").replace("/USDT", "")
        lines.append(
            f"- #{s.id} {sym} {s.timeframe} | AI {a.direction} → {v.ai_outcome} | "
            f"R={v.ai_pnl_r:+.2f}（理论最优 R={v.optimal_pnl_r:+.2f}）"
        )
    return "\n".join(lines)


def _build_user_message(
    template: str,
    market: dict,
    indicators: dict,
    account: dict | None,
    *,
    recent_lessons_max_items: int = 10,
) -> str:
    """填充用户消息模板。"""
    captured_at = datetime.fromtimestamp(
        market["captured_at"], tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    settings = get_settings()
    account = account or {}

    daily = indicators.get("1d", {})
    h4 = indicators.get("4h", {})
    h1 = indicators.get("1h", {})

    derivatives_block = _format_derivatives_block(market.get("derivatives"))
    macro_block = _format_macro_block(market.get("macro"))
    recent_lessons = _recent_ai_lessons_markdown(max_items=recent_lessons_max_items)

    return template.format(
        symbol=market["symbol"],
        captured_at=captured_at,
        primary_timeframe=market.get("primary_timeframe") or settings.default_timeframe,
        current_price=market["current_price"],
        high_24h=market["high_24h"],
        low_24h=market["low_24h"],
        high_7d=market["high_7d"],
        low_7d=market["low_7d"],
        high_30d=market["high_30d"],
        low_30d=market["low_30d"],
        # 日线
        d_dif=_safe(daily, "macd", "dif"),
        d_dea=_safe(daily, "macd", "dea"),
        d_hist=_safe(daily, "macd", "histogram"),
        d_zero=_safe(daily, "macd", "above_zero"),
        d_signal=_safe(daily, "macd", "cross_signal"),
        d_ema7=_safe(daily, "ema", "ema7"),
        d_ema30=_safe(daily, "ema", "ema30"),
        d_ema52=_safe(daily, "ema", "ema52"),
        d_boll_u=_safe(daily, "boll", "upper"),
        d_boll_m=_safe(daily, "boll", "middle"),
        d_boll_l=_safe(daily, "boll", "lower"),
        d_vol_signal=_safe(daily, "volume", "signal"),
        d_obv_trend=_safe(daily, "volume", "obv_trend"),
        d_vol_ratio=_safe(daily, "volume", "ratio"),
        # 4h
        h4_dif=_safe(h4, "macd", "dif"),
        h4_dea=_safe(h4, "macd", "dea"),
        h4_hist=_safe(h4, "macd", "histogram"),
        h4_zero=_safe(h4, "macd", "above_zero"),
        h4_signal=_safe(h4, "macd", "cross_signal"),
        h4_ema7=_safe(h4, "ema", "ema7"),
        h4_ema30=_safe(h4, "ema", "ema30"),
        h4_ema52=_safe(h4, "ema", "ema52"),
        h4_boll_u=_safe(h4, "boll", "upper"),
        h4_boll_m=_safe(h4, "boll", "middle"),
        h4_boll_l=_safe(h4, "boll", "lower"),
        h4_vol_signal=_safe(h4, "volume", "signal"),
        h4_obv_trend=_safe(h4, "volume", "obv_trend"),
        h4_vol_ratio=_safe(h4, "volume", "ratio"),
        # 1h
        h1_dif=_safe(h1, "macd", "dif"),
        h1_dea=_safe(h1, "macd", "dea"),
        h1_hist=_safe(h1, "macd", "histogram"),
        h1_zero=_safe(h1, "macd", "above_zero"),
        h1_signal=_safe(h1, "macd", "cross_signal"),
        h1_ema7=_safe(h1, "ema", "ema7"),
        h1_ema30=_safe(h1, "ema", "ema30"),
        h1_ema52=_safe(h1, "ema", "ema52"),
        h1_vol_signal=_safe(h1, "volume", "signal"),
        h1_obv_trend=_safe(h1, "volume", "obv_trend"),
        # 资金面 + 宏观
        derivatives_block=derivatives_block,
        macro_block=macro_block,
        recent_lessons=recent_lessons,
        # 账户
        account_usd=account.get("account_usd", settings.default_account_usd),
        max_risk_pct=account.get("max_risk_pct", settings.max_risk_per_trade_pct),
        max_leverage=account.get("max_leverage", settings.max_leverage),
    )


def _payload_to_plan(payload: dict) -> TradePlan:
    def _opt_float(key: str) -> float | None:
        v = payload.get(key)
        if v is None or (isinstance(v, str) and v.strip().lower() in ("", "null", "none")):
            return None
        return float(v)

    def _req_float(key: str) -> float:
        v = payload.get(key)
        if v is None or (isinstance(v, str) and v.strip().lower() in ("", "null", "none")):
            raise ValueError(f"字段 {key!r} 为 null 或缺失，无法生成计划")
        return float(v)

    return TradePlan(
        direction=payload["direction"],
        entry_low=_req_float("entry_low"),
        entry_high=_req_float("entry_high"),
        stop_loss=_req_float("stop_loss"),
        take_profit_1=_req_float("take_profit_1"),
        take_profit_2=_opt_float("take_profit_2"),
        rr_ratio=_req_float("rr_ratio"),
        rationale=payload.get("rationale", ""),
    )
