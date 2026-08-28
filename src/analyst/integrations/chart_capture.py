"""K 线 DOM 截图：lightweight-charts 离页渲染 → PNG（供币安广场配图）。

依赖 playwright（可选）：uv sync --extra square && playwright install chromium
未安装时 capture 返回 None，发帖降级为纯文字。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analyst.config import get_settings
from analyst.monitor.serialize import candle_to_dict

logger = logging.getLogger(__name__)

_CAPTURE_HTML = Path(__file__).resolve().parents[1] / "web" / "static" / "chart_capture.html"

_LEVEL_DEFENSE = "#f6465d"
_LEVEL_NEAR = "#f0b90b"
_LEVEL_TARGET = "#5eb8f0"
_LEVEL_TOUCH = "#c77dff"
_LEVEL_NOW = "#eaecef"


def _fmt_chart_price(x: float | None) -> str:
    from analyst.monitor.square_posts import _fmt_price

    return _fmt_price(x)


def _level(price: float, *, label: str, color: str, solid: bool = False) -> dict[str, Any]:
    px = _fmt_chart_price(price)
    return {
        "price": float(price),
        "color": color,
        "label": label,
        "title": f"{label} {px}",
        "lineStyle": "solid" if solid else "dashed",
        "lineWidth": 2,
    }


def _normalize_extra_level(lv: dict[str, Any]) -> dict[str, Any]:
    if lv.get("label"):
        out = dict(lv)
        p = float(out["price"])
        if not out.get("title"):
            out["title"] = f"{out['label']} {_fmt_chart_price(p)}"
        return out
    title = str(lv.get("title") or "位")
    solid = lv.get("lineStyle") == "solid"
    return _level(
        float(lv["price"]),
        label=title,
        color=str(lv.get("color") or _LEVEL_TOUCH),
        solid=solid,
    )


def _above(price: float, x: float | None, *, tol: float = 0.001) -> bool:
    return x is not None and float(x) > float(price) * (1 + tol)


def _below(price: float, x: float | None, *, tol: float = 0.001) -> bool:
    return x is not None and float(x) < float(price) * (1 - tol)


def _dup_level(levels: list[dict[str, Any]], price: float, ref: float) -> bool:
    return any(abs(float(x["price"]) - float(price)) < max(abs(ref) * 1e-5, 1e-6) for x in levels)


def _chart_levels(regime: Any | None, jack: Any | None, price: float) -> list[dict[str, Any]]:
    from analyst.monitor.square_posts import _post_levels

    if regime is None:
        return []
    p = float(price)
    side = getattr(regime, "trade_side", None)
    defense, near, target = _post_levels(regime, jack, p)
    out: list[dict[str, Any]] = []
    if defense is not None and _below(p, defense):
        out.append(_level(defense, label="防守", color=_LEVEL_DEFENSE))
    if near is not None:
        ok = _below(p, near) if side == "short" else _above(p, near)
        if ok:
            lbl = "近支" if side == "short" else "近压"
            out.append(_level(near, label=lbl, color=_LEVEL_NEAR))
    tgt = target or near
    if tgt is not None and tgt not in (defense, near):
        ok = _below(p, tgt) if side == "short" else _above(p, tgt)
        if ok:
            lbl = "下看" if side == "short" else "目标"
            out.append(_level(tgt, label=lbl, color=_LEVEL_TARGET))
    # 补充支撑/阻力：必须在现价下方/上方，避免「近压」画在 K 线下面
    ns = getattr(regime, "nearest_support", None)
    nr = getattr(regime, "nearest_resistance", None)
    if ns is not None and _below(p, ns) and not _dup_level(out, float(ns), p):
        out.append(_level(float(ns), label="支撑", color="#3d9970"))
    if nr is not None and _above(p, nr) and not _dup_level(out, float(nr), p):
        out.append(_level(float(nr), label="阻力", color="#e67e22"))
    return out


@dataclass
class SquareChartRequest:
    symbol: str
    timeframe: str
    price: float
    regime: Any | None = None
    jack: Any | None = None
    title: str | None = None
    subtitle: str | None = None
    extra_levels: list[dict[str, Any]] | None = None


@dataclass
class EricChartRequest:
    """Eric 超卖帖：K 线 + 波段过滤器双面板。"""

    symbol: str
    timeframe: str
    price: float
    bf_value: float | None = None
    kind: str | None = None
    title: str | None = None
    subtitle: str | None = None
    extra_levels: list[dict[str, Any]] | None = None


_ERIC_KIND_ZH = {
    "weekly_watch": "周线超卖",
    "weekly_entry": "周线拐头",
    "weekly_entry_half": "周线半仓",
    "daily_oversold": "日线超卖",
    "weekly_tp1": "周线止盈①",
    "weekly_tp2": "周线止盈②",
    "weekly_stop": "周线止损",
}


def _bf_bar_color(v: float) -> str:
    if v <= -48:
        return "#0ecb81"
    if v <= -40:
        return "#26a69a"
    if v >= 48:
        return "#f6465d"
    if v >= 40:
        return "#e74c3c"
    return "#5c6670"


def build_eric_chart_payload(req: EricChartRequest) -> dict[str, Any]:
    """Eric 帖：价格 K 线 + 底部波段过滤器柱。"""
    from analyst.compute.band_filter import band_filter_series
    from analyst.data.fetcher import fetch_candles

    s = get_settings()
    sym = req.symbol
    tf = (req.timeframe or "1d").strip().lower()
    bars = int(getattr(s, "square_post_chart_bars", 120) or 120)
    series = fetch_candles(sym, tf, limit=max(40, bars), market="futures")
    raw_candles = series.candles[-bars:]
    candles = [candle_to_dict(c) for c in raw_candles]
    live_price = float(candles[-1]["close"]) if candles else float(req.price)
    chart_price = live_price

    highs = [float(c.high) for c in raw_candles]
    lows = [float(c.low) for c in raw_candles]
    closes = [float(c.close) for c in raw_candles]
    bf_vals = band_filter_series(highs, lows, closes)
    filter_bars = [
        {
            "time": candles[i]["time"],
            "value": round(float(bf_vals[i]), 2),
            "color": _bf_bar_color(float(bf_vals[i])),
        }
        for i in range(len(candles))
    ]
    current_bf = float(req.bf_value) if req.bf_value is not None else float(bf_vals[-1])

    base = sym.split("/")[0].replace("USDT", "")
    tag = f"${base}"
    kind_zh = _ERIC_KIND_ZH.get(str(req.kind or ""), "波段过滤器")
    title = req.title or f"{tag} {tf.upper()} · Eric {kind_zh}"
    subtitle = req.subtitle or f"现价 {_fmt_chart_price(chart_price)} · 读数 {current_bf:+.0f}（≤-40 超卖）"

    levels: list[dict[str, Any]] = []
    if req.extra_levels:
        for lv in req.extra_levels:
            levels.append(_normalize_extra_level(lv))

    seen: set[float] = set()
    uniq: list[dict[str, Any]] = []
    for lv in levels:
        p = round(float(lv["price"]), 8)
        if p in seen:
            continue
        seen.add(p)
        uniq.append(lv)

    return {
        "mode": "eric",
        "title": title,
        "subtitle": subtitle,
        "price": chart_price,
        "candles": candles,
        "levels": uniq[:6],
        "filter": {
            "bars": filter_bars,
            "current": round(current_bf, 2),
            "os_level": -40,
            "ob_level": 40,
        },
    }


def build_chart_payload(req: SquareChartRequest) -> dict[str, Any]:
    """拉 K 线 + 点位标注，供 chart_capture.html 渲染。"""
    from analyst.data.fetcher import fetch_candles

    s = get_settings()
    sym = req.symbol
    tf = (req.timeframe or "4h").strip().lower()
    bars = int(getattr(s, "square_post_chart_bars", 120) or 120)
    series = fetch_candles(sym, tf, limit=max(40, bars), market="futures")
    candles = [candle_to_dict(c) for c in series.candles[-bars:]]
    # 用 K 线最新收盘价对齐标注，避免文案价 65000 但图上实际 79000 导致「近压」全在地下
    live_price = float(candles[-1]["close"]) if candles else float(req.price)
    chart_price = live_price
    base = sym.split("/")[0].replace("USDT", "")
    tag = f"${base}"
    regime_zh = getattr(req.regime, "regime_zh", None) if req.regime is not None else None
    side = getattr(req.regime, "trade_side", None) if req.regime is not None else None
    side_zh = {"long": "偏多", "short": "偏空"}.get(str(side or ""), "观望")
    title = req.title or f"{tag} {tf.upper()} · {regime_zh or '—'}"
    subtitle = req.subtitle or f"现价 {_fmt_chart_price(chart_price)} · {side_zh}"
    levels = _chart_levels(req.regime, req.jack, chart_price)
    if req.extra_levels:
        for lv in req.extra_levels:
            levels.append(_normalize_extra_level(lv))
    # 去重价位（保留先出现的标签）
    seen: set[float] = set()
    uniq: list[dict[str, Any]] = []
    for lv in levels:
        p = round(float(lv["price"]), 8)
        if p in seen:
            continue
        seen.add(p)
        uniq.append(lv)
    return {
        "title": title,
        "subtitle": subtitle,
        "price": chart_price,
        "candles": candles,
        "levels": uniq[:8],
    }


def capture_chart_png(payload: dict[str, Any], out_path: Path | None = None) -> Path | None:
    """Playwright 打开 chart_capture.html，截图 #capture（含标题与点位图例）。"""
    if not _CAPTURE_HTML.is_file():
        logger.warning("chart_capture.html 不存在：%s", _CAPTURE_HTML)
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.info("未安装 playwright，跳过 K 线截图（uv sync --extra square && playwright install chromium）")
        return None

    s = get_settings()
    width = int(getattr(s, "square_post_chart_width", 900) or 900)
    default_h = int(getattr(s, "square_post_chart_height", 600) or 600)
    height = 600 if (payload or {}).get("mode") == "eric" else default_h
    if out_path is None:
        cache = Path(s.data_cache_dir) / "square_charts"
        cache.mkdir(parents=True, exist_ok=True)
        out_path = cache / f"chart_{int(time.time())}.png"

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(_CAPTURE_HTML.resolve().as_uri(), wait_until="load", timeout=30_000)
            page.evaluate("(payload) => window.renderChart(payload)", payload)
            page.wait_for_selector("body[data-ready='1']", timeout=20_000)
            page.locator("#capture").screenshot(path=str(out_path), type="png")
            browser.close()
    except Exception:
        logger.exception("K 线 DOM 截图失败")
        return None
    return out_path


def render_eric_chart(req: EricChartRequest) -> Path | None:
    """Eric 双面板截图。"""
    try:
        payload = build_eric_chart_payload(req)
    except Exception:
        logger.exception("构建 Eric K 线 payload 失败 %s %s", req.symbol, req.timeframe)
        return None
    if not payload.get("candles") or not payload.get("filter", {}).get("bars"):
        return None
    return capture_chart_png(payload)


def render_square_chart(req: SquareChartRequest) -> Path | None:
    """构建 payload 并截图；失败返回 None。"""
    try:
        payload = build_chart_payload(req)
    except Exception:
        logger.exception("构建 K 线 payload 失败 %s %s", req.symbol, req.timeframe)
        return None
    if not payload.get("candles"):
        return None
    return capture_chart_png(payload)
