"""数据库连接与初始化。"""

from sqlalchemy import text
from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, create_engine

from analyst.config import get_settings


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        connect_args = (
            {"check_same_thread": False}
            if settings.database_url.startswith("sqlite")
            else {}
        )
        _engine = create_engine(settings.database_url, echo=False, connect_args=connect_args)
    return _engine


# ─────────────────────────────────────
# 轻量迁移
# ─────────────────────────────────────
# create_all 不会给已存在的表加列，这里维护一份增量 schema 变更列表。
# 每条用 (table, column, ddl) 描述。启动时按顺序检测并补齐。
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("session", "verify_after_hours", "INTEGER"),
    ("session", "ai_error", "VARCHAR"),
    ("session", "chat_log", "JSON"),
]


def _apply_migrations(engine) -> None:
    """SQLite 兼容的轻量列迁移。"""
    with engine.begin() as conn:
        for table, column, ddl in _MIGRATIONS:
            cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if column not in cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def init_db() -> None:
    """创建所有表 + 应用迁移。"""
    from analyst.storage import models  # noqa: F401  - 触发模型注册

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _apply_migrations(engine)


def get_db_session() -> DBSession:
    """获取一个 DB 会话。"""
    return DBSession(get_engine())
