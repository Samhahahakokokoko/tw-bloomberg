"""價格警報檢查器 — 支援絕對價、漲跌幅%、停損停利"""
import httpx
from datetime import datetime
from sqlalchemy import select
from ..models.database import AsyncSessionLocal, settings
from ..models.models import Alert, Subscriber
from ..services.twse_service import fetch_realtime_quote
from loguru import logger


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
    quote  = await fetch_realtime_quote(alert.stock_code)
    price  = quote.get("price", 0)
    change = quote.get("change", 0)
    if not price:
        return

    prev_close = price - change
    change_pct = change / prev_close * 100 if prev_close else 0

    triggered    = False
    trigger_msg  = ""

    if alert.alert_type == "price_above" and price >= alert.threshold:
        triggered   = True
        trigger_msg = f"🚀 突破 {alert.threshold} 元，現價 {price}"
    elif alert.alert_type == "price_below" and price <= alert.threshold:
        triggered   = True
        trigger_msg = f"🔻 跌破 {alert.threshold} 元，現價 {price}"
    elif alert.alert_type == "change_pct_above" and change_pct >= alert.threshold:
        triggered   = True
        trigger_msg = f"📈 當日漲幅 {change_pct:+.2f}%，觸發 +{alert.threshold}% 警報"
    elif alert.alert_type == "change_pct_below" and change_pct <= alert.threshold:
        triggered   = True
        trigger_msg = f"📉 當日跌幅 {change_pct:+.2f}%，觸發 {alert.threshold}% 警報"

    if not triggered:
        return

    name = quote.get("name", alert.stock_code)
    await _send_alert(alert, trigger_msg, name)

    # 標記已觸發
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Alert).where(Alert.id == alert.id))
        a = result.scalar_one_or_none()
        if a:
            a.is_active    = False
            a.triggered_at = datetime.utcnow()
            await db.commit()


async def _resolve_line_id(alert: Alert) -> str | None:
    """
    優先取 alert.line_user_id；
    若無，則用 alert.user_id 去 subscribers 表找對應的 LINE ID。
    """
    if alert.line_user_id:
        return alert.line_user_id

    if alert.user_id:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Subscriber).where(Subscriber.line_user_id == alert.user_id)
            )
            sub = result.scalar_one_or_none()
            if sub:
                return sub.line_user_id
    return None


async def _send_alert(alert: Alert, trigger_msg: str, name: str):
    line_id = await _resolve_line_id(alert)

    msg = (
        f"⚠️ 股價警報\n"
        f"{alert.stock_code} {name}\n"
        f"{trigger_msg}\n"
        f"類型：{_type_label(alert.alert_type)}"
    )

    if not line_id or not settings.line_channel_access_token:
        logger.info(f"Alert triggered (no LINE push): {alert.stock_code} — {trigger_msg}")
        return

    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    payload = {"to": line_id, "messages": [{"type": "text", "text": msg}]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.line.me/v2/bot/message/push", json=payload, headers=headers
            )
            logger.info(f"Alert pushed {alert.stock_code} → {line_id}: {r.status_code}")
    except Exception as e:
        logger.error(f"Alert LINE push error: {e}")


def _type_label(alert_type: str) -> str:
    return {
        "price_above":      "突破價（停利）",
        "price_below":      "跌破價（停損）",
        "change_pct_above": "漲幅%",
        "change_pct_below": "跌幅%",
    }.get(alert_type, alert_type)


# ── 自選股停損停利檢查 ────────────────────────────────────────────────────────

async def check_watchlist_triggers():
    """
    定時（交易時段每 5 分鐘）掃描自選股，
    若觸及停損或目標價，推送 LINE 通知。
    """
    from ..models.models import Watchlist
    from .morning_report import _push_to_users

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Watchlist).where(
                (Watchlist.stop_loss != None) | (Watchlist.target_price != None)
            )
        )
        items = result.scalars().all()

    for item in items:
        try:
            quote = await fetch_realtime_quote(item.stock_code)
            price = quote.get("price", 0)
            if not price:
                continue

            triggered_msgs = []
            if item.stop_loss and price <= item.stop_loss:
                triggered_msgs.append(
                    f"🔻 [{item.stock_code}] {item.stock_name or ''}\n"
                    f"現價 {price} 已觸及停損價 {item.stop_loss}！"
                )
            if item.target_price and price >= item.target_price:
                triggered_msgs.append(
                    f"🚀 [{item.stock_code}] {item.stock_name or ''}\n"
                    f"現價 {price} 已達目標價 {item.target_price}！"
                )

            for msg in triggered_msgs:
                # 找出有 LINE ID 的 subscriber
                if item.user_id and settings.line_channel_access_token:
                    async with AsyncSessionLocal() as db2:
                        res = await db2.execute(
                            select(Subscriber).where(Subscriber.line_user_id == item.user_id)
                        )
                        sub = res.scalar_one_or_none()
                    if sub:
                        await _push_to_users([sub.line_user_id], msg)
                logger.info(f"Watchlist trigger: {msg[:60]}")

        except Exception as e:
            logger.error(f"Watchlist check error {item.stock_code}: {e}")
