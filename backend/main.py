import sys, os, traceback, asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

# ── sys.path setup ────────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from .models.database import init_db
from .api.routes import router
from .utils.scheduler import start_scheduler
try:
    from backtest.api import router as backtest_router
except Exception as _e:
    backtest_router = None
    logger.warning(f"Backtest router not loaded: {_e}")

try:
    from quant.main import router as quant_router
except Exception as _e:
    quant_router = None
    logger.warning(f"Quant router not loaded: {_e}")


_startup_error: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _startup_error
    scheduler = None
    try:
        logger.info(f"Python {sys.version}")
        logger.info(f"CWD: {os.getcwd()}")
        logger.info(f"sys.path: {sys.path[:3]}")
        logger.info("Initialising database...")
        await asyncio.wait_for(init_db(), timeout=30)
        logger.info("Starting background scheduler...")
        scheduler = start_scheduler()
        # 預熱 TWSE 即時快取（讓 all_screener 啟動後立即有全市場資料）
        try:
            from backend.services.report_screener import _fetch_rt_cache
            asyncio.create_task(_fetch_rt_cache())
            logger.info("TWSE cache warm-up scheduled")
        except Exception as _ce:
            logger.warning(f"Cache warm-up failed: {_ce}")
        logger.info("Startup complete.")
    except asyncio.TimeoutError:
        _startup_error = "Database connection timed out after 30s"
        logger.error(_startup_error)
    except Exception as e:
        _startup_error = f"{type(e).__name__}: {e}"
        logger.error(f"Startup failed: {_startup_error}\n{traceback.format_exc()}")
    yield
    if scheduler:
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
if backtest_router:
    app.include_router(backtest_router, prefix="/api")
if quant_router:
    app.include_router(quant_router, prefix="/api")

# 靜態報告圖片（選股表圖片由此 URL 提供給 LINE Image Message）
_STATIC_REPORTS = os.path.join(os.getcwd(), "static", "reports")
os.makedirs(_STATIC_REPORTS, exist_ok=True)
app.mount("/static/reports", StaticFiles(directory=_STATIC_REPORTS), name="reports")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/health/detail")
async def health_detail():
    if _startup_error:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "detail": _startup_error},
        )
    return {"status": "ok", "version": "1.0.0"}


# ── 全域例外處理（讓錯誤可見，不只是 500）────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled: {request.url} → {exc}\n{traceback.format_exc()}")
    return JSONResponse(status_code=500, content={"error": str(exc)})


# ── WebSocket 即時市場資料推送 ───────────────────────────────────────────────
_ws_clients: list[WebSocket] = []

@app.websocket("/ws/market")
async def ws_market(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    try:
        while True:
            try:
                from .services.twse_service import fetch_market_overview
                ov = await fetch_market_overview()
                if ov:
                    await ws.send_json({"type": "market", "data": ov})
            except Exception:
                pass
            await asyncio.sleep(60)
    except WebSocketDisconnect:
        _ws_clients.remove(ws)
    except Exception:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


# ── LINE Bot webhook（失敗不影響主 app）──────────────────────────────────────
try:
    from line_webhook.handler import router as _webhook_router
    app.include_router(_webhook_router)
    logger.info("LINE Bot webhook mounted at /webhook")
except Exception as e:
    logger.warning(f"LINE Bot not mounted (non-fatal): {e}")
    logger.debug(traceback.format_exc())
