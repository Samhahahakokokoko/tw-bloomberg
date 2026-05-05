"""news_agent.py — AI 新聞研究員"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from .base_agent import AgentVote

logger = logging.getLogger(__name__)
AGENT_NAME = "新聞Agent"


async def run(stock_id: str = "", sector: str = "") -> AgentVote:
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import NewsArticle
        from sqlalchemy import select

        cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        async with AsyncSessionLocal() as db:
            q = select(NewsArticle).where(NewsArticle.published_at >= cutoff)
            if stock_id:
                q = q.where(NewsArticle.content.contains(stock_id))
            elif sector:
                q = q.where(NewsArticle.content.contains(sector))
            r = await db.execute(q.limit(30))
            articles = r.scalars().all()

        if not articles:
            return AgentVote(AGENT_NAME, "neutral", 0.4,
                             ["近期無相關新聞"], data_quality=0.5)

        bullish_kw  = ["超預期", "上修", "訂單", "需求強勁", "突破", "創高", "法說上調"]
        bearish_kw  = ["下修", "庫存", "競爭", "降價", "裁員", "展望保守", "需求疲弱"]

        bull_cnt = bear_cnt = 0
        event_summaries: list[str] = []
        for a in articles:
            text = f"{a.title} {a.content or ''}".lower()
            b = sum(1 for kw in bullish_kw if kw in text)
            be = sum(1 for kw in bearish_kw if kw in text)
            bull_cnt += b
            bear_cnt += be
            if b > be and a.title:
                event_summaries.append(f"↑ {a.title[:30]}")
            elif be > b and a.title:
                event_summaries.append(f"↓ {a.title[:30]}")

        total = bull_cnt + bear_cnt or 1
        conf  = min(0.9, len(articles) / 20)

        if bull_cnt / total >= 0.6:
            opinion = "bullish"
            reasons = [f"正面新聞較多（{bull_cnt}則）"] + event_summaries[:2]
        elif bear_cnt / total >= 0.6:
            opinion = "bearish"
            reasons = [f"負面新聞較多（{bear_cnt}則）"] + event_summaries[:2]
        else:
            opinion = "neutral"
            reasons = [f"多空訊號均衡（多:{bull_cnt} 空:{bear_cnt}）"]

        return AgentVote(AGENT_NAME, opinion, conf, reasons, data_quality=conf)

    except Exception as e:
        logger.warning("[NewsAgent] failed: %s", e)
        return AgentVote(AGENT_NAME, "neutral", 0.3, [f"資料取得失敗：{type(e).__name__}"],
                         data_quality=0.2)
