"""数据库模型定义 - SQLModel。

设计要点：
- 三个核心表：sessions / user_plans / ai_plans / verifications
- 一对一关系：1 session 对应 1 user_plan, 1 ai_plan, 1 verification
- 用 JSON 字段存复杂结构（market_snapshot 等）
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, Relationship, SQLModel


class Session(SQLModel, table=True):
    """一次训练会话。"""

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expire_at: datetime
    symbol: str
    timeframe: str
    status: str = Field(default="created")
    verify_after_hours: int | None = Field(default=None)
    ai_error: str | None = Field(default=None)
    market_snapshot: dict = Field(default_factory=dict, sa_column=Column(JSON))
    indicators_snapshot: dict = Field(default_factory=dict, sa_column=Column(JSON))
    # 监控页追问：[{role, content, created_at, model?}, ...]
    chat_log: list = Field(default_factory=list, sa_column=Column(JSON))

    # 关系
    user_plan: Optional["UserPlan"] = Relationship(back_populates="session")
    ai_plan: Optional["AIPlan"] = Relationship(back_populates="session")
    verification: Optional["Verification"] = Relationship(back_populates="session")


class UserPlan(SQLModel, table=True):
    """你的判断。"""

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="session.id")
    direction: str
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None = None
    confidence: int = 3              # 1-5
    rationale: str = ""
    rr_ratio: float = 0.0

    session: Session = Relationship(back_populates="user_plan")


class AIPlan(SQLModel, table=True):
    """AI 的判断。"""

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="session.id")
    direction: str
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None = None
    confidence: int = 3
    rationale: str = ""
    rr_ratio: float = 0.0
    raw_response: str = ""
    prompt_version: str = "v1"
    model_id: str = ""
    cost_usd: float = 0.0

    session: Session = Relationship(back_populates="ai_plan")


class Verification(SQLModel, table=True):
    """验证结果。"""

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="session.id")
    verified_at: datetime = Field(default_factory=datetime.utcnow)
    actual_high: float
    actual_low: float
    actual_close: float
    user_outcome: str
    user_pnl_r: float
    ai_outcome: str
    ai_pnl_r: float
    optimal_pnl_r: float
    notes: str = ""

    session: Session = Relationship(back_populates="verification")
