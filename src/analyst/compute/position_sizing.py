"""头仓 / 补仓仓位模型（零下二度风格）。

防踏空用小头仓；总短线仓位封顶；回踩补仓与突破补仓二选一，不可叠加。
与 Kelly 模块互补：本模块管「分层结构」，Kelly 管「单笔风险比例」。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class SeedPositionPlan:
    account_usd: float
    leverage: float
    seed_pct: float
    add_pct: float
    max_total_pct: float
    seed_margin: float
    add_margin: float
    total_margin: float
    seed_notional: float
    add_notional: float
    total_notional: float
    add_mode: str  # pullback | breakout | none
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


def plan_seed_position(
    account_usd: float,
    *,
    leverage: float = 25.0,
    seed_pct: float = 0.04,
    max_total_pct: float = 0.18,
    add_mode: str = "pullback",
) -> SeedPositionPlan:
    """计算头仓与补仓保证金/名义。

    Args:
        account_usd: 账户权益（USDT）
        leverage: 杠杆倍数（仅用于名义仓位展示）
        seed_pct: 头仓占权益比例（建议 0.03–0.04）
        max_total_pct: 短线总仓占权益上限（建议 0.18）
        add_mode: pullback=回踩补仓；breakout=突破补仓；none=只开头仓
    """
    if account_usd <= 0:
        raise ValueError("account_usd 必须 > 0")
    if leverage <= 0:
        raise ValueError("leverage 必须 > 0")
    seed_pct = min(max(seed_pct, 0.01), 0.10)
    max_total_pct = min(max(max_total_pct, seed_pct), 0.50)
    add_mode = add_mode if add_mode in ("pullback", "breakout", "none") else "pullback"

    add_pct = 0.0 if add_mode == "none" else max(0.0, max_total_pct - seed_pct)
    seed_margin = account_usd * seed_pct
    add_margin = account_usd * add_pct
    total_margin = seed_margin + add_margin
    seed_notional = seed_margin * leverage
    add_notional = add_margin * leverage
    total_notional = total_margin * leverage

    mode_zh = {
        "pullback": "回踩补仓（原计划低多点）",
        "breakout": "突破补仓（站稳后加）",
        "none": "仅头仓",
    }[add_mode]

    note = (
        f"头仓 {seed_pct*100:.1f}%≈{seed_margin:.0f}U 保证金（名义≈{seed_notional:.0f}U @ {leverage:.0f}x）；"
        f"补仓模式={mode_zh}，补仓 {add_pct*100:.1f}%≈{add_margin:.0f}U；"
        f"合计≤{max_total_pct*100:.0f}%。若已回踩补仓则禁止再在突破处二次加仓。"
    )

    return SeedPositionPlan(
        account_usd=account_usd,
        leverage=leverage,
        seed_pct=seed_pct,
        add_pct=add_pct,
        max_total_pct=max_total_pct,
        seed_margin=seed_margin,
        add_margin=add_margin,
        total_margin=total_margin,
        seed_notional=seed_notional,
        add_notional=add_notional,
        total_notional=total_notional,
        add_mode=add_mode,
        note=note,
    )
