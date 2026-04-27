"""新聞爬蟲 — 工商時報 + 經濟日報 RSS，Claude API 情緒分析"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
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


async def scrape_all():
    from backend.models.database import AsyncSessionLocal, settings
    articles = []
    for source, urls in SOURCES.items():
        for url in urls:
            items = await _fetch_rss(url, source)
            articles.extend(items)

    if not articles:
        logger.warning("No articles fetched")
        return

    from backend.models.models import NewsArticle
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        for art in articles:
            result = await db.execute(select(NewsArticle).where(NewsArticle.url == art["url"]))
            if result.scalar_one_or_none():
                continue
            sentiment, score = await _analyze_sentiment(
                art["title"] + "\n" + art.get("content", ""),
                settings.anthropic_api_key,
            )
            codes = STOCK_CODE_RE.findall(art["title"] + art.get("content", ""))
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
        await db.commit()
    logger.info(f"Saved {len(articles)} articles")


async def _fetch_rss(url: str, source: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
            feed = feedparser.parse(resp.text)
        results = []
        for entry in feed.entries[:10]:
            content = _strip_html(entry.get("summary", ""))
            pub = _parse_date(entry.get("published", ""))
            results.append({
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "content": content[:1000],
                "source": source,
                "published_at": pub,
            })
        return results
    except Exception as e:
        logger.error(f"RSS fetch error {url}: {e}")
        return []


def _strip_html(html: str) -> str:
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)


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
    if not api_key or len(text) < 10:
        return "neutral", 0.0
    import anthropic, json, re
    client = anthropic.AsyncAnthropic(api_key=api_key)
    prompt = (
        "以下是台股相關新聞，請判斷情緒。\n"
        "只回傳 JSON，格式：{\"sentiment\": \"positive|negative|neutral\", \"score\": 0.0~1.0}\n\n"
        + text[:800]
    )
    for attempt in range(2):
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
            return result.get("sentiment", "neutral"), float(result.get("score", 0.5))
        except Exception as e:
            logger.error(f"Sentiment analysis error (attempt {attempt + 1}): {e}")
    return "neutral", 0.0


if __name__ == "__main__":
    asyncio.run(scrape_all())
