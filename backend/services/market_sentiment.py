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

    # Factor 2: Institutional net — foreign investor net (BFI82U row[3] = net TWD)
    try:
        import httpx
        url = "https://www.twse.com.tw/fund/BFI82U?response=json&type=day"
        async with httpx.AsyncClient(timeout=10) as c:
            import json as _json
            raw = await c.get(url)
            data = _json.loads(raw.content)
        rows = data.get("data", [])

        def _n(v):
            try: return int(str(v).replace(",", "").replace("+", "") or 0)
            except (ValueError, TypeError): return 0

        # Find foreign row: "外資及陸資(不含自營商)" has both 外資 and 陸資
        # Row "外資自營商" has 外資 but NOT 陸資 — so 陸資 is the discriminator
        foreign_net = None
        for r in rows:
            if len(r) >= 4 and "外資" in str(r[0]) and "陸資" in str(r[0]):
                foreign_net = _n(r[3])
                break
        if foreign_net is None:
            total_row = next((r for r in rows if "合計" in str(r[0])), None)
            if total_row and len(total_row) >= 4:
                foreign_net = _n(total_row[3])

        if foreign_net is not None:
            # Each 1B TWD net buy → +1.5 points (capped ±15)
            delta = max(-15, min(15, foreign_net / 1e9 * 1.5))
            score += delta
            sign = "+" if foreign_net >= 0 else ""
            factors["institutional"] = f"外資{sign}{foreign_net/1e8:.1f}億"
    except Exception as e:
        logger.debug(f"[sentiment] institutional factor skip: {e}")

    # Factor 3: Margin balance change — rising margin = overheated (tables[0].data row 2)
    try:
        import httpx, json as _json2
        url2 = "https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&type=ALL"
        async with httpx.AsyncClient(timeout=10) as c:
            raw2 = await c.get(url2)
        data2 = _json2.loads(raw2.content)
        tables = data2.get("tables", [])
        margin_chg_bil = 0.0
        if tables:
            rows2 = tables[0].get("data", [])
            # Row index 2: 融資金額(仟元) — [label, buy, sell, repay, prev_bal, today_bal]
            for r2 in rows2:
                if len(r2) >= 6:
                    try:
                        prev = int(str(r2[4]).replace(",", "") or 0)
                        today = int(str(r2[5]).replace(",", "") or 0)
                        if prev > 1e6:  # only the 融資金額(仟元) row is large enough
                            margin_chg_bil = (today - prev) / 1e6  # billions NTD
                            break
                    except Exception as e:
                        continue
        # Rising margin (+1B) → -1 sentiment pt (overheating), capped ±8
        delta = max(-8, min(4, -margin_chg_bil))
        score += delta
        sign = "+" if margin_chg_bil >= 0 else ""
        factors["margin"] = f"融資{sign}{margin_chg_bil:.1f}億"
    except Exception as e:
        logger.debug(f"[sentiment] margin factor skip: {e}")

    # Factor 4: Advance/Decline breadth (weight: ±12)
    try:
        from .report_screener import _rt_cache
        prices = _rt_cache.get("prices", {})
        if prices:
            up   = sum(1 for v in prices.values() if float(v.get("change_pct", 0) or 0) > 0)
            down = sum(1 for v in prices.values() if float(v.get("change_pct", 0) or 0) < 0)
            total_bd = up + down
            if total_bd >= 100:
                ratio = up / total_bd
                # 70%+ advancing → +12, 30%- → -12, linear
                delta = max(-12, min(12, (ratio - 0.5) * 24))
                score += delta
                factors["breadth"] = f"漲{up}跌{down}({ratio*100:.0f}%漲)"
    except Exception as e:
        logger.debug(f"[sentiment] breadth factor skip: {e}")

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
    score = data["score"]
    if score >= 70:
        position = "📈 建議倉位：滿倉（90%+）"
    elif score >= 50:
        position = "📊 建議倉位：七成倉（70%）"
    elif score >= 30:
        position = "📉 建議倉位：五成倉（50%）"
    else:
        position = "🛡️ 建議倉位：三成倉或空手（≤30%）"
    lines.append("")
    lines.append(position)
    if data.get("factors"):
        lines.append("")
        lines.append("構成因子：")
        for k, v in data["factors"].items():
            lines.append(f"  · {v}")
    return "\n".join(lines)
