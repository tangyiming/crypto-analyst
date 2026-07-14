"""仓储层 - 业务层只调这里，不直接碰 SQL。"""

from datetime import datetime, timedelta

from sqlmodel import select

from analyst.config import get_settings
from analyst.storage.db import get_db_session
from analyst.storage.models import AIPlan, Session, UserPlan, Verification


# ─────────────────────────────────────
# Session
# ─────────────────────────────────────
def create_session(
    symbol: str,
    timeframe: str,
    expire_at: datetime,
    market_snapshot: dict,
    indicators_snapshot: dict,
    verify_after_hours: int | None = None,
) -> Session:
    session = Session(
        symbol=symbol,
        timeframe=timeframe,
        expire_at=expire_at,
        verify_after_hours=verify_after_hours,
        market_snapshot=market_snapshot,
        indicators_snapshot=indicators_snapshot,
        status="created",
    )
    with get_db_session() as db:
        db.add(session)
        db.commit()
        db.refresh(session)
    return session


def update_session_status(session_id: int, status: str) -> None:
    with get_db_session() as db:
        s = db.get(Session, session_id)
        if s:
            s.status = status
            db.add(s)
            db.commit()


def set_session_ai_error(session_id: int, message: str | None) -> None:
    with get_db_session() as db:
        s = db.get(Session, session_id)
        if s:
            s.ai_error = message
            db.add(s)
            db.commit()


def append_session_chat(
    session_id: int,
    turns: list[dict],
    *,
    max_turns: int = 100,
) -> list[dict]:
    """追加会话追问记录，返回最新 chat_log。"""
    with get_db_session() as db:
        s = db.get(Session, session_id)
        if not s:
            raise ValueError(f"会话 #{session_id} 不存在")
        log = list(s.chat_log or [])
        for t in turns:
            if not isinstance(t, dict):
                continue
            role = t.get("role")
            content = (t.get("content") or "").strip()
            if role not in ("user", "assistant") or not content:
                continue
            log.append(
                {
                    "role": role,
                    "content": content[:4000],
                    "created_at": t.get("created_at") or datetime.utcnow().isoformat() + "Z",
                    "model": t.get("model"),
                }
            )
        s.chat_log = log[-max_turns:]
        db.add(s)
        db.commit()
        db.refresh(s)
        return list(s.chat_log or [])


def get_session_chat_log(session_id: int) -> list[dict]:
    with get_db_session() as db:
        s = db.get(Session, session_id)
        if not s:
            return []
        return list(s.chat_log or [])


def get_session(session_id: int) -> Session | None:
    with get_db_session() as db:
        return db.get(Session, session_id)


def list_sessions(
    limit: int = 20,
    symbol: str | None = None,
    status: str | None = None,
) -> list[Session]:
    with get_db_session() as db:
        stmt = select(Session).order_by(Session.created_at.desc())
        if symbol:
            stmt = stmt.where(Session.symbol == symbol)
        if status:
            stmt = stmt.where(Session.status == status)
        stmt = stmt.limit(limit)
        return list(db.exec(stmt))


def list_pending_verification() -> list[Session]:
    """所有 ai_planned 状态、且距创建已超过 verification_delay_hours 的会话。"""
    settings = get_settings()
    cutoff = datetime.utcnow() - timedelta(hours=settings.verification_delay_hours)

    with get_db_session() as db:
        stmt = select(Session).where(
            Session.status == "ai_planned",
            Session.created_at <= cutoff,
        )
        return list(db.exec(stmt))


# ─────────────────────────────────────
# UserPlan / AIPlan
# ─────────────────────────────────────
def save_user_plan(plan: UserPlan) -> UserPlan:
    with get_db_session() as db:
        db.add(plan)
        db.commit()
        db.refresh(plan)
    return plan


def save_ai_plan(plan: AIPlan) -> AIPlan:
    with get_db_session() as db:
        db.add(plan)
        db.commit()
        db.refresh(plan)
    return plan


def get_user_plan(session_id: int) -> UserPlan | None:
    with get_db_session() as db:
        stmt = select(UserPlan).where(UserPlan.session_id == session_id)
        return db.exec(stmt).first()


def get_ai_plan(session_id: int) -> AIPlan | None:
    with get_db_session() as db:
        stmt = select(AIPlan).where(AIPlan.session_id == session_id)
        return db.exec(stmt).first()


# ─────────────────────────────────────
# Verification
# ─────────────────────────────────────
def save_verification(v: Verification) -> Verification:
    with get_db_session() as db:
        db.add(v)
        db.commit()
        db.refresh(v)
    return v


def get_verification(session_id: int) -> Verification | None:
    with get_db_session() as db:
        stmt = select(Verification).where(Verification.session_id == session_id)
        return db.exec(stmt).first()


def delete_session(session_id: int) -> None:
    """删除会话及其关联的 user_plan / ai_plan / verification。"""
    with get_db_session() as db:
        for model in (Verification, AIPlan, UserPlan):
            obj = db.exec(select(model).where(model.session_id == session_id)).first()
            if obj:
                db.delete(obj)
        s = db.get(Session, session_id)
        if s:
            db.delete(s)
        db.commit()


# ─────────────────────────────────────
# 复合查询
# ─────────────────────────────────────
def list_verified_sessions(
    period_days: int = 30,
) -> list[tuple[Session, UserPlan | None, AIPlan, Verification]]:
    """获取所有已验证会话及其完整数据。

    返回 [(Session, UserPlan, AIPlan, Verification), ...]
    UserPlan 可能为 None（quick 模式跑过的会话）。
    """
    cutoff = datetime.utcnow() - timedelta(days=period_days)

    with get_db_session() as db:
        stmt = (
            select(Session)
            .where(
                Session.status == "verified",
                Session.created_at >= cutoff,
            )
            .order_by(Session.created_at.desc())
        )
        sessions = list(db.exec(stmt))

        results: list[tuple[Session, UserPlan | None, AIPlan, Verification]] = []
        for s in sessions:
            up = db.exec(select(UserPlan).where(UserPlan.session_id == s.id)).first()
            ap = db.exec(select(AIPlan).where(AIPlan.session_id == s.id)).first()
            v = db.exec(select(Verification).where(Verification.session_id == s.id)).first()
            if ap and v:
                results.append((s, up, ap, v))
        return results
