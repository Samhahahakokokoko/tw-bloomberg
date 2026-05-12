"""產業情緒分析服務（升級版）

功能：
  - 按產業別（半導體、散熱、金融、航運等）分析新聞情緒
  - 計算利多/利空評分 + 影響個股清單
  - 每日產生產業情緒快照
"""
from datetime import date, datetime, timedelta
from loguru import logger
from sqlalchemy import select

from ..models.database import AsyncSessionLocal, settings
from ..models.models import NewsArticle, IndustrySentiment


# 產業 → 關鍵字對應
INDUSTRY_KEYWORDS: dict[str, list[str]] = {
    "半導體": ["台積電", "聯電", "晶圓", "IC設計", "封測", "矽晶圓", "半導體",
               "晶片", "TSMC", "CoWoS", "HBM", "先進封裝"],
    "AI/伺服器": ["AI", "人工智慧", "伺服器", "散熱", "液冷", "GB200",
                  "輝達", "NVIDIA", "算力", "資料中心", "GPU"],
    "散熱": ["散熱", "液冷", "均溫板", "熱管", "奇鋐", "超眾", "雙鴻"],
    "金融": ["金控", "壽險", "銀行", "證券", "升息", "降息", "存款準備率",
             "外匯", "金管會", "保險"],
    "航運": ["航運", "貨櫃", "長榮", "陽明", "貨運費率", "BDI", "運費"],
    "電動車": ["電動車", "EV", "電池", "特斯拉", "比亞迪", "充電樁"],
    "面板": ["面板", "LCD", "OLED", "友達", "群創", "顯示器"],
    "傳產": ["鋼鐵", "石化", "紡織", "食品", "水泥"],
}

# 產業 → 代表股票
INDUSTRY_STOCKS: dict[str, list[str]] = {
    "半導體":   ["2330", "2303", "2454", "3711", "2449"],
    "AI/伺服器": ["2330", "3017", "2382", "2317", "3231"],
    "散熱":     ["3017", "8499", "3491", "6230"],
    "金融":     ["2882", "2891", "2884", "2886", "2885"],
    "航運":     ["2603", "2609", "2615"],
    "電動車":   ["1590", "1504"],
    "面板":     ["2409", "3481"],
    "傳產":     ["2002", "1303", "1301"],
}


async def analyze_industry(industry: str, news_days: int = 3) -> dict:
    """分析特定產業的近 N 日新聞情緒"""
    keywords = INDUSTRY_KEYWORDS.get(industry, [industry])
    cutoff_dt = datetime.combine(date.today() - timedelta(days=news_days), datetime.min.time())

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(NewsArticle)
            .where(NewsArticle.published_at >= cutoff_dt)
            .order_by(NewsArticle.published_at.desc())
            .limit(200)
        )
        all_news = r.scalars().all()

    # 找相關新聞
    related = [
        n for n in all_news
        if any(kw in (n.title or "") for kw in keywords)
        or any(kw in (n.content or "")[:500] for kw in keywords)
    ]

    if not related:
        return {
            "industry":       industry,
            "bullish_score":  50.0,
            "bearish_score":  50.0,
            "net_sentiment":  0.0,
            "news_count":     0,
            "key_stocks":     INDUSTRY_STOCKS.get(industry, []),
            "ai_summary":     "近期無相關新聞",
        }

    # 計算情緒
    bullish = sum(1 for n in related if n.sentiment == "positive")
    bearish = sum(1 for n in related if n.sentiment == "negative")
    neutral = len(related) - bullish - bearish

    total = len(related)
    bull_score = round(bullish / total * 100, 1)
    bear_score = round(bearish / total * 100, 1)
    net = round(bull_score - bear_score, 1)

    # 找影響個股（new 中提到代碼）
    stock_mentions: dict[str, int] = {}
    for n in related:
        for code in (INDUSTRY_STOCKS.get(industry, [])):
            name_mapping = {"2330": "台積電", "2603": "長榮", "2882": "國泰金"}
            stock_name = name_mapping.get(code, code)
            if code in (n.related_stocks or "") or stock_name in (n.title or ""):
                stock_mentions[code] = stock_mentions.get(code, 0) + 1

    key_stocks = sorted(stock_mentions, key=lambda x: stock_mentions[x], reverse=True)[:5]
    if not key_stocks:
        key_stocks = INDUSTRY_STOCKS.get(industry, [])[:3]

    # AI 分析
    ai_summary = await _ai_industry_summary(industry, related[:10], net)

    return {
        "industry":       industry,
        "bullish_score":  bull_score,
        "bearish_score":  bear_score,
        "net_sentiment":  net,
        "news_count":     total,
        "key_stocks":     key_stocks,
        "bullish_count":  bullish,
        "bearish_count":  bearish,
        "neutral_count":  neutral,
        "ai_summary":     ai_summary,
        "analysis_date":  date.today().isoformat(),
    }


async def run_all_industries():
    """每日分析所有產業並存入 DB"""
    today = date.today().isoformat()
    logger.info("[IndustrySentiment] 開始分析各產業情緒...")

    for industry in INDUSTRY_KEYWORDS:
        try:
            data = await analyze_industry(industry)
            async with AsyncSessionLocal() as db:
                existing = await db.execute(
                    select(IndustrySentiment).where(
                        IndustrySentiment.industry == industry,
                        IndustrySentiment.analysis_date == today,
                    )
                )
                rec = existing.scalar_one_or_none()
                vals = dict(
                    bullish_score  = data["bullish_score"],
                    bearish_score  = data["bearish_score"],
                    net_sentiment  = data["net_sentiment"],
                    key_stocks     = ",".join(data["key_stocks"]),
                    ai_summary     = data.get("ai_summary", ""),
                    news_count     = data["news_count"],
                    updated_at     = datetime.utcnow(),
                )
                if rec:
                    for k, v in vals.items():
                        setattr(rec, k, v)
                else:
                    db.add(IndustrySentiment(
                        industry      = industry,
                        analysis_date = today,
                        **vals,
                    ))
                await db.commit()
            logger.info(f"[IndustrySentiment] {industry}: net={data['net_sentiment']:+.1f} news={data['news_count']}")
        except Exception as e:
            logger.error(f"[IndustrySentiment] {industry} error: {e}")


async def get_all_sentiments(analysis_date: str = "") -> list[dict]:
    """取得最新各產業情緒"""
    if not analysis_date:
        analysis_date = date.today().isoformat()
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(IndustrySentiment)
            .where(IndustrySentiment.analysis_date == analysis_date)
            .order_by(IndustrySentiment.net_sentiment.desc())
        )
        rows = r.scalars().all()

    if not rows:
        # 找最近一天
        async with AsyncSessionLocal() as db:
            r2 = await db.execute(
                select(IndustrySentiment.analysis_date)
                .order_by(IndustrySentiment.analysis_date.desc())
                .limit(1)
            )
            latest = r2.scalar()
        if latest and latest != analysis_date:
            return await get_all_sentiments(latest)
        return []

    return [
        {
            "industry":       r.industry,
            "bullish_score":  r.bullish_score,
            "bearish_score":  r.bearish_score,
            "net_sentiment":  r.net_sentiment,
            "key_stocks":     r.key_stocks.split(",") if r.key_stocks else [],
            "ai_summary":     r.ai_summary or "",
            "news_count":     r.news_count,
            "analysis_date":  r.analysis_date,
        }
        for r in rows
    ]


async def _ai_industry_summary(industry: str, news: list, net: float) -> str:
    if not settings.anthropic_api_key:
        return ""
    try:
        import anthropic
        headlines = "\n".join(f"- {n.title}" for n in news[:8] if n.title)
        sentiment_desc = "偏多" if net > 10 else "偏空" if net < -10 else "中性"
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"{industry}產業近期新聞（情緒：{sentiment_desc} {net:+.1f}分）：\n"
                    f"{headlines}\n\n"
                    "請用2句繁體中文說明：目前市場對此產業的主要看法，和短期可能影響。"
                ),
            }],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.error(f"[IndustrySentiment] AI summary error: {e}")
        return ""
