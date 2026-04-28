"""市場盤態偵測模組（可獨立運行）

對整體大盤（TAIEX）偵測多頭/空頭/盤整，並推薦對應策略。
"""
from __future__ import annotations
from .engine import detect_market_regime, recommend_strategy_for_regime
from backend.services.twse_service import fetch_kline, fetch_market_overview
from loguru import logger
import pandas as pd


async def get_market_regime() -> dict:
    """
    抓大盤（加權指數）的近 252 日 K 線，偵測目前盤態。
    TAIEX 代碼在 TWSE API 中用 "Y9999" 或直接用大盤 overview。
    若無法取得 K 線，改用大盤 overview 的漲跌幅做簡單判斷。
    """
    # 先嘗試用台積電 (2330) 的走勢代理市場走勢（暫用，TWSE K線API無直接大盤歷史）
    # TODO: 可換成 FinMind 的 TAIEX 歷史資料
    try:
        kline = await fetch_kline("2330")  # 代理指標
        if len(kline) >= 20:
            df = pd.DataFrame(kline)
            for col in ["close", "open", "high", "low", "volume"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
            regime = detect_market_regime(df)
            regime["recommended_strategy"] = recommend_strategy_for_regime(regime["current"])
            regime["data_source"] = "2330_proxy"
            return regime
    except Exception as e:
        logger.error(f"[Regime] kline error: {e}")

    # Fallback：用當日大盤漲跌
    try:
        ov = await fetch_market_overview()
        pct = ov.get("change_pct", 0)
        if pct > 0.5:
            regime_str = "bull"
        elif pct < -0.5:
            regime_str = "bear"
        else:
            regime_str = "sideways"
        return {
            "current": regime_str,
            "pct": {},
            "ma5": None, "ma20": None, "ma200": None,
            "recommended_strategy": recommend_strategy_for_regime(regime_str),
            "data_source": "daily_change",
        }
    except Exception as e:
        logger.error(f"[Regime] overview fallback error: {e}")
        return {"current": "unknown", "recommended_strategy": "macd", "pct": {}}


REGIME_DESCRIPTION = {
    "bull":     "多頭行情 📈 (均線多頭排列，建議動能策略)",
    "bear":     "空頭行情 📉 (均線空頭排列，建議防禦策略)",
    "sideways": "盤整行情 ↔️ (均線糾結，建議均值回歸策略)",
    "unknown":  "盤態未知（資料不足）",
}

REGIME_STRATEGY_TIPS = {
    "bull":     "momentum（追漲），或 ma_cross 短線操作",
    "bear":     "defensive（低波動），以防禦為主，減少持倉",
    "sideways": "mean_reversion（布林/RSI 超賣買入），波段操作",
}
