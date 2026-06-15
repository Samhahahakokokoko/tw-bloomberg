"""Midday Service — 盤中即時解說（10:30 / 13:00 自動推播）"""
from __future__ import annotations
import time
import asyncio
from datetime import datetime
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 1800  # 30 min


async def get_midday_report() -> dict:
    key = "midday"
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_midday()
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_yahoo(symbol: str, interval: str = "1d", range_: str = "2d") -> dict:
    """Fetch chart data from Yahoo Finance v8 API."""
    try:
        import httpx
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"interval": interval, "range": range_}
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning(f"[midday] Yahoo fetch failed for {symbol}: {e}")
        return {}


def _extract_price_change(data: dict) -> tuple[float, float, float]:
    """Return (current_price, change_pct, volume) from Yahoo chart response."""
    try:
        result = data["chart"]["result"][0]
        meta = result["meta"]
        current = meta.get("regularMarketPrice", 0.0)
        prev_close = meta.get("chartPreviousClose", meta.get("previousClose", current))
        volume = meta.get("regularMarketVolume", 0)
        change_pct = ((current - prev_close) / prev_close * 100) if prev_close else 0.0
        return float(current), float(change_pct), float(volume)
    except Exception as e:
        return 0.0, 0.0, 0.0


def _extract_history(data: dict, days: int = 3) -> list[float]:
    """Return last N closing prices from Yahoo chart response."""
    try:
        result = data["chart"]["result"][0]
        closes = result["indicators"]["quote"][0].get("close", [])
        closes = [c for c in closes if c is not None]
        return closes[-days:] if len(closes) >= days else closes
    except Exception as e:
        return []


async def _fetch_midday() -> dict:
    """Fetch all midday market data in parallel."""
    symbols = {
        "twii": "%5ETWII",
        "sox": "%5ESOX",
        "ndx": "%5ENDX",
        "tsmc": "2330.TW",
        "mediatek": "2454.TW",
        "delta": "2308.TW",
        "largan": "3008.TW",
        "fubon": "2882.TW",
        "cathay": "2881.TW",
        "foxconn": "2317.TW",
    }

    tasks = {k: _fetch_yahoo(v, interval="1d", range_="5d") for k, v in symbols.items()}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    raw = {k: (v if not isinstance(v, Exception) else {}) for k, v in zip(tasks.keys(), results)}

    # Market overview
    twii_price, twii_chg, twii_vol = _extract_price_change(raw.get("twii", {}))
    sox_price, sox_chg, sox_vol = _extract_price_change(raw.get("sox", {}))

    # Individual stocks
    stocks = {}
    for name in ["tsmc", "mediatek", "delta", "largan", "fubon", "cathay", "foxconn"]:
        price, chg, vol = _extract_price_change(raw.get(name, {}))
        hist_vols = []
        try:
            r = raw.get(name, {})
            result = r["chart"]["result"][0]
            vols = result["indicators"]["quote"][0].get("volume", [])
            hist_vols = [v for v in vols if v is not None]
        except Exception as e:
            hist_vols = []
        avg_vol = sum(hist_vols[:-1]) / max(len(hist_vols) - 1, 1) if len(hist_vols) > 1 else 1
        vol_ratio = vol / avg_vol if avg_vol > 0 else 1.0
        stocks[name] = {"price": price, "chg": chg, "vol": vol, "vol_ratio": vol_ratio}

    # Sector performance: 3-day returns
    sector_perf = {}
    for sector, names in [
        ("半導體", ["tsmc", "mediatek"]),
        ("金融", ["fubon", "cathay"]),
        ("科技", ["foxconn", "largan"]),
    ]:
        returns = []
        for name in names:
            hist = _extract_history(raw.get(name, {}), days=4)
            if len(hist) >= 2:
                ret = (hist[-1] - hist[0]) / hist[0] * 100 if hist[0] else 0.0
                returns.append(ret)
        sector_perf[sector] = sum(returns) / len(returns) if returns else 0.0

    # Anomaly detection
    anomalies = []
    name_map = {
        "tsmc": "台積電(2330)",
        "mediatek": "聯發科(2454)",
        "delta": "台達電(2308)",
        "largan": "大立光(3008)",
        "fubon": "富邦金(2882)",
        "cathay": "國泰金(2881)",
        "foxconn": "鴻海(2317)",
    }
    for name, info in stocks.items():
        if abs(info["chg"]) > 5 or info["vol_ratio"] > 2.0:
            anomalies.append({
                "name": name_map.get(name, name),
                "chg": info["chg"],
                "vol_ratio": info["vol_ratio"],
            })

    # Rule-based AI direction forecast
    if twii_chg > 0.5 and sox_chg > 1.0:
        forecast = "偏多 — 大盤強勢，費半昨夜大漲，下午盤看多半導體族群"
        forecast_emoji = "📈"
    elif twii_chg < -1.0:
        forecast = "偏空 — 大盤跌幅擴大，建議觀望或避險，留意支撐位"
        forecast_emoji = "📉"
    elif twii_chg > 0.2:
        forecast = "小幅偏多 — 大盤溫和上漲，關注量能是否放大"
        forecast_emoji = "↗️"
    elif twii_chg < -0.3:
        forecast = "小幅偏空 — 大盤微跌，謹慎操作，等待方向確認"
        forecast_emoji = "↘️"
    else:
        forecast = "中性觀望 — 大盤盤整，等待突破方向，控制部位"
        forecast_emoji = "➡️"

    # Strongest / weakest sectors
    sorted_sectors = sorted(sector_perf.items(), key=lambda x: x[1], reverse=True)
    strong_sector = sorted_sectors[0] if sorted_sectors else ("N/A", 0.0)
    weak_sector = sorted_sectors[-1] if sorted_sectors else ("N/A", 0.0)

    return {
        "twii": {"price": twii_price, "chg": twii_chg, "vol": twii_vol},
        "sox": {"price": sox_price, "chg": sox_chg},
        "stocks": stocks,
        "sector_perf": sector_perf,
        "strong_sector": strong_sector,
        "weak_sector": weak_sector,
        "anomalies": anomalies,
        "forecast": forecast,
        "forecast_emoji": forecast_emoji,
        "report_time": datetime.now().strftime("%H:%M"),
        "report_date": datetime.now().strftime("%Y-%m-%d"),
        "fallback": twii_price == 0.0,
    }


def format_midday_report(data: dict) -> str:
    """Format midday report as LINE-friendly string (max ~4500 chars)."""
    lines = []
    date_str = data.get("report_date", "")
    time_str = data.get("report_time", "")

    lines.append(f"【盤中即時解說】{date_str} {time_str}")
    lines.append("─" * 20)

    # Market overview
    twii = data.get("twii", {})
    price = twii.get("price", 0.0)
    chg = twii.get("chg", 0.0)
    chg_icon = "▲" if chg >= 0 else "▼"
    chg_color = "+" if chg >= 0 else ""
    lines.append(f"📊 大盤走勢")
    lines.append(f"  加權指數: {price:,.0f} 點  {chg_icon} {chg_color}{chg:.2f}%")

    sox = data.get("sox", {})
    sox_chg = sox.get("chg", 0.0)
    sox_icon = "▲" if sox_chg >= 0 else "▼"
    lines.append(f"  費城半導體(^SOX): {sox_icon} {'+' if sox_chg >= 0 else ''}{sox_chg:.2f}%（昨收）")
    lines.append("")

    # Sector performance
    sector_perf = data.get("sector_perf", {})
    strong = data.get("strong_sector", ("N/A", 0.0))
    weak = data.get("weak_sector", ("N/A", 0.0))
    lines.append("📈 強勢族群")
    s_icon = "+" if strong[1] >= 0 else ""
    lines.append(f"  {strong[0]}: {s_icon}{strong[1]:.2f}%（近3日）")

    lines.append("📉 弱勢族群")
    w_icon = "+" if weak[1] >= 0 else ""
    lines.append(f"  {weak[0]}: {w_icon}{weak[1]:.2f}%（近3日）")
    lines.append("")

    # All sector breakdown
    lines.append("📋 各族群近3日表現")
    for sector, ret in sorted(sector_perf.items(), key=lambda x: x[1], reverse=True):
        icon = "▲" if ret >= 0 else "▼"
        lines.append(f"  {sector}: {icon} {'+' if ret >= 0 else ''}{ret:.2f}%")
    lines.append("")

    # Anomalies
    anomalies = data.get("anomalies", [])
    if anomalies:
        lines.append("⚠️ 異常個股")
        for a in anomalies[:5]:
            flags = []
            if abs(a["chg"]) > 5:
                flags.append(f"漲跌{'+' if a['chg'] >= 0 else ''}{a['chg']:.1f}%")
            if a["vol_ratio"] > 2.0:
                flags.append(f"量能{a['vol_ratio']:.1f}x均量")
            lines.append(f"  {a['name']}: {' | '.join(flags)}")
        lines.append("")

    # AI forecast
    fe = data.get("forecast_emoji", "➡️")
    fc = data.get("forecast", "中性觀望")
    lines.append(f"{fe} 下午盤方向AI預測")
    lines.append(f"  {fc}")
    lines.append("")

    if data.get("fallback"):
        lines.append("⚠️ 注意：部分資料來自備援，請以實際行情為準")

    lines.append(f"⏱ 報告時間: {time_str}")

    result = "\n".join(lines)
    return result[:4500]


async def push_midday_to_all() -> None:
    """Called by scheduler at 10:30 and 13:00"""
    try:
        from .line_push import push_to_admin
        data = await get_midday_report()
        text = format_midday_report(data)
        await push_to_admin(f"📊 盤中解說\n\n{text[:4000]}")
    except Exception as e:
        logger.error(f"[midday] push failed: {e}")
