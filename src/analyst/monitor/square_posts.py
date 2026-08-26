"""Jack 三盘变化 → 币安广场短评（真发）。

仅处理 rule=jack_regime；品种/周期白名单与冷却由 Settings 控制。
"""

from __future__ import annotations

import json
import logging
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
    if ax >= 1000:
        return f"{x:.2f}"
    if ax >= 1:
        return f"{x:.4f}"
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


def _outlook_line(regime: JackRegime, jack: JackLevels | None, price: float) -> str:
    """一句话涨跌预测 + 关键点位（数字来自预计算）。"""
    side = regime.trade_side
    if side == "long":
        tgt = None
        if jack is not None:
            tgt = jack.rebound_618 if price < jack.rebound_618 else jack.rebound_382
            if jack.touch_level and jack.touch_level > price:
                tgt = jack.touch_level
        elif regime.nearest_resistance is not None:
            tgt = regime.nearest_resistance
        stop = jack.defense_level if jack is not None else regime.nearest_support
        if tgt is not None and stop is not None:
            return (
                f"预测：短线偏涨，上看 {_fmt_price(tgt)}；"
                f"跌破 {_fmt_price(stop)} 则看涨失效"
            )
        return "预测：短线偏涨，站稳后再加仓；破防守转观望"
    if side == "short":
        if regime.below_waist:
            return "预测：已近腰斩，暂不看更深下跌，宁可空仓等反抽"
        tgt = regime.nearest_support
        stop = jack.defense_level if jack is not None else regime.nearest_resistance
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
    if jack is not None:
        lines.append(
            f"点位｜防守 {_fmt_price(jack.defense_level)} · "
            f"近压 {_fmt_price(jack.rebound_382)} · "
            f"目标0.618 {_fmt_price(jack.rebound_618)}"
        )
    elif regime.nearest_support is not None or regime.nearest_resistance is not None:
        lines.append(
            f"点位｜近支 {_fmt_price(regime.nearest_support)} · "
            f"近压 {_fmt_price(regime.nearest_resistance)}"
        )
    play = (regime.playbook_line or "").strip()
    if play:
        lines.append(f"打法：{play[:100]}")
    if regime.trade_side == "long":
        lines.append("想跟单先看防守是否守住，别追在鱼尾。")
    elif regime.trade_side == "short" and not regime.below_waist:
        lines.append("想开空等反弹靠近阻力，别贴着支撑追空。")
    lines.append(DISCLAIMER)
    lines.append(_tag_line(symbol))
    text = "\n".join(lines)
    if len(text) > 900:
        text = text[:897] + "…"
    return text


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
    raw = (getattr(s, "square_post_timeframes", "") or "1h,4h").strip()
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
