"""US2TW Service — 台股與美股聯動分析（/us2tw）"""
from __future__ import annotations
import time
import asyncio
import math
from datetime import datetime
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 3600  # 1 hour


async def get_us2tw() -> dict:
    key = "us2tw"
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_us2tw()
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_yahoo(symbol: str, interval: str = "1d", range_: str = "2mo") -> dict:
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
        logger.warning(f"[us2tw] Yahoo fetch failed for {symbol}: {e}")
        return {}


def _extract_closes(data: dict, days: int = 30) -> list[float]:
    """Extract last N close prices from Yahoo chart response."""
    try:
        result = data["chart"]["result"][0]
        closes = result["indicators"]["quote"][0].get("close", [])
        closes = [c for c in closes if c is not None]
        return closes[-days:] if len(closes) >= days else closes
    except Exception:
        return []


def _extract_last_price_change(data: dict) -> tuple[float, float]:
    """Return (last_close, change_pct) from Yahoo chart response."""
    try:
        result = data["chart"]["result"][0]
        meta = result.get("meta", {})
        current = meta.get("regularMarketPrice", 0.0)
        prev = meta.get("chartPreviousClose", meta.get("previousClose", current))
        chg = ((current - prev) / prev * 100) if prev else 0.0
        return float(current), float(chg)
    except Exception:
        return 0.0, 0.0


def _pearson_correlation(x: list[float], y: list[float]) -> float:
    """Calculate Pearson correlation coefficient between two lists."""
    n = min(len(x), len(y))
    if n < 5:
        return 0.0
    x = x[-n:]
    y = y[-n:]
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    den_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x))
    den_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y))
    if den_x == 0 or den_y == 0:
        return 0.0
    return round(num / (den_x * den_y), 4)


def _pct_returns(prices: list[float]) -> list[float]:
    """Convert price list to daily percentage return list."""
    if len(prices) < 2:
        return []
    return [(prices[i] - prices[i - 1]) / prices[i - 1] * 100
            for i in range(1, len(prices))]


def _avg_next_day_return(trigger_returns: list[float],
                         next_day_returns: list[float],
                         threshold: float = 2.0) -> tuple[float, int]:
    """
    Find days when trigger moved > threshold%, then average the next-day return.
    Returns (avg_next_day_pct, count_of_events).
    """
    n = min(len(trigger_returns), len(next_day_returns) - 1)
    events = []
    for i in range(n):
        if trigger_returns[i] > threshold:
            if i + 1 < len(next_day_returns):
                events.append(next_day_returns[i + 1])
    if not events:
        return 0.0, 0
    return round(sum(events) / len(events), 2), len(events)


async def _fetch_us2tw() -> dict:
    """Fetch US and TW market data and compute correlations."""
    us_symbols = {
        "sox": "%5ESOX",
        "ndx": "%5ENDX",
        "gspc": "%5EGSPC",
    }
    tw_symbols = {
        "tsmc": "2330.TW",
        "mediatek": "2454.TW",
        "foxconn": "2317.TW",
    }
    all_symbols = {**us_symbols, **tw_symbols}

    tasks = {k: _fetch_yahoo(v, interval="1d", range_="3mo") for k, v in all_symbols.items()}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    raw = {k: (v if not isinstance(v, Exception) else {}) for k, v in zip(tasks.keys(), results)}

    # Last close and change
    sox_price, sox_chg = _extract_last_price_change(raw.get("sox", {}))
    ndx_price, ndx_chg = _extract_last_price_change(raw.get("ndx", {}))
    gspc_price, gspc_chg = _extract_last_price_change(raw.get("gspc", {}))

    # Close price series (last 30 days)
    sox_closes = _extract_closes(raw.get("sox", {}), 30)
    ndx_closes = _extract_closes(raw.get("ndx", {}), 30)
    tsmc_closes = _extract_closes(raw.get("tsmc", {}), 30)
    mtek_closes = _extract_closes(raw.get("mediatek", {}), 30)
    foxconn_closes = _extract_closes(raw.get("foxconn", {}), 30)

    # Semiconductor proxy: avg of TSMC and MediaTek daily returns
    n = min(len(tsmc_closes), len(mtek_closes))
    semi_proxy = [(tsmc_closes[i] + mtek_closes[i]) / 2
                  for i in range(-n, 0)] if n > 0 else []

    # Tech proxy: avg of all three TW stocks
    nt = min(len(tsmc_closes), len(mtek_closes), len(foxconn_closes))
    tech_proxy = [(tsmc_closes[i] + mtek_closes[i] + foxconn_closes[i]) / 3
                  for i in range(-nt, 0)] if nt > 0 else []

    # Correlations (on price returns)
    sox_returns = _pct_returns(sox_closes)
    ndx_returns = _pct_returns(ndx_closes)
    semi_returns = _pct_returns(semi_proxy)
    tech_returns = _pct_returns(tech_proxy)

    sox_semi_corr = _pearson_correlation(sox_returns, semi_returns)
    ndx_tech_corr = _pearson_correlation(ndx_returns, tech_returns)

    # Historical analysis: SOX >2% up days → TSMC next day avg
    tsmc_returns_full = _pct_returns(
        _extract_closes(raw.get("tsmc", {}), 60))
    sox_returns_full = _pct_returns(
        _extract_closes(raw.get("sox", {}), 60))

    sox_large_up_tsmc_next, n_events = _avg_next_day_return(
        sox_returns_full, tsmc_returns_full, threshold=2.0)

    # Today's prediction (rule-based)
    if sox_chg > 2.0:
        prediction = f"台積電等半導體族群今日偏多 🚀（費半昨漲 {sox_chg:+.2f}%）"
        pred_signal = "bullish"
    elif sox_chg > 1.0:
        prediction = f"半導體族群今日小幅偏多（費半 {sox_chg:+.2f}%），量能待觀察"
        pred_signal = "mildly_bullish"
    elif sox_chg < -2.0:
        prediction = f"半導體族群今日偏空 ⚠️（費半昨跌 {sox_chg:.2f}%），留意殺盤"
        pred_signal = "bearish"
    elif sox_chg < -1.0:
        prediction = f"半導體族群今日小幅偏空（費半 {sox_chg:.2f}%），謹慎操作"
        pred_signal = "mildly_bearish"
    else:
        prediction = f"今日半導體族群方向中性（費半 {sox_chg:+.2f}%），跟隨盤面操作"
        pred_signal = "neutral"

    # NDX vs TWSE tech additional note
    ndx_note = ""
    if ndx_chg > 1.5:
        ndx_note = f"那斯達克昨大漲 {ndx_chg:+.2f}%，台灣科技股今日正向效應可期"
    elif ndx_chg < -1.5:
        ndx_note = f"那斯達克昨大跌 {ndx_chg:.2f}%，台灣科技股今日注意承壓"

    return {
        "sox": {"price": round(sox_price, 2), "chg": round(sox_chg, 2)},
        "ndx": {"price": round(ndx_price, 2), "chg": round(ndx_chg, 2)},
        "gspc": {"price": round(gspc_price, 2), "chg": round(gspc_chg, 2)},
        "sox_semi_correlation": sox_semi_corr,
        "ndx_tech_correlation": ndx_tech_corr,
        "sox_large_up_tsmc_next_avg": sox_large_up_tsmc_next,
        "sox_large_up_event_count": n_events,
        "prediction": prediction,
        "pred_signal": pred_signal,
        "ndx_note": ndx_note,
        "fallback": (sox_price == 0.0 and ndx_price == 0.0),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def format_us2tw_report(data: dict) -> str:
    """Format US-TW linkage report as LINE-friendly string."""
    lines = []
    lines.append("【美台股聯動分析】")
    lines.append("─" * 20)

    # US indices
    sox = data.get("sox", {})
    ndx = data.get("ndx", {})
    gspc = data.get("gspc", {})

    def fmt_chg(chg: float) -> str:
        icon = "▲" if chg >= 0 else "▼"
        return f"{icon} {'+' if chg >= 0 else ''}{chg:.2f}%"

    lines.append("🇺🇸 美股昨日收盤")
    lines.append(f"  費城半導體(^SOX):  {sox.get('price', 0):,.1f}  {fmt_chg(sox.get('chg', 0))}")
    lines.append(f"  那斯達克(^NDX):    {ndx.get('price', 0):,.1f}  {fmt_chg(ndx.get('chg', 0))}")
    lines.append(f"  標普500(^GSPC):    {gspc.get('price', 0):,.1f}  {fmt_chg(gspc.get('chg', 0))}")
    lines.append("")

    # Correlations
    sox_corr = data.get("sox_semi_correlation", 0.0)
    ndx_corr = data.get("ndx_tech_correlation", 0.0)

    def corr_desc(r: float) -> str:
        if abs(r) >= 0.8:
            return "高度相關"
        elif abs(r) >= 0.5:
            return "中度相關"
        elif abs(r) >= 0.3:
            return "低度相關"
        return "相關性弱"

    lines.append("📈 聯動相關性（近30日）")
    lines.append(f"  費半 vs 台灣半導體: {sox_corr:+.4f}  ({corr_desc(sox_corr)})")
    lines.append(f"  那斯達克 vs 台灣科技: {ndx_corr:+.4f}  ({corr_desc(ndx_corr)})")
    lines.append("")

    # Historical event study
    n_ev = data.get("sox_large_up_event_count", 0)
    next_avg = data.get("sox_large_up_tsmc_next_avg", 0.0)
    if n_ev > 0:
        lines.append("📊 歷史統計（近60日）")
        lines.append(f"  費半大漲(>2%)後，台積電次日平均表現: {'+' if next_avg >= 0 else ''}{next_avg:.2f}%")
        lines.append(f"  （共 {n_ev} 個樣本）")
        lines.append("")

    # Today's prediction
    lines.append("🤖 AI今日預測")
    lines.append(f"  {data.get('prediction', '')}")
    ndx_note = data.get("ndx_note", "")
    if ndx_note:
        lines.append(f"  {ndx_note}")
    lines.append("")

    if data.get("fallback"):
        lines.append("⚠️ 注意：部分資料來自備援，僅供參考")

    lines.append(f"⏱ 更新: {data.get('updated_at', '')}")

    return "\n".join(lines)
