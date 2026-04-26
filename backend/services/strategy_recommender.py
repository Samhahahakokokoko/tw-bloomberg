"""策略推薦引擎 — 根據持股特性自動選策略並執行回測"""
from __future__ import annotations
import asyncio
import pandas as pd
import numpy as np
from loguru import logger
from .twse_service import fetch_kline


# ── 策略分類門檻 ──────────────────────────────────────────────────────────────
VOLATILITY_HIGH   = 0.30   # 年化波動率 > 30% → RSI
TREND_STRONG      = 0.08   # 60日報酬率 > 8%  → MACD
LARGE_CAP_PRICE   = 500    # 均價 > 500        → 布林通道
INST_VOL_RATIO    = 1.5    # 近5日均量/總均量  → 籌碼面


async def recommend_for_portfolio(holdings: list[dict]) -> list[dict]:
    """對每個持股平行執行推薦分析"""
    tasks = [_recommend_one(h) for h in holdings]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


async def _recommend_one(holding: dict) -> dict:
    code = holding["stock_code"]
    try:
        kline = await fetch_kline(code)
        if len(kline) < 20:
            return _fallback_rec(holding)

        df = pd.DataFrame(kline)
        for col in ["close", "open", "high", "low", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])

        strategy, reason, params = _classify_strategy(df, holding)
        backtest_result = await _run_mini_backtest(code, df, strategy, params)

        return {
            "stock_code":  code,
            "stock_name":  holding.get("stock_name", ""),
            "strategy":    strategy,
            "reason":      reason,
            "params":      params,
            "metrics": {
                "volatility":    round(float(_volatility(df)), 4),
                "trend_60d":     round(float(_trend(df)), 4),
                "volume_ratio":  round(float(_vol_ratio(df)), 2),
                "avg_price":     round(float(df["close"].mean()), 1),
            },
            "backtest":    backtest_result,
            "holding":     holding,
        }
    except Exception as e:
        logger.error(f"Recommend error {code}: {e}")
        return _fallback_rec(holding)


def _classify_strategy(df: pd.DataFrame, holding: dict) -> tuple[str, str, dict]:
    vol     = _volatility(df)
    trend   = _trend(df)
    vr      = _vol_ratio(df)
    avg_px  = float(df["close"].mean())

    if vol > VOLATILITY_HIGH:
        return (
            "rsi",
            f"波動率 {vol*100:.0f}% 偏高，RSI 策略適合捕捉超買超賣反轉點",
            {"period": 14, "overbought": 70, "oversold": 30},
        )
    if avg_px > LARGE_CAP_PRICE:
        return (
            "bollinger",
            f"均價 {avg_px:.0f} 元屬大型權值股，布林通道策略適合區間操作",
            {"period": 20, "std_mult": 2.0},
        )
    if abs(trend) > TREND_STRONG:
        direction = "上漲" if trend > 0 else "下跌"
        return (
            "macd",
            f"近期趨勢明顯（60日 {trend*100:+.1f}%）{direction}，MACD 追蹤趨勢",
            {"fast": 12, "slow": 26, "signal": 9},
        )
    if vr > INST_VOL_RATIO:
        return (
            "institutional",
            f"近日成交量為均量 {vr:.1f}×，量能異常，籌碼面策略追蹤主力動向",
            {"consec_buy": 2, "consec_sell": 2},
        )
    return (
        "macd",
        "個股特性均衡，MACD 策略作為基本趨勢追蹤",
        {"fast": 12, "slow": 26, "signal": 9},
    )


async def _run_mini_backtest(code: str, df: pd.DataFrame, strategy: str, params: dict) -> dict:
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from backtest.engine import BacktestEngine
        engine = BacktestEngine(df.copy(), initial_capital=1_000_000)
        result = engine.run(strategy, **params)
        return {
            "total_return":    result.total_return,
            "annualized_return": result.annualized_return,
            "max_drawdown":    result.max_drawdown,
            "sharpe_ratio":    result.sharpe_ratio,
            "win_rate":        result.win_rate,
            "total_trades":    result.total_trades,
            "start_date":      result.start_date,
            "end_date":        result.end_date,
        }
    except Exception as e:
        logger.error(f"Mini backtest error {code}: {e}")
        return {"total_return": 0, "win_rate": 0, "max_drawdown": 0,
                "sharpe_ratio": 0, "total_trades": 0}


def _fallback_rec(holding: dict) -> dict:
    return {
        "stock_code": holding["stock_code"],
        "stock_name": holding.get("stock_name", ""),
        "strategy":   "macd",
        "reason":     "資料不足，建議使用 MACD 基本策略觀察",
        "params":     {"fast": 12, "slow": 26, "signal": 9},
        "metrics":    {},
        "backtest":   {"total_return": 0, "win_rate": 0, "max_drawdown": 0,
                       "sharpe_ratio": 0, "total_trades": 0},
        "holding":    holding,
    }


def _volatility(df: pd.DataFrame) -> float:
    returns = df["close"].pct_change().dropna()
    return float(returns.std() * np.sqrt(252)) if len(returns) > 1 else 0.0


def _trend(df: pd.DataFrame) -> float:
    if len(df) < 2:
        return 0.0
    return float((df["close"].iloc[-1] / df["close"].iloc[0]) - 1)


def _vol_ratio(df: pd.DataFrame) -> float:
    if len(df) < 10 or df["volume"].sum() == 0:
        return 1.0
    avg_all  = df["volume"].mean()
    avg_last = df["volume"].iloc[-5:].mean()
    return float(avg_last / avg_all) if avg_all else 1.0
