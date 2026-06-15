"""Breaking News Service — 即時重大財經新聞（Claude評分>0.8才推播）"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from loguru import logger

_cache: dict = {}
_cache_ts: float = 0.0
_TTL = 1800  # 30 min

_pushed_ids: set[str] = set()   # 已推播的新聞 ID，避免重複

# 關鍵字評分規則（不呼叫 Claude API，節省額度）
_HIGH_IMPACT_KEYWORDS = {
    # 重大事件（直接 0.9 分）
    "央行升息": 0.95, "Fed升息": 0.95, "Fed降息": 0.95,
    "升息": 0.90, "降息": 0.90,
    "台積電": 0.85, "輝達": 0.85, "NVIDIA": 0.85,
    "中美貿易": 0.90, "關稅": 0.85,
    "台海": 0.95, "地緣政治": 0.90,
    "財報": 0.82, "EPS大幅": 0.88,
    "盈警": 0.90, "獲利預警": 0.90,
    "重大合約": 0.85, "大訂單": 0.85,
    "倒閉": 0.92, "破產": 0.95,
    "股價崩跌": 0.90, "熔斷": 0.95,
    "外資大買": 0.82, "外資大賣": 0.85,
    "融資斷頭": 0.88, "流動性危機": 0.92,
    "AI晶片": 0.83, "HBM": 0.82, "CoWoS": 0.82,
    "供應鏈斷鏈": 0.88,
    # 中等事件（0.7-0.8 分）
    "法說會": 0.75, "股東會": 0.72,
    "庫藏股": 0.78, "現金增資": 0.75,
    "改選董事": 0.72,
}

_STOCK_KEYWORDS: dict[str, list[str]] = {
    "2330": ["台積電", "TSMC", "台積"],
    "2454": ["聯發科", "MediaTek"],
    "2317": ["鴻海", "Foxconn"],
    "2382": ["廣達", "Quanta"],
    "2308": ["台達電", "Delta"],
    "2881": ["富邦金", "富邦"],
    "2882": ["國泰金", "國泰"],
    "3443": ["創意電子"],
    "6669": ["緯穎"],
    "NVDA": ["NVIDIA", "輝達"],
    "AAPL": ["Apple", "蘋果", "iPhone"],
}


def _score_news(title: str, content: str = "") -> tuple[float, list[str]]:
    """規則式新聞重要性評分（0-1），回傳（分數, 受影響股票列表）"""
    text = (title + " " + content).strip()
    max_score = 0.0

    for kw, score in _HIGH_IMPACT_KEYWORDS.items():
        if kw in text:
            max_score = max(max_score, score)

    # 多個高分關鍵字加分
    hit_count = sum(1 for kw in _HIGH_IMPACT_KEYWORDS if kw in text)
    if hit_count >= 3:
        max_score = min(1.0, max_score + 0.05)

    # 受影響個股
    affected: list[str] = []
    for code, keywords in _STOCK_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            affected.append(code)

    return round(max_score, 3), affected[:5]


async def _fetch_news_sources() -> list[dict]:
    """從多個來源抓取最新財經新聞"""
    import asyncio, httpx
    news: list[dict] = []

    # Yahoo Finance 台股新聞
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/search"
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url,
                            params={"q": "台股 股市", "lang": "zh-TW", "region": "TW",
                                    "quotesCount": 0, "newsCount": 10},
                            headers={"User-Agent": "Mozilla/5.0"})
        items = r.json().get("news", [])
        for item in items:
            news.append({
                "id":        item.get("uuid", ""),
                "title":     item.get("title", ""),
                "content":   item.get("summary", ""),
                "source":    item.get("publisher", "Yahoo Finance"),
                "pub_time":  item.get("providerPublishTime", 0),
                "url":       item.get("link", ""),
            })
    except Exception as e:
        logger.debug(f"[breaking] yahoo news fetch: {e}")

    # Yahoo Finance 全球市場新聞
    for ticker in ["^TWII", "TSM", "NVDA"]:
        try:
            url2 = f"https://query1.finance.yahoo.com/v1/finance/search"
            async with httpx.AsyncClient(timeout=8) as c:
                r2 = await c.get(url2,
                                  params={"q": ticker, "quotesCount": 0, "newsCount": 5},
                                  headers={"User-Agent": "Mozilla/5.0"})
            for item in r2.json().get("news", []):
                if item.get("uuid") not in {n["id"] for n in news}:
                    news.append({
                        "id":       item.get("uuid", ""),
                        "title":    item.get("title", ""),
                        "content":  item.get("summary", ""),
                        "source":   item.get("publisher", "Yahoo"),
                        "pub_time": item.get("providerPublishTime", 0),
                        "url":      item.get("link", ""),
                    })
        except Exception:
            pass

    return news


async def get_breaking_news(force: bool = False) -> dict:
    global _cache, _cache_ts
    now = time.time()
    if not force and _cache and now - _cache_ts < _TTL:
        return _cache

    raw_news = await _fetch_news_sources()

    # 評分和過濾
    scored: list[dict] = []
    cutoff_ts = time.time() - 3 * 3600  # 最近 3 小時新聞

    for n in raw_news:
        if n.get("pub_time", 0) < cutoff_ts:
            continue
        score, affected = _score_news(n["title"], n.get("content", ""))
        if score >= 0.70:
            scored.append({
                **n,
                "score":    score,
                "affected": affected,
                "pub_dt":   datetime.utcfromtimestamp(n["pub_time"]).strftime("%H:%M") if n.get("pub_time") else "--:--",
            })

    scored.sort(key=lambda x: (x["score"], x.get("pub_time", 0)), reverse=True)
    top_news = scored[:8]

    result = {
        "news":         top_news,
        "total":        len(scored),
        "updated_at":   time.strftime("%Y-%m-%d %H:%M"),
        "high_impact":  [n for n in top_news if n["score"] >= 0.85],
    }
    _cache = result
    _cache_ts = now
    return result


async def push_breaking_news() -> int:
    """每 30 分鐘排程呼叫：只推播尚未推播過的高分新聞（≥ 0.8）"""
    import os
    from .line_push import push_line_messages
    admin_uid = os.getenv("ADMIN_LINE_UID", "")
    if not admin_uid:
        return 0

    try:
        data = await get_breaking_news(force=True)
        new_items = [
            n for n in data.get("news", [])
            if n["score"] >= 0.80 and n.get("id") and n["id"] not in _pushed_ids
        ]

        if not new_items:
            return 0

        msgs = []
        for n in new_items[:3]:
            affected_str = "  ".join(n.get("affected", [])[:3])
            affected_part = f"\n影響個股：{affected_str}" if affected_str else ""
            msgs.append(
                f"🚨 重大快訊（評分{n['score']:.2f}）\n"
                f"📰 {n['title']}\n"
                f"來源：{n.get('source','─')}  {n.get('pub_dt','')}"
                f"{affected_part}"
            )
            _pushed_ids.add(n["id"])
            if len(_pushed_ids) > 500:
                oldest = list(_pushed_ids)[:100]
                for oid in oldest:
                    _pushed_ids.discard(oid)

        combined = "\n\n".join(msgs)[:4000]
        ok = await push_line_messages(
            admin_uid,
            [{"type": "text", "text": combined}],
            context="breaking_news.push",
        )
        pushed = len(new_items) if ok else 0
        logger.info(f"[breaking] pushed {pushed} breaking news items")
        return pushed

    except Exception as e:
        logger.error(f"[breaking] push error: {e}")
        return 0


def format_breaking_report(data: dict) -> str:
    news    = data.get("news", [])
    total   = data.get("total", 0)
    updated = data.get("updated_at", "")

    lines = [
        "🚨 即時重大快訊",
        "─" * 32, "",
        f"最近3小時重要新聞：{total} 則",
        f"更新時間：{updated}",
        "",
    ]

    if not news:
        lines.append("✅ 目前無重大市場快訊（評分 ≥ 0.70）")
    else:
        SCORE_ICON = {
            (0.90, 1.01): "🔴",
            (0.80, 0.90): "🟠",
            (0.70, 0.80): "🟡",
        }
        for n in news[:6]:
            score = n.get("score", 0)
            icon = "🔴" if score >= 0.90 else ("🟠" if score >= 0.80 else "🟡")
            title = n.get("title", "")[:60]
            src   = n.get("source", "")[:20]
            pub   = n.get("pub_dt", "")
            aff   = "  ".join(n.get("affected", [])[:3])
            affected_str = f"\n     影響：{aff}" if aff else ""

            lines.append(f"{icon} [{pub}] 評分{score:.2f}")
            lines.append(f"   {title}{affected_str}")
            lines.append("")

    lines += [
        "─" * 28,
        "評分說明：🔴 極重大 / 🟠 重要 / 🟡 值得注意",
        "系統每30分鐘自動掃描，評分≥0.80自動推播",
        "輸入 /breaking 手動刷新 | /news 查看完整新聞",
    ]
    return "\n".join(lines)
