"""
yfinance_service.py — Yahoo Finance 歷史股價服務

架構分工：
  即時收盤價  →  TWSE OpenAPI STOCK_DAY_ALL  (report_screener._fetch_rt_cache)
  歷史K線    →  Yahoo Finance  [本模組]      (twse_service.fetch_kline 主要來源)
  調整後股價  →  Yahoo Finance  [本模組]      (finmind_service.fetch_adj_price 主要來源)
  基本面     →  FinMind        (data_pipeline / score_updater)

台股 Yahoo Finance 代碼規則：
  上市 (TWSE): 2330 → 2330.TW
  上櫃 (TPEX): 6669 → 6669.TWO  (若 .TW 無資料自動 fallback)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ── 代碼轉換 ──────────────────────────────────────────────────────────────────

def _tw_ticker(code: str) -> str:
    return f"{str(code).strip()}.TW"


def _otc_ticker(code: str) -> str:
    return f"{str(code).strip()}.TWO"


# ── DataFrame → list[dict] ────────────────────────────────────────────────────

def _df_to_records(df) -> list[dict]:
    """
    yf.Ticker.history() DataFrame → [{date, open, high, low, close, volume}]

    欄位對齊 twse_service.fetch_kline 和 finmind_service.fetch_adj_price 的輸出格式。
    auto_adjust=True 時 Close 已還原除權息。
    """
    if df is None or df.empty:
        return []

    out: list[dict] = []
    for ts, row in df.iterrows():
        try:
            d_str = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
            close = float(row.get("Close", 0) or 0)
            if close <= 0:
                continue
            out.append({
                "date":   d_str,
                "open":   float(row.get("Open",   close) or close),
                "high":   float(row.get("High",   close) or close),
                "low":    float(row.get("Low",    close) or close),
                "close":  close,
                "volume": int(row.get("Volume", 0) or 0),
            })
        except (TypeError, ValueError, KeyError):
            continue

    return sorted(out, key=lambda x: x["date"])


# ── 同步下載核心（在 executor 執行，不阻塞 event loop）─────────────────────────

def _sync_fetch(code: str, start: str, end: Optional[str] = None) -> list[dict]:
    """
    用 yfinance.Ticker.history() 下載歷史股價。
    先嘗試上市（.TW），無資料再嘗試上櫃（.TWO）。
    """
    try:
        import yfinance as yf  # noqa: PLC0415
    except ImportError:
        logger.error("[yf] yfinance 未安裝，請執行: pip install 'yfinance>=0.2.40'")
        return []

    # yfinance 1.x 移除了 progress 參數；1.x 和 0.2.x 都支援 auto_adjust
    kw: dict = dict(interval="1d", auto_adjust=True)
    if end:
        kw["end"] = end

    for ticker_str in (_tw_ticker(code), _otc_ticker(code)):
        try:
            t   = yf.Ticker(ticker_str)
            df  = t.history(start=start, **kw)
            rec = _df_to_records(df)
            if rec:
                logger.info("[yf] %s → %d records (start=%s)", ticker_str, len(rec), start)
                return rec
        except Exception as e:
            logger.debug("[yf] %s failed: %s", ticker_str, e)

    logger.warning("[yf] %s：.TW / .TWO 均無資料，嘗試 FinMind fallback (start=%s)", code, start)

    # ── 最終 fallback：FinMind TaiwanStockPrice（上市 + 上櫃均支援）────────────
    try:
        import asyncio as _asyncio
        import httpx as _httpx
        params = {"dataset": "TaiwanStockPrice", "data_id": code, "start_date": start}
        resp = _httpx.get("https://api.finmindtrade.com/api/v4/data",
                          params=params, timeout=20)
        if resp.status_code == 200:
            payload = resp.json()
            if payload.get("status") == 200:
                raw = payload.get("data", [])
                out = []
                for r in raw:
                    try:
                        c = float(r.get("close", 0) or 0)
                        if c <= 0:
                            continue
                        out.append({
                            "date":   r.get("date", ""),
                            "open":   float(r.get("open",   c) or c),
                            "high":   float(r.get("max",    c) or c),
                            "low":    float(r.get("min",    c) or c),
                            "close":  c,
                            "volume": int(float(r.get("Trading_Volume", 0) or 0)),
                        })
                    except (ValueError, TypeError):
                        pass
                if out:
                    logger.info("[yf/finmind] %s → %d records", code, len(out))
                    return sorted(out, key=lambda x: x["date"])
    except Exception as fm_err:
        logger.debug("[yf/finmind] %s fallback failed: %s", code, fm_err)

    return []


# ── 公開 async API ────────────────────────────────────────────────────────────

async def fetch_kline_yf(stock_code: str, months: int = 6) -> list[dict]:
    """
    歷史 K 線 — 取代 twse_service.fetch_kline。

    特點：
    - 一次取 6 個月（原本 TWSE 只取 3 個月）
    - auto_adjust=True 已還原除權息
    - 支援上市 (.TW) / 上櫃 (.TWO) 自動切換

    回傳格式與 twse_service.fetch_kline 相容：
      [{"date": "2026-05-14", "open": 940.0, "high": 945.0,
        "low": 935.0, "close": 942.0, "volume": 23456789}, ...]
    """
    start = (date.today() - timedelta(days=months * 31)).strftime("%Y-%m-%d")
    loop  = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_fetch, stock_code, start)


async def fetch_price_history(
    stock_code: str,
    start_date: str = "",
    days: int = 120,
) -> list[dict]:
    """
    調整後歷史股價 — 取代 finmind_service.fetch_adj_price。

    特點：
    - auto_adjust=True 已精確還原除權息（比 FinMind 免費版更準確）
    - 無 rate limit（FinMind 免費版 30 req/min）
    - 同樣支援 .TW / .TWO 自動切換

    回傳格式與 finmind_service.fetch_adj_price 相容：
      [{"date": "2026-05-14", "open": ..., "high": ...,
        "low": ..., "close": ..., "volume": ...}, ...]
    """
    if not start_date:
        start_date = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_fetch, stock_code, start_date)
