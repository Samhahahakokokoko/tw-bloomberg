"""Enhanced News Service — 多源新聞聚合 + Claude 情緒評分"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 300  # 5 min


async def get_stock_news_enhanced(code: str) -> dict:
    now = time.time()
    if code in _cache and now - _cache_ts.get(code, 0) < _TTL:
        return _cache[code]

    result = await _aggregate_news(code)
    _cache[code] = result
    _cache_ts[code] = now
    return result


async def _aggregate_news(code: str) -> dict:
    sources = await asyncio.gather(
        _fetch_yahoo_news(code),
        _fetch_cnyes_news(code),
        _fetch_ctee_news(code),
        return_exceptions=True,
    )

    all_news = []
    for src in sources:
        if isinstance(src, list):
            all_news.extend(src)

    deduped = _deduplicate(all_news)
    scored  = await _score_sentiment(code, deduped)

    return {
        "code":   code,
        "news":   scored,
        "count":  len(scored),
        "ts":     time.strftime("%H:%M"),
    }


async def _fetch_yahoo_news(code: str) -> list[dict]:
    try:
        import httpx
        url = f"https://tw.stock.yahoo.com/quote/{code}.TW/news"
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=10, headers=headers, follow_redirects=True) as c:
            r = await c.get(url)
            if r.status_code != 200:
                return []

        import re
        titles = re.findall(r'"title"\s*:\s*"([^"]{10,120})"', r.text)
        results = []
        for t in titles[:5]:
            if _is_stock_related(t, code):
                results.append({"title": t, "source": "Yahoo奇摩"})
        return results
    except Exception as e:
        logger.debug(f"[news] yahoo {code}: {e}")
        return []


async def _fetch_cnyes_news(code: str) -> list[dict]:
    try:
        import httpx
        url = f"https://news.cnyes.com/news/cat/tw_stock_news?limit=10&keyword={code}"
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(url)
            if r.status_code != 200:
                return []

        import re, json
        # Try to extract titles from JSON data in page
        matches = re.findall(r'"title"\s*:\s*"([^"]{10,120})"', r.text)
        results = []
        for t in matches[:5]:
            if _is_stock_related(t, code):
                results.append({"title": t, "source": "鉅亨網"})
        return results
    except Exception as e:
        logger.debug(f"[news] cnyes {code}: {e}")
        return []


async def _fetch_ctee_news(code: str) -> list[dict]:
    try:
        import httpx
        url = f"https://ctee.com.tw/?s={code}"
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=10, headers=headers, follow_redirects=True) as c:
            r = await c.get(url)
            if r.status_code != 200:
                return []

        import re
        titles = re.findall(r'<h2[^>]*class="[^"]*entry-title[^"]*"[^>]*>\s*<a[^>]*>([^<]{10,120})</a>', r.text)
        if not titles:
            titles = re.findall(r'<title>([^<]{10,80})</title>', r.text)

        results = []
        for t in titles[:3]:
            clean = t.strip()
            if clean and _is_stock_related(clean, code):
                results.append({"title": clean, "source": "工商時報"})
        return results
    except Exception as e:
        logger.debug(f"[news] ctee {code}: {e}")
        return []


def _is_stock_related(title: str, code: str) -> bool:
    bad = ["廣告", "cookie", "隱私", "會員", "登入", "訂閱"]
    return not any(b in title for b in bad)


def _deduplicate(news: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for item in news:
        title = item.get("title", "")
        h = hashlib.md5(title.encode()).hexdigest()[:8]
        # 也比對前 20 字元避免微小差異
        short_key = title[:20]
        if h not in seen and short_key not in seen:
            seen.add(h)
            seen.add(short_key)
            result.append(item)
    return result


async def _score_sentiment(code: str, news: list[dict]) -> list[dict]:
    """用 Claude API 做情緒評分；若無 API key 則用規則式"""
    if not news:
        return []

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if api_key:
        return await _claude_sentiment(code, news)
    return _rule_based_sentiment(news)


async def _claude_sentiment(code: str, news: list[dict]) -> list[dict]:
    try:
        import anthropic

        titles_text = "\n".join(f"{i+1}. {n['title']}" for i, n in enumerate(news))
        prompt = (
            f"以下是台股 {code} 的新聞標題，請對每則新聞做情緒評分：\n\n"
            f"{titles_text}\n\n"
            f"請輸出 JSON 陣列，格式：[{{\"idx\":1,\"sentiment\":\"正面/負面/中性\",\"score\":0.8,\"summary\":\"15字摘要\"}}]\n"
            f"只輸出 JSON，不要其他文字。"
        )

        client = anthropic.AsyncAnthropic(api_key=api_key)
        msg    = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()

        import json, re
        m = re.search(r'\[.*\]', text, re.DOTALL)
        if m:
            scores = json.loads(m.group())
            score_map = {item["idx"]: item for item in scores}
            result = []
            for i, n in enumerate(news, 1):
                sc = score_map.get(i, {})
                result.append({
                    **n,
                    "sentiment": sc.get("sentiment", "中性"),
                    "score":     sc.get("score", 0.5),
                    "summary":   sc.get("summary", n["title"][:20]),
                })
            return result
    except Exception as e:
        logger.debug(f"[news] claude sentiment failed: {e}")

    return _rule_based_sentiment(news)


def _rule_based_sentiment(news: list[dict]) -> list[dict]:
    positive_kw = ["大漲", "創高", "獲利", "訂單", "增加", "成長", "突破", "漲停", "買進", "上調", "買超"]
    negative_kw = ["下跌", "虧損", "減少", "衰退", "跌停", "賣出", "下調", "看跌", "賣超", "警告", "風險"]

    result = []
    for n in news:
        title = n.get("title", "")
        pos = sum(1 for kw in positive_kw if kw in title)
        neg = sum(1 for kw in negative_kw if kw in title)

        if pos > neg:
            sent, score = "正面", 0.7 + pos * 0.05
        elif neg > pos:
            sent, score = "負面", 0.3 - neg * 0.05
        else:
            sent, score = "中性", 0.5

        result.append({
            **n,
            "sentiment": sent,
            "score":     round(max(0, min(1, score)), 2),
            "summary":   title[:20] + ("..." if len(title) > 20 else ""),
        })
    return result


def format_news_report(data: dict) -> str:
    code  = data["code"]
    news  = data.get("news", [])
    ts    = data.get("ts", "")

    if not news:
        return f"📰 {code} 新聞聚合\n查無近期新聞，請稍後再試"

    emoji_map = {"正面": "🟢", "負面": "🔴", "中性": "⚪"}

    lines = [f"📰 {code} 新聞聚合 ({ts})", f"共 {len(news)} 則（Yahoo/鉅亨/工商）", "─" * 28, ""]

    sent_counts = {"正面": 0, "負面": 0, "中性": 0}
    for n in news:
        sent = n.get("sentiment", "中性")
        sent_counts[sent] = sent_counts.get(sent, 0) + 1
        emoji = emoji_map.get(sent, "⚪")
        src   = n.get("source", "")
        title = n.get("title", "")[:50]
        lines.append(f"{emoji} [{src}] {title}")
        if n.get("summary") and n["summary"] != title[:20]:
            lines.append(f"   摘要：{n['summary']}")
        lines.append("")

    lines += [
        "─" * 28,
        f"情緒統計：🟢正面{sent_counts['正面']} / ⚪中性{sent_counts['中性']} / 🔴負面{sent_counts['負面']}",
    ]

    # 整體情緒
    if sent_counts["正面"] > sent_counts["負面"]:
        lines.append("📊 Claude 評估：整體新聞偏正面，市場情緒樂觀")
    elif sent_counts["負面"] > sent_counts["正面"]:
        lines.append("📊 Claude 評估：整體新聞偏負面，注意風險")
    else:
        lines.append("📊 Claude 評估：新聞情緒中性，靜待觀察")

    return "\n".join(lines)
