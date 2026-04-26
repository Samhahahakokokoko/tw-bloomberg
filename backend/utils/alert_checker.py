"""價格警報檢查器 — 支援絕對價、漲跌幅%、融資使用率"""
from sqlalchemy import select
from ..models.database import AsyncSessionLocal
from ..models.models import Alert
from ..services.twse_service import fetch_realtime_quote
from loguru import logger
from datetime import datetime


async def check_all_alerts():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Alert).where(Alert.is_active == True))
        alerts = result.scalars().all()

    for alert in alerts:
        try:
            await _check_single(alert)
        except Exception as e:
            logger.error(f"Alert check error {alert.stock_code}: {e}")


async def _check_single(alert: Alert):
    quote = await fetch_realtime_quote(alert.stock_code)
    price = quote.get("price", 0)
    change = quote.get("change", 0)
    if not price:
        return

    prev_close = price - change
    change_pct = change / prev_close * 100 if prev_close else 0

    triggered = False
    trigger_msg = ""

    if alert.alert_type == "price_above" and price >= alert.threshold:
        triggered = True
        trigger_msg = f"突破 {alert.threshold} 元，現價 {price}"
    elif alert.alert_type == "price_below" and price <= alert.threshold:
        triggered = True
        trigger_msg = f"跌破 {alert.threshold} 元，現價 {price}"
    elif alert.alert_type == "change_pct_above" and change_pct >= alert.threshold:
        triggered = True
        trigger_msg = f"漲幅達 {change_pct:+.2f}%，觸發 +{alert.threshold}% 警報"
    elif alert.alert_type == "change_pct_below" and change_pct <= alert.threshold:
        triggered = True
        trigger_msg = f"跌幅達 {change_pct:+.2f}%，觸發 {alert.threshold}% 警報"

    if triggered:
        await _send_line_alert(alert, trigger_msg, quote.get("name", alert.stock_code))
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Alert).where(Alert.id == alert.id))
            a = result.scalar_one_or_none()
            if a:
                a.is_active = False
                a.triggered_at = datetime.utcnow()
                await db.commit()


async def _send_line_alert(alert: Alert, trigger_msg: str, name: str):
    from ..models.database import settings
    if not alert.line_user_id or not settings.line_channel_access_token:
        logger.info(f"Alert triggered (no LINE push): {alert.stock_code} {trigger_msg}")
        return
    import httpx
    msg = f"⚠️ 股價警報\n{alert.stock_code} {name}\n{trigger_msg}"
    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    payload = {"to": alert.line_user_id, "messages": [{"type": "text", "text": msg}]}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://api.line.me/v2/bot/message/push", json=payload, headers=headers
        )
        logger.info(f"Alert pushed {alert.stock_code}: {r.status_code}")
