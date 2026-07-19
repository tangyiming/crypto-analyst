"""AI 每日交易复盘：事实 JSON → LLM 中文日报（失败降级为模板文本）。

原则：AI 只做「总结呈现」，事实全部来自系统数据，绝不让 AI 编数字。
定时推送见 monitor/digest_loop（UTC 每日一条）；按需生成 `analyst digest`。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analyst.config import get_settings

logger = logging.getLogger(__name__)


def _cache_path(name: str) -> Path:
    return Path(get_settings().data_cache_dir) / name


def _save_cache(name: str, out: dict[str, Any]) -> None:
    """缓存最近一次 AI 产出，供 Web 页面展示（失败静默）。"""
    try:
        out.setdefault(
            "generated_at",
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        )
        p = _cache_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, default=str))
    except Exception:
        logger.exception("%s save failed", name)


def _load_cache(name: str) -> dict[str, Any] | None:
    try:
        p = _cache_path(name)
        if p.is_file():
            data = json.loads(p.read_text())
            if isinstance(data, dict) and data.get("text"):
                return data
    except Exception:
        logger.warning("%s load failed", name, exc_info=True)
    return None


def save_last_digest(out: dict[str, Any]) -> None:
    _save_cache("last_digest.json", out)


def load_last_digest() -> dict[str, Any] | None:
    return _load_cache("last_digest.json")


def load_last_research() -> dict[str, Any] | None:
    return _load_cache("last_research.json")

DIGEST_SYSTEM = (
    "你是加密量化交易系统的复盘助手。用户给你一份 JSON 事实（纸面账户、"
    "持仓、分策略表现、资金费套利台账、市场相位、风控熔断状态）。"
    "写一份不超过 250 字的中文日报：\n"
    "1) 首行一句话总结（权益与当日变化）\n"
    "2) 各策略在做什么（有仓说仓，无仓说原因）\n"
    "3) 相位与展望一句话\n"
    "4) 若有熔断/停用策略/负收费等异常，必须点出\n"
    "只用 JSON 里的数字，禁止编造；语气平实，可用少量 emoji 分节。"
)


def build_digest_facts() -> dict[str, Any]:
    """聚合系统事实（全部来自本地状态与轻量 REST，可离线降级）。"""
    from analyst.trading.paper import get_paper_broker

    st = get_paper_broker().status()
    curve = st.get("equity_curve") or []
    day_chg_pct = None
    if len(curve) >= 2:
        prev, cur = curve[-2].get("equity"), curve[-1].get("equity")
        if prev:
            day_chg_pct = round((cur / prev - 1) * 100, 2)

    facts: dict[str, Any] = {
        "as_of_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "equity": st.get("equity"),
        "day_change_pct": day_chg_pct,
        "return_pct_total": st.get("return_pct"),
        "win_rate": st.get("win_rate"),
        "open_positions": [
            {
                "symbol": p.get("symbol"),
                "strategy": p.get("strategy"),
                "direction": p.get("direction"),
                "unrealized_pnl": p.get("unrealized_pnl"),
                "target_weight": p.get("target_weight"),
            }
            for p in (st.get("positions") or [])
        ],
        "by_strategy": st.get("by_strategy"),
        "carry_book": st.get("carry_book"),
        "risk_fuse": {
            "daily_fuse_active": (st.get("risk_fuse") or {}).get("daily_fuse_active"),
            "disabled_strategies": (st.get("risk_fuse") or {}).get(
                "disabled_strategies"
            ),
        },
        "recent_closed_trades": (st.get("recent_trades") or [])[:5],
    }

    # 市场相位（轻量：日线 800 根 + 200 日 EMA 双确认，与周期页同口径）
    try:
        from analyst.compute.indicators import ema
        from analyst.compute.strategies.cycle_switch import halving_phase
        from analyst.data.fetcher import fetch_candles_history

        series = fetch_candles_history(
            "BTC/USDT", "1d", days=800, market="futures", use_cache=True
        )
        closes = [c.close for c in series.candles]
        e200 = ema(closes, 200)
        band = 0.03
        ma_state = "bull"
        for i, c in enumerate(series.candles):
            if c.close > e200[i] * (1 + band):
                ma_state = "bull"
            elif c.close < e200[i] * (1 - band):
                ma_state = "bear"
        cal = halving_phase(series.candles[-1].timestamp)
        regime = (
            "bear" if (ma_state == "bear" and cal == "bear")
            else ("bull" if ma_state == "bull" else "accum")
        )
        facts["market"] = {
            "regime": regime,
            "calendar_phase": cal,
            "btc_price": round(closes[-1], 1),
            "btc_vs_ema200d_pct": round((closes[-1] / e200[-1] - 1) * 100, 1),
        }
    except Exception as e:
        logger.warning("digest 相位获取失败（降级跳过）：%s", e)

    # 汇率对相对强弱（ETH/BTC 等；轻量 REST + 缓存，失败降级跳过）
    try:
        from analyst.data.fetcher import fetch_candles_history
        from analyst.monitor.ratio import (
            build_ratio_closes,
            evaluate_ratio_state,
            parse_ratio_pair,
        )

        s = get_settings()
        if getattr(s, "monitor_ratio_enabled", True):
            ema_days = int(s.monitor_ratio_ema_days or 200)
            hist: dict[str, list] = {}

            def _daily(sym: str) -> list:
                if sym not in hist:
                    hist[sym] = fetch_candles_history(
                        sym, "1d", days=ema_days * 2,
                        market="futures", use_cache=True,
                    ).candles
                return hist[sym]

            ratios: dict[str, Any] = {}
            for pair in (s.monitor_ratio_pairs or "").split(","):
                legs = parse_ratio_pair(pair)
                if not legs:
                    continue
                _, closes = build_ratio_closes(_daily(legs[0]), _daily(legs[1]))
                st = evaluate_ratio_state(
                    closes,
                    ema_n=ema_days,
                    band=float(s.monitor_ratio_band or 0.02),
                    break_n=int(s.monitor_ratio_break_days or 40),
                )
                if st:
                    ratios[pair.strip().upper()] = {
                        "ratio": round(st.ratio, 6),
                        "vs_ema_pct": st.ema_dist_pct,
                        "state": st.ema_state,
                    }
            if ratios:
                facts["relative_strength"] = {
                    "note": f"vs {ema_days}日EMA；above=资金外溢山寨，below=BTC独强",
                    "pairs": ratios,
                }
    except Exception as e:
        logger.warning("digest 汇率对获取失败（降级跳过）：%s", e)
    return facts


def _template_digest(facts: dict[str, Any]) -> str:
    """LLM 不可用时的确定性模板（日报绝不缺席）。"""
    eq = facts.get("equity")
    chg = facts.get("day_change_pct")
    lines = [
        f"📋 交易日报 {facts.get('as_of_utc', '')} UTC",
        f"权益 {eq}U"
        + (f"（当日 {chg:+.2f}%）" if isinstance(chg, (int, float)) else ""),
    ]
    pos = facts.get("open_positions") or []
    if pos:
        lines.append(
            "持仓：" + "；".join(
                f"{p['strategy']} {p['direction']} {p['symbol']}"
                f"（浮 {p.get('unrealized_pnl', 0):+.2f}）"
                for p in pos
            )
        )
    else:
        lines.append("持仓：无")
    for c in facts.get("carry_book") or []:
        lines.append(
            f"carry：{c['symbol']} 名义 {c['notional']}U 累计收费 {c['accrued']:+.4f}U"
        )
    m = facts.get("market") or {}
    if m:
        zh = {"bull": "牛市", "bear": "熊市", "accum": "筑底"}
        dev = m.get("btc_vs_ema200d_pct")
        dev_s = f"{dev:+.1f}%" if isinstance(dev, (int, float)) else "-"
        lines.append(
            f"相位：{zh.get(m.get('regime'), m.get('regime'))}"
            f" · BTC {m.get('btc_price')}（距200日线 {dev_s}）"
        )
    rs = (facts.get("relative_strength") or {}).get("pairs") or {}
    if rs:
        zh = {"above": "山寨强", "below": "BTC强"}
        lines.append(
            "相对强弱：" + "；".join(
                f"{k} {zh.get(v['state'], v['state'])}（{v['vs_ema_pct']:+.1f}%）"
                for k, v in rs.items()
            )
        )
    fuse = facts.get("risk_fuse") or {}
    if fuse.get("daily_fuse_active") or fuse.get("disabled_strategies"):
        lines.append(
            f"⚠️ 熔断：日内={'是' if fuse.get('daily_fuse_active') else '否'}"
            f" 停用={fuse.get('disabled_strategies') or '无'}"
        )
    lines.append("（模板版：LLM 线路不可用）")
    return "\n".join(lines)


def compose_daily_digest(facts: dict[str, Any] | None = None) -> dict[str, Any]:
    """生成日报文本。返回 {text, model, source}。"""
    facts = facts or build_digest_facts()
    settings = get_settings()
    payload = json.dumps(facts, ensure_ascii=False, default=str)

    from analyst.llm.chat import _iter_chat_clients

    start = time.time()
    for client, model, prov in _iter_chat_clients(settings):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": DIGEST_SYSTEM},
                    {"role": "user", "content": payload},
                ],
                temperature=0.3,
                max_tokens=700,
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                out = {
                    "text": text,
                    "model": model,
                    "provider": prov,
                    "latency_ms": int((time.time() - start) * 1000),
                    "source": "llm",
                    "facts": facts,
                }
                save_last_digest(out)
                return out
        except Exception as e:
            logger.warning("digest LLM %s 失败：%s", prov, e)
    out = {"text": _template_digest(facts), "source": "template", "facts": facts}
    save_last_digest(out)
    return out


# ── 研究助手：AI 提可回测假设，回测当法官 ──

# 已证伪清单：防 AI 重提走过的死路（与项目记忆 regime-strategy-lessons 同步维护）
FALSIFIED_IDEAS = [
    "自建 EMA 快慢线相位检测替代减半日历×200日线（全面跑输）",
    "山寨币各自判相位（必须 BTC 定调）",
    "bull/accum 相位内做多向均值回归（不稳健）",
    "唐奇安入场加 ATR 突破缓冲（三币不一致）",
    "chop_range 止损后冷却期（震荡段收益转负）",
    "double_line 双线反转实盘开仓（近2年 1h/4h 净亏已退役）",
    "手动/规则化跳策略切换（零延迟也跑输 cycle_switch 自动版）",
]

RESEARCH_SYSTEM = (
    "你是加密量化策略研究员。系统现有策略：cycle_switch（减半日历×200日线相位，"
    "牛市唐奇安只多/熊市反弹空+破位空）、xs_momentum（14天横截面动量 top2，周调仓）、"
    "funding_carry（费率EMA门槛 delta 中性收费）。回测框架支持：分市况统计、"
    "资金费成本、波动率目标化、滚动窗口验证（5年×多币，4h）。\n"
    "根据 system_facts 提出 3~5 个【可用现有回测框架验证】的改进假设。"
    "每个假设给出：名称、逻辑依据（一句话）、如何验证（具体到可执行的回测对比）、"
    "预期风险。禁止提 falsified_ideas 里已证伪的路线；禁止提无法回测的想法"
    "（如让 AI 预测价格）。用中文，紧凑排版。"
)


def compose_research_ideas() -> dict[str, Any]:
    """生成研究假设。返回 {text, model, source}；LLM 全挂时 text 为空并带 error。"""
    facts = build_digest_facts()
    settings = get_settings()
    payload = json.dumps(
        {"system_facts": facts, "falsified_ideas": FALSIFIED_IDEAS},
        ensure_ascii=False,
        default=str,
    )

    from analyst.llm.chat import _iter_chat_clients

    start = time.time()
    for client, model, prov in _iter_chat_clients(settings):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": RESEARCH_SYSTEM},
                    {"role": "user", "content": payload},
                ],
                temperature=0.6,
                max_tokens=1500,
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                out = {
                    "text": text,
                    "model": model,
                    "provider": prov,
                    "latency_ms": int((time.time() - start) * 1000),
                    "source": "llm",
                }
                _save_cache("last_research.json", out)
                return out
        except Exception as e:
            logger.warning("research LLM %s 失败：%s", prov, e)
    return {"text": "", "source": "none", "error": "无可用 LLM 线路"}
