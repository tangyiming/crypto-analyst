"""FastAPI 应用入口。"""

from __future__ import annotations

import logging
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse

from analyst.storage.db import init_db
from analyst.web.routes import router
from analyst.web.monitor_routes import router as monitor_router

logger = logging.getLogger("uvicorn.error")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    import analyst.web.routes as routes_mod
    from analyst.monitor.hub import get_monitor_hub

    logger.info(
        "Crypto Analyst web 源码: server=%s routes=%s",
        Path(__file__).resolve(),
        Path(routes_mod.__file__).resolve(),
    )
    init_db()
    try:
        info = await get_monitor_hub().start_always_on_workers()
        if info.get("enabled"):
            logger.info(
                "常驻盯盘: tfs=%s symbols=%s tg=%s",
                info.get("timeframes") or info.get("timeframe"),
                info.get("symbols"),
                info.get("telegram_ready"),
            )
    except Exception:
        logger.exception("start always-on workers failed")
    yield


def create_app() -> FastAPI:
    """创建 FastAPI 应用。"""
    app = FastAPI(
        title="Crypto Analyst",
        description="U 本位合约监控 + AI 行情分析",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(router)
    app.include_router(monitor_router)

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """启动 uvicorn 服务器。"""
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
