import sys, os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

# 確保 project root 在 sys.path（LINE Bot handler 需要）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from .models.database import init_db
from .api.routes import router
from .utils.scheduler import start_scheduler
from backtest.api import router as backtest_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initialising database...")
    await init_db()
    logger.info("Starting background scheduler...")
    scheduler = start_scheduler()
    yield
    scheduler.shutdown()


app = FastAPI(
    title="TW Bloomberg Terminal API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")
app.include_router(backtest_router, prefix="/api")


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── LINE Bot webhook（合併進主 app，Railway 只需一個服務）───────────────────
try:
    from line_webhook.handler import (
        app as _linebot_app,
        webhook as _webhook_handler,
    )
    from fastapi import Request

    @app.post("/webhook")
    async def webhook(request: Request):
        return await _webhook_handler(request)

    logger.info("LINE Bot webhook mounted at /webhook")
except Exception as e:
    logger.warning(f"LINE Bot not mounted: {e}")
