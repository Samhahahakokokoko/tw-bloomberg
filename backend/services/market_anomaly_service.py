"""大盤異常警報服務"""
import httpx
from loguru import logger
from .twse_service import fetch_market_overview

THRESHOLDS = {
    "circuit_breaker": -7.0,
    "big_drop": -3.0,
    "big_rise": 3.0,
    "high_volume_ratio": 1.5,   # 成交量為近 5 日均量的 1.5 倍
}


async def check_market_anomaly() -> dict:
    """
    偵測大盤異常：
    - 跌幅 < -7%: 熔斷警報
    - 跌幅 < -3%: 急跌警報
    - 漲幅 > +3%: 急漲警報
    """
    try:
        overview = await fetch_market_overview()
        if not overview:
            return {"has_anomaly": False, "level": "normal", "message": ""}

        pct   = overview.get("change_pct", 0)
        value = overview.get("value", 0)
        change = overview.get("change", 0)

        level = "normal"
        message = ""

        if pct <= THRESHOLDS["circuit_breaker"]:
            level   = "critical"
            message = f"⛔ 大盤熔斷警報！跌幅 {pct:.2f}%，指數 {value:,.2f}（{change:+.2f}點）"
        elif pct <= THRESHOLDS["big_drop"]:
            level   = "warning"
            message = f"📉 大盤急跌警報！跌幅 {pct:.2f}%，指數 {value:,.2f}（{change:+.2f}點）"
        elif pct >= THRESHOLDS["big_rise"]:
            level   = "info"
            message = f"📈 大盤異常急漲！漲幅 +{pct:.2f}%，指數 {value:,.2f}（{change:+.2f}點）"

        return {
            "has_anomaly": level != "normal",
            "level":       level,
            "message":     message,
            "change_pct":  pct,
            "value":       value,
            "change":      change,
        }
    except Exception as e:
        logger.error(f"Market anomaly check error: {e}")
        return {"has_anomaly": False, "level": "normal", "message": ""}


async def push_anomaly_alert(anomaly: dict):
    """推播大盤異常警報給所有訂閱者"""
    if not anomaly.get("has_anomaly"):
        return
    try:
        from ..models.database import AsyncSessionLocal
        from ..models.models import Subscriber
        from sqlalchemy import select
        from .morning_report import _push_to_users

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Subscriber))
            subs = result.scalars().all()

        if subs:
            await _push_to_users([s.line_user_id for s in subs], anomaly["message"])
            logger.info(f"Anomaly alert pushed: {anomaly['message']}")
    except Exception as e:
        logger.error(f"Anomaly alert push error: {e}")
