import sys, os, traceback
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

# ── sys.path setup ────────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from .models.database import init_db
from .api.routes import router
from .utils.scheduler import start_scheduler
from backtest.api import router as backtest_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        logger.info(f"Python {sys.version}")
        logger.info(f"CWD: {os.getcwd()}")
        logger.info(f"sys.path: {sys.path[:3]}")
        logger.info("Initialising database...")
        await init_db()
        logger.info("Starting background scheduler...")
        scheduler = start_scheduler()
    except Exception as e:
        logger.error(f"Startup failed: {e}\n{traceback.format_exc()}")
        raise   # 讓 uvicorn 知道啟動失敗
    yield
    try:
        scheduler.shutdown()
    except Exception:
        pass


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
    return {"status": "ok", "version": "1.0.0"}


# ── 全域例外處理（讓錯誤可見，不只是 500）────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled: {request.url} → {exc}\n{traceback.format_exc()}")
    return JSONResponse(status_code=500, content={"error": str(exc)})


# ── LINE Bot webhook（失敗不影響主 app）──────────────────────────────────────
try:
    from line_webhook.handler import webhook as _webhook_handler

    @app.post("/webhook")
    async def webhook(request: Request):
        return await _webhook_handler(request)

    logger.info("LINE Bot webhook mounted at /webhook")
except Exception as e:
    logger.warning(f"LINE Bot not mounted (non-fatal): {e}")
    logger.debug(traceback.format_exc())
