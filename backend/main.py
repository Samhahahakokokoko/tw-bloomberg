import asyncio
import os
import sys
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from .api.routes import router
from .models.database import init_db
from .utils.scheduler import start_scheduler


async def _startup_catchup() -> None:
    """補跑因 deploy 錯過的當日 Agent A/B。

    Railway log 時間戳為 UTC，Agent B 排在台灣 18:30（UTC 10:30）。
    若容器在 18:30 之後才啟動，APScheduler 不補跑已過時 cron，
    導致當日 stock_scores 永遠是空的。
    """
    import asyncio as _aio
    from datetime import datetime, timezone, timedelta

    await _aio.sleep(10)          # 等 DB / scheduler 完全就緒
    try:
        tw = timezone(timedelta(hours=8))
        now_tw = datetime.now(tw)
        today  = now_tw.strftime("%Y-%m-%d")
        # 僅在平日 18:30 之後才需要補跑
        if now_tw.weekday() >= 5 or now_tw.hour < 18 or (now_tw.hour == 18 and now_tw.minute < 30):
            return

        from .models.database import AsyncSessionLocal
        from .models.models import StockScore
        from sqlalchemy import select, func

        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(func.count()).where(StockScore.score_date == today)
            )
            count = r.scalar() or 0

        if count > 0:
            logger.info(f"[Catchup] stock_scores for {today} already has {count} rows, skip.")
            return

        logger.warning(f"[Catchup] {today} stock_scores=0, deployed after 18:30 — running Agent A+B now")

        # Agent A first (data fetch), then Agent B (scoring)
        try:
            from .services.data_pipeline import run_daily_pipeline
            await run_daily_pipeline(trigger_scoring=False)
            logger.info("[Catchup] Agent A done")
        except Exception as e:
            logger.error(f"[Catchup] Agent A failed: {e}")

        try:
            from .services.score_updater import run_score_update
            await run_score_update()
            logger.info("[Catchup] Agent B done")
        except Exception as e:
            logger.error(f"[Catchup] Agent B failed: {e}")

    except Exception as e:
        logger.error(f"[Catchup] startup_catchup error: {e}")

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
        try:
            from .services.analyst_tracker import init_default_analysts

            await init_default_analysts()
        except Exception as _ae:
            logger.warning(f"Analyst init skipped: {_ae}")
        logger.info("Starting background scheduler...")
        scheduler = start_scheduler()
        try:
            from backend.services.report_screener import _fetch_rt_cache

            asyncio.create_task(_fetch_rt_cache())
            logger.info("TWSE cache warm-up scheduled")
        except Exception as _ce:
            logger.warning(f"Cache warm-up failed: {_ce}")
        # Catch-up: if deployed after 18:30 Taiwan time and today's scores are missing, run Agent B now
        asyncio.create_task(_startup_catchup())
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
        except Exception as e:
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


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled: {request.url} - {exc}\n{traceback.format_exc()}")
    return JSONResponse(status_code=500, content={"error": str(exc)})


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
            except Exception as e:
                pass
            await asyncio.sleep(60)
    except WebSocketDisconnect:
        _ws_clients.remove(ws)
    except Exception as e:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


try:
    from line_webhook.handler import router as _webhook_router

    app.include_router(_webhook_router)
    logger.info("LINE Bot webhook mounted at /webhook")
except Exception as e:
    logger.warning(f"LINE Bot not mounted (non-fatal): {e}")
    logger.debug(traceback.format_exc())


_FRONTEND_DIST = os.path.join(_ROOT, "frontend", "dist")
_FRONTEND_INDEX = os.path.join(_FRONTEND_DIST, "index.html")
_FRONTEND_ASSETS = os.path.join(_FRONTEND_DIST, "assets")

if os.path.exists(_FRONTEND_INDEX):
    if os.path.isdir(_FRONTEND_ASSETS):
        app.mount("/assets", StaticFiles(directory=_FRONTEND_ASSETS), name="frontend-assets")

    @app.get("/", include_in_schema=False)
    async def frontend_index():
        return FileResponse(_FRONTEND_INDEX)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def frontend_spa(full_path: str):
        candidate = os.path.join(_FRONTEND_DIST, full_path)
        if os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(_FRONTEND_INDEX)
else:
    logger.warning(f"Frontend dist not found at {_FRONTEND_DIST}; root URL will return 404")
