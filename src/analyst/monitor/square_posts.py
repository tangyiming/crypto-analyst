"""三盘变化 → 币安广场短评（真发）。

仅处理 rule=market_regime；品种/周期白名单与冷却由 Settings 控制。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from analyst.compute.swing_levels import SwingLevels
from analyst.compute.market_regime import MarketRegime
from analyst.config import get_settings
from analyst.integrations.binance_square import SquareApiError, mask_key, post_content, upload_image

logger = logging.getLogger(__name__)

DISCLAIMER = "⚠️ 非投资建议，仅供参考，盈亏自负。"

# 引流优先：cashtag 进币种页（高意向），再补 1～2 个币种话题；少堆通用标签
_COIN_TAGS: dict[str, tuple[str, ...]] = {
    "BTC": ("$BTC", "#BTC", "#Bitcoin"),
    "ETH": ("$ETH", "#ETH", "#Ethereum"),
    "BNB": ("$BNB", "#BNB"),
    "SOL": ("$SOL", "#SOL", "#Solana"),
    "AAVE": ("$AAVE", "#AAVE"),
    "UNI": ("$UNI", "#UNI", "#Uniswap"),
    "HYPE": ("$HYPE", "#HYPE"),
    "ASTER": ("$ASTER", "#ASTER"),
    "DOGE": ("$DOGE", "#DOGE"),
    "LINK": ("$LINK", "#LINK"),
    "AVAX": ("$AVAX", "#AVAX"),
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


def _is_compact(settings=None) -> bool:
    s = settings or get_settings()
    return bool(getattr(s, "square_post_compact", True))


def _cta_line(
    symbol: str,
    side: str,
    *,
    defense: float | None = None,
    target: float | None = None,
    near: float | None = None,
    scene: str | None = None,
) -> str:
    """促点击 $ 标签的行动号召。

    scene:
      resist_test — 阻力测试 + 偏多：突破 near 可追，破 defense 走
      support_hold — 支撑触碰 + 偏多：守住 defense 可跟，跌破走
      move_breakout — 加速拉升后：别追，突破 near 再跟
    """
    tag = _cashtag(symbol)

    def _with_target(prefix: str) -> str:
        if target is not None:
            return f"{prefix}，上看 {_fmt_price(target)}。"
        return f"{prefix}。"

    if side == "long":
        if scene == "resist_test" and near is not None and defense is not None:
            return _with_target(
                f"点 {tag} 看永续，突破 {_fmt_price(near)} 可追，破 {_fmt_price(defense)} 走"
            )
        if scene == "support_hold" and defense is not None:
            return _with_target(f"点 {tag} 看永续，守住 {_fmt_price(defense)} 可跟，跌破就走")
        if scene == "move_breakout" and near is not None and defense is not None:
            return _with_target(
                f"点 {tag} 看永续，别追，突破 {_fmt_price(near)} 再跟，破 {_fmt_price(defense)} 走"
            )
        if defense is not None and target is not None:
            return (
                f"点 {tag} 看永续，站稳 {_fmt_price(defense)} 可跟，上看 {_fmt_price(target)}。"
            )
        if near is not None:
            return f"点 {tag} 看行情，突破 {_fmt_price(near)} 可追，破防守就走。"
        return f"点 {tag} 看永续，偏多思路见上。"
    if side == "short":
        if defense is not None and target is not None:
            return (
                f"点 {tag} 看永续，反弹 {_fmt_price(defense)} 附近可空，下看 {_fmt_price(target)}。"
            )
        if near is not None:
            return f"点 {tag} 看行情，靠近 {_fmt_price(near)} 再空，别追在支撑上。"
        return f"点 {tag} 看永续，偏空思路见上。"
    if defense is not None:
        return f"点 {tag} 看行情，守住 {_fmt_price(defense)} 再动手，方向不明先等。"
    return f"点 {tag} 看行情，等方向明朗再动手。"


def _fmt_price(x: float | None) -> str:
    if x is None:
        return "—"
    ax = abs(float(x))
    if ax >= 10:
        return f"{x:.2f}"  # 两位小数：SOL 106.75 / BNB 812.34 / BTC 63750.00
    if ax >= 1:
        return f"{x:.3f}"
    return f"{x:.6f}"


def _side_zh(side: str) -> str:
    if side == "long":
        return "偏多"
    if side == "short":
        return "偏空"
    return "观望"


def _prediction_hook(regime: MarketRegime, tf: str) -> str:
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


def _post_levels(regime: MarketRegime, swing: SwingLevels | None, price: float) -> tuple[float | None, float | None, float | None]:
    """帖子用（防守, 近压, 目标）：按方向做合理性过滤——多头目标/近压必须在现价上方，防守在下方。

    24h 锁点的 rebound_382/618 只在「跌后反弹」语境有意义，涨势里会落在现价下方，不能直接拿来当近压/目标。
    """
    above = lambda x: x is not None and x > price * 1.001  # noqa: E731
    below = lambda x: x is not None and x < price * 0.999  # noqa: E731
    j_def = getattr(swing, "defense_level", None) if swing is not None else None
    j_382 = getattr(swing, "rebound_382", None) if swing is not None else None
    j_618 = getattr(swing, "rebound_618", None) if swing is not None else None
    j_touch = getattr(swing, "touch_level", None) if swing is not None else None
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


def _outlook_line(regime: MarketRegime, swing: SwingLevels | None, price: float) -> str:
    """一句话涨跌预测 + 关键点位（数字来自预计算）。"""
    side = regime.trade_side
    if side == "long":
        stop, near, tgt = _post_levels(regime, swing, price)
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
        stop, near, tgt = _post_levels(regime, swing, price)
        tgt = tgt or near
        if tgt is not None and stop is not None:
            return (
                f"预测：短线偏跌，下看 {_fmt_price(tgt)}；"
                f"涨破 {_fmt_price(stop)} 则看跌失效"
            )
        return "预测：短线偏跌，反弹再空；破防守转观望"
    return "预测：方向不明，先观望，不追涨杀跌"


def _compose_regime_compact(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    swing: SwingLevels | None,
    regime: MarketRegime,
) -> str:
    """短讯：钩子 + 现价/方向 + 关键位 + CTA（内容挖矿转化向）。"""
    tag = _cashtag(symbol)
    tf = (timeframe or "").strip().lower()
    defense, near, target = _post_levels(regime, swing, float(price))
    tgt = target or near
    lines = [
        f"{_prediction_hook(regime, tf)} {tag}",
        f"现价 {_fmt_price(price)} · {_side_zh(regime.trade_side)} · {regime.regime_zh}",
    ]
    lvl: list[str] = []
    if defense is not None:
        lvl.append(f"防守 {_fmt_price(defense)}")
    if near is not None and near != defense:
        lbl = "近支" if regime.trade_side == "short" else "近压"
        lvl.append(f"{lbl} {_fmt_price(near)}")
    if tgt is not None and tgt not in (defense, near):
        lbl = "下看" if regime.trade_side == "short" else "上看"
        lvl.append(f"{lbl} {_fmt_price(tgt)}")
    if lvl:
        lines.append(" · ".join(lvl))
    play = (regime.playbook_line or "").strip()
    if play:
        lines.append(_clip_sentence(play, 72))
    lines.append(
        _cta_line(
            symbol,
            regime.trade_side,
            defense=defense,
            target=tgt,
            near=near,
        )
    )
    text = "\n".join(lines)
    if len(text) > MAX_POST_LEN:
        text = text[: MAX_POST_LEN - 1] + "…"
    return text


def compose_playbook_setup_post(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    swing: SwingLevels | None,
    regime: MarketRegime,
    flag_labels: list[str] | None = None,
) -> str:
    """打法提示（playbook_setup）：新 flag 出现时的短讯。"""
    tag = _cashtag(symbol)
    tf = (timeframe or "").strip().lower()
    hint = " · ".join((flag_labels or [])[:2]) or "打法更新"
    defense, near, target = _post_levels(regime, swing, float(price))
    tgt = target or near
    lines = [
        f"⚡ {tag} {tf} 打法：{hint}",
        f"现价 {_fmt_price(price)} · {_side_zh(regime.trade_side)} · {regime.regime_zh}",
    ]
    if near is not None:
        lines.append(f"关键位 {_fmt_price(near)}")
    lines.append(
        _cta_line(
            symbol,
            regime.trade_side,
            defense=defense,
            target=tgt,
            near=near,
        )
    )
    text = "\n".join(lines)
    if len(text) > MAX_POST_LEN:
        text = text[: MAX_POST_LEN - 1] + "…"
    return text


def compose_level_touch_post(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    level: float,
    kind: str,
    swing: SwingLevels | None,
    regime: MarketRegime | None,
) -> str:
    """关键位触碰短讯。"""
    tag = _cashtag(symbol)
    tf = (timeframe or "").strip().lower()
    is_support = kind == "support"
    lvl_label = "支撑" if is_support else "阻力"
    reg = regime
    cta_side = reg.trade_side if reg is not None else ("long" if is_support else "short")
    side_zh = _side_zh(cta_side)
    regime_zh = reg.regime_zh if reg else "—"
    lines = [
        f"{'👆' if is_support else '👇'} {tag} {tf} 触及{lvl_label} {_fmt_price(level)}"
        + (" 守住" if is_support else " 测试"),
        f"现价 {_fmt_price(price)} · {side_zh} · {regime_zh}",
    ]
    if reg is not None:
        defense, near, target = _post_levels(reg, swing, float(price))
        if is_support:
            hold = level if level < price * 0.999 else (defense if defense else level)
            if cta_side == "long":
                lines.append(
                    _cta_line(
                        symbol,
                        "long",
                        defense=hold,
                        target=target,
                        scene="support_hold",
                    )
                )
            else:
                lines.append(
                    _cta_line(
                        symbol,
                        cta_side,
                        defense=defense if defense else level,
                        target=target,
                        near=near if near else level,
                    )
                )
        elif cta_side == "short":
            entry = level if level > price else (near if near and near > price else defense)
            lines.append(
                _cta_line(
                    symbol,
                    "short",
                    defense=entry,
                    target=target,
                    near=near,
                )
            )
        else:
            # 阻力触碰 + 偏多：等突破，不把多头防守位塞进「可空」文案
            brk = level if level > price else near
            lines.append(
                _cta_line(
                    symbol,
                    "long",
                    defense=defense,
                    target=target,
                    near=brk,
                    scene="resist_test",
                )
            )
    else:
        lines.append(
            _cta_line(
                symbol,
                cta_side,
                defense=level if is_support else None,
                near=level if not is_support else None,
            )
        )
    text = "\n".join(lines)
    if len(text) > MAX_POST_LEN:
        text = text[: MAX_POST_LEN - 1] + "…"
    return text


def compose_daily_recap_post(
    *,
    facts: dict[str, Any] | None = None,
    movers: list[tuple[str, float, float]] | None = None,
) -> str:
    """每日复盘短讯：保证 7 天窗口内持续有新帖 + 多 $ 标签。"""
    facts = facts or {}
    m = facts.get("market") or {}
    zh = {"bull": "牛", "bear": "熊", "accum": "筑底"}
    regime = m.get("regime")
    btc_p = m.get("btc_price")
    lines = ["📋 盯盘日报 · 点 $BTC $ETH 看行情"]
    if btc_p is not None:
        dev = m.get("btc_vs_ema200d_pct")
        dev_s = f"（距200日线 {dev:+.1f}%）" if isinstance(dev, (int, float)) else ""
        lines.append(
            f"BTC {_fmt_price(float(btc_p))} · 相位 {zh.get(regime, regime or '—')}{dev_s}"
        )
    rs = (facts.get("relative_strength") or {}).get("pairs") or {}
    if rs:
        bits = []
        for pair, v in list(rs.items())[:2]:
            st = v.get("state")
            st_zh = "山寨强" if st == "above" else ("BTC强" if st == "below" else st)
            bits.append(f"{pair} {st_zh}")
        if bits:
            lines.append("相对强弱：" + " · ".join(bits))
    if movers:
        top = sorted(movers, key=lambda x: abs(x[2]), reverse=True)[:3]
        lines.append(
            "今日波动："
            + " · ".join(
                f"${_base_asset(sym)} {chg:+.1f}%" for sym, _, chg in top
            )
        )
    lines.append("具体点位看最新短评；盈亏自负。")
    text = "\n".join(lines)
    if len(text) > MAX_POST_LEN:
        text = text[: MAX_POST_LEN - 1] + "…"
    return text


def _fetch_24h_movers(symbols: set[str]) -> list[tuple[str, float, float]]:
    """REST 拉 24h 涨跌幅，供复盘帖选波动榜。"""
    import httpx

    out: list[tuple[str, float, float]] = []
    for sym in sorted(symbols):
        fsym = _norm_symbol(sym).replace("/", "")
        try:
            with httpx.Client(timeout=6.0) as client:
                resp = client.get(
                    "https://fapi.binance.com/fapi/v1/ticker/24hr",
                    params={"symbol": fsym},
                )
            data = resp.json()
            if isinstance(data, dict) and data.get("lastPrice"):
                out.append(
                    (
                        _norm_symbol(sym),
                        float(data["lastPrice"]),
                        float(data.get("priceChangePercent") or 0),
                    )
                )
        except Exception:
            logger.debug("square mover fetch failed %s", sym, exc_info=True)
    return out


def compose_regime_square_post(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    swing: SwingLevels | None,
    regime: MarketRegime,
    compact: bool | None = None,
) -> str:
    """生成带币种标签 + 涨跌预测 + 点位的广场短评。"""
    use_compact = compact if compact is not None else _is_compact()
    if use_compact:
        return _compose_regime_compact(
            symbol=symbol,
            timeframe=timeframe,
            price=price,
            swing=swing,
            regime=regime,
        )
    tag = _cashtag(symbol)
    tf = (timeframe or "").strip().lower()
    side = _side_zh(regime.trade_side)
    lines = [
        f"{_prediction_hook(regime, tf)} {tag}",
        f"现价 {_fmt_price(price)} · 方向 {side}",
        _outlook_line(regime, swing, float(price)),
    ]
    defense, near, target = _post_levels(regime, swing, float(price))
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
    lines.extend(indicator_block(regime, swing, price=price, timeframe=timeframe))
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

_NEAR_BY_TF = {"4h": 0.10, "6h": 0.12, "8h": 0.12, "12h": 0.15, "1d": 0.25, "3d": 0.35, "1w": 0.40, "1M": 0.60}


def _near_threshold(timeframe: str | None) -> float:
    return _NEAR_BY_TF.get((timeframe or "4h").strip().lower(), 0.10)


def indicator_block(
    regime: MarketRegime,
    swing: SwingLevels | None,
    eric_readings: list[str] | None = None,
    price: float = 0.0,
    timeframe: str = "4h",
) -> list[str]:
    """指标分析段：多周期 BOLL / MACD 动能 / 均线 / 关键位 / 大周期，给读者「为什么这么看」。

    远端点位距离过滤：离现价超过周期门槛（4h ±10%、1d ±25%、1w ±40%）的位不写进帖子，
    大周期段只留腰斩线定性、最近一档反转梯子和最近一个变盘位，避免 4h 帖被月度级别的数字淹没。
    """
    f = _fmt_price
    thr = _near_threshold(timeframe)

    def near(x: float | None) -> bool:
        if x is None or not price or price <= 0:
            return x is not None
        return abs(float(x) / price - 1.0) <= thr

    def dist(x: float) -> float:
        return abs(float(x) / price - 1.0) if price else 0.0

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
        line = f"4h BOLL：下轨 {f(regime.boll_4h_lower)} / 中轨 {f(regime.boll_4h_mid)} / 上轨 {f(regime.boll_4h_upper)}"
        if near(regime.boll_12h_mid):
            line += f"；12h 中轨 {f(regime.boll_12h_mid)}"
        out.append(line + "。")

    ma_bits = []
    if near(regime.ema12h_6):
        ma_bits.append(f"12h EMA6 {f(regime.ema12h_6)}（扎针参考）")
    mids = [x for x in (regime.boll_mid_3d, regime.boll_mid_5d) if near(x)]
    if mids:
        ma_bits.append(f"{'3日/5日' if len(mids) == 2 else ('3日' if regime.boll_mid_3d in mids else '5日')} BOLL 中轨 {' / '.join(f(x) for x in mids)}（强势盘减仓防守）")
    if near(regime.ema5d_6):
        ma_bits.append(f"5日 EMA6 {f(regime.ema5d_6)}")
    if ma_bits:
        out.append("均线：" + "；".join(ma_bits) + "。")

    lv = []
    if regime.pullback_618 is not None and (near(regime.pullback_618) or near(regime.pullback_50)):
        lv.append(f"回踩做多位 {f(regime.pullback_50)} / {f(regime.pullback_618)}")
    lv.append(f"近支撑 {f(regime.nearest_support)} · 近阻力 {f(regime.nearest_resistance)}")
    exts = [x for x in (regime.ext_150, regime.ext_1618) if near(x)]
    if exts:
        lv.append("本波延伸目标 " + " / ".join(f(x) for x in exts))
    if _round_near(regime, price, thr) and regime.barrier_below and regime.barrier_above:
        lv.append(f"整数关口 {f(regime.round_level)}（下方屏障 {f(regime.barrier_below[0])}-{f(regime.barrier_below[1])}，上方首压 {f(regime.barrier_above[0])}-{f(regime.barrier_above[1])}）")
    out.append("点位：" + "；".join(lv) + "。")

    big = []
    if regime.waist_line is not None:
        if near(regime.waist_line):
            big.append(f"腰斩线 {f(regime.waist_line)}" + _waist_note(regime, price))
        elif price and price > regime.waist_line:
            big.append(f"现价在腰斩线 {f(regime.waist_line)} 上方 {dist(regime.waist_line) * 100:.0f}%，牛市结构没坏")
        else:
            big.append(f"腰斩线 {f(regime.waist_line)}" + _waist_note(regime, price))
    rungs = [x for x in (regime.cycle_382, regime.cycle_500, regime.cycle_618) if x is not None]
    if rungs:
        near_rungs = [x for x in rungs if near(x)]
        if near_rungs:
            big.append(f"大周期 {f(regime.cycle_low)}→{f(regime.cycle_high)} 反转梯子最近一档 {' / '.join(f(x) for x in near_rungs)}")
        else:
            up = [x for x in rungs if price and x > price]
            nxt = min(up) if up else min(rungs, key=dist)
            big.append(f"大周期反转梯子下一档在 {f(nxt)}（{'上方' if price and nxt > price else '下方'} {dist(nxt) * 100:.0f}%）")
    pivots = []
    if regime.monthly_boll_mid is not None:
        pivots.append(("月线 BOLL 中轨", float(regime.monthly_boll_mid), "（突破即大方向反转）"))
    if regime.weekly_boll_upper is not None:
        pivots.append(("周线 BOLL 上轨", float(regime.weekly_boll_upper), ""))
    near_p = [p_ for p_ in pivots if near(p_[1])]
    far_p = [p_ for p_ in pivots if not near(p_[1])]
    for name, val, note in near_p:
        big.append(f"{name} {f(val)}{note}")
    if far_p and not near_p:
        name, val, note = min(far_p, key=lambda t: dist(t[1]))
        big.append(f"更远的变盘位：{name} {f(val)}{note}")
    if big:
        out.append("大周期：" + "；".join(big) + "。")
    if eric_readings:
        out.append("波段过滤器：" + "；".join(eric_readings) + "。")
    return out


POLISH_SYSTEM = """你是一位在币安广场写短评的中文加密货币交易员，多年合约实盘，语气干脆、像笔记不像喊麦。
把用户给你的「模板短评」改写成你自己发帖的口吻：
- 第一人称、短句、有判断；冷静专业，不喊「兄弟们/家人们/冲啊」，不用感叹号轰炸、不堆 emoji、不用项目符号和小标题。
- 把「我们的系统/引擎/指标读数」这类机器表述换成交易员会说的话（比如「日线超卖了」「回踩位在 xxx」）。
- 币种标签 $BTC $ETH $SOL 必须原样保留在正文里，禁止改成纯文字 BTC/比特币/以太坊。
- 所有价格、点位、百分比、倍数、日期必须原样保留，一个数字都不能改、不能删、不能新增。
- 不改变原文的方向判断和操作建议；不要编造原文没有的理由。
- 原文偏多/看涨/站稳/上看/突破可追，禁止改成偏空/可空/下看；反之亦然；末行 CTA 语义不得与原文互换。
- 篇幅控制在原文 2–3 倍：只展开关键位怎么理解、破了/守住怎么办；不灌水、不重复同一句话。
- 末行「点 $XXX 看永续/看行情」行动号召不要写进正文段落，留给系统单独追加；正文里不要复述该行。
- 不要加免责声明、不要加话题标签、不要加「仅供参考」之类的套话。
- 不要出现「原文」「模板」「系统」这类字眼，不要对原文做点评或加括号注释；若原文某句自相矛盾或看不懂，直接略过那句，不要解释。
- 总长度不超过 900 字。只输出改写后的正文，不要解释。"""

POLISH_SYSTEM_SHORT = """你是一位在币安广场写短评的中文加密货币合约交易员，多年实盘，语气干脆、像笔记不像喊麦。
把模板改写成第一人称、短句、有判断的短评：
- 冷静专业，禁止「兄弟们/家人们/老铁/冲啊」等群聊口癖；emoji 最多保留原文里的 1 个。
- 币种标签 $BTC $ETH $SOL 必须原样出现在正文，禁止改成纯文字 BTC/比特币。
- 所有价格、点位、百分比必须原样保留，不能改、不能删、不能新增。
- 不改变方向与操作建议；不编造理由；不加免责声明和话题标签。
- 原文偏多/看涨/站稳/上看/突破可追，禁止改成偏空/可空/下看；反之亦然。
- 末行行动号召语义不得互换；「点 $XXX 看永续…」不要写进正文，留给系统单独追加。
- 全文 220–380 字，3–5 段即可，不重复、不凑字数。只输出正文，不要解释。"""

_CASHTAG_RE = re.compile(r"\$[A-Za-z0-9]{2,12}")
_CTA_LINE_RE = re.compile(r"^点\s+\$")
_CTA_INLINE_RE = re.compile(
    r"点\s+\$[A-Za-z0-9]{2,12}\s+看(?:永续|行情)[^。\n]*。"
)
_COEFFS = {0.236, 0.382, 0.5, 0.618, 0.786, 1.5, 1.618, 2.618}


def _extract_cashtags(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _CASHTAG_RE.finditer(text or ""):
        t = m.group(0)
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _extract_cta_lines(text: str) -> list[str]:
    lines: list[str] = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s and _CTA_LINE_RE.match(s):
            lines.append(s)
    return lines


def _spot_price_from_text(text: str) -> float | None:
    m = re.search(r"现价\s*([\d.]+)", text or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _trade_side_markers(text: str) -> str | None:
    t = text or ""
    has_long = bool(
        re.search(
            r"看涨|偏多|可跟|上看|站稳\s*[\d.]+\s*可跟|突破\s*[\d.]+\s*可追|守住\s*[\d.]+\s*可跟|低多",
            t,
        )
    )
    has_short = bool(re.search(r"看跌|偏空|可空|下看|反弹\s*[\d.]+\s*附近可空|高空", t))
    if has_long and not has_short:
        return "long"
    if has_short and not has_long:
        return "short"
    return None


def _cta_price_logic_ok(text: str) -> bool:
    """CTA 价位与现价方向自洽：上看在上方、下看在下方、站稳在下方、反弹可空在上方。"""
    price = _spot_price_from_text(text)
    if price is None or price <= 0:
        return True
    tol = max(price * 0.001, 1e-6)
    if m := re.search(r"上看\s*([\d.]+)", text):
        if float(m.group(1)) <= price - tol:
            return False
    if m := re.search(r"下看\s*([\d.]+)", text):
        if float(m.group(1)) >= price + tol:
            return False
    if m := re.search(r"站稳\s*([\d.]+)", text):
        if float(m.group(1)) >= price + tol:
            return False
    if m := re.search(r"反弹\s*([\d.]+)\s*附近可空", text):
        if float(m.group(1)) <= price - tol:
            return False
    if m := re.search(r"突破\s*([\d.]+)", text):
        if float(m.group(1)) <= price - tol:
            return False
    if m := re.search(r"守住\s*([\d.]+)", text):
        if float(m.group(1)) >= price + tol:
            return False
    return True


def _cta_direction_conflict(text: str) -> bool:
    """同一帖里不能既有「站稳可跟」又有「附近可空」。"""
    t = text or ""
    long_cta = bool(re.search(r"站稳\s*[\d.]+\s*可跟", t))
    short_cta = bool(re.search(r"附近可空", t))
    return long_cta and short_cta


def _direction_consistent(original: str, polished: str) -> bool:
    orig_side = _trade_side_markers(original)
    pol = polished or ""
    if orig_side == "long" and re.search(r"附近可空|反弹\s*[\d.]+\s*附近可空", pol):
        return False
    if orig_side == "short" and re.search(r"站稳\s*[\d.]+\s*可跟", pol):
        return False
    pol_side = _trade_side_markers(pol)
    if orig_side and pol_side and orig_side != pol_side:
        return False
    return _cta_price_logic_ok(pol)


def _square_post_ok(text: str, *, template: str | None = None) -> bool:
    """发帖前最后一道：CTA 价位方向 + 不与模板多空相反。"""
    if not _cta_price_logic_ok(text):
        return False
    if _cta_direction_conflict(text):
        return False
    if template:
        return _direction_consistent(template, text)
    return True


def _finalize_square_text(text: str, template: str, *, polished: bool) -> tuple[str, str]:
    """润色后校验；失败则回退模板原文（并强制模板 CTA）。"""
    if not polished:
        return text, "template:disabled"
    if _square_post_ok(text, template=template):
        return text, "ok"
    logger.warning("Square 文案逻辑校验失败，回退模板")
    return template, "template:logic_fallback"

def _strip_inline_ctas(text: str) -> str:
    """去掉正文段落里内嵌的「点 $XXX 看永续…」避免与末行 CTA 重复。"""
    out = _CTA_INLINE_RE.sub("", text or "")
    return re.sub(r"[ \t]+", " ", out).strip(" \t，,。；;")


def _enforce_square_anchors(polished: str, original: str) -> str:
    """润色后补回 $ 标签与 CTA 行（内容挖矿点击入口）。"""
    out = (polished or "").strip()
    tags = _extract_cashtags(original)
    orig_ctas = _extract_cta_lines(original)
    for tag in tags:
        if tag in out:
            continue
        base = tag[1:]
        # 常见：模型把 $BTC 写成 BTC
        replaced = re.sub(rf"(?<![\$#/]){re.escape(base)}(?![A-Za-z0-9])", tag, out, count=1)
        if tag in replaced:
            out = replaced
            continue
        out = f"{tag} {out.lstrip()}"
    if orig_ctas:
        canonical = orig_ctas[-1]
        body = [ln for ln in out.splitlines() if not _CTA_LINE_RE.match(ln.strip())]
        if "可跟" in canonical or "上看" in canonical or "可追" in canonical:
            body = [ln for ln in body if not re.search(r"附近可空|下看\s*[\d.]", ln)]
        elif "可空" in canonical or "下看" in canonical:
            body = [ln for ln in body if not re.search(r"可跟|上看\s*[\d.]", ln)]
        body = [_strip_inline_ctas(ln) for ln in body]
        body = [ln for ln in body if ln.strip()]
        out = "\n".join(body).rstrip()
        out = f"{out}\n{canonical}"
    if len(out) > MAX_POST_LEN:
        out = out[: MAX_POST_LEN - 1] + "…"
    return out


def _anchors_ok(out: str, original: str) -> bool:
    for tag in _extract_cashtags(original):
        if tag not in out:
            return False
    ctas = _extract_cta_lines(original)
    if ctas and not any(cta in out for cta in ctas):
        return False
    return True


_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


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

def polish_square_text(text: str, *, settings=None, compact: bool = False) -> tuple[str, str]:
    """LLM 润色广场短评。返回 (最终文本, 来源 'llm:<provider>' | 'template:<原因>')。

    校验：数字全保留；$ 标签与 CTA 行强制补回；长度 ≤ MAX_POST_LEN。
    compact=True 用短评 prompt（220–380 字），同样走 LLM。
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
    system = POLISH_SYSTEM_SHORT if compact else POLISH_SYSTEM
    max_out = 420 if compact else 950
    start = _time.time()
    for client, model, prov in _iter_chat_clients(s):
        if _time.time() - start > 60:
            break
        try:
            create_kw: dict = {
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
                "temperature": 0.7,
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
        out = _enforce_square_anchors(out, text)
        if not _anchors_ok(out, text):
            logger.warning("square polish %s 补回 $/CTA 失败，回退模板", prov)
            continue
        if not _direction_consistent(text, out):
            logger.warning("square polish %s 方向/CTA 逻辑不一致，回退模板", prov)
            continue
        if not _cta_price_logic_ok(out):
            logger.warning("square polish %s CTA 价位逻辑错误，回退模板", prov)
            continue
        if len(out) > max_out:
            logger.warning("square polish %s 过长 %d，回退模板", prov, len(out))
            continue
        return out, f"llm:{prov}{':short' if compact else ''}"
    return text, "template:fallback"


def _cooldown_path() -> Path:
    return Path(get_settings().data_cache_dir) / "square_regime_cooldown.json"


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


def _square_api_key(settings=None) -> str:
    s = settings or get_settings()
    return (getattr(s, "binance_square_openapi_key", "") or "").strip()


def _cooldown_remain(cool_key: str, cooldown_hours: float, state: dict) -> float | None:
    """冷却剩余秒数；None 表示可发。"""
    cool_h = float(cooldown_hours or 0)
    if cool_h <= 0:
        return None
    last = state.get(cool_key)
    if last is None:
        return None
    remain = cool_h * 3600 - (time.time() - last)
    return remain if remain > 0 else None


def _chart_for_post(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    swing: SwingLevels | None = None,
    regime: MarketRegime | None = None,
    title: str | None = None,
    subtitle: str | None = None,
    extra_levels: list[dict[str, Any]] | None = None,
) -> Any:
    """构建 K 线截图请求（Playwright 截 chart_capture.html）。"""
    from analyst.integrations.chart_capture import SquareChartRequest

    return SquareChartRequest(
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        swing=swing,
        regime=regime,
        title=title,
        subtitle=subtitle,
        extra_levels=extra_levels,
    )


def _chart_for_eric_post(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    bf_value: float | None = None,
    kind: str | None = None,
    title: str | None = None,
    subtitle: str | None = None,
    extra_levels: list[dict[str, Any]] | None = None,
) -> Any:
    from analyst.integrations.chart_capture import EricChartRequest

    return EricChartRequest(
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        bf_value=bf_value,
        kind=kind,
        title=title,
        subtitle=subtitle,
        extra_levels=extra_levels,
    )


def _square_image_urls(chart: Any | None, api_key: str, settings=None) -> list[str] | None:
    """渲染 K 线 PNG 并上传广场；失败返回 None（降级纯文字）。"""
    s = settings or get_settings()
    if chart is None or not getattr(s, "square_post_chart_enabled", True):
        return None
    try:
        from analyst.integrations.chart_capture import EricChartRequest, render_eric_chart, render_square_chart

        if isinstance(chart, EricChartRequest):
            png = render_eric_chart(chart)
        else:
            png = render_square_chart(chart)
        if png is not None:
            return [upload_image(api_key, png)]
    except Exception:
        logger.exception("Square 配图失败，降级纯文字")
    return None


def _publish_square_post(
    text: str,
    *,
    cool_key: str,
    cooldown_hours: float,
    settings=None,
    polish: bool | None = None,
    compact: bool | None = None,
    chart: Any | None = None,
) -> dict[str, Any] | None:
    """通用发帖：校验 key / 冷却 / 润色 / 可选 K 线截图 / POST / 写冷却。"""
    s = settings or get_settings()
    if not getattr(s, "square_post_enabled", False):
        return None
    key = _square_api_key(s)
    if not key:
        logger.warning("Square 已启用但未配置 BINANCE_SQUARE_OPENAPI_KEY，跳过")
        return None
    state = _load_cooldown()
    remain = _cooldown_remain(cool_key, cooldown_hours, state)
    if remain is not None:
        logger.info("Square 冷却中 %s remain=%.0fs", cool_key, remain)
        return None
    use_compact = compact if compact is not None else _is_compact(s)
    template_text = text
    do_polish = polish if polish is not None else getattr(s, "square_post_ai_polish", True)
    if do_polish:
        text, polish_src = polish_square_text(text, settings=s, compact=use_compact)
        text, logic = _finalize_square_text(text, template_text, polished=True)
        if logic == "template:logic_fallback":
            polish_src = f"{polish_src}|logic_fallback"
    else:
        polish_src = "template:disabled"
    if not _square_post_ok(text, template=template_text):
        logger.error("Square 模板文案逻辑仍不通过，跳过发帖 %s", cool_key)
        return None
    image_urls: list[str] | None = None
    if chart is not None:
        image_urls = _square_image_urls(chart, key, settings=s)
        if image_urls:
            polish_src = f"{polish_src}+chart"
    logger.info("Square 文案来源 %s（%s）", polish_src, cool_key)
    try:
        result = post_content(key, text, image_urls=image_urls)
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
    state[cool_key] = time.time()
    _save_cooldown(state)
    logger.info(
        "Square 已发帖 %s id=%s link=%s",
        cool_key,
        result.get("id"),
        result.get("shareLink"),
    )
    return {"text": text, "result": result, "cool_key": cool_key}


def square_symbols_set(settings=None) -> set[str]:
    s = settings or get_settings()
    raw = (getattr(s, "square_post_symbols", "") or "").strip()
    if raw:
        return set(s._csv_symbols(raw))
    return {
        "BTC/USDT",
        "ETH/USDT",
        "BNB/USDT",
        "SOL/USDT",
        "AAVE/USDT",
        "UNI/USDT",
        "HYPE/USDT",
        "ASTER/USDT",
        "DOGE/USDT",
        "LINK/USDT",
        "AVAX/USDT",
    }


def square_timeframes_set(settings=None) -> set[str]:
    s = settings or get_settings()
    # 默认只发 4h 及以上：1h 三盘来回切换，帖子观点变来变去
    raw = (getattr(s, "square_post_timeframes", "") or "4h,1d,1w").strip()
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def maybe_post_market_regime(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    swing: SwingLevels | None,
    regime: MarketRegime,
) -> dict[str, Any] | None:
    """三盘变化时发广场短文。未启用/不在白名单/冷却中 → None。"""
    settings = get_settings()
    sym = _norm_symbol(symbol)
    tf = (timeframe or "").strip().lower()
    if sym not in square_symbols_set(settings):
        return None
    if tf not in square_timeframes_set(settings):
        return None
    cool_h = float(getattr(settings, "square_post_cooldown_hours", 2) or 0)
    cool_key = f"swing|{sym}|{tf}"
    text = compose_regime_square_post(
        symbol=sym,
        timeframe=tf,
        price=price,
        swing=swing,
        regime=regime,
    )
    chart = _chart_for_post(
        symbol=sym, timeframe=tf, price=price, swing=swing, regime=regime
    )
    out = _publish_square_post(
        text, cool_key=cool_key, cooldown_hours=cool_h, settings=settings, chart=chart
    )
    if out:
        out.update({"symbol": sym, "timeframe": tf, "kind": "market_regime"})
    return out


def maybe_post_playbook_setup(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    swing: SwingLevels | None,
    regime: MarketRegime,
    flag_labels: list[str] | None = None,
) -> dict[str, Any] | None:
    """打法提示 playbook_setup 发帖。"""
    settings = get_settings()
    if not getattr(settings, "square_post_setup_enabled", True):
        return None
    sym = _norm_symbol(symbol)
    tf = (timeframe or "").strip().lower()
    if sym not in square_symbols_set(settings):
        return None
    if tf not in square_timeframes_set(settings):
        return None
    cool_h = float(getattr(settings, "square_post_setup_cooldown_hours", 6) or 0)
    cool_key = f"setup|{sym}|{tf}"
    text = compose_playbook_setup_post(
        symbol=sym,
        timeframe=tf,
        price=price,
        swing=swing,
        regime=regime,
        flag_labels=flag_labels,
    )
    chart = _chart_for_post(
        symbol=sym, timeframe=tf, price=price, swing=swing, regime=regime
    )
    out = _publish_square_post(
        text,
        cool_key=cool_key,
        cooldown_hours=cool_h,
        settings=settings,
        compact=True,
        chart=chart,
    )
    if out:
        out.update({"symbol": sym, "timeframe": tf, "kind": "playbook_setup"})
    return out


def maybe_post_level_touch(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    level: float,
    kind: str,
    swing: SwingLevels | None,
    regime: MarketRegime | None,
) -> dict[str, Any] | None:
    """关键位触碰 structure_touch 发帖。"""
    settings = get_settings()
    if not getattr(settings, "square_post_touch_enabled", True):
        return None
    sym = _norm_symbol(symbol)
    tf = (timeframe or "").strip().lower()
    if sym not in square_symbols_set(settings):
        return None
    if tf not in square_timeframes_set(settings):
        return None
    cool_h = float(getattr(settings, "square_post_touch_cooldown_hours", 4) or 0)
    lvl_key = f"{kind}:{round(float(level), 4)}"
    cool_key = f"touch|{sym}|{tf}|{lvl_key}"
    text = compose_level_touch_post(
        symbol=sym,
        timeframe=tf,
        price=price,
        level=level,
        kind=kind,
        swing=swing,
        regime=regime,
    )
    chart = _chart_for_post(
        symbol=sym,
        timeframe=tf,
        price=price,
        swing=swing,
        regime=regime,
        extra_levels=[
            {
                "price": float(level),
                "color": "#c77dff",
                "title": "触碰",
                "lineStyle": "solid",
            }
        ],
    )
    out = _publish_square_post(
        text,
        cool_key=cool_key,
        cooldown_hours=cool_h,
        settings=settings,
        compact=True,
        chart=chart,
    )
    if out:
        out.update({"symbol": sym, "timeframe": tf, "kind": "touch"})
    return out


def maybe_post_daily_recap(
    *,
    facts: dict[str, Any] | None = None,
    digest_text: str | None = None,
) -> dict[str, Any] | None:
    """UTC 每日复盘帖（与日报同刻触发）。"""
    settings = get_settings()
    if not getattr(settings, "square_post_recap_enabled", True):
        return None
    today = time.strftime("%Y-%m-%d", time.gmtime())
    cool_key = f"recap|{today}"
    movers = _fetch_24h_movers(square_symbols_set(settings))
    if digest_text and len(digest_text.strip()) <= 600 and "$BTC" in digest_text:
        text = digest_text.strip()
    else:
        text = compose_daily_recap_post(facts=facts, movers=movers)
    out = _publish_square_post(
        text, cool_key=cool_key, cooldown_hours=20.0, settings=settings, compact=True
    )
    if out:
        out.update({"kind": "recap", "day": today})
    return out


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
    stop = plan.get("stop")
    tp1 = plan.get("tp1")
    if stop is not None or tp1 is not None:
        lines.append(_cta_line(symbol, "long", defense=stop, target=tp1))
    else:
        lines.append(f"点 {tag} 看永续，超卖反弹思路见上。")
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
    use_compact = _is_compact(settings)
    text, polish_src = polish_square_text(text, settings=settings, compact=use_compact)
    tf = "1w" if kind.startswith("weekly") else "1d"
    extra: list[dict[str, Any]] = []
    plan = plan or {}
    if plan.get("stop") is not None:
        extra.append({"price": float(plan["stop"]), "color": "#f6465d", "title": "止损"})
    if plan.get("tp1") is not None:
        extra.append({"price": float(plan["tp1"]), "color": "#5eb8f0", "title": "目标"})
    chart = _chart_for_eric_post(
        symbol=sym,
        timeframe=tf,
        price=price,
        bf_value=bf_value,
        kind=kind,
        extra_levels=extra or None,
    )
    image_urls = _square_image_urls(chart, key, settings=settings)
    if image_urls:
        polish_src = f"{polish_src}+chart"
    logger.info("Square Eric 文案来源 %s（%s）", polish_src, cool_key)
    try:
        result = post_content(key, text, image_urls=image_urls)
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

_MOVE_COOLDOWN_H = 4.0


def compose_move_square_post(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    change_pct: float,
    vol_ratio: float,
    swing: SwingLevels | None,
    regime: MarketRegime,
    eric_readings: list[str] | None = None,
    compact: bool | None = None,
) -> str:
    """加速行情短文：发生了什么 → 我怎么看 →（长文含指标分析）。"""
    use_compact = compact if compact is not None else _is_compact()
    tag = _cashtag(symbol)
    tf = (timeframe or "4h").lower()
    up = change_pct > 0
    f = _fmt_price
    hook = (
        f"{tag} 这根 {tf} 直接拉了 {change_pct:+.1f}%，量放到平时的 {vol_ratio:.1f} 倍，加速了"
        if up
        else f"{tag} 这根 {tf} 直接砸了 {change_pct:+.1f}%，量放到平时的 {vol_ratio:.1f} 倍，加速下跌"
    )
    if use_compact:
        defense, near, target = _post_levels(regime, swing, float(price))
        tgt = target or near
        lines = [
            hook,
            f"现价 {f(price)} · {regime.regime_zh} · {_side_zh(regime.trade_side)}",
        ]
        if regime.nearest_resistance is not None and up:
            lines.append(f"近阻力 {f(regime.nearest_resistance)}")
        elif regime.nearest_support is not None and not up:
            lines.append(f"近支撑 {f(regime.nearest_support)}")
        if up and regime.trade_side == "long":
            brk = near or regime.nearest_resistance
            lines.append(
                _cta_line(
                    symbol,
                    "long",
                    defense=defense,
                    target=tgt,
                    near=brk,
                    scene="move_breakout",
                )
            )
        else:
            lines.append(
                _cta_line(
                    symbol,
                    regime.trade_side,
                    defense=defense,
                    target=tgt,
                    near=near,
                )
            )
        text = "\n".join(lines)
        if len(text) > MAX_POST_LEN:
            text = text[: MAX_POST_LEN - 1] + "…"
        return text
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
    lines.extend(indicator_block(regime, swing, eric_readings, price=price, timeframe=timeframe))
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
    swing: SwingLevels | None,
    regime: MarketRegime,
    eric_readings: list[str] | None = None,
) -> dict[str, Any] | None:
    """加速行情发帖：受 square_post_enabled / 品种白名单 / 冷却约束。"""
    settings = get_settings()
    if not getattr(settings, "square_post_move_enabled", True):
        return None
    sym = _norm_symbol(symbol)
    if sym not in square_symbols_set(settings):
        return None
    cool_key = f"move|{sym}"
    cool_h = float(getattr(settings, "square_move_cooldown_hours", _MOVE_COOLDOWN_H) or _MOVE_COOLDOWN_H)
    text = compose_move_square_post(
        symbol=sym,
        timeframe=timeframe,
        price=price,
        change_pct=change_pct,
        vol_ratio=vol_ratio,
        swing=swing,
        regime=regime,
        eric_readings=eric_readings,
    )
    tf = (timeframe or "4h").strip().lower()
    chart = _chart_for_post(
        symbol=sym, timeframe=tf, price=price, swing=swing, regime=regime
    )
    out = _publish_square_post(
        text, cool_key=cool_key, cooldown_hours=cool_h, settings=settings, chart=chart
    )
    if out:
        out.update({"symbol": sym, "kind": "move"})
    return out
