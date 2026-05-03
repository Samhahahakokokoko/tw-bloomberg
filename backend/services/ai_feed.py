"""AI Feed — 每日 08:30 智能市場簡報（Flex Message + Quick Reply）"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import httpx
from loguru import logger

from .twse_service import fetch_market_overview, fetch_institutional


async def generate_ai_feed() -> dict:
    """
    組合 AI Feed 資料。
    回傳 dict 包含所有欄位；失敗欄位使用安全預設值。
    """
    today = datetime.now().strftime("%m/%d")
    data: dict = {
        "date":          today,
        "sectors":       "散熱 AI伺服器 PCB",
        "top_stocks":    [],
        "risk_warning":  "",
        "ai_action":     "建議觀察，等待方向確認",
        "foreign_flow":  0,
        "flow_sector":   "半導體",
        "sentiment":     50,
        "market_bias":   "中性",
    }

    try:
        ov = await fetch_market_overview()
        if ov:
            chg = ov.get("change", 0) or 0
            pct = ov.get("change_pct", 0) or 0
            data["index_value"]  = ov.get("value", 0)
            data["index_change"] = chg
            data["index_pct"]    = pct
            # 大盤情緒估算
            if pct >= 1.0:
                data["sentiment"]   = 72
                data["market_bias"] = "偏多"
            elif pct <= -1.0:
                data["sentiment"]   = 32
                data["market_bias"] = "偏空"
            else:
                data["sentiment"]   = 52
                data["market_bias"] = "中性"
    except Exception as e:
        logger.warning(f"[ai_feed] market_overview failed: {e}")

    try:
        inst = await fetch_institutional("2330")
        if inst:
            fn = inst.get("foreign_net", 0) or 0
            data["foreign_flow"]  = fn / 1e8   # 億
            data["flow_sector"] = "半導體" if fn > 0 else "防禦類股"
            if fn < -5e8:
                data["risk_warning"] = f"外資大量賣超 {abs(fn)/1e8:.1f}億，注意風險"
    except Exception as e:
        logger.warning(f"[ai_feed] institutional failed: {e}")

    try:
        from .report_screener import momentum_screener
        rows = momentum_screener(10)
        if rows:
            top = rows[:2]
            data["top_stocks"] = [
                {"code": r.stock_id, "name": r.name, "change_pct": r.change_pct}
                for r in top
            ]
    except Exception as e:
        logger.warning(f"[ai_feed] screener failed: {e}")

    return data


def build_ai_feed_text(data: dict) -> str:
    """把 AI Feed 資料轉成 LINE 文字訊息"""
    today = data.get("date", datetime.now().strftime("%m/%d"))
    lines = [f"🌅 今日市場簡報  {today}", "─" * 22]

    # 主流族群
    lines.append(f"📈 今日主流族群：{data.get('sectors', '--')}")

    # 最強個股
    top = data.get("top_stocks", [])
    if top:
        parts = []
        for s in top[:2]:
            sign = "+" if s["change_pct"] >= 0 else ""
            parts.append(f"{s['code']} {sign}{s['change_pct']:.1f}%")
        lines.append(f"🔥 最強個股：{' / '.join(parts)}")

    # 風險警告
    risk = data.get("risk_warning", "")
    if risk:
        lines.append(f"⚠️ 風險警告：{risk}")

    # AI 操作
    lines.append(f"🤖 AI今日操作：{data.get('ai_action', '--')}")

    # 資金流向
    fn = data.get("foreign_flow", 0)
    if fn:
        direction = "買超" if fn > 0 else "賣超"
        lines.append(f"💰 資金流向：外資{direction}{abs(fn):.0f}億，集中{data.get('flow_sector', '--')}")

    # 市場情緒
    sent  = data.get("sentiment", 50)
    bias  = data.get("market_bias", "中性")
    lines.append(f"📰 市場情緒：{bias} {sent}/100")

    return "\n".join(lines)


def build_ai_feed_qr() -> dict:
    """AI Feed Quick Reply 按鈕"""
    return {"items": [
        {"type": "action", "action": {"type": "postback",
         "label": "📊 查看選股", "data": "act=screener_qr",
         "displayText": "查看選股"}},
        {"type": "action", "action": {"type": "postback",
         "label": "💼 看庫存",  "data": "act=portfolio_view",
         "displayText": "看庫存"}},
        {"type": "action", "action": {"type": "postback",
         "label": "🤖 AI分析", "data": "act=ai_menu",
         "displayText": "AI分析"}},
        {"type": "action", "action": {"type": "postback",
         "label": "📈 大盤行情", "data": "act=market_card",
         "displayText": "大盤行情"}},
    ]}


async def push_ai_feed():
    """推送 AI Feed 給所有訂閱早報的用戶"""
    from ..models.database import AsyncSessionLocal, settings
    from ..models.models import Subscriber
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        r    = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
        subs = r.scalars().all()

    if not subs:
        logger.info("[ai_feed] no subscribers")
        return

    data = await generate_ai_feed()
    text = build_ai_feed_text(data)
    qr   = build_ai_feed_qr()

    message = {
        "type": "text",
        "text": text,
        "quickReply": qr,
    }

    uids    = [s.line_user_id for s in subs]
    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}

    async with httpx.AsyncClient(timeout=20) as c:
        for uid in uids:
            try:
                await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": uid, "messages": [message]},
                    headers=headers,
                )
            except Exception as e:
                logger.warning(f"[ai_feed] push failed uid={uid[:8]}: {e}")

    logger.info(f"[ai_feed] pushed to {len(uids)} subscribers")
