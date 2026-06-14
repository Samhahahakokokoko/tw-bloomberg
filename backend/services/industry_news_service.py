"""Industry News Service — 特定產業新聞追蹤與分析"""
from __future__ import annotations

import time
from loguru import logger
import asyncio
import re

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 1800  # 30 min

INDUSTRY_KEYWORDS: dict[str, list[str]] = {
    "AI伺服器":   ["AI伺服器", "雲端運算", "輝達", "CoWoS", "HBM", "GB200"],
    "半導體":     ["半導體", "晶圓", "台積電", "製程", "先進封裝"],
    "電動車":     ["電動車", "EV", "電池", "特斯拉", "充電"],
    "航運":       ["航運", "貨櫃", "運費", "BDI", "長榮", "陽明"],
    "金融":       ["金融", "升息", "降息", "聯準會", "Fed", "利率"],
    "生技":       ["生技", "新藥", "臨床試驗", "FDA", "藥廠"],
    "散熱電源":   ["散熱", "熱管", "液冷", "電源供應器", "伺服器電源"],
    "PCB":        ["PCB", "電路板", "覆銅板", "HDI"],
    "通訊":       ["5G", "通訊", "基地台", "衛星", "星鏈"],
    "傳產鋼鐵":   ["鋼鐵", "鋼價", "中鋼", "原物料", "中鋼"],
}

INDUSTRY_STOCKS: dict[str, list[str]] = {
    "AI伺服器":  ["3231", "6669", "4938", "3017", "2382"],
    "半導體":    ["2330", "2303", "2344", "3711"],
    "電動車":    ["1590", "6223", "8054", "2355"],
    "航運":      ["2603", "2609", "2615", "2623"],
    "金融":      ["2882", "2881", "2891", "2886"],
    "生技":      ["4763", "6548", "4174", "1736"],
    "散熱電源":  ["3443", "1590", "6285", "2313"],
    "PCB":       ["2382", "3037", "6269", "3376"],
    "通訊":      ["2412", "3045", "4977", "6277"],
    "傳產鋼鐵":  ["2002", "2006", "9910", "1301"],
}


async def get_industry_news(industry: str) -> dict:
    key = industry.strip()
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_industry_news(key)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_industry_news(industry: str) -> dict:
    import asyncio
    keywords = INDUSTRY_KEYWORDS.get(industry, [industry])
    stocks   = INDUSTRY_STOCKS.get(industry, [])

    news_task  = _scrape_news(keywords)
    quote_task = _get_industry_quotes(stocks)
    news, quotes = await asyncio.gather(news_task, quote_task, return_exceptions=True)
    news   = news   if isinstance(news, list)   else []
    quotes = quotes if isinstance(quotes, list) else []

    deduped  = _dedupe(news)[:8]
    analysis = _analyze_news(industry, deduped, quotes)

    return {
        "industry": industry,
        "news":     deduped,
        "quotes":   quotes,
        "analysis": analysis,
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


async def _scrape_news(keywords: list[str]) -> list[dict]:
    import httpx, re
    results = []
    kw = keywords[0] if keywords else ""
    sources = [
        f"https://tw.stock.yahoo.com/news/search/?q={kw}",
        f"https://cnyes.com/search/?keyword={kw}",
    ]
    for url in sources:
        try:
            async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "Mozilla/5.0"}) as cl:
                r = await cl.get(url)
            titles = re.findall(r'<h3[^>]*>([^<]{10,80})</h3>', r.text)
            for t in titles[:4]:
                t = t.strip()
                if any(k in t for k in keywords):
                    results.append({"title": t, "source": url.split("/")[2]})
        except Exception as e:
            logger.debug(f"[industry_news] scrape {url}: {e}")
    return results


def _dedupe(news: list[dict]) -> list[dict]:
    seen, out = set(), []
    for n in news:
        t = n.get("title", "")
        if t and t[:20] not in seen:
            seen.add(t[:20])
            out.append(n)
    return out


async def _get_industry_quotes(stocks: list[str]) -> list[dict]:
    import asyncio
    async def _q(code):
        try:
            from .twse_service import fetch_realtime_quote
            q = await fetch_realtime_quote(code)
            if q:
                return {"code": code, "name": q.get("name", code),
                        "chg": float(q.get("change_pct") or 0)}
        except Exception as e:
            pass
        return None
    results = await asyncio.gather(*[_q(c) for c in stocks[:5]], return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


def _analyze_news(industry: str, news: list[dict], quotes: list[dict]) -> str:
    if not news and not quotes:
        return f"{industry} 產業今日新聞偏少，維持觀察。"
    avg_chg = sum(q.get("chg", 0) for q in quotes) / len(quotes) if quotes else 0
    pos = sum(1 for n in news if any(w in n.get("title","")
              for w in ["漲","增","成長","突破","強","擴","訂單"]))
    neg = sum(1 for n in news if any(w in n.get("title","")
              for w in ["跌","減","衰退","下修","弱","砍單"]))
    if pos > neg and avg_chg > 0:
        sentiment = "產業新聞偏正面，股價同步上漲，短線動能偏強"
    elif neg > pos or avg_chg < -1:
        sentiment = "產業新聞偏負面或股價走弱，留意下行風險"
    else:
        sentiment = "新聞情緒中性，產業進入整理期"
    return f"{industry}：{sentiment}。平均漲幅 {avg_chg:+.1f}%，共 {len(news)} 則新聞。"


def format_industry_news(data: dict) -> str:
    if not data:
        return "❌ 無法取得產業新聞"
    industry = data["industry"]; news = data["news"]
    quotes   = data["quotes"];   analysis = data["analysis"]; ts = data["updated_at"]

    lines = [f"📰 產業新聞  {industry}", "─" * 32, ""]
    if quotes:
        lines.append("📈 代表股今日表現")
        for q in quotes:
            chg = q.get("chg", 0)
            icon = "▲" if chg > 0 else ("▼" if chg < 0 else "─")
            lines.append(f"  [{q['code']}] {q['name']:<6} {icon}{chg:>+5.1f}%")
        lines.append("")

    lines.append("📰 相關新聞")
    if news:
        for n in news[:6]:
            lines.append(f"  • {n.get('title','')[:40]}")
            if n.get("source"):
                lines.append(f"    ({n['source']})")
    else:
        lines.append("  今日暫無相關新聞")

    lines += ["", "─" * 32, "🤖 AI 產業分析", analysis, "", f"更新：{ts}"]
    return "\n".join(lines)


INDUSTRY_LIST = list(INDUSTRY_KEYWORDS.keys())


async def push_daily_industry_summary() -> int:
    """每日早晨推播重點產業新聞摘要"""
    import asyncio
    from .line_push import push_to_admin
    top_industries = ["AI伺服器", "半導體", "電動車"]
    pushed = 0
    for ind in top_industries:
        try:
            data    = await get_industry_news(ind)
            report  = format_industry_news(data)
            await push_to_admin(report[:2000])
            pushed += 1
        except Exception as e:
            logger.error(f"[industry_news] push {ind}: {e}")
    return pushed
