"""新聞爬蟲 — 工商時報 + 經濟日報 RSS，Claude API 情緒分析

修復清單：
  [FIX-1] 每篇新聞獨立 try/except，單篇失敗直接跳過不中斷流程
  [FIX-2] API key 未設定時完全跳過情緒分析，預設 neutral / 0.5
  [FIX-3] retry 提升至 3 次，全部失敗回傳預設值繼續
  [FIX-4] scrape_all() 永遠回傳 list，不會 crash
  [FIX-5] 預設情緒分數改為 0.5（中性），0.0 會被誤判為「最負面」
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import json
import re
from datetime import datetime
from typing import Optional
import feedparser
import httpx
from bs4 import BeautifulSoup
from loguru import logger

SOURCES = {
    "工商時報": [
        "https://ctee.com.tw/feed",
        "https://ctee.com.tw/news/stock/feed",
    ],
    "經濟日報": [
        "https://money.udn.com/rssfeed/news/1001/5591?ch=money",
        "https://money.udn.com/rssfeed/news/1001/10846?ch=money",
    ],
}

STOCK_CODE_RE = re.compile(r"[（(](\d{4})[）)]")

# 情緒分析預設值（API key 未設定或失敗時使用）
_DEFAULT_SENTIMENT = "neutral"
_DEFAULT_SCORE     = 0.5   # [FIX-5] 改為 0.5，避免被解讀為極度負面


async def scrape_all() -> list:
    """
    爬取所有來源新聞，儲存至 DB。
    [FIX-4] 永遠回傳 list，任何錯誤都 log 並跳過。
    """
    saved: list[str] = []
    try:
        from backend.models.database import AsyncSessionLocal, settings
        from backend.models.models import NewsArticle
        from sqlalchemy import select

        articles: list[dict] = []
        for source, urls in SOURCES.items():
            for url in urls:
                try:
                    items = await _fetch_rss(url, source)
                    articles.extend(items)
                except Exception as e:
                    logger.warning(f"[Scraper] 跳過 {url}: {e}")

        if not articles:
            logger.warning("[Scraper] 本次無新文章")
            return saved

        api_key = getattr(settings, "anthropic_api_key", "") or ""

        async with AsyncSessionLocal() as db:
            for art in articles:
                try:                                          # [FIX-1] 單篇獨立保護
                    # 去重
                    result = await db.execute(
                        select(NewsArticle).where(NewsArticle.url == art["url"])
                    )
                    if result.scalar_one_or_none():
                        continue

                    sentiment, score = await _analyze_sentiment(
                        art["title"] + "\n" + art.get("content", ""),
                        api_key,
                    )
                    codes = STOCK_CODE_RE.findall(
                        art["title"] + art.get("content", "")
                    )
                    news = NewsArticle(
                        title=art["title"],
                        content=art.get("content", ""),
                        url=art["url"],
                        source=art["source"],
                        published_at=art.get("published_at"),
                        sentiment=sentiment,
                        sentiment_score=score,
                        related_stocks=",".join(set(codes[:5])),
                    )
                    db.add(news)
                    saved.append(art["title"][:40])
                except Exception as e:
                    logger.warning(f"[Scraper] 文章處理失敗（跳過）: {e}")

            await db.commit()

        logger.info(f"[Scraper] 儲存 {len(saved)}/{len(articles)} 篇")

    except Exception as e:
        logger.error(f"[Scraper] scrape_all 頂層錯誤: {e}")

    return saved                                              # [FIX-4] 永遠回傳 list


async def get_recent_news(limit: int = 10) -> list[dict]:
    """取得最近 N 篇新聞（供 /n 指令使用）"""
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import NewsArticle
        from sqlalchemy import select
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(NewsArticle)
                .order_by(NewsArticle.published_at.desc().nullslast(),
                          NewsArticle.id.desc())
                .limit(limit)
            )
            rows = r.scalars().all()
            return [
                {
                    "title":     row.title,
                    "source":    row.source,
                    "sentiment": row.sentiment,
                    "score":     row.sentiment_score,
                    "published": str(row.published_at or "")[:10],
                    "stocks":    row.related_stocks or "",
                }
                for row in rows
            ]
    except Exception as e:
        logger.warning(f"[Scraper] get_recent_news 失敗: {e}")
        return []


async def _fetch_rss(url: str, source: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
            feed = feedparser.parse(resp.text)
        results: list[dict] = []
        for entry in feed.entries[:10]:
            content = _strip_html(entry.get("summary", ""))
            pub     = _parse_date(entry.get("published", ""))
            title   = entry.get("title", "").strip()
            link    = entry.get("link",  "").strip()
            if not title or not link:
                continue
            results.append({
                "title":        title,
                "url":          link,
                "content":      content[:1000],
                "source":       source,
                "published_at": pub,
            })
        return results
    except Exception as e:
        logger.error(f"[Scraper] RSS 抓取失敗 {url}: {e}")
        return []


def _strip_html(html: str) -> str:
    try:
        return BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html).strip()


def _parse_date(date_str: str) -> Optional[datetime]:
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_str).replace(tzinfo=None)
    except Exception:
        try:
            return datetime.strptime(date_str[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None


async def _analyze_sentiment(text: str, api_key: str) -> tuple[str, float]:
    """
    [FIX-2] api_key 未設定 → 立即回傳預設值
    [FIX-3] retry 3 次，全部失敗回傳預設值
    """
    if not api_key or len(text.strip()) < 10:               # [FIX-2]
        return _DEFAULT_SENTIMENT, _DEFAULT_SCORE

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)
    prompt = (
        "以下是台股相關新聞，請判斷情緒。\n"
        "只回傳 JSON，格式：{\"sentiment\": \"positive|negative|neutral\", \"score\": 0.0~1.0}\n\n"
        + text[:800]
    )

    for attempt in range(3):                                  # [FIX-3] 3 次 retry
        try:
            msg = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": prompt}],
            )
            if not msg.content:
                continue
            raw = msg.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
            if not raw:
                continue
            result = json.loads(raw)
            sentiment = result.get("sentiment", _DEFAULT_SENTIMENT)
            score     = float(result.get("score", _DEFAULT_SCORE))
            score     = max(0.0, min(1.0, score))
            return sentiment, score
        except Exception as e:
            logger.warning(f"[Scraper] 情緒分析 attempt {attempt+1}/3 失敗: {e}")
            if attempt < 2:
                await asyncio.sleep(1)

    logger.warning("[Scraper] 情緒分析全部失敗，使用預設值")
    return _DEFAULT_SENTIMENT, _DEFAULT_SCORE                 # [FIX-3] fallback


def format_news_for_line(news_list: list[dict], limit: int = 6) -> str:
    """
    格式化新聞為 LINE 文字訊息。
    [FIX] 無新聞時回傳 '今日暫無新聞'。
    """
    if not news_list:
        return "📰 今日暫無新聞\n\n財經新聞每 30 分鐘自動更新"

    emoji_map = {"positive": "📈", "negative": "📉", "neutral": "📊"}
    lines = [f"📰 最新財經新聞（{len(news_list[:limit])} 則）", "─" * 20]

    for n in news_list[:limit]:
        e      = emoji_map.get(n.get("sentiment", "neutral"), "📊")
        pub    = n.get("published", "")[:10]
        stocks = n.get("stocks", "")
        stock_tag = f"  [{stocks}]" if stocks else ""
        lines.append(f"{e} {n['title'][:38]}{stock_tag}")
        if pub:
            lines.append(f"   {n['source']} · {pub}")

    return "\n".join(lines)


if __name__ == "__main__":
    asyncio.run(scrape_all())
