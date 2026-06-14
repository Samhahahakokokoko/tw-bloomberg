"""Stock Rating Service — 週度股票評級（強力買進/買進/持有/減碼/賣出）"""
from __future__ import annotations

import time
from loguru import logger
import asyncio

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 3600  # 1 hr (評級每小時最多更新一次)

RATINGS = ["強力買進", "買進", "持有", "減碼", "賣出"]
RATING_ICONS = {"強力買進": "🚀", "買進": "✅", "持有": "⚖️", "減碼": "⚠️", "賣出": "🚫"}


async def get_stock_rating(code: str) -> dict:
    now = time.time()
    if code in _cache and now - _cache_ts.get(code, 0) < _TTL:
        return _cache[code]

    result = await _calc_rating(code)
    _cache[code] = result
    _cache_ts[code] = now
    return result


async def update_watchlist_ratings(uid: str) -> list[dict]:
    """更新使用者自選股評級（每週排程呼叫）"""
    import asyncio
    from backend.models.database import AsyncSessionLocal
    try:
        from backend.models.models import Watchlist
        from sqlalchemy import select
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Watchlist.stock_code).where(Watchlist.user_id == uid)
            )
            codes = [r[0] for r in result.fetchall()]
    except Exception as e:
        logger.error(f"[rating] watchlist fetch: {e}")
        codes = []

    tasks = [get_stock_rating(c) for c in codes[:20]]  # 最多 20 支
    ratings = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in ratings if isinstance(r, dict)]


async def _calc_rating(code: str) -> dict:
    import asyncio
    from .twse_service import fetch_realtime_quote, fetch_kline
    from .chip_service import get_chip_data

    quote_task = _safe_quote(code)
    kline_task = _safe_kline(code)
    chip_task  = _safe_chip(code)

    quote, kline, chip = await asyncio.gather(
        quote_task, kline_task, chip_task, return_exceptions=True
    )
    quote = quote if isinstance(quote, dict) else {}
    kline = kline if isinstance(kline, list) else []
    chip  = chip  if isinstance(chip, dict)  else {}

    closes  = [float(k.get("close", 0) or 0) for k in kline if k.get("close")]
    volumes = [float(k.get("volume", 0) or 0) for k in kline if k.get("volume")]

    tech_score  = _technical_score(closes, volumes, quote)
    chip_score  = _chip_score(chip, quote)
    news_score  = _news_sentiment_score(quote)

    composite = tech_score * 0.40 + chip_score * 0.35 + news_score * 0.25
    rating    = _score_to_rating(composite)
    reasons   = _build_reasons(tech_score, chip_score, news_score, closes, quote, chip)

    return {
        "code":       code,
        "name":       quote.get("name", code),
        "rating":     rating,
        "icon":       RATING_ICONS.get(rating, ""),
        "composite":  round(composite, 1),
        "tech_score": round(tech_score, 1),
        "chip_score": round(chip_score, 1),
        "news_score": round(news_score, 1),
        "reasons":    reasons,
        "price":      float(quote.get("close") or quote.get("price") or 0),
        "updated_at": time.strftime("%Y-%m-%d"),
    }


def _technical_score(closes: list[float], volumes: list[float], quote: dict) -> float:
    if len(closes) < 5:
        return 50.0
    score = 50.0

    # RSI
    rsi = _rsi(closes)
    if rsi < 30:   score += 15  # 超賣
    elif rsi < 45: score += 8
    elif rsi < 60: score += 3
    elif rsi < 70: score -= 3
    else:          score -= 12  # 超買

    # 趨勢：均線
    ma5  = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else ma5
    if closes[-1] > ma5 > ma20:  score += 12
    elif closes[-1] > ma5:       score += 5
    elif closes[-1] < ma5 < ma20: score -= 12
    elif closes[-1] < ma5:       score -= 5

    # 近 1 個月漲跌
    if len(closes) >= 20:
        ret = (closes[-1] - closes[-20]) / closes[-20] * 100 if closes[-20] > 0 else 0
        if ret > 10:  score += 5
        elif ret > 3: score += 2
        elif ret < -10: score -= 8
        elif ret < -3:  score -= 3

    return min(100, max(0, score))


def _chip_score(chip: dict, quote: dict) -> float:
    score = 50.0
    foreign = float(chip.get("foreign_net") or quote.get("foreign_buy") or 0)
    invest  = float(chip.get("invest_net") or 0)
    margin  = float(chip.get("margin_increase") or 0)

    if foreign > 5000:   score += 15
    elif foreign > 1000: score += 8
    elif foreign < -5000: score -= 15
    elif foreign < -1000: score -= 8

    if invest > 0:  score += 5
    if invest < 0:  score -= 5

    if margin > 0:  score += 3
    if margin < -2000: score -= 5

    return min(100, max(0, score))


def _news_sentiment_score(quote: dict) -> float:
    # 簡化：用 PE + 漲跌 proxy
    score = 50.0
    pe = float(quote.get("pe_ratio") or quote.get("pe") or 0)
    chg = float(quote.get("change_pct") or 0)
    if 5 < pe < 15:   score += 10
    elif 15 < pe < 25: score += 5
    elif pe > 40:      score -= 10
    if chg > 2:   score += 8
    elif chg < -2: score -= 8
    return min(100, max(0, score))


def _score_to_rating(score: float) -> str:
    if score >= 75: return "強力買進"
    if score >= 60: return "買進"
    if score >= 40: return "持有"
    if score >= 25: return "減碼"
    return "賣出"


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 50.0
    gains = losses = 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff > 0: gains += diff
        else:        losses -= diff
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def _build_reasons(tech: float, chip: float, news: float,
                   closes: list[float], quote: dict, chip_data: dict) -> list[str]:
    reasons = []
    rsi = _rsi(closes)
    if rsi < 30:   reasons.append("RSI 超賣，反彈機率高")
    elif rsi > 70: reasons.append("RSI 超買，注意回調壓力")

    if len(closes) >= 20:
        ma5  = sum(closes[-5:]) / 5
        ma20 = sum(closes[-20:]) / 20
        if closes[-1] > ma5 > ma20:
            reasons.append("均線多頭排列，趨勢向上")
        elif closes[-1] < ma5 < ma20:
            reasons.append("均線空頭排列，趨勢向下")

    foreign = float(chip_data.get("foreign_net") or quote.get("foreign_buy") or 0)
    if foreign > 1000:  reasons.append(f"外資買超 {foreign:,.0f} 張，籌碼偏正")
    if foreign < -1000: reasons.append(f"外資賣超 {abs(foreign):,.0f} 張，籌碼偏負")

    pe = float(quote.get("pe_ratio") or 0)
    if 0 < pe < 15: reasons.append(f"PE {pe:.1f}，估值偏低具吸引力")
    if pe > 35:     reasons.append(f"PE {pe:.1f}，估值偏高注意風險")

    if not reasons:
        reasons.append("各項指標表現中性")
    return reasons[:4]


def format_rating_report(data: dict) -> str:
    if not data:
        return "❌ 無法取得評級資料"

    icon    = data["icon"]
    rating  = data["rating"]
    code    = data["code"]
    name    = data["name"]
    score   = data["composite"]
    tech    = data["tech_score"]
    chip    = data["chip_score"]
    news    = data["news_score"]
    reasons = data["reasons"]
    price   = data["price"]
    ts      = data["updated_at"]

    def _bar(s: float) -> str:
        n = int(s / 10)
        return "█" * n + "░" * (10 - n) + f" {s:.0f}"

    lines = [
        f"{icon} 評級報告  {code} {name}",
        "─" * 32,
        "",
        f"📊 綜合評級：【{rating}】",
        f"   綜合分數：{score:.1f} / 100",
        "",
        "─" * 28,
        "評分明細",
        f"技術面：{_bar(tech)}",
        f"籌碼面：{_bar(chip)}",
        f"情緒面：{_bar(news)}",
        "",
        "─" * 28,
        "主要理由",
    ]
    for r in reasons:
        lines.append(f"• {r}")

    lines += [
        "",
        f"現價：{price:,.1f}",
        f"評級日期：{ts}",
        "",
        "─" * 28,
        "評級說明",
        "🚀強力買進(75+)  ✅買進(60+)",
        "⚖️持有(40+)  ⚠️減碼(25+)  🚫賣出",
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


async def _safe_chip(code: str) -> dict:
    try:
        from .chip_service import get_chip_data
        return await get_chip_data(code) or {}
    except Exception as e:
        return {}
