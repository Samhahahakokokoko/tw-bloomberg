"""Autonomous Daily Research — 每日 17:30 自動執行研究流程"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx
from loguru import logger


@dataclass
class ResearchOpportunity:
    rank:       int
    stock_id:   str
    stock_name: str
    sector:     str
    thesis:     list[str]   # 進場理由
    confidence: float
    ai_score:   float

    def to_text(self) -> str:
        lines = [f"{self.rank}. {self.stock_id} {self.stock_name}"]
        for t in self.thesis[:3]:
            lines.append(f"→ {t}")
        return "\n".join(lines)


async def run_daily_research() -> list[ResearchOpportunity]:
    """執行每日研究流程，回傳機會列表"""
    opportunities: list[ResearchOpportunity] = []

    try:
        # Step 1: 掃描動能股
        from quant.movers_engine import MoversEngine
        movers = await MoversEngine().scan()
        if not movers:
            movers = MoversEngine().scan_mock(20)

        # Step 2: 三層分類
        from quant.scanner_engine import ScannerEngine
        scan = ScannerEngine().classify(movers)
        candidates = (scan.core + scan.medium)[:10]

        # Step 3: 為每個候選股生成研究摘要
        from .report_screener import all_screener
        screener_rows = all_screener(200)
        score_map = {r.stock_id: r for r in screener_rows}

        for i, rec in enumerate(candidates[:5], 1):
            sid  = rec.stock_id if hasattr(rec, "stock_id") else rec.get("stock_id", "")
            name = rec.name if hasattr(rec, "name") else rec.get("name", sid)
            sect = rec.sector if hasattr(rec, "sector") else rec.get("sector", "其他")
            row  = score_map.get(sid)

            thesis: list[str] = []
            if row:
                if row.confidence >= 70:
                    thesis.append(f"{sect}題材延燒，信心指數{row.confidence:.0f}")
                if row.chip_5d > 0:
                    thesis.append("法人積極布局，籌碼健康")
                if row.breakout_pct >= 3:
                    thesis.append(f"技術面突破平台 +{row.breakout_pct:.1f}%")
                if row.change_pct >= 1.5:
                    thesis.append(f"動能強勁，今日+{row.change_pct:.1f}%")
            if not thesis:
                thesis = ["進入觀察名單", "等待更多確認訊號"]

            opportunities.append(ResearchOpportunity(
                rank=i, stock_id=sid, stock_name=name, sector=sect,
                thesis=thesis,
                confidence=row.confidence if row else 55,
                ai_score=row.model_score if row else 55,
            ))

    except Exception as e:
        logger.error(f"[autonomous_research] scan failed: {e}")

    return opportunities


def format_research_report(opps: list[ResearchOpportunity]) -> str:
    today = datetime.now().strftime("%m/%d")
    if not opps:
        return f"🔬 今日自動研究報告 {today}\n\n今日無顯著機會，市場觀望"

    lines = [
        f"🔬 今日自動研究報告  {today}",
        f"發現 {len(opps)} 個值得關注的機會：",
        "─" * 22,
    ]
    for o in opps:
        lines.append("")
        lines.append(o.to_text())
    lines += [
        "",
        "[查看完整報告] /daily",
        "[加入自選] /watch [代碼]",
        "[AI分析] /ai [代碼]",
    ]
    return "\n".join(lines)


def build_research_qr(opps: list[ResearchOpportunity]) -> dict:
    items = []
    for o in opps[:3]:
        items.append({"type": "action", "action": {
            "type": "message", "label": f"➕{o.stock_id}",
            "text": f"/watch {o.stock_id}",
        }})
    items.append({"type": "action", "action": {
        "type": "postback", "label": "📊 今日選股",
        "data": "act=screener_qr", "displayText": "今日選股",
    }})
    return {"items": items[:13]}


async def push_daily_research():
    """17:30 推送今日研究報告給所有訂閱者"""
    from ..models.database import AsyncSessionLocal, settings
    from ..models.models import Subscriber, DailyResearchLog
    from sqlalchemy import select

    opps   = await run_daily_research()
    text   = format_research_report(opps)
    qr     = build_research_qr(opps)

    # 儲存到資料庫
    try:
        async with AsyncSessionLocal() as db:
            log = DailyResearchLog(
                date         = datetime.now().strftime("%Y-%m-%d"),
                opportunities = json.dumps([{
                    "stock_id": o.stock_id, "name": o.stock_name,
                    "thesis": o.thesis, "confidence": o.confidence,
                } for o in opps]),
                market_state = "unknown",
            )
            db.add(log)
            await db.commit()
    except Exception as e:
        logger.debug(f"[autonomous_research] log save failed: {e}")

    async with AsyncSessionLocal() as db:
        r    = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
        subs = r.scalars().all()

    if not subs:
        return

    msg     = {"type": "text", "text": text, "quickReply": qr}
    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    async with httpx.AsyncClient(timeout=30) as c:
        for sub in subs:
            try:
                await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": sub.line_user_id, "messages": [msg]},
                    headers=headers,
                )
            except Exception as e:
                logger.warning(f"[autonomous_research] push failed: {e}")

    logger.info(f"[autonomous_research] pushed {len(opps)} opps to {len(subs)} subscribers")
