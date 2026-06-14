"""Main Cost Service — 主力成本估算與行為分析"""
from __future__ import annotations

import time
from loguru import logger
import asyncio

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 600  # 10 min


async def get_main_cost(code: str) -> dict:
    now = time.time()
    if code in _cache and now - _cache_ts.get(code, 0) < _TTL:
        return _cache[code]

    result = await _calc_main_cost(code)
    _cache[code] = result
    _cache_ts[code] = now
    return result


async def _calc_main_cost(code: str) -> dict:
    import asyncio
    from .twse_service import fetch_realtime_quote, fetch_kline

    quote_task = _safe_quote(code)
    kline_task = _safe_kline(code)

    quote, kline = await asyncio.gather(quote_task, kline_task, return_exceptions=True)
    quote = quote if isinstance(quote, dict) else {}
    kline = kline if isinstance(kline, list) else []

    current_price = float(quote.get("close") or quote.get("price") or 0)
    name          = quote.get("name", code)

    # 取近 60 日 K 線
    kline60 = kline[-60:] if len(kline) >= 60 else kline

    closes  = [float(k.get("close", 0) or 0) for k in kline60]
    volumes = [float(k.get("volume", 0) or 0) for k in kline60]

    cost_zone_low, cost_zone_high, vwap = _vwap_cost(closes, volumes)
    trend  = _cost_trend(closes, volumes)
    action = _predict_action(current_price, cost_zone_low, cost_zone_high, trend, quote)
    ai_msg = _ai_narrative(code, name, current_price, cost_zone_low, cost_zone_high, action)

    return {
        "code":            code,
        "name":            name,
        "current_price":   current_price,
        "cost_zone_low":   round(cost_zone_low, 1),
        "cost_zone_high":  round(cost_zone_high, 1),
        "vwap_60":         round(vwap, 1),
        "position":        _position_label(current_price, cost_zone_low, cost_zone_high),
        "profit_loss_pct": _pnl_pct(current_price, vwap),
        "trend":           trend,
        "action":          action,
        "ai_analysis":     ai_msg,
        "updated_at":      time.strftime("%Y-%m-%d %H:%M"),
    }


def _vwap_cost(closes: list[float], volumes: list[float]) -> tuple[float, float, float]:
    if not closes or not volumes:
        return 0.0, 0.0, 0.0
    total_vol = sum(volumes) or 1
    vwap = sum(c * v for c, v in zip(closes, volumes)) / total_vol
    std  = (sum(v * (c - vwap) ** 2 for c, v in zip(closes, volumes)) / total_vol) ** 0.5
    return max(0, vwap - std * 0.5), vwap + std * 0.5, vwap


def _cost_trend(closes: list[float], volumes: list[float]) -> str:
    if len(closes) < 10:
        return "無法判斷"
    # 近 10 日 vs 前 10 日均量
    recent_vol = sum(volumes[-10:]) / 10 if len(volumes) >= 10 else 0
    prev_vol   = sum(volumes[-20:-10]) / 10 if len(volumes) >= 20 else recent_vol
    recent_chg = (closes[-1] - closes[-10]) / closes[-10] * 100 if closes[-10] > 0 else 0
    if recent_vol > prev_vol * 1.2 and recent_chg > 0:
        return "量增價漲（主力積極買進）"
    if recent_vol > prev_vol * 1.2 and recent_chg < -1:
        return "量增價跌（主力可能出貨）"
    if recent_vol < prev_vol * 0.8 and recent_chg > 0:
        return "量縮價漲（籌碼鎖定）"
    if recent_vol < prev_vol * 0.8 and recent_chg < 0:
        return "量縮價跌（觀望整理）"
    return "量價平穩（區間整理）"


def _position_label(price: float, low: float, high: float) -> str:
    if price <= 0 or low <= 0:
        return "資料不足"
    if price < low * 0.97:
        return "深度套牢區"
    if price < low:
        return "輕微套牢"
    if price <= high:
        return "成本區附近"
    if price <= high * 1.05:
        return "小幅獲利"
    if price <= high * 1.15:
        return "獲利區間"
    return "豐厚獲利"


def _pnl_pct(price: float, vwap: float) -> float:
    if vwap <= 0:
        return 0.0
    return round((price - vwap) / vwap * 100, 2)


def _predict_action(price: float, low: float, high: float, trend: str, quote: dict) -> str:
    inst = float(quote.get("foreign_buy") or quote.get("inst_net") or 0)
    if "量增價漲" in trend and price > high * 1.05:
        return "主力可能逢高出貨，留意量縮訊號"
    if "量增價漲" in trend and price <= high:
        return "主力積極吸籌，有護盤意圖"
    if "量增價跌" in trend:
        return "主力可能出貨，注意跌破支撐"
    if "量縮價漲" in trend:
        return "籌碼鎖定，主力靜待拉抬時機"
    if price < low * 0.95:
        return "股價遠低於成本，主力面臨護盤壓力"
    if inst > 0:
        return "法人買進中，主力偏多操作"
    if inst < 0:
        return "法人賣出，主力可能跟隨調節"
    return "觀望整理中，等待明確訊號"


def _ai_narrative(code: str, name: str, price: float, low: float, high: float, action: str) -> str:
    pnl = _pnl_pct(price, (low + high) / 2) if low > 0 else 0
    if price < low:
        sentiment = f"目前股價 {price:.0f} 低於主力成本區 {low:.0f}–{high:.0f}，主力帳面虧損 {abs(pnl):.1f}%"
    elif price <= high:
        sentiment = f"目前股價 {price:.0f} 處於主力成本區 {low:.0f}–{high:.0f} 內，主力損益持平"
    else:
        sentiment = f"目前股價 {price:.0f} 高於主力成本區 {low:.0f}–{high:.0f}，主力帳面獲利 {pnl:.1f}%"
    return f"{sentiment}。{action}"


def format_main_cost_report(data: dict) -> str:
    if not data:
        return "❌ 無法取得主力成本資料"

    code  = data["code"]
    name  = data["name"]
    price = data["current_price"]
    low   = data["cost_zone_low"]
    high  = data["cost_zone_high"]
    pos   = data["position"]
    pnl   = data["profit_loss_pct"]
    trend = data["trend"]
    ai    = data["ai_analysis"]
    ts    = data["updated_at"]

    pnl_icon = "📈" if pnl > 0 else ("📉" if pnl < 0 else "➡️")

    # 視覺化位置
    def _price_bar(p: float, l: float, h: float) -> str:
        if l <= 0:
            return "───────────"
        rng = h - l
        if rng <= 0:
            return "───────────"
        rel = (p - l) / rng
        pos_idx = int(rel * 9)
        bar = ["─"] * 11
        bar[5] = "║"  # 中心（成本區）
        bar[0] = "▼"  # 低
        bar[10] = "▲"  # 高
        idx = max(0, min(10, pos_idx + 1))
        bar[idx] = "◆"
        return "".join(bar)

    bar = _price_bar(price, low, high)

    lines = [
        f"💰 主力成本分析  {code} {name}",
        "─" * 32,
        "",
        f"現價：{price:,.1f}",
        f"主力成本區：{low:,.1f} ─ {high:,.1f}",
        f"VWAP60：{data['vwap_60']:,.1f}",
        "",
        f"位置：{pos}",
        f"帳面損益：{pnl_icon} {pnl:+.1f}%",
        "",
        f"低{bar}高",
        f"   {low:,.0f}{'':^8}{high:,.0f}",
        "",
        "─" * 32,
        f"📊 量價趨勢：{trend}",
        "",
        f"🤖 AI 研判：",
        ai,
        "",
        f"更新：{ts}",
    ]
    return "\n".join(lines)


async def _safe_quote(code: str) -> dict:
    try:
        from .twse_service import fetch_realtime_quote
        return await fetch_realtime_quote(code) or {}
    except Exception as e:
        return {}


async def _safe_kline(code: str) -> list:
    try:
        from .twse_service import fetch_kline
        return await fetch_kline(code) or []
    except Exception as e:
        return []
