"""FastAPI 应用入口。"""

from __future__ import annotations

import asyncio
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


def _configure_analyst_logging() -> None:
    """让盯盘/Telegram 的 INFO 打到 uvicorn 控制台。"""
    level = logging.INFO
    uv = logging.getLogger("uvicorn.error")
    for name in (
        "analyst",
        "analyst.monitor",
        "analyst.monitor.hub",
        "analyst.monitor.notifier",
    ):
        log = logging.getLogger(name)
        log.setLevel(level)
        if getattr(log, "_analyst_uv_wired", False):
            continue
        if uv.handlers:
            for h in uv.handlers:
                log.addHandler(h)
            log.propagate = False
            setattr(log, "_analyst_uv_wired", True)
        else:
            log.propagate = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    import analyst.web.routes as routes_mod
    from analyst.monitor.hub import get_monitor_hub

    _configure_analyst_logging()
    logger.info(
        "Crypto Analyst web 源码: server=%s routes=%s",
        Path(__file__).resolve(),
        Path(routes_mod.__file__).resolve(),
    )
    init_db()
    hub = get_monitor_hub()
    heartbeat_task: asyncio.Task | None = None
    try:
        info = await hub.start_always_on_workers()
        if info.get("enabled"):
            logger.info(
                "常驻盯盘: tfs=%s symbols=%s tg=%s workers=%s",
                info.get("timeframes") or info.get("timeframe"),
                info.get("symbols"),
                info.get("telegram_ready"),
                len(info.get("running") or []),
            )
        else:
            logger.info(
                "常驻盯盘未开启（MONITOR_ALWAYS_ON=false 或品种为空）tg=%s",
                info.get("telegram_ready"),
            )

        async def _heartbeat_loop() -> None:
            while True:
                await asyncio.sleep(60)
                try:
                    hub.log_heartbeat()
                except Exception:
                    logger.exception("monitor heartbeat failed")

        heartbeat_task = asyncio.create_task(_heartbeat_loop(), name="monitor-heartbeat")
    except Exception:
        logger.exception("start always-on workers failed")
    yield
    if heartbeat_task:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


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

    @app.get("/favicon.ico")
    def favicon_ico():
        return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")

    @app.get("/favicon.svg")
    def favicon_svg():
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    return app


app = create_app()


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """启动 uvicorn 服务器。"""
    import uvicorn

    _configure_analyst_logging()
    uvicorn.run(app, host=host, port=port, log_level="info")
