"""Market Sentiment Index — composite 0-100 score"""
import time
from loguru import logger

_cache: dict = {}
_cache_ts: float = 0.0
_TTL = 1800  # 30 min


async def get_sentiment_score() -> dict:
    """Composite market sentiment 0-100"""
    global _cache, _cache_ts
    if _cache and time.time() - _cache_ts < _TTL:
        return _cache

    score = 50  # neutral baseline
    factors = {}

    # Factor 1: TAIEX change_pct (weight: 35)
    try:
        from .twse_service import fetch_market_overview
        ov = await fetch_market_overview()
        pct = float(ov.get("change_pct", 0) or 0)
        # +1.5% → +20 points, -1.5% → -20 points, linear clamp
        delta = max(-20, min(20, pct * 13.3))
        score += delta
        factors["taiex"] = f"{pct:+.2f}%"
    except Exception as e:
        logger.debug(f"[sentiment] taiex factor skip: {e}")

    # Factor 2: Institutional net (weight: 30)
    try:
        import httpx
        url = "https://www.twse.com.tw/fund/BFI82U?response=json&type=day"
        async with httpx.AsyncClient(timeout=10) as c:
            data = (await c.get(url)).json()
        rows = data.get("data", [])
        total = next((r for r in rows if "合計" in str(r)), None)
        if total and len(total) >= 3:
            def _n(v):
                return int(str(v).replace(",", "").replace("+", "") or 0)
            foreign = _n(total[2]) if len(total) > 2 else 0
            # Each 1B TWD net buy → +1.5 points (capped ±15)
            delta = max(-15, min(15, foreign / 1e8 * 1.5))
            score += delta
            factors["institutional"] = f"外資{foreign:+,}張"
    except Exception as e:
        logger.debug(f"[sentiment] institutional factor skip: {e}")

    # Factor 3: Margin balance change (weight: 15) — rising margin = overheated
    try:
        import httpx
        url2 = "https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&type=ALL"
        async with httpx.AsyncClient(timeout=10) as c:
            data2 = (await c.get(url2)).json()
        total_chg = sum(
            int(str(r[5]).replace(",", "") or 0)
            for r in data2.get("data", [])
            if len(r) > 5 and str(r[5]).lstrip("-").replace(",", "").isdigit()
        )
        # Margin increasing = overheated bearish, decreasing = deleveraging (mild bullish)
        delta = max(-10, min(5, -total_chg / 5e5))
        score += delta
        factors["margin"] = f"融資增減{total_chg:+,}"
    except Exception as e:
        logger.debug(f"[sentiment] margin factor skip: {e}")

    score = max(0, min(100, round(score)))

    if score >= 80:
        label = "極度樂觀"
        icon = "🔥"
        advice = "注意過熱，適時減碼"
    elif score >= 60:
        label = "偏多"
        icon = "📈"
        advice = "可積極操作"
    elif score >= 40:
        label = "中性"
        icon = "↔️"
        advice = "謹慎觀望"
    elif score >= 20:
        label = "偏空"
        icon = "📉"
        advice = "減少持倉"
    else:
        label = "極度恐慌"
        icon = "🔻"
        advice = "留意反彈機會"

    result = {
        "score": score,
        "label": label,
        "icon": icon,
        "advice": advice,
        "factors": factors,
    }
    _cache = result
    _cache_ts = time.time()
    return result


def format_sentiment(data: dict) -> str:
    score = data["score"]
    bar_filled = round(score / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    lines = [
        f"📊 大盤情緒指數",
        f"{'─'*22}",
        f"{data['icon']} {data['label']} ({score}/100)",
        f"[{bar}]",
        f"",
        f"說明：",
        f"  80-100 極度樂觀，注意過熱",
        f"  60-80  偏多，可積極操作",
        f"  40-60  中性，謹慎觀望",
        f"  20-40  偏空，減少持倉",
        f"  0-20   極度恐慌，留意反彈",
        f"",
        f"建議：{data['advice']}",
    ]
    if data.get("factors"):
        lines.append("")
        lines.append("構成因子：")
        for k, v in data["factors"].items():
            lines.append(f"  · {v}")
    return "\n".join(lines)
