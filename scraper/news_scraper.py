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

from backend.utils.retry import retry

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


@retry(max_attempts=3, delay=2.0)
async def _raw_rss_fetch(url: str) -> str:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


async def _fetch_rss(url: str, source: str) -> list[dict]:
    try:
        text = await _raw_rss_fetch(url)
        feed = feedparser.parse(text)
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
            if "credit balance is too low" in str(e):
                break
            if attempt < 2:
                await asyncio.sleep(1)

    logger.warning("[Scraper] 情緒分析全部失敗，使用預設值")
    return _DEFAULT_SENTIMENT, _DEFAULT_SCORE                 # [FIX-3] fallback


_BULLISH_KW = frozenset([
    "上修", "買超", "創高", "突破", "法說正面", "看好", "利多", "漲停", "轉機",
    "獲利成長", "上調", "正面", "強勁", "優於預期", "大幅成長", "創新高", "連漲",
])
_BEARISH_KW = frozenset([
    "下修", "賣超", "跌破", "警示", "獲利下滑", "利空", "調降目標", "虧損", "停損",
    "下調", "負面", "疲軟", "低於預期", "大幅衰退", "連跌", "外資賣超", "減資",
])


def analyze_sentiment_local(text: str) -> tuple[str, str]:
    """
    本地關鍵字情緒分析，回傳 (sentiment_label, icon)。
    sentiment_label: bullish / bearish / neutral
    """
    bull = sum(1 for kw in _BULLISH_KW if kw in text)
    bear = sum(1 for kw in _BEARISH_KW if kw in text)
    if bull > bear:
        return "bullish", "🟢"
    if bear > bull:
        return "bearish", "🔴"
    return "neutral", "⚪"


def _relative_time(dt) -> str:
    """將 datetime 轉成「X小時前」形式"""
    if not dt:
        return ""
    try:
        now   = datetime.utcnow()
        delta = now - dt
        mins  = int(delta.total_seconds() / 60)
        if mins < 60:
            return f"{max(mins, 1)}分鐘前"
        hours = mins // 60
        if hours < 24:
            return f"{hours}小時前"
        days = hours // 24
        return f"{days}天前"
    except Exception:
        return ""


async def get_stock_news(code: str, stock_name: str, limit: int = 5) -> list[dict]:
    """搜尋包含股票代碼或名稱的新聞"""
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import NewsArticle
        from sqlalchemy import select, or_

        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(NewsArticle)
                .where(
                    or_(
                        NewsArticle.related_stocks.contains(code),
                        NewsArticle.title.contains(code),
                        NewsArticle.title.contains(stock_name),
                        NewsArticle.content.contains(stock_name),
                    )
                )
                .order_by(
                    NewsArticle.published_at.desc().nullslast(),
                    NewsArticle.id.desc(),
                )
                .limit(limit)
            )
            rows = r.scalars().all()
            return [
                {
                    "title":      row.title,
                    "source":     row.source,
                    "sentiment":  row.sentiment,
                    "score":      row.sentiment_score,
                    "published":  row.published_at,
                    "stocks":     row.related_stocks or "",
                }
                for row in rows
            ]
    except Exception as e:
        logger.warning(f"[Scraper] get_stock_news 失敗: {e}")
        return []


def format_stock_news_for_line(code: str, name: str, news_list: list[dict]) -> str:
    """格式化個股新聞為 LINE 文字"""
    header = f"📰 {code} {name} 相關新聞"
    sep    = "─" * 18

    if not news_list:
        return (
            f"{header}\n{sep}\n"
            "目前無相關新聞\n\n"
            "新聞每 30 分鐘自動更新"
        )

    lines = [header, sep]
    for n in news_list:
        title = n["title"]
        # 本地情緒分析（覆寫 DB 儲存的 positive/negative）
        sentiment_db = n.get("sentiment", "neutral")
        if sentiment_db == "positive":
            icon = "🟢"
        elif sentiment_db == "negative":
            icon = "🔴"
        else:
            _, icon = analyze_sentiment_local(title)

        source  = n.get("source", "")
        rel_time = _relative_time(n.get("published"))
        src_line = " · ".join(filter(None, [source, rel_time]))

        lines.append(f"{icon} {title[:44]}")
        if src_line:
            lines.append(f"   {src_line}")

    return "\n".join(lines)


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
