"""Fund Cost Service — 個股資金成本分析（/fundcost CODE）"""
from __future__ import annotations
import time
import asyncio
from datetime import datetime
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 3600  # 1 hour


async def get_fundcost(code: str) -> dict:
    key = f"fundcost_{code}"
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_fundcost(code)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_yahoo_history(symbol: str, interval: str = "1d", range_: str = "1y") -> dict:
    """Fetch historical chart data from Yahoo Finance v8 API."""
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
        logger.warning(f"[fundcost] Yahoo fetch failed for {symbol}: {e}")
        return {}


def _extract_closes_and_volumes(data: dict) -> tuple[list[float], list[float]]:
    """Extract close prices and volumes from Yahoo chart response."""
    try:
        result = data["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]
        closes = [c for c in quote.get("close", []) if c is not None]
        volumes = [v for v in quote.get("volume", []) if v is not None]
        return closes, volumes
    except Exception as e:
        return [], []


def _weighted_avg_price(prices: list[float], weights: list[float] | None = None) -> float:
    """Calculate weighted average price. If no weights given, use equal weights."""
    if not prices:
        return 0.0
    if weights is None or len(weights) != len(prices):
        return sum(prices) / len(prices)
    total_w = sum(weights)
    if total_w == 0:
        return sum(prices) / len(prices)
    return sum(p * w for p, w in zip(prices, weights)) / total_w


def _calc_rsi(closes: list[float], period: int = 14) -> float:
    """Simple RSI calculation."""
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
    return 100.0 - (100.0 / (1.0 + rs))


async def _fetch_fundcost(code: str) -> dict:
    """Fetch stock data and estimate institutional cost bases."""
    symbol = f"{code}.TW"
    raw = await _fetch_yahoo_history(symbol, interval="1d", range_="1y")

    closes, volumes = _extract_closes_and_volumes(raw)

    # Fallback data if API fails
    if not closes:
        logger.warning(f"[fundcost] No data for {code}, using fallback")
        return {
            "code": code,
            "current_price": 0.0,
            "week52_high": 0.0,
            "week52_low": 0.0,
            "foreign_avg_cost": 0.0,
            "domestic_avg_cost": 0.0,
            "foreign_upper": 0.0,
            "foreign_lower": 0.0,
            "domestic_upper": 0.0,
            "domestic_lower": 0.0,
            "price_vs_foreign": "unknown",
            "price_vs_domestic": "unknown",
            "rsi14": 50.0,
            "support_level": 0.0,
            "resistance_level": 0.0,
            "analysis": "資料暫時無法取得，請稍後再試",
            "fallback": True,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    current_price = closes[-1]
    week52_high = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    week52_low = min(closes[-252:]) if len(closes) >= 252 else min(closes)

    # Foreign institutional (外資) avg cost: volume-weighted avg of last 60 days
    foreign_window = min(60, len(closes))
    foreign_prices = closes[-foreign_window:]
    foreign_vols = volumes[-foreign_window:] if len(volumes) >= foreign_window else volumes
    # Trim volumes to match prices length
    if len(foreign_vols) > len(foreign_prices):
        foreign_vols = foreign_vols[-len(foreign_prices):]
    elif len(foreign_vols) < len(foreign_prices):
        foreign_vols = foreign_vols + [1.0] * (len(foreign_prices) - len(foreign_vols))
    foreign_avg_cost = _weighted_avg_price(foreign_prices, foreign_vols)

    # Domestic institutional (投信) avg cost: volume-weighted avg of last 20 days
    domestic_window = min(20, len(closes))
    domestic_prices = closes[-domestic_window:]
    domestic_vols = volumes[-domestic_window:] if len(volumes) >= domestic_window else volumes
    if len(domestic_vols) > len(domestic_prices):
        domestic_vols = domestic_vols[-len(domestic_prices):]
    elif len(domestic_vols) < len(domestic_prices):
        domestic_vols = domestic_vols + [1.0] * (len(domestic_prices) - len(domestic_vols))
    domestic_avg_cost = _weighted_avg_price(domestic_prices, domestic_vols)

    # Pressure / support zones: cost ±5%
    foreign_upper = foreign_avg_cost * 1.05
    foreign_lower = foreign_avg_cost * 0.95
    domestic_upper = domestic_avg_cost * 1.05
    domestic_lower = domestic_avg_cost * 0.95

    # Price vs cost zones
    if current_price > foreign_upper:
        price_vs_foreign = "above_cost"  # Potential selling pressure from foreign
    elif current_price < foreign_lower:
        price_vs_foreign = "below_cost"  # Potential foreign support
    else:
        price_vs_foreign = "in_zone"

    if current_price > domestic_upper:
        price_vs_domestic = "above_cost"
    elif current_price < domestic_lower:
        price_vs_domestic = "below_cost"
    else:
        price_vs_domestic = "in_zone"

    # RSI for momentum context
    rsi14 = _calc_rsi(closes, 14)

    # Simple support/resistance from 60d high/low
    recent_60 = closes[-60:] if len(closes) >= 60 else closes
    support_level = min(recent_60)
    resistance_level = max(recent_60)

    # Rule-based analysis text
    analysis_parts = []
    if price_vs_foreign == "below_cost":
        analysis_parts.append("股價低於外資估計成本，外資可能形成支撐，留意買盤進場")
    elif price_vs_foreign == "above_cost":
        analysis_parts.append("股價高於外資估計成本，外資帳上獲利，注意潛在賣壓")
    else:
        analysis_parts.append("股價位於外資成本區間內，多空拉鋸，方向待確認")

    if price_vs_domestic == "below_cost":
        analysis_parts.append("投信成本套牢，可能護盤或停損")
    elif price_vs_domestic == "above_cost":
        analysis_parts.append("投信帳上獲利，留意調節賣壓")
    else:
        analysis_parts.append("股價位於投信成本區間，籌碼相對穩定")

    if rsi14 > 70:
        analysis_parts.append("RSI偏高(>{:.0f})，短線注意超買")
    elif rsi14 < 30:
        analysis_parts.append("RSI偏低(<{:.0f})，短線有超賣反彈機會")

    analysis = "；".join(analysis_parts) + "。"

    return {
        "code": code,
        "current_price": round(current_price, 2),
        "week52_high": round(week52_high, 2),
        "week52_low": round(week52_low, 2),
        "foreign_avg_cost": round(foreign_avg_cost, 2),
        "domestic_avg_cost": round(domestic_avg_cost, 2),
        "foreign_upper": round(foreign_upper, 2),
        "foreign_lower": round(foreign_lower, 2),
        "domestic_upper": round(domestic_upper, 2),
        "domestic_lower": round(domestic_lower, 2),
        "price_vs_foreign": price_vs_foreign,
        "price_vs_domestic": price_vs_domestic,
        "rsi14": round(rsi14, 1),
        "support_level": round(support_level, 2),
        "resistance_level": round(resistance_level, 2),
        "analysis": analysis,
        "fallback": False,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def format_fundcost_report(data: dict, code: str = "") -> str:
    """Format fund cost report as LINE-friendly string."""
    c = code or data.get("code", "")
    lines = []
    lines.append(f"【資金成本分析】{c}")
    lines.append("─" * 20)

    cur = data.get("current_price", 0.0)
    h52 = data.get("week52_high", 0.0)
    l52 = data.get("week52_low", 0.0)
    lines.append(f"📌 現價: {cur:.2f}")
    lines.append(f"   52週高: {h52:.2f}  低: {l52:.2f}")
    lines.append("")

    # Foreign cost
    fc = data.get("foreign_avg_cost", 0.0)
    fu = data.get("foreign_upper", 0.0)
    fl = data.get("foreign_lower", 0.0)
    pvf = data.get("price_vs_foreign", "unknown")
    pvf_map = {
        "above_cost": "⬆️ 高於成本（潛在賣壓）",
        "below_cost": "⬇️ 低於成本（潛在支撐）",
        "in_zone": "↔️ 位於成本區間",
        "unknown": "❓ 資料不足",
    }
    lines.append(f"🏦 外資估計成本（近60日均）")
    lines.append(f"   均成本: {fc:.2f}")
    lines.append(f"   壓力區: {fl:.2f} ~ {fu:.2f}")
    lines.append(f"   現價評估: {pvf_map.get(pvf, pvf)}")
    lines.append("")

    # Domestic cost
    dc = data.get("domestic_avg_cost", 0.0)
    du = data.get("domestic_upper", 0.0)
    dl = data.get("domestic_lower", 0.0)
    pvd = data.get("price_vs_domestic", "unknown")
    pvd_map = {
        "above_cost": "⬆️ 高於成本（潛在調節）",
        "below_cost": "⬇️ 低於成本（可能護盤）",
        "in_zone": "↔️ 位於成本區間",
        "unknown": "❓ 資料不足",
    }
    lines.append(f"🏢 投信估計成本（近20日均）")
    lines.append(f"   均成本: {dc:.2f}")
    lines.append(f"   壓力區: {dl:.2f} ~ {du:.2f}")
    lines.append(f"   現價評估: {pvd_map.get(pvd, pvd)}")
    lines.append("")

    # Technical
    rsi = data.get("rsi14", 50.0)
    sup = data.get("support_level", 0.0)
    res = data.get("resistance_level", 0.0)
    lines.append(f"📊 技術指標")
    lines.append(f"   RSI(14): {rsi:.1f}")
    lines.append(f"   近60日支撐: {sup:.2f}")
    lines.append(f"   近60日壓力: {res:.2f}")
    lines.append("")

    # Analysis
    lines.append(f"🤖 AI解析")
    analysis = data.get("analysis", "")
    # Wrap long analysis
    words = analysis.split("；")
    for w in words:
        if w.strip():
            lines.append(f"  • {w.strip().rstrip('。')}")
    lines.append("")

    if data.get("fallback"):
        lines.append("⚠️ 注意：資料來自備援，僅供參考")

    lines.append(f"⏱ 更新: {data.get('updated_at', '')}")

    return "\n".join(lines)
