"""Jack 三盘变化 → 币安广场短评（真发）。

仅处理 rule=jack_regime；品种/周期白名单与冷却由 Settings 控制。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from analyst.compute.jack_levels import JackLevels
from analyst.compute.jack_regime import JackRegime
from analyst.config import get_settings
from analyst.integrations.binance_square import SquareApiError, mask_key, post_text

logger = logging.getLogger(__name__)

DISCLAIMER = "⚠️ 非投资建议，仅供参考，盈亏自负。"

# 引流优先：cashtag 进币种页（高意向），再补 1～2 个币种话题；少堆通用标签
_COIN_TAGS: dict[str, tuple[str, ...]] = {
    "BTC": ("$BTC", "#BTC", "#Bitcoin"),
    "ETH": ("$ETH", "#ETH", "#Ethereum"),
    "BNB": ("$BNB", "#BNB"),
    "SOL": ("$SOL", "#SOL", "#Solana"),
    "AAVE": ("$AAVE", "#AAVE"),
}


def _norm_symbol(symbol: str) -> str:
    s = (symbol or "").upper().strip().replace("-", "/")
    if "/" not in s:
        if s.endswith("USDT") and len(s) > 4:
            s = f"{s[:-4]}/USDT"
        else:
            s = f"{s}/USDT"
    return s.split(":")[0]


def _base_asset(symbol: str) -> str:
    return _norm_symbol(symbol).split("/")[0]


def _cashtag(symbol: str) -> str:
    return f"${_base_asset(symbol)}"


def _tag_line(symbol: str) -> str:
    """只留 cashtag + 币种话题，进币种页/话题流；不堆通用标签。"""
    base = _base_asset(symbol)
    tags = _COIN_TAGS.get(base, (f"${base}", f"#{base}"))
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return " ".join(out)


def _fmt_price(x: float | None) -> str:
    if x is None:
        return "—"
    ax = abs(float(x))
    if ax >= 10:
        return f"{x:.2f}"  # Jack 写法：SOL 106.75 / BNB 812.34 / BTC 63750.00
    if ax >= 1:
        return f"{x:.3f}"
    return f"{x:.6f}"


def _side_zh(side: str) -> str:
    if side == "long":
        return "偏多"
    if side == "short":
        return "偏空"
    return "观望"


def _prediction_hook(regime: JackRegime, tf: str) -> str:
    """首行钩子：明确涨跌倾向，提高点击；结论仍绑三盘事实。"""
    side = regime.trade_side
    zh = regime.regime_zh or regime.regime
    if side == "long":
        if regime.regime == "strong_trend":
            return f"📈 看涨｜{zh} · {tf} 偏多延续，突破可跟"
        if regime.regime == "range":
            return f"📈 震荡看涨｜{zh} · {tf} 回踩低多"
        return f"📈 偏向看涨｜{zh} · {tf}"
    if side == "short":
        if regime.below_waist:
            return f"⏸ 不追空｜{zh} · 近腰斩线，穷寇莫追"
        if regime.regime == "weak_trend":
            return f"📉 看跌｜{zh} · {tf} 反弹高空"
        return f"📉 偏向看跌｜{zh} · {tf}"
    return f"👀 观望｜{zh} · {tf} 等边界再动手"


def _post_levels(regime: JackRegime, jack: JackLevels | None, price: float) -> tuple[float | None, float | None, float | None]:
    """帖子用（防守, 近压, 目标）：按方向做合理性过滤——多头目标/近压必须在现价上方，防守在下方。

    24h 锁点的 rebound_382/618 只在「跌后反弹」语境有意义，涨势里会落在现价下方，不能直接拿来当近压/目标。
    """
    above = lambda x: x is not None and x > price * 1.001  # noqa: E731
    below = lambda x: x is not None and x < price * 0.999  # noqa: E731
    j_def = getattr(jack, "defense_level", None) if jack is not None else None
    j_382 = getattr(jack, "rebound_382", None) if jack is not None else None
    j_618 = getattr(jack, "rebound_618", None) if jack is not None else None
    j_touch = getattr(jack, "touch_level", None) if jack is not None else None
    if regime.trade_side == "short":
        defense = next((x for x in (j_def, regime.nearest_resistance) if above(x)), None)
        near = next((x for x in (regime.nearest_support, j_618, j_382) if below(x)), None)
        target = next((x for x in (j_618, regime.nearest_support) if below(x) and (near is None or x <= near)), near)
        return defense, near, target
    defense = next((x for x in (j_def, regime.nearest_support) if below(x)), None)
    near = next((x for x in (regime.nearest_resistance, j_382, j_618, j_touch) if above(x)), None)
    target = next(
        (x for x in (j_618, j_touch, getattr(regime, "ext_150", None), getattr(regime, "ext_1618", None)) if above(x) and (near is None or x >= near)),
        None,
    )
    if target is None:
        target = next((x for x in (getattr(regime, "ext_150", None), getattr(regime, "ext_1618", None)) if above(x)), near)
    return defense, near, target


def _clip_sentence(text: str, n: int) -> str:
    """按句号/分号截断，避免半句话。"""
    t = (text or "").strip()
    if len(t) <= n:
        return t
    cut = t[:n]
    k = max(cut.rfind("。"), cut.rfind("；"))
    return cut[: k + 1] if k >= n // 3 else cut.rstrip("，、,") + "…"


def _outlook_line(regime: JackRegime, jack: JackLevels | None, price: float) -> str:
    """一句话涨跌预测 + 关键点位（数字来自预计算）。"""
    side = regime.trade_side
    if side == "long":
        stop, near, tgt = _post_levels(regime, jack, price)
        tgt = tgt or near
        if tgt is not None and stop is not None:
            return (
                f"预测：短线偏涨，上看 {_fmt_price(tgt)}；"
                f"跌破 {_fmt_price(stop)} 则看涨失效"
            )
        return "预测：短线偏涨，站稳后再加仓；破防守转观望"
    if side == "short":
        if regime.below_waist:
            return "预测：已近腰斩，暂不看更深下跌，宁可空仓等反抽"
        stop, near, tgt = _post_levels(regime, jack, price)
        tgt = tgt or near
        if tgt is not None and stop is not None:
            return (
                f"预测：短线偏跌，下看 {_fmt_price(tgt)}；"
                f"涨破 {_fmt_price(stop)} 则看跌失效"
            )
        return "预测：短线偏跌，反弹再空；破防守转观望"
    return "预测：方向不明，先观望，不追涨杀跌"


def compose_jack_square_post(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    jack: JackLevels | None,
    regime: JackRegime,
) -> str:
    """生成带币种标签 + 涨跌预测 + 点位的广场短评。"""
    tag = _cashtag(symbol)
    tf = (timeframe or "").strip().lower()
    side = _side_zh(regime.trade_side)
    lines = [
        f"{_prediction_hook(regime, tf)} {tag}",
        f"现价 {_fmt_price(price)} · 方向 {side}",
        _outlook_line(regime, jack, float(price)),
    ]
    defense, near, target = _post_levels(regime, jack, float(price))
    if regime.trade_side == "short":
        parts = [f"防守 {_fmt_price(defense)}" if defense else None, f"近支 {_fmt_price(near)}" if near else None,
                 f"下看 {_fmt_price(target)}" if target and target != near else None]
    else:
        parts = [f"防守 {_fmt_price(defense)}" if defense else None, f"近压 {_fmt_price(near)}" if near else None,
                 f"目标 {_fmt_price(target)}" if target and target != near else None]
    parts = [x for x in parts if x]
    if parts:
        lines.append("点位｜" + " · ".join(parts))
    play = (regime.playbook_line or "").strip()
    if play:
        lines.append(f"打法：{_clip_sentence(play, 160)}")
    if regime.trade_side == "long":
        lines.append("想跟单先看防守是否守住，别追在鱼尾。")
    elif regime.trade_side == "short" and not regime.below_waist:
        lines.append("想开空等反弹靠近阻力，别贴着支撑追空。")
    lines.append("")
    lines.extend(indicator_block(regime, jack, price=price))
    text = "\n".join(lines)
    if len(text) > MAX_POST_LEN:
        text = text[: MAX_POST_LEN - 1] + "…"
    return text


MAX_POST_LEN = 1500



def _round_near(regime, price: float, max_dist: float) -> bool:
    """整数关口离现价太远（如 SOL 102 → 150）就别当首压写进帖子。"""
    lv = getattr(regime, "round_level", None)
    if lv is None or not price or price <= 0:
        return False
    return abs(lv / price - 1.0) <= max_dist


def _waist_note(regime, price: float) -> str:
    """腰斩线备注：below_waist 带 2% 缓冲，措辞要按真实位置区分「在其下」和「贴着」。"""
    wl = getattr(regime, "waist_line", None)
    if not wl or not getattr(regime, "below_waist", False):
        return ""
    if price and price < wl:
        return "（现价在其下，只低吸不追空）"
    return "（现价贴着腰斩线，只低吸不追空）"

def indicator_block(regime: JackRegime, jack: JackLevels | None, eric_readings: list[str] | None = None, price: float = 0.0) -> list[str]:
    """指标分析段：多周期 BOLL / MACD 动能 / 均线 / 关键位 / 大周期，给读者「为什么这么看」。"""
    f = _fmt_price
    out: list[str] = ["指标怎么看："]
    macd_bits = []
    if regime.macd_8h_decel or regime.macd_12h_decel:
        macd_bits.append("8h/12h MACD 柱在零下缩短，下跌动能减弱" if regime.trade_side != "long" else "8h/12h MACD 归零，回调动能在衰减")
    if regime.weekly_macd_zero:
        macd_bits.append("周线 MACD 归零轴，大级别回调接近尾声")
    if regime.accel_2d:
        macd_bits.append("2 日线 MACD 触零加速")
    if regime.golden_3d or regime.golden_5d:
        macd_bits.append(f"{'3日' if regime.golden_3d else ''}{'/' if regime.golden_3d and regime.golden_5d else ''}{'5日' if regime.golden_5d else ''}线金叉在形成")
    if regime.hollow_daily:
        macd_bits.append("日线空心阳加速")
    if macd_bits:
        out.append("动能：" + "；".join(macd_bits) + "。")
    if regime.boll_4h_mid is not None:
        out.append(f"4h BOLL：下轨 {f(regime.boll_4h_lower)} / 中轨 {f(regime.boll_4h_mid)} / 上轨 {f(regime.boll_4h_upper)}；12h 中轨 {f(regime.boll_12h_mid)}。")
    ma_bits = []
    if regime.ema12h_6 is not None:
        ma_bits.append(f"12h EMA6 {f(regime.ema12h_6)}（扎针参考）")
    if regime.boll_mid_3d is not None or regime.boll_mid_5d is not None:
        ma_bits.append(f"3日/5日 BOLL 中轨 {f(regime.boll_mid_3d)} / {f(regime.boll_mid_5d)}（强势盘减仓防守）")
    if regime.ema5d_6 is not None:
        ma_bits.append(f"5日 EMA6 {f(regime.ema5d_6)}")
    if ma_bits:
        out.append("均线：" + "；".join(ma_bits) + "。")
    lv = []
    if regime.pullback_618 is not None:
        lv.append(f"回踩做多位 {f(regime.pullback_50)} / {f(regime.pullback_618)}")
    lv.append(f"近支撑 {f(regime.nearest_support)} · 近阻力 {f(regime.nearest_resistance)}")
    if regime.ext_150 is not None:
        lv.append(f"本波延伸目标 {f(regime.ext_150)} / {f(regime.ext_1618)}")
    if _round_near(regime, price, 0.15) and regime.barrier_below and regime.barrier_above:
        lv.append(f"整数关口 {f(regime.round_level)}（下方屏障 {f(regime.barrier_below[0])}-{f(regime.barrier_below[1])}，上方首压 {f(regime.barrier_above[0])}-{f(regime.barrier_above[1])}）")
    out.append("点位：" + "；".join(lv) + "。")
    big = []
    if regime.waist_line is not None:
        big.append(f"腰斩线 {f(regime.waist_line)}" + _waist_note(regime, price))
    if regime.cycle_382 is not None:
        big.append(f"大周期 {f(regime.cycle_low)}→{f(regime.cycle_high)} 反转梯子 {f(regime.cycle_382)} / {f(regime.cycle_500)} / {f(regime.cycle_618)}")
    if regime.monthly_boll_mid is not None:
        big.append(f"月线 BOLL 中轨 {f(regime.monthly_boll_mid)}（突破即大方向反转）")
    if regime.weekly_boll_upper is not None:
        big.append(f"周线 BOLL 上轨 {f(regime.weekly_boll_upper)}")
    if big:
        out.append("大周期：" + "；".join(big) + "。")
    if eric_readings:
        out.append("波段过滤器：" + "；".join(eric_readings) + "。")
    return out


POLISH_SYSTEM = """你是一位在币安广场写短评的中文加密货币交易员，多年合约实盘，说话像人不像机器。
把用户给你的「模板短评」改写成你自己发帖的口吻：
- 第一人称、口语、短句，有态度、有判断，像在群里跟兄弟说话；可以有一点情绪，但不油腻、不喊单式营销、不用感叹号轰炸、不堆 emoji、不用项目符号和小标题。
- 把「我们的系统/引擎/指标读数」这类机器表述换成交易员会说的话（比如「日线超卖了」「回踩位在 xxx」）。
- 所有价格、点位、百分比、倍数、日期、币种标签（$BTC #BTC 这种）必须原样保留，一个数字都不能改、不能删、不能新增。
- 不改变原文的方向判断和操作建议；不要编造原文没有的理由。
- 篇幅可以比原文长一些：把「指标怎么看」那段展开成交易员的推理（为什么这些位置重要、破了/守住分别怎么办），但只能用原文给出的指标和数字，不要新增数字。
- 不要加免责声明、不要加话题标签、不要加「仅供参考」之类的套话。
- 不要出现「原文」「模板」「系统」这类字眼，不要对原文做点评或加括号注释；若原文某句自相矛盾或看不懂，直接略过那句，不要解释。
- 总长度不超过 1300 字。只输出改写后的正文，不要解释。"""

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
_COEFFS = {0.236, 0.382, 0.5, 0.618, 0.786, 1.5, 1.618, 2.618}


def _numbers(text: str) -> set[str]:
    """按数值归一（63750.00 与 63750 视为同一个数；忽略 0.382 这类系数以外的差异由长度校验兜底）。"""
    out: set[str] = set()
    for m in _NUM_RE.finditer(text):
        raw = m.group(0).replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        if abs(v) < 10 and (v == int(v) or round(v, 3) in _COEFFS):
            continue  # 「1/2」「第2次」「0.618」这类计数/系数允许改写成文字
        out.add(f"{v:.6g}")
    return out



def _missing_numbers(want: set[str], got: set[str], rel_tol: float = 1e-3) -> set[str]:
    """原文数字在润色稿里找不到的集合；允许 ±0.1% 的四舍五入（101.5747 → 101.57 视为保留）。"""
    if not want:
        return set()
    got_vals = []
    for g in got:
        try:
            got_vals.append(float(g))
        except ValueError:
            continue
    missing: set[str] = set()
    for w in want - got:
        try:
            wv = float(w)
        except ValueError:
            continue
        tol = max(abs(wv) * rel_tol, 1e-9)
        if not any(abs(gv - wv) <= tol for gv in got_vals):
            missing.add(w)
    return missing

def polish_square_text(text: str, *, settings=None) -> tuple[str, str]:
    """LLM 润色广场短评。返回 (最终文本, 来源 'llm:<provider>' | 'template:<原因>')。

    校验：原文里的每个数字必须在润色稿里出现；长度 ≤ MAX_POST_LEN。任一不满足回退原文。
    """
    import time as _time

    s = settings or get_settings()
    if not getattr(s, "square_post_ai_polish", True):
        return text, "template:disabled"
    try:
        from analyst.llm.chat import _iter_chat_clients
    except Exception as e:  # noqa: BLE001
        return text, f"template:import({e})"
    want_nums = _numbers(text)
    start = _time.time()
    for client, model, prov in _iter_chat_clients(s):
        if _time.time() - start > 60:
            break
        try:
            create_kw: dict = {
                "model": model,
                "messages": [{"role": "system", "content": POLISH_SYSTEM}, {"role": "user", "content": text}],
                "temperature": 0.7,
                # 帖子最长 1500 字，中文≈1 字 1 token，留足余量，否则被 length 截成空正文
                "max_tokens": 2000,
            }
            if prov == "b.ai" and str(model).lower().startswith("deepseek-v4"):
                # Flash 默认把输出额度花在 thinking 上，长帖会截成空 content
                create_kw["extra_body"] = {"thinking": {"type": "disabled"}}
            resp = client.chat.completions.create(**create_kw)
            choice = resp.choices[0]
            out = (choice.message.content or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("square polish %s 失败：%s", prov, e)
            continue
        if not out:
            logger.warning("square polish %s 空回复（finish=%s），换下一条", prov, getattr(choice, "finish_reason", None))
            continue
        out = out.strip("`").strip()
        got = _numbers(out)
        missing = _missing_numbers(want_nums, got)
        if missing:
            logger.warning("square polish %s 丢了数字 %s，回退模板", prov, sorted(missing)[:6])
            continue
        if len(out) > MAX_POST_LEN:
            logger.warning("square polish %s 过长 %d，回退模板", prov, len(out))
            continue
        return out, f"llm:{prov}"
    return text, "template:fallback"


def _cooldown_path() -> Path:
    return Path(get_settings().data_cache_dir) / "square_jack_cooldown.json"


def _load_cooldown() -> dict[str, float]:
    p = _cooldown_path()
    try:
        if p.is_file():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(k): float(v) for k, v in raw.items() if v is not None}
    except Exception:
        logger.warning("load square cooldown failed", exc_info=True)
    return {}


def _save_cooldown(data: dict[str, float]) -> None:
    p = _cooldown_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # 只留最近 80 条
        items = sorted(data.items(), key=lambda kv: kv[1])[-80:]
        p.write_text(
            json.dumps(dict(items), ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("save square cooldown failed")


def square_symbols_set(settings=None) -> set[str]:
    s = settings or get_settings()
    raw = (getattr(s, "square_post_symbols", "") or "").strip()
    if raw:
        return set(s._csv_symbols(raw))
    # 默认：BTC / ETH / BNB / SOL / AAVE
    return {
        "BTC/USDT",
        "ETH/USDT",
        "BNB/USDT",
        "SOL/USDT",
        "AAVE/USDT",
    }


def square_timeframes_set(settings=None) -> set[str]:
    s = settings or get_settings()
    # 默认只发 4h 及以上：1h 三盘来回切换，帖子观点变来变去
    raw = (getattr(s, "square_post_timeframes", "") or "4h,1d,1w").strip()
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def maybe_post_jack_regime(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    jack: JackLevels | None,
    regime: JackRegime,
) -> dict[str, Any] | None:
    """三盘变化时发广场短文。未启用/不在白名单/冷却中 → None。"""
    settings = get_settings()
    if not getattr(settings, "square_post_enabled", False):
        return None
    key = (getattr(settings, "binance_square_openapi_key", "") or "").strip()
    if not key:
        logger.warning("Square 已启用但未配置 BINANCE_SQUARE_OPENAPI_KEY，跳过")
        return None

    sym = _norm_symbol(symbol)
    tf = (timeframe or "").strip().lower()
    if sym not in square_symbols_set(settings):
        return None
    if tf not in square_timeframes_set(settings):
        return None

    cool_h = float(getattr(settings, "square_post_cooldown_hours", 4) or 0)
    cool_key = f"{sym}|{tf}"
    now = time.time()
    state = _load_cooldown()
    last = state.get(cool_key)
    if cool_h > 0 and last is not None and (now - last) < cool_h * 3600:
        logger.info(
            "Square 冷却中 %s remain=%.0fs",
            cool_key,
            cool_h * 3600 - (now - last),
        )
        return None

    text = compose_jack_square_post(
        symbol=sym,
        timeframe=tf,
        price=price,
        jack=jack,
        regime=regime,
    )
    text, polish_src = polish_square_text(text, settings=settings)
    logger.info("Square 文案来源 %s（%s）", polish_src, cool_key)
    try:
        result = post_text(key, text)
    except SquareApiError as e:
        logger.error(
            "Square 发帖失败 code=%s msg=%s key=%s %s",
            e.code,
            e.message,
            mask_key(key),
            cool_key,
        )
        raise
    except Exception:
        logger.exception("Square 发帖异常 key=%s %s", mask_key(key), cool_key)
        raise

    state[cool_key] = now
    _save_cooldown(state)
    logger.info(
        "Square 已发帖 %s id=%s link=%s",
        cool_key,
        result.get("id"),
        result.get("shareLink"),
    )
    return {"text": text, "result": result, "symbol": sym, "timeframe": tf}


# ── Eric 超卖信号（BTC/ETH × 日线/周线）→ 广场短文 ──

_ERIC_HOOK = {
    "weekly_watch": "周线超卖来了，这是 {tag} 过去几年最值钱的信号之一",
    "weekly_entry": "{tag} 周线超卖后拐头确认，反弹窗口打开",
    "weekly_entry_half": "{tag} 周线超卖叠上支撑/背离，先进半仓等拐头",
    "daily_oversold": "{tag} 日线进入超卖区，先看反弹，不赌反转",
    "weekly_tp1": "{tag} 周线超卖多单到第一目标，机械止盈一半",
    "weekly_tp2": "{tag} 余仓离场，这一轮周线超卖反弹交卷",
    "weekly_stop": "{tag} 周线超卖多单止损，认错不扛单",
}


def compose_eric_square_post(
    *,
    symbol: str,
    kind: str,
    price: float,
    bf_value: float | None,
    reasons: list[str] | None = None,
    plan: dict[str, Any] | None = None,
) -> str:
    """Eric 波段过滤器超卖短文：钩子 + 读数 + 计划点位 + 定性 + 免责 + 标签。"""
    tag = _cashtag(symbol)
    hook = _ERIC_HOOK.get(kind, "{tag} 波段过滤器触发超卖").format(tag=tag)
    lines = [
        hook,
        f"现价 {_fmt_price(price)}"
        + (f" · 过滤器读数 {bf_value:+.0f}（≤-40 为超卖）" if bf_value is not None else ""),
    ]
    plan = plan or {}
    if kind == "weekly_watch":
        lines.append("历史上周线超卖后 8 周中位涨幅约 +20%，但首根就买 45% 会先被止损打掉——等读数拐头再进。")
        if plan.get("episode_low"):
            lines.append(f"预备止损：段最低 {_fmt_price(plan['episode_low'])} 下方 3%。")
    elif kind == "weekly_entry":
        if plan.get("stop") is not None and plan.get("tp1") is not None:
            lines.append(
                f"计划｜止损 {_fmt_price(plan['stop'])} · 一半止盈 {_fmt_price(plan['tp1'])}"
                "（或日线超买/周EMA21）· 余仓从高点回撤 20% 离场"
            )
        lines.append("仓位按止损距离反推：每笔只拿权益 2% 去亏。")
    elif kind == "weekly_entry_half":
        lines.append(
            f"Buff 叠够（{plan.get('buff', '—')} 分）但读数还没拐头：先进一半，止损 {_fmt_price(plan.get('stop'))}，拐头再加另一半。"
        )
        lines.append("超卖 + 前低支撑 + 底背离，是 Eric 真正的组合进场，不是看到超卖就买。")
    elif kind == "weekly_tp1":
        pnl = plan.get("pnl_pct")
        lines.append(
            "卖出 1/2"
            + (f"，这一半 {pnl:+.1f}%" if pnl is not None else "")
            + f"；止损上移到成本 {_fmt_price(plan.get('stop'))}，余仓从最高价回撤 20% 再走。"
        )
        lines.append("止盈不是看顶，是把利润锁一半、让另一半免费跑。")
    elif kind == "weekly_tp2":
        pnl = plan.get("pnl_pct")
        first = plan.get("tp1_pnl_pct")
        seg = []
        if first is not None:
            seg.append(f"第一半 {first:+.1f}%")
        if pnl is not None:
            seg.append(f"余仓 {pnl:+.1f}%")
        lines.append("全部离场" + ("：" + " · ".join(seg) if seg else "") + "。反弹目标达成，不猜后面是继续涨还是拐头。")
        lines.append("下一次周线超卖，我们再见。")
    elif kind == "weekly_stop":
        pnl = plan.get("pnl_pct")
        lines.append(
            "跌破止损"
            + (f"，本笔 {pnl:+.1f}%" if pnl is not None else "")
            + "。计划内的亏损，等下一次信号。"
        )
    else:
        lines.append("日线超卖 = 技术性反弹机会，目标看 EMA21/前高；结构破位后的超卖参考价值打折。")
    for r in (reasons or [])[:1]:
        if r and "过滤器" not in r and len(r) < 60:
            lines.append(r)
    lines.append("做反弹，不赌反转。")
    text = "\n".join(lines)
    if len(text) > MAX_POST_LEN:
        text = text[: MAX_POST_LEN - 1] + "…"
    return text


def maybe_post_eric_signal(
    *,
    symbol: str,
    kind: str,
    price: float,
    bf_value: float | None,
    marker_time: int | None,
    reasons: list[str] | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Eric 超卖信号发广场。仅 BTC/ETH；同一根 K 只发一次；受 square_post_enabled 与 key 约束。"""
    from analyst.compute.band_filter import eric_symbol_validated

    settings = get_settings()
    if not getattr(settings, "square_post_enabled", False):
        return None
    if not getattr(settings, "square_post_eric_enabled", True):
        return None
    key = (getattr(settings, "binance_square_openapi_key", "") or "").strip()
    if not key:
        logger.warning("Square 已启用但未配置 BINANCE_SQUARE_OPENAPI_KEY，跳过 Eric 短文")
        return None
    sym = _norm_symbol(symbol)
    if not eric_symbol_validated(sym):
        return None
    if kind not in _ERIC_HOOK:
        return None
    cool_key = f"eric|{sym}|{kind}"
    state = _load_cooldown()
    bar = float(marker_time or 0)
    if bar > 0 and state.get(cool_key) == bar:
        return None
    # 同一品种同类信号至少间隔 20 小时（日线一根一次；周线 watch/entry 各一次）
    last_at = state.get(cool_key + "|at")
    now = time.time()
    if last_at is not None and now - last_at < 20 * 3600:
        return None
    text = compose_eric_square_post(
        symbol=sym, kind=kind, price=price, bf_value=bf_value, reasons=reasons, plan=plan
    )
    text, polish_src = polish_square_text(text, settings=settings)
    logger.info("Square Eric 文案来源 %s（%s）", polish_src, cool_key)
    try:
        result = post_text(key, text)
    except SquareApiError as e:
        logger.error(
            "Square Eric 发帖失败 code=%s msg=%s key=%s %s", e.code, e.message, mask_key(key), cool_key
        )
        raise
    state[cool_key] = bar
    state[cool_key + "|at"] = now
    _save_cooldown(state)
    logger.info(
        "Square 已发 Eric 短文 %s id=%s link=%s", cool_key, result.get("id"), result.get("shareLink")
    )
    return {"text": text, "result": result, "symbol": sym, "kind": kind}


# ── 加速行情（单边急涨/急跌）→ 广场短文 ──

_MOVE_COOLDOWN_H = 8.0


def compose_move_square_post(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    change_pct: float,
    vol_ratio: float,
    jack: JackLevels | None,
    regime: JackRegime,
    eric_readings: list[str] | None = None,
) -> str:
    """加速行情短文：发生了什么 → 我怎么看 → 指标分析 → 接下来盯什么。"""
    tag = _cashtag(symbol)
    tf = (timeframe or "4h").lower()
    up = change_pct > 0
    f = _fmt_price
    hook = (
        f"{tag} 这根 {tf} 直接拉了 {change_pct:+.1f}%，量放到平时的 {vol_ratio:.1f} 倍，加速了"
        if up
        else f"{tag} 这根 {tf} 直接砸了 {change_pct:+.1f}%，量放到平时的 {vol_ratio:.1f} 倍，加速下跌"
    )
    lines = [hook, f"现价 {f(price)}，盘面 {regime.regime_zh}，方向 {_side_zh(regime.trade_side)}。"]
    if up:
        if regime.regime == "strong_trend" and regime.trade_side == "long":
            lines.append(
                f"单边加速不等回踩，一味挂低多只会踏空；要追就追突破，突破近阻力 {f(regime.nearest_resistance)} 再补，"
                + (f"回踩位 {f(regime.pullback_618)} 附近是低多位，" if regime.pullback_618 is not None else "")
                + f"跌破 {f(regime.nearest_support)} 就先出来。"
            )
        else:
            back = f"，回踩 {f(regime.pullback_618)} 不破再拿" if regime.pullback_618 is not None else f"，跌回 {f(regime.nearest_support)} 下方就走"
            lines.append(f"日线还没转强，这种拉升先当反弹看：近阻力 {f(regime.nearest_resistance)} 附近先减一部分{back}。")
        if regime.ext_150 is not None:
            ext = f"这波如果延续，看 {f(regime.ext_150)} / {f(regime.ext_1618)}"
            if _round_near(regime, price, 0.08) and regime.barrier_above:
                ext += f"；整数关口 {f(regime.round_level)} 上方 {f(regime.barrier_above[0])} 附近是首个压力，首次冲关一般站不稳，先止盈一部分"
            lines.append(ext + "。")
    else:
        if regime.below_waist:
            where = "之下" if (regime.waist_line and price < regime.waist_line) else "边上"
            lines.append(f"已经在腰斩线 {f(regime.waist_line)} {where}，这里不追空，只等止跌信号低吸。")
        else:
            lines.append(f"急跌先看近支撑 {f(regime.nearest_support)} 能不能接住；反弹到 {f(regime.nearest_resistance)} 附近是短空位，破 {f(regime.nearest_support)} 再看下一档。")
    play = (regime.playbook_line or "").strip()
    if play:
        lines.append(f"打法：{_clip_sentence(play, 200)}")
    lines.append("")
    lines.extend(indicator_block(regime, jack, eric_readings, price=price))
    text = "\n".join(lines)
    if len(text) > MAX_POST_LEN:
        text = text[: MAX_POST_LEN - 1] + "…"
    return text


def maybe_post_market_move(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    change_pct: float,
    vol_ratio: float,
    jack: JackLevels | None,
    regime: JackRegime,
    eric_readings: list[str] | None = None,
) -> dict[str, Any] | None:
    """加速行情发帖：受 square_post_enabled / 品种白名单 / 8h 冷却约束。"""
    settings = get_settings()
    if not getattr(settings, "square_post_enabled", False):
        return None
    if not getattr(settings, "square_post_move_enabled", True):
        return None
    key = (getattr(settings, "binance_square_openapi_key", "") or "").strip()
    if not key:
        return None
    sym = _norm_symbol(symbol)
    if sym not in square_symbols_set(settings):
        return None
    cool_key = f"move|{sym}"
    state = _load_cooldown()
    now = time.time()
    last = state.get(cool_key)
    cool_h = float(getattr(settings, "square_move_cooldown_hours", _MOVE_COOLDOWN_H) or _MOVE_COOLDOWN_H)
    if last is not None and now - last < cool_h * 3600:
        return None
    text = compose_move_square_post(
        symbol=sym, timeframe=timeframe, price=price, change_pct=change_pct, vol_ratio=vol_ratio,
        jack=jack, regime=regime, eric_readings=eric_readings,
    )
    text, polish_src = polish_square_text(text, settings=settings)
    logger.info("Square 加速行情文案来源 %s（%s %+.1f%%）", polish_src, cool_key, change_pct)
    try:
        result = post_text(key, text)
    except SquareApiError as e:
        logger.error("Square move 发帖失败 code=%s msg=%s key=%s %s", e.code, e.message, mask_key(key), cool_key)
        raise
    state[cool_key] = now
    _save_cooldown(state)
    logger.info("Square 已发加速行情 %s id=%s link=%s", cool_key, result.get("id"), result.get("shareLink"))
    return {"text": text, "result": result, "symbol": sym, "kind": "move"}
