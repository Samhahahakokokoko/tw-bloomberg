"""Tech Rating Service — 個股技術評級（/techrating CODE）+ 每日自選股掃描"""
from __future__ import annotations
import time
import asyncio
import math
from datetime import datetime
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 1800  # 30 min

# Persist previous ratings for change detection across calls
_last_ratings: dict = {}

RATING_LABELS = {
    "突破": "突破",
    "強勢整理": "強勢整理",
    "回檔測試": "回檔測試",
    "中性": "中性",
    "跌破": "跌破",
    "弱勢": "弱勢",
}

RATING_EMOJI = {
    "突破": "🚀",
    "強勢整理": "💪",
    "回檔測試": "🔍",
    "中性": "➡️",
    "跌破": "⚠️",
    "弱勢": "📉",
}


async def get_techrating(code: str) -> dict:
    key = f"techrating_{code}"
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_techrating(code)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_yahoo(symbol: str, interval: str = "1d", range_: str = "3mo") -> dict:
    """Fetch chart data from Yahoo Finance v8 API."""
    try:
        import httpx
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"interval": interval, "range": range_}
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning(f"[techrating] Yahoo fetch failed for {symbol}: {e}")
        return {}


def _extract_ohlcv(data: dict) -> tuple[list, list, list, list, list]:
    """Extract open, high, low, close, volume lists from Yahoo chart response."""
    try:
        result = data["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]
        opens = [v for v in quote.get("open", []) if v is not None]
        highs = [v for v in quote.get("high", []) if v is not None]
        lows = [v for v in quote.get("low", []) if v is not None]
        closes = [v for v in quote.get("close", []) if v is not None]
        volumes = [v for v in quote.get("volume", []) if v is not None]
        return opens, highs, lows, closes, volumes
    except Exception as e:
        return [], [], [], [], []


def _sma(prices: list[float], period: int) -> float:
    """Simple moving average of last `period` prices."""
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    return sum(prices[-period:]) / period


def _ema(prices: list[float], period: int) -> list[float]:
    """Exponential moving average series."""
    if not prices:
        return []
    k = 2.0 / (period + 1)
    ema_vals = [prices[0]]
    for p in prices[1:]:
        ema_vals.append(p * k + ema_vals[-1] * (1 - k))
    return ema_vals


def _calc_rsi(closes: list[float], period: int = 14) -> float:
    """Calculate RSI(period)."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def _calc_macd(closes: list[float],
               fast: int = 12, slow: int = 26, signal: int = 9
               ) -> tuple[float, float, float]:
    """
    Calculate MACD line, signal line, and histogram.
    Returns (macd_line, signal_line, histogram).
    """
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    n = min(len(ema_fast), len(ema_slow))
    macd_line_series = [ema_fast[i] - ema_slow[i] for i in range(-n, 0)]
    signal_series = _ema(macd_line_series, signal)
    macd_val = macd_line_series[-1] if macd_line_series else 0.0
    signal_val = signal_series[-1] if signal_series else 0.0
    histogram = macd_val - signal_val
    return round(macd_val, 4), round(signal_val, 4), round(histogram, 4)


def _determine_rating(closes: list[float], volumes: list[float],
                       ma5: float, ma20: float, ma60: float,
                       rsi: float) -> str:
    """Apply rating rules based on indicators."""
    if not closes:
        return "中性"

    cur = closes[-1]

    # 5-day average volume vs 20-day average volume
    avg_vol_5 = sum(volumes[-5:]) / max(len(volumes[-5:]), 1) if volumes else 0
    avg_vol_20 = sum(volumes[-20:]) / max(len(volumes[-20:]), 1) if volumes else 1
    vol_ratio = avg_vol_5 / avg_vol_20 if avg_vol_20 > 0 else 1.0

    # 5-day price range (%)
    recent_5 = closes[-5:]
    price_range_pct = ((max(recent_5) - min(recent_5)) / min(recent_5) * 100
                       if min(recent_5) > 0 else 0.0)

    # Check if price crossed below 20MA recently (in last 3 days)
    crossed_below_20ma = False
    if len(closes) >= 4 and ma20 > 0:
        for i in range(-4, -1):
            if (i - 1) >= -len(closes) and closes[i - 1] >= ma20 > closes[i]:
                crossed_below_20ma = True
                break

    # Rating rules
    if cur > ma20 and rsi > 60 and vol_ratio > 1.5:
        return "突破"
    elif cur > ma20 and 50 <= rsi <= 70 and price_range_pct < 3.0:
        return "強勢整理"
    elif crossed_below_20ma and rsi > 45:
        return "回檔測試"
    elif cur < ma20 and cur < ma60 and rsi < 45:
        return "跌破"
    elif cur < ma60 and rsi < 40 and vol_ratio < 0.8:
        return "弱勢"
    else:
        return "中性"


async def _fetch_techrating(code: str) -> dict:
    """Fetch 60-day price data and compute technical rating."""
    symbol = f"{code}.TW"
    raw = await _fetch_yahoo(symbol, interval="1d", range_="3mo")

    opens, highs, lows, closes, volumes = _extract_ohlcv(raw)

    # Fallback if no data
    if not closes:
        logger.warning(f"[techrating] No data for {code}, using fallback")
        prev_rating = _last_ratings.get(code, "中性")
        return {
            "code": code,
            "current_price": 0.0,
            "ma5": 0.0,
            "ma20": 0.0,
            "ma60": 0.0,
            "rsi14": 50.0,
            "macd": 0.0,
            "macd_signal": 0.0,
            "macd_hist": 0.0,
            "vol_ratio_5_20": 1.0,
            "price_range_5d_pct": 0.0,
            "rating": "中性",
            "prev_rating": prev_rating,
            "rating_changed": False,
            "fallback": True,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    # Keep last 60 days
    closes = closes[-60:]
    volumes = volumes[-60:] if len(volumes) >= 60 else volumes

    current_price = closes[-1]

    # Moving averages
    ma5 = _sma(closes, 5)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)

    # RSI
    rsi14 = _calc_rsi(closes, 14)

    # MACD
    macd_val, macd_signal, macd_hist = _calc_macd(closes, 12, 26, 9)

    # Volume ratio
    avg_vol_5 = sum(volumes[-5:]) / max(len(volumes[-5:]), 1) if volumes else 0
    avg_vol_20 = sum(volumes[-20:]) / max(len(volumes[-20:]), 1) if volumes else 1
    vol_ratio = round(avg_vol_5 / avg_vol_20, 2) if avg_vol_20 > 0 else 1.0

    # 5-day price range
    recent_5 = closes[-5:]
    price_range_pct = round((max(recent_5) - min(recent_5)) / min(recent_5) * 100, 2) if min(recent_5) > 0 else 0.0

    # Determine rating
    rating = _determine_rating(closes, volumes, ma5, ma20, ma60, rsi14)

    # Change detection
    prev_rating = _last_ratings.get(code, rating)
    rating_changed = (rating != prev_rating)
    _last_ratings[code] = rating

    return {
        "code": code,
        "current_price": round(current_price, 2),
        "ma5": round(ma5, 2),
        "ma20": round(ma20, 2),
        "ma60": round(ma60, 2),
        "rsi14": rsi14,
        "macd": macd_val,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "vol_ratio_5_20": vol_ratio,
        "price_range_5d_pct": price_range_pct,
        "rating": rating,
        "prev_rating": prev_rating,
        "rating_changed": rating_changed,
        "fallback": False,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def format_techrating_report(data: dict, code: str = "") -> str:
    """Format technical rating report as LINE-friendly string."""
    c = code or data.get("code", "")
    rating = data.get("rating", "中性")
    prev_rating = data.get("prev_rating", "中性")
    rating_changed = data.get("rating_changed", False)
    emoji = RATING_EMOJI.get(rating, "➡️")

    lines = []
    lines.append(f"【技術評級分析】{c}")
    lines.append("─" * 20)

    # Rating headline
    change_note = ""
    if rating_changed:
        prev_emoji = RATING_EMOJI.get(prev_rating, "➡️")
        change_note = f"  （前次: {prev_emoji} {prev_rating} → 變動！）"
    lines.append(f"{emoji} 評級: {rating}{change_note}")
    lines.append("")

    # Price vs MAs
    cur = data.get("current_price", 0.0)
    ma5 = data.get("ma5", 0.0)
    ma20 = data.get("ma20", 0.0)
    ma60 = data.get("ma60", 0.0)

    def vs_ma(price: float, ma: float) -> str:
        if ma == 0:
            return "N/A"
        diff = (price - ma) / ma * 100
        icon = "▲" if diff >= 0 else "▼"
        return f"{icon}{abs(diff):.1f}%"

    lines.append(f"📌 現價: {cur:.2f}")
    lines.append(f"📊 均線比較")
    lines.append(f"   MA5:  {ma5:.2f}  現價 {vs_ma(cur, ma5)}")
    lines.append(f"   MA20: {ma20:.2f}  現價 {vs_ma(cur, ma20)}")
    lines.append(f"   MA60: {ma60:.2f}  現價 {vs_ma(cur, ma60)}")
    lines.append("")

    # RSI
    rsi = data.get("rsi14", 50.0)
    rsi_note = ""
    if rsi >= 70:
        rsi_note = " ⚠️ 超買"
    elif rsi <= 30:
        rsi_note = " ⚠️ 超賣"
    elif rsi >= 60:
        rsi_note = " 偏強"
    elif rsi <= 40:
        rsi_note = " 偏弱"
    lines.append(f"📈 RSI(14): {rsi:.1f}{rsi_note}")

    # MACD
    macd = data.get("macd", 0.0)
    macd_sig = data.get("macd_signal", 0.0)
    macd_hist = data.get("macd_hist", 0.0)
    macd_cross = ""
    if macd > macd_sig and macd_hist > 0:
        macd_cross = " 📈 多頭排列"
    elif macd < macd_sig and macd_hist < 0:
        macd_cross = " 📉 空頭排列"
    lines.append(f"📉 MACD: {macd:.4f}  Signal: {macd_sig:.4f}  Hist: {macd_hist:.4f}{macd_cross}")
    lines.append("")

    # Volume
    vol_ratio = data.get("vol_ratio_5_20", 1.0)
    vol_note = ""
    if vol_ratio > 1.5:
        vol_note = " 🔥 量能放大"
    elif vol_ratio < 0.7:
        vol_note = " 💤 量能萎縮"
    lines.append(f"📦 量比(5日/20日): {vol_ratio:.2f}x{vol_note}")

    # 5-day price range
    prange = data.get("price_range_5d_pct", 0.0)
    lines.append(f"📐 近5日波動幅度: {prange:.2f}%")
    lines.append("")

    # Rating interpretation
    interpretations = {
        "突破": "價格站上均線且量能放大，RSI偏強 — 可考慮追買，設止損於MA20",
        "強勢整理": "均線之上盤整，籌碼穩定 — 持股不動，等待再突破",
        "回檔測試": "近期跌破MA20但RSI未弱 — 觀察是否守穩，守穩可分批承接",
        "中性": "多空指標混雜 — 暫時觀望，等待方向明朗",
        "跌破": "均線全面失守且RSI偏弱 — 控制部位，注意下方支撐",
        "弱勢": "量縮價跌，均線空頭排列 — 避免追空，等反彈確認後再評估",
    }
    interp = interpretations.get(rating, "")
    if interp:
        lines.append(f"🤖 操作建議")
        lines.append(f"  {interp}")
        lines.append("")

    if data.get("fallback"):
        lines.append("⚠️ 注意：資料來自備援，僅供參考")

    lines.append(f"⏱ 更新: {data.get('updated_at', '')}")

    return "\n".join(lines)


async def update_watchlist_ratings() -> dict:
    """
    Called by scheduler daily at 15:30 after market close.
    Rate each watchlist stock, push notification if rating changed.
    """
    results = {}
    changed_stocks = []

    try:
        from .stock_favorites import get_all_user_favorites
        all_favorites = await get_all_user_favorites()
    except Exception as e:
        logger.warning(f"[techrating] Failed to get favorites: {e}")
        all_favorites = {}

    # Collect unique stock codes across all users
    all_codes: set[str] = set()
    for codes in all_favorites.values():
        if isinstance(codes, (list, set)):
            all_codes.update(str(c) for c in codes)

    if not all_codes:
        logger.info("[techrating] No watchlist stocks to rate")
        return {"rated": 0, "changed": 0}

    # Rate all stocks (in batches of 5 to avoid rate limiting)
    codes_list = list(all_codes)
    batch_size = 5
    for i in range(0, len(codes_list), batch_size):
        batch = codes_list[i:i + batch_size]
        tasks = [_fetch_techrating(code) for code in batch]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)
        for code, res in zip(batch, batch_results):
            if isinstance(res, Exception):
                logger.warning(f"[techrating] Failed to rate {code}: {res}")
                continue
            results[code] = res
            if res.get("rating_changed") and not res.get("fallback"):
                changed_stocks.append(res)

    # Push notifications for changed ratings
    if changed_stocks:
        try:
            from .line_push import push_to_admin
            lines = ["📊 技術評級變動通知"]
            lines.append("─" * 18)
            for stock in changed_stocks[:10]:  # Max 10 alerts
                c = stock["code"]
                old_r = stock.get("prev_rating", "N/A")
                new_r = stock.get("rating", "N/A")
                em = RATING_EMOJI.get(new_r, "➡️")
                lines.append(f"{em} {c}: {old_r} → {new_r}")
                lines.append(f"   現價: {stock.get('current_price', 0):.2f}  RSI: {stock.get('rsi14', 0):.1f}")
            msg = "\n".join(lines)
            await push_to_admin(msg[:4000])
        except Exception as e:
            logger.error(f"[techrating] Push notification failed: {e}")

    logger.info(f"[techrating] Rated {len(results)} stocks, {len(changed_stocks)} changed")
    return {
        "rated": len(results),
        "changed": len(changed_stocks),
        "results": {k: v.get("rating") for k, v in results.items()},
    }
