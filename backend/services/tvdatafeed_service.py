"""
tvdatafeed_service.py — TradingView 歷史 K 線服務

資料來源分工（優先順序）：
  1. tvDatafeed  → 台股歷史 OHLCV（日/週/月，無需登入）
  2. yfinance    → 備援（tvDatafeed 失敗時）
  3. TWSE API    → 最終備援

同時提供技術指標計算：
  RSI(14)、MACD(12/26/9)、Bollinger Bands(20/2)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ── tvDatafeed Interval 對應 ────────────────────────────────────────────────

_INTERVAL_MAP = {
    "daily":   "in_daily",
    "weekly":  "in_weekly",
    "monthly": "in_monthly",
    "1d":      "in_daily",
    "1w":      "in_weekly",
    "1M":      "in_monthly",
    "60":      "in_1_hour",
    "15":      "in_15_minute",
}


# ── 技術指標計算 ──────────────────────────────────────────────────────────────

def _calc_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """RSI(14)"""
    delta = closes.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=True, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=True, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).round(2)


def _calc_macd(closes: pd.Series,
               fast: int = 12, slow: int = 26, signal: int = 9
               ) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD線、Signal線、Histogram"""
    ema_fast   = closes.ewm(span=fast,   adjust=False).mean()
    ema_slow   = closes.ewm(span=slow,   adjust=False).mean()
    macd_line  = (ema_fast - ema_slow).round(4)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean().round(4)
    histogram  = (macd_line - signal_line).round(4)
    return macd_line, signal_line, histogram


def _calc_bollinger(closes: pd.Series, period: int = 20, std_mult: float = 2.0
                    ) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands：上軌、中軌（MA20）、下軌"""
    ma    = closes.rolling(period).mean().round(2)
    std   = closes.rolling(period).std().round(4)
    upper = (ma + std_mult * std).round(2)
    lower = (ma - std_mult * std).round(2)
    return upper, ma, lower


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    在 OHLCV DataFrame 加入技術指標欄位：
      rsi14, macd, macd_signal, macd_hist,
      bb_upper, bb_mid, bb_lower, bb_pct_b
    """
    if df is None or df.empty or "close" not in df.columns:
        return df

    closes = df["close"]

    df["rsi14"]      = _calc_rsi(closes)
    macd, sig, hist  = _calc_macd(closes)
    df["macd"]       = macd
    df["macd_signal"]= sig
    df["macd_hist"]  = hist

    bb_upper, bb_mid, bb_lower = _calc_bollinger(closes)
    df["bb_upper"]   = bb_upper
    df["bb_mid"]     = bb_mid
    df["bb_lower"]   = bb_lower
    # %B：0=下軌，1=上軌，>1=超買，<0=超賣
    range_ = (bb_upper - bb_lower).replace(0, np.nan)
    df["bb_pct_b"]   = ((closes - bb_lower) / range_).round(4)

    return df


# ── tvDatafeed 同步下載核心 ───────────────────────────────────────────────────

def _sync_fetch_tv(stock_code: str, interval_str: str, n_bars: int) -> list[dict]:
    """
    tvDatafeed 同步下載（在 executor 執行）。
    台股代碼：2330 → symbol='2330', exchange='TWSE'
    """
    try:
        from tvDatafeed import TvDatafeed, Interval  # noqa: PLC0415

        attr = _INTERVAL_MAP.get(interval_str, "in_daily")
        iv   = getattr(Interval, attr, Interval.in_daily)

        tv = TvDatafeed()
        df = tv.get_hist(stock_code, "TWSE", interval=iv, n_bars=n_bars)

        if df is None or df.empty:
            # 嘗試上櫃 (TPEX)
            df = tv.get_hist(stock_code, "TPEX", interval=iv, n_bars=n_bars)

        if df is None or df.empty:
            logger.warning("[tv] %s: no data returned", stock_code)
            return []

        # 欄位對齊（tvDatafeed columns: open/high/low/close/volume）
        df = df.rename(columns=str.lower)
        out = []
        for ts, row in df.iterrows():
            try:
                c = float(row.get("close", 0) or 0)
                if c <= 0:
                    continue
                d_str = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
                out.append({
                    "date":   d_str,
                    "open":   float(row.get("open",   c) or c),
                    "high":   float(row.get("high",   c) or c),
                    "low":    float(row.get("low",    c) or c),
                    "close":  c,
                    "volume": int(row.get("volume", 0) or 0),
                })
            except (TypeError, ValueError):
                continue

        logger.info("[tv] %s %s → %d records", stock_code, interval_str, len(out))
        return sorted(out, key=lambda x: x["date"])

    except ImportError:
        logger.error("[tv] tvDatafeed 未安裝: pip install git+https://github.com/rongardF/tvdatafeed")
        return []
    except Exception as e:
        logger.warning("[tv] %s fetch error: %s", stock_code, e)
        return []


# ── 公開 async API ────────────────────────────────────────────────────────────

async def fetch_kline_tv(
    stock_code: str,
    interval:   str = "daily",
    n_bars:     int = 120,
) -> list[dict]:
    """
    TradingView 歷史 K 線（非同步）。

    Args:
        stock_code: 台股代碼，例如 "2330"
        interval:   "daily" / "weekly" / "monthly" / "60" / "15"
        n_bars:     要抓幾根 K 線（最多 5000）

    Returns:
        [{date, open, high, low, close, volume}, ...]  與 fetch_kline 格式相容
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _sync_fetch_tv, stock_code, interval, n_bars
    )


async def fetch_kline_with_indicators(
    stock_code: str,
    interval:   str = "daily",
    n_bars:     int = 120,
) -> dict:
    """
    K 線 + 技術指標（非同步）。

    Returns:
        {
          "kline":    [{date, open, high, low, close, volume}, ...],
          "latest":   最新一根的 dict（含所有指標欄位）,
          "rsi14":    最新 RSI,
          "macd":     {"macd", "signal", "hist"},
          "bb":       {"upper", "mid", "lower", "pct_b"},
          "source":   "tvdatafeed"
        }
    """
    records = await fetch_kline_tv(stock_code, interval, n_bars)

    if not records:
        return {"kline": [], "latest": {}, "source": "none"}

    df = pd.DataFrame(records)
    df = df.set_index("date").sort_index()
    df[["open", "high", "low", "close", "volume"]] = \
        df[["open", "high", "low", "close", "volume"]].astype(float)

    df = add_indicators(df)
    df = df.reset_index()

    latest = df.iloc[-1].to_dict()

    return {
        "kline":  records,
        "latest": {k: (round(v, 4) if isinstance(v, float) else v)
                   for k, v in latest.items()},
        "rsi14":  round(float(latest.get("rsi14", 50) or 50), 2),
        "macd":   {
            "macd":   round(float(latest.get("macd",        0) or 0), 4),
            "signal": round(float(latest.get("macd_signal", 0) or 0), 4),
            "hist":   round(float(latest.get("macd_hist",   0) or 0), 4),
        },
        "bb":     {
            "upper":  round(float(latest.get("bb_upper",  0) or 0), 2),
            "mid":    round(float(latest.get("bb_mid",    0) or 0), 2),
            "lower":  round(float(latest.get("bb_lower",  0) or 0), 2),
            "pct_b":  round(float(latest.get("bb_pct_b",  0) or 0), 4),
        },
        "source": "tvdatafeed",
    }
