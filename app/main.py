"""FastAPI 应用工厂与生命周期管理。

启动顺序：
  1. 加载配置（config.yaml + .env）
  2. 初始化日志
  3. 启动 TradingEngine（连接 IB，订阅 K 线）
  4. 挂载 API 路由 + 静态 Web UI

访问：http://127.0.0.1:8000/
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import router as api_router
from core.engine import TradingEngine
from core.logging_setup import setup_logging
from core.settings import get_settings

logger = logging.getLogger(__name__)

_WEBUI_DIR = Path(__file__).parent.parent / "webui"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── 启动 ──────────────────────────────────────────────────────────────
    setup_logging()
    logger.info("IB Bot 启动中…")

    settings = get_settings()
    engine = TradingEngine(settings)

    try:
        await engine.start()
    except Exception as e:
        logger.error(f"引擎启动失败: {e}")
        raise

    app.state.engine = engine
    logger.info("IB Bot 就绪")

    yield

    # ── 关闭 ──────────────────────────────────────────────────────────────
    logger.info("IB Bot 关闭中…")
    await engine.stop()
    logger.info("IB Bot 已关闭")


def create_app() -> FastAPI:
    app = FastAPI(
        title="IB Bot",
        description="Interactive Brokers 期货止损机器人",
        version="1.0.0",
        lifespan=lifespan,
    )

    # API 路由
    app.include_router(api_router)

    # 静态资源（CSS / JS）
    if _WEBUI_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_WEBUI_DIR)), name="static")

    # 根路由 → Web 控制台
    @app.get("/", include_in_schema=False)
    async def index():
        html = _WEBUI_DIR / "index.html"
        if html.exists():
            return FileResponse(str(html))
        return {"message": "IB Bot is running"}

    return app


app = create_app()
