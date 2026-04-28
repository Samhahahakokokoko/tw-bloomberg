"""回測 API v2 — 含真實成本、盤態偵測、Feedback 存儲"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from .engine import BacktestEngine, BacktestResult, StrategyType, detect_market_regime
from backend.services.twse_service import fetch_kline
from loguru import logger

router = APIRouter(prefix="/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    stock_code:      str
    strategy:        StrategyType
    start_date:      Optional[str] = None
    initial_capital: float = 1_000_000
    save_result:     bool  = True   # 是否存入 feedback DB
    # MA Cross
    short:    int   = 5
    long_:    int   = 20
    # RSI
    period:   int   = 14
    overbought: float = 70
    oversold:   float = 30
    # MACD
    fast:    int = 12
    slow:    int = 26
    signal:  int = 9
    # KD
    k_period: int = 3
    d_period: int = 3
    # Bollinger
    std_mult: float = 2.0
    # PVD
    pvd_period: int = 10
    # Institutional
    consec_buy:  int = 3
    consec_sell: int = 2
    # Momentum / MeanReversion
    lookback:   int   = 20
    threshold:  float = 0.05


@router.post("/run")
async def run_backtest(req: BacktestRequest):
    kline = await fetch_kline(req.stock_code, req.start_date)
    if len(kline) < 30:
        raise HTTPException(422, f"K 線資料不足（{len(kline)} 筆），至少需要 30 筆")

    import pandas as pd
    df = pd.DataFrame(kline)
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    params = dict(
        short=req.short, long=req.long_,
        period=req.period, overbought=req.overbought, oversold=req.oversold,
        fast=req.fast, slow=req.slow, signal=req.signal,
        k_period=req.k_period, d_period=req.d_period,
        std_mult=req.std_mult,
        pvd_period=req.pvd_period,
        consec_buy=req.consec_buy, consec_sell=req.consec_sell,
        lookback=req.lookback, threshold=req.threshold,
    )

    engine = BacktestEngine(df, initial_capital=req.initial_capital)
    result = engine.run(req.strategy, **params)
    result.stock_code = req.stock_code

    # 存入 Feedback DB（非同步，不阻塞回應）
    if req.save_result:
        try:
            import asyncio
            from backtest.feedback_engine import save_backtest_result
            asyncio.create_task(save_backtest_result(result, req.stock_code))
        except Exception as e:
            logger.warning(f"Feedback save skipped: {e}")

    from dataclasses import asdict
    return asdict(result)


@router.get("/regime/{stock_code}")
async def get_regime(stock_code: str):
    """取得特定股票或大盤的市場盤態"""
    kline = await fetch_kline(stock_code)
    if not kline:
        raise HTTPException(404, f"No kline data for {stock_code}")
    import pandas as pd
    df = pd.DataFrame(kline)
    for col in ["close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    from .engine import recommend_strategy_for_regime
    regime = detect_market_regime(df)
    regime["recommended_strategy"] = recommend_strategy_for_regime(regime["current"])
    return regime


@router.get("/market-regime")
async def get_market_regime():
    """偵測整體大盤盤態（用代理指標）"""
    from .market_regime import get_market_regime as _gmr, REGIME_DESCRIPTION, REGIME_STRATEGY_TIPS
    regime = await _gmr()
    regime["description"] = REGIME_DESCRIPTION.get(regime.get("current", "unknown"), "")
    regime["strategy_tip"] = REGIME_STRATEGY_TIPS.get(regime.get("current", ""), "")
    return regime


@router.get("/strategies")
async def list_strategies():
    return [
        {"id": "ma_cross",       "name": "MA 均線交叉",         "regime": "all",      "params": ["short","long_"]},
        {"id": "rsi",            "name": "RSI 超買超賣",         "regime": "sideways", "params": ["period","overbought","oversold"]},
        {"id": "macd",           "name": "MACD 黃金死叉",        "regime": "bull",     "params": ["fast","slow","signal"]},
        {"id": "kd",             "name": "KD 隨機指標",          "regime": "all",      "params": ["k_period","d_period"]},
        {"id": "bollinger",      "name": "布林通道突破",          "regime": "sideways", "params": ["period","std_mult"]},
        {"id": "pvd",            "name": "價量背離",              "regime": "all",      "params": ["pvd_period"]},
        {"id": "institutional",  "name": "籌碼面（外資連買）",    "regime": "bull",     "params": ["consec_buy","consec_sell"]},
        {"id": "momentum",       "name": "動能追漲（多頭專用）",  "regime": "bull",     "params": ["lookback","threshold"]},
        {"id": "mean_reversion", "name": "均值回歸（盤整專用）",  "regime": "sideways", "params": ["period","std_mult"]},
        {"id": "defensive",      "name": "防禦型（空頭專用）",    "regime": "bear",     "params": ["period"]},
    ]


@router.get("/sessions")
async def list_sessions(limit: int = 20):
    from backend.models.database import AsyncSessionLocal
    from backend.models.models import BacktestSession
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(BacktestSession)
            .order_by(BacktestSession.created_at.desc())
            .limit(limit)
        )
        rows = r.scalars().all()
    return [
        {
            "session_id":       s.session_id,
            "stock_code":       s.stock_code,
            "strategy":         s.strategy,
            "total_return":     s.total_return,
            "sharpe_ratio":     s.sharpe_ratio,
            "win_rate":         s.win_rate,
            "max_drawdown":     s.max_drawdown,
            "market_regime":    s.market_regime,
            "cost_impact":      s.cost_impact,
            "created_at":       s.created_at.isoformat() if s.created_at else "",
        }
        for s in rows
    ]


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    from backtest.feedback_engine import get_session_detail
    data = await get_session_detail(session_id)
    if not data:
        raise HTTPException(404, "Session not found")
    return data


@router.get("/performance-summary")
async def performance_summary():
    from backtest.feedback_engine import get_strategy_performance_summary
    return await get_strategy_performance_summary()


@router.post("/portfolio/optimize")
async def portfolio_optimize(holdings: list[dict]):
    """
    輸入 [{"code":"2330","sector":"半導體","name":"台積電"}, ...]
    回傳馬可維茲最佳化權重（帶 max 20% per stock, max 40% per sector 約束）
    """
    from backtest.portfolio_engine import build_optimal_portfolio
    result = await build_optimal_portfolio(holdings)
    if "error" in result:
        raise HTTPException(422, result["error"])
    return result
