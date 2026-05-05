"""youtube_agent.py — AI YouTube 分析師追蹤員"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from .base_agent import AgentVote

logger = logging.getLogger(__name__)
AGENT_NAME = "YouTube Agent"

SENTIMENT_VAL = {
    "strong_bullish": 2, "bullish": 1, "neutral": 0,
    "bearish": -1, "strong_bearish": -2,
}


async def run(stock_id: str = "", sector: str = "") -> AgentVote:
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import AnalystCall, Analyst
        from sqlalchemy import select

        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        async with AsyncSessionLocal() as db:
            q = (select(AnalystCall, Analyst.tier, Analyst.name)
                 .join(Analyst, AnalystCall.analyst_id == Analyst.analyst_id)
                 .where(AnalystCall.date >= cutoff)
                 .where(Analyst.is_active == True))
            if stock_id:
                q = q.where(AnalystCall.stock_id == stock_id)
            r = await db.execute(q.limit(50))
            rows = r.all()

        if not rows:
            return AgentVote(AGENT_NAME, "neutral", 0.3, ["近7日無分析師推薦記錄"],
                             data_quality=0.3)

        # Tier 加權
        TIER_W = {"S": 1.5, "A": 1.0, "B": 0.5, "C": -0.3}
        weighted_sum = 0.0
        weight_total = 0.0
        analyst_names: list[str] = []

        for call, tier, name in rows:
            tw  = TIER_W.get(tier or "B", 0.5)
            sv  = SENTIMENT_VAL.get(call.sentiment, 0)
            if tier == "C" and sv > 0:
                sv = -abs(sv) * 0.3
            weighted_sum += tw * sv
            weight_total += abs(tw)
            if name and name not in analyst_names and sv != 0:
                analyst_names.append(name)

        score = weighted_sum / weight_total if weight_total else 0
        conf  = min(0.9, len(rows) / 15)

        bull_count = sum(1 for c, _, _ in rows if c.sentiment in ("bullish", "strong_bullish"))
        bear_count = sum(1 for c, _, _ in rows if c.sentiment in ("bearish", "strong_bearish"))

        if score > 0.3:
            opinion = "bullish"
            reasons = [f"{bull_count}位分析師看多", f"加權分數：{score:.2f}"] + analyst_names[:2]
        elif score < -0.3:
            opinion = "bearish"
            reasons = [f"{bear_count}位分析師看空", f"加權分數：{score:.2f}"]
        else:
            opinion = "neutral"
            reasons = [f"多空分歧（多:{bull_count} 空:{bear_count}）"]

        return AgentVote(AGENT_NAME, opinion, conf, reasons, data_quality=conf)

    except Exception as e:
        logger.warning("[YouTubeAgent] failed: %s", e)
        return AgentVote(AGENT_NAME, "neutral", 0.3, [f"資料取得失敗：{type(e).__name__}"],
                         data_quality=0.2)
