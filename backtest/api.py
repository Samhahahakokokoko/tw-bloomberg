"""回測 API — FastAPI router，掛到主 backend"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from .engine import BacktestEngine, StrategyType
from backend.services.twse_service import fetch_kline
from loguru import logger

router = APIRouter(prefix="/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    stock_code: str
    strategy: StrategyType
    start_date: Optional[str] = None
    initial_capital: float = 1_000_000
    # MA Cross
    short: int = 5
    long_: int = 20
    # RSI
    period: int = 14
    overbought: float = 70
    oversold: float = 30
    # MACD
    fast: int = 12
    slow: int = 26
    signal: int = 9
    # KD
    k_period: int = 3
    d_period: int = 3
    # Bollinger Bands
    std_mult: float = 2.0
    # PVD
    pvd_period: int = 10
    # Institutional
    consec_buy: int = 3
    consec_sell: int = 2


@router.post("/run")
async def run_backtest(req: BacktestRequest):
    kline = await fetch_kline(req.stock_code, req.start_date)
    if len(kline) < 30:
        raise HTTPException(422, f"K線資料不足（{len(kline)} 筆），至少需要 30 筆")

    import pandas as pd
    df = pd.DataFrame(kline)
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    params = {
        "short": req.short, "long": req.long_,
        "period": req.period if req.strategy not in ("pvd",) else req.pvd_period,
        "overbought": req.overbought, "oversold": req.oversold,
        "fast": req.fast, "slow": req.slow, "signal": req.signal,
        "k_period": req.k_period, "d_period": req.d_period,
        "std_mult": req.std_mult,
        "pvd_period": req.pvd_period,
        "consec_buy": req.consec_buy, "consec_sell": req.consec_sell,
    }

    engine = BacktestEngine(df, initial_capital=req.initial_capital)
    result = engine.run(req.strategy, **params)
    result.stock_code = req.stock_code

    from dataclasses import asdict
    return asdict(result)


@router.get("/strategies")
async def list_strategies():
    return [
        {"id": "ma_cross",     "name": "MA 均線交叉",      "params": ["short", "long_"]},
        {"id": "rsi",          "name": "RSI 超買超賣",      "params": ["period", "overbought", "oversold"]},
        {"id": "macd",         "name": "MACD 黃金死叉",     "params": ["fast", "slow", "signal"]},
        {"id": "kd",           "name": "KD 隨機指標",       "params": ["k_period", "d_period"]},
        {"id": "bollinger",    "name": "布林通道突破",       "params": ["period", "std_mult"]},
        {"id": "pvd",          "name": "價量背離",           "params": ["pvd_period"]},
        {"id": "institutional","name": "籌碼面（外資連買）",  "params": ["consec_buy", "consec_sell"]},
    ]
