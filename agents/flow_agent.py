"""flow_agent.py — AI 資金流向分析員"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from .base_agent import AgentVote

logger = logging.getLogger(__name__)
AGENT_NAME = "資金Agent"


async def run(stock_id: str = "", sector: str = "") -> AgentVote:
    try:
        # 取三大法人資料
        from backend.services.twse_service import fetch_institutional
        reasons: list[str] = []
        bull_signals = 0
        bear_signals = 0
        data_quality = 0.5

        if stock_id:
            inst = await fetch_institutional(stock_id)
            if inst:
                data_quality = 0.85
                foreign_net  = inst.get("foreign_net", 0) or 0
                trust_net    = inst.get("trust_net", 0) or 0
                dealer_net   = inst.get("dealer_net", 0) or 0
                total_net    = foreign_net + trust_net + dealer_net

                if foreign_net > 0:
                    bull_signals += 2
                    reasons.append(f"外資買超{foreign_net:.0f}張")
                elif foreign_net < 0:
                    bear_signals += 2
                    reasons.append(f"外資賣超{abs(foreign_net):.0f}張")

                if trust_net > 0:
                    bull_signals += 1
                    reasons.append(f"投信買超{trust_net:.0f}張")
                elif trust_net < 0:
                    bear_signals += 1

                if total_net > 0:
                    bull_signals += 1
                elif total_net < 0:
                    bear_signals += 1
            else:
                reasons.append("法人資料取得失敗")
                data_quality = 0.3
        else:
            # 大盤整體資金流向
            try:
                from backend.models.database import AsyncSessionLocal
                from backend.models.models import CapitalFlowLog
                from sqlalchemy import select
                async with AsyncSessionLocal() as db:
                    r = await db.execute(
                        select(CapitalFlowLog).order_by(CapitalFlowLog.created_at.desc()).limit(1)
                    )
                    flow = r.scalar_one_or_none()
                if flow:
                    data_quality = 0.80
                    if (flow.foreign_futures_net or 0) > 0:
                        bull_signals += 2
                        reasons.append(f"外資期貨多單+{flow.foreign_futures_net:.0f}口")
                    elif (flow.foreign_futures_net or 0) < 0:
                        bear_signals += 2
                        reasons.append(f"外資期貨空單{flow.foreign_futures_net:.0f}口")
                    if flow.rotation_warning:
                        reasons.append("資金輪動警示中")
            except Exception:
                reasons.append("大盤資金資料不足")
                data_quality = 0.4

        total = bull_signals + bear_signals or 1
        conf  = min(0.88, data_quality)

        if bull_signals / total >= 0.6:
            opinion = "bullish"
        elif bear_signals / total >= 0.6:
            opinion = "bearish"
        else:
            opinion = "neutral"
            if not reasons:
                reasons = ["法人多空均衡"]

        return AgentVote(AGENT_NAME, opinion, conf, reasons, data_quality=data_quality)

    except Exception as e:
        logger.warning("[FlowAgent] failed: %s", e)
        return AgentVote(AGENT_NAME, "neutral", 0.3, [f"資料取得失敗：{type(e).__name__}"],
                         data_quality=0.2)
