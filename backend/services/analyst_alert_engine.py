"""Analyst Alert Engine — 偵測分析師觀點突然改變並推送 LINE 通知"""
from __future__ import annotations

import httpx
from dataclasses import dataclass
from datetime import datetime, timedelta
from loguru import logger
from sqlalchemy import select, desc, and_

SENTIMENT_RANK = {
    "strong_bullish": 2,
    "bullish":         1,
    "neutral":         0,
    "bearish":        -1,
    "strong_bearish": -2,
}


@dataclass
class ViewChangeAlert:
    analyst_name:   str
    analyst_tier:   str
    stock_id:       str
    stock_name:     str
    prev_sentiment: str
    new_sentiment:  str
    change_type:    str
    source_title:   str

    def to_line_text(self) -> str:
        tier_icon = {"S": "⭐⭐⭐", "A": "⭐⭐", "B": "⭐", "C": "⚠️"}.get(self.analyst_tier, "")
        prev_zh   = _sentiment_zh(self.prev_sentiment)
        new_zh    = _sentiment_zh(self.new_sentiment)
        lines = [
            f"⚡ 分析師觀點轉變",
            f"",
            f"{tier_icon} {self.analyst_name} 剛更新影片：",
            f"之前：{prev_zh}  {self.stock_id} {self.stock_name}",
            f"現在：改口「{new_zh}」",
            f"",
            f"影響評估：",
        ]
        if self.analyst_tier in ("S", "A"):
            lines.append(f"- 該分析師{tier_icon}，歷史可信度高")
            lines.append(f"- 建議重新評估 {self.stock_id} 持倉")
        else:
            lines.append(f"- 該分析師評級較低，僅供參考")
        return "\n".join(lines)

    def to_line_qr(self) -> dict:
        return {"items": [
            {"type": "action", "action": {
                "type": "postback",
                "label": "🔍 查看分析",
                "data": f"act=recommend_detail&code={self.stock_id}",
                "displayText": f"分析 {self.stock_id}"}},
            {"type": "action", "action": {
                "type": "message",
                "label": "🛡️ 設停損",
                "text": f"/alert {self.stock_id} below 0"}},
            {"type": "action", "action": {
                "type": "postback",
                "label": "忽略",
                "data": "act=market_card",
                "displayText": "忽略"}},
        ]}


def _sentiment_zh(s: str) -> str:
    return {
        "strong_bullish": "強力看多",
        "bullish":         "看多",
        "neutral":         "中性",
        "bearish":         "看空",
        "strong_bearish":  "強力看空",
    }.get(s, s)


async def detect_view_changes() -> list[ViewChangeAlert]:
    """偵測所有分析師在過去24小時的觀點轉變"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import AnalystCall, Analyst, AnalystViewChange

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    week_ago  = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    today     = datetime.now().strftime("%Y-%m-%d")

    alerts: list[ViewChangeAlert] = []

    async with AsyncSessionLocal() as db:
        # 取得今日新推薦
        r = await db.execute(
            select(AnalystCall).where(AnalystCall.date >= yesterday)
        )
        new_calls = r.scalars().all()

        # 取得分析師 tier 資訊
        r2 = await db.execute(select(Analyst))
        analyst_map = {a.analyst_id: a for a in r2.scalars().all()}

        for new_call in new_calls:
            # 查詢同一分析師對同一股票的歷史觀點（最近7日）
            r3 = await db.execute(
                select(AnalystCall)
                .where(and_(
                    AnalystCall.analyst_id == new_call.analyst_id,
                    AnalystCall.stock_id == new_call.stock_id,
                    AnalystCall.date >= week_ago,
                    AnalystCall.date < new_call.date,
                ))
                .order_by(desc(AnalystCall.date))
                .limit(3)
            )
            prev_calls = r3.scalars().all()
            if not prev_calls:
                continue

            prev_sentiment = prev_calls[0].sentiment
            new_sentiment  = new_call.sentiment

            # 判斷是否有顯著轉變
            prev_rank = SENTIMENT_RANK.get(prev_sentiment, 0)
            new_rank  = SENTIMENT_RANK.get(new_sentiment, 0)
            delta     = abs(new_rank - prev_rank)

            if delta < 2:  # 需要至少 2 級變化才算顯著（如 bullish → bearish）
                continue

            # 判斷轉變類型
            if prev_rank > 0 and new_rank < 0:
                change_type = "reversal"
            elif prev_rank > 0 and new_rank == 0:
                change_type = "silent_exit"
            else:
                change_type = "reversal"

            # 避免重複記錄
            r4 = await db.execute(
                select(AnalystViewChange)
                .where(and_(
                    AnalystViewChange.analyst_id == new_call.analyst_id,
                    AnalystViewChange.stock_id == new_call.stock_id,
                    AnalystViewChange.date == today,
                ))
            )
            if r4.scalar_one_or_none():
                continue

            # 儲存轉變記錄
            a = analyst_map.get(new_call.analyst_id)
            change = AnalystViewChange(
                date          = today,
                analyst_id    = new_call.analyst_id,
                analyst_name  = a.name if a else new_call.analyst_id,
                stock_id      = new_call.stock_id,
                stock_name    = new_call.stock_name,
                prev_sentiment = prev_sentiment,
                new_sentiment  = new_sentiment,
                change_type   = change_type,
                source_title  = new_call.source_title[:200],
            )
            db.add(change)

            alerts.append(ViewChangeAlert(
                analyst_name   = a.name if a else new_call.analyst_id,
                analyst_tier   = a.tier if a else "B",
                stock_id       = new_call.stock_id,
                stock_name     = new_call.stock_name,
                prev_sentiment = prev_sentiment,
                new_sentiment  = new_sentiment,
                change_type    = change_type,
                source_title   = new_call.source_title,
            ))

        await db.commit()

    return alerts


async def push_view_change_alerts(alerts: list[ViewChangeAlert]):
    """推送觀點轉變警告給所有訂閱者"""
    if not alerts:
        return

    from ..models.database import AsyncSessionLocal, settings
    from ..models.models import Subscriber
    from sqlalchemy import select as sa_select

    # 只推 S/A 級分析師的轉變（B/C 級靜默記錄）
    important = [a for a in alerts if a.analyst_tier in ("S", "A")]
    if not important:
        logger.info(f"[alert] {len(alerts)} view changes, none from S/A tier")
        return

    async with AsyncSessionLocal() as db:
        r    = await db.execute(sa_select(Subscriber).where(Subscriber.subscribed_morning == True))
        subs = r.scalars().all()

    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    async with httpx.AsyncClient(timeout=20) as c:
        for sub in subs:
            msgs = []
            for alert in important[:3]:
                msgs.append({
                    "type": "text",
                    "text": alert.to_line_text(),
                    "quickReply": alert.to_line_qr(),
                })
            if not msgs:
                continue
            try:
                await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": sub.line_user_id, "messages": msgs[:5]},
                    headers=headers,
                )
            except Exception as e:
                logger.warning(f"[alert] push failed: {e}")

    logger.info(f"[alert] pushed {len(important)} view changes to {len(subs)} subscribers")


async def run_daily_alert_check():
    """每日抓片後執行觀點轉變偵測"""
    try:
        alerts = await detect_view_changes()
        if alerts:
            logger.info(f"[alert] detected {len(alerts)} view changes")
            await push_view_change_alerts(alerts)
        else:
            logger.debug("[alert] no view changes detected")
    except Exception as e:
        logger.error(f"[alert] run failed: {e}")
