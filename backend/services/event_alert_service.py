"""event_alert_service.py — 事件驅動即時提醒"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
logger = logging.getLogger(__name__)

# 去重快取：(code, event_type) → timestamp
_ALERT_CACHE: dict = {}
_DEDUP_TTL = 3600  # 1小時去重


def _is_dup(code: str, etype: str) -> bool:
    last = _ALERT_CACHE.get((code, etype), 0.0)
    return (time.time() - last) < _DEDUP_TTL


def _mark(code: str, etype: str):
    _ALERT_CACHE[(code, etype)] = time.time()


@dataclass
class EventAlert:
    event_type: str      # price_surge, price_drop, volume_spike, foreign_buy, new_high, new_low, margin_surge
    stock_id:   str
    stock_name: str
    value:      float
    threshold:  float
    message:    str
    severity:   str = "warning"  # warning / opportunity

    def to_text(self) -> str:
        icon = {
            "price_surge":  "🚀",
            "price_drop":   "📉",
            "volume_spike": "💥",
            "foreign_buy":  "🏦",
            "new_high":     "🌟",
            "new_low":      "⚠️",
            "margin_surge": "💳",
        }.get(self.event_type, "🔔")
        return f"{icon} 事件警報\n{self.stock_name}（{self.stock_id}）\n{self.message}"


async def scan_event_alerts() -> list:
    """掃描全市場事件"""
    alerts: list = []
    try:
        from backend.services.report_screener import _rt_cache, _fetch_rt_cache
        prices = _rt_cache.get("prices", {})
        if not prices:
            await _fetch_rt_cache()
            prices = _rt_cache.get("prices", {})

        for code, data in list(prices.items())[:2000]:
            name = data.get("name", code) or code
            change_pct = float(data.get("change_pct", 0) or 0)
            close      = float(data.get("close", 0) or 0)

            # 1. 單日漲超 5%
            if change_pct >= 5.0 and not _is_dup(code, "price_surge"):
                alerts.append(EventAlert(
                    event_type="price_surge", stock_id=code, stock_name=name,
                    value=change_pct, threshold=5.0,
                    message=f"今日上漲 {change_pct:.1f}%（超過5%警戒線）\n現價：{close:.1f}",
                    severity="opportunity"
                ))
                _mark(code, "price_surge")

            # 2. 單日跌超 5%
            if change_pct <= -5.0 and not _is_dup(code, "price_drop"):
                alerts.append(EventAlert(
                    event_type="price_drop", stock_id=code, stock_name=name,
                    value=change_pct, threshold=-5.0,
                    message=f"今日下跌 {change_pct:.1f}%（超過-5%警戒線）\n現價：{close:.1f}",
                    severity="warning"
                ))
                _mark(code, "price_drop")

    except Exception as e:
        logger.warning("[event_alert] scan failed: %s", e)

    try:
        # 3. 成交量爆量（從 screener）
        from backend.services.report_screener import all_screener
        rows = all_screener(limit=200)
        for row in rows:
            code = getattr(row, "stock_id", "") or ""
            name = getattr(row, "name", code) or code
            if not code:
                continue
            vol   = float(getattr(row, "volume", 0) or 0)
            avg20 = float(getattr(row, "vol_20d_max", 0) or 0)

            # 爆量（> 3倍均量）
            if avg20 > 0 and vol > avg20 * 3 and not _is_dup(code, "volume_spike"):
                alerts.append(EventAlert(
                    event_type="volume_spike", stock_id=code, stock_name=name,
                    value=vol, threshold=avg20 * 3,
                    message=f"成交量爆量！今日 {vol/1000:.0f}K 張，為均量 {vol/avg20:.1f} 倍",
                    severity="opportunity"
                ))
                _mark(code, "volume_spike")

    except Exception as e:
        logger.warning("[event_alert] screener scan failed: %s", e)

    try:
        # 4. 外資大買（從法人資料）—— 只掃熱門個股清單
        from backend.services.twse_service import fetch_institutional
        import asyncio as _asyncio
        WATCH_CODES = ["2330", "2454", "2317", "2308", "3034", "2382", "2395", "6669", "2303", "2412"]

        async def _safe_inst(c):
            try:
                return c, await fetch_institutional(c)
            except Exception as e:
                return c, {}

        inst_results = await _asyncio.gather(*[_safe_inst(c) for c in WATCH_CODES])
        for code, inst in inst_results:
            if not isinstance(inst, dict):
                continue
            fn = float(inst.get("foreign_net", 0) or 0) / 1000  # 轉為張
            if fn >= 5000 and not _is_dup(code, "foreign_buy"):
                from backend.services.report_screener import _rt_cache
                name = _rt_cache.get("prices", {}).get(code, {}).get("name", code) or code
                alerts.append(EventAlert(
                    event_type="foreign_buy", stock_id=code, stock_name=name,
                    value=fn, threshold=5000,
                    message=f"外資大買 {fn:,.0f} 張！單日大量買入",
                    severity="opportunity"
                ))
                _mark(code, "foreign_buy")
    except Exception as e:
        logger.warning("[event_alert] foreign buy scan: %s", e)

    return alerts


async def push_event_alerts_to_subscribers(alerts: list, token: str):
    """推送事件警報給訂閱者"""
    if not alerts:
        return
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import Subscriber, Watchlist
        from backend.services.line_push import push_line_messages
        from sqlalchemy import select
        import asyncio as _asyncio

        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
            subs = r.scalars().all()

        async def _get_watched(uid):
            try:
                async with AsyncSessionLocal() as db:
                    r = await db.execute(select(Watchlist.stock_code).where(Watchlist.user_id == uid))
                    return uid, {row[0] for row in r.fetchall() if row[0]}
            except Exception as e:
                return uid, set()

        watched_results = await _asyncio.gather(*[_get_watched(s.line_user_id) for s in subs if s.line_user_id])
        watched_map = dict(watched_results)

        for sub in subs:
            uid = sub.line_user_id
            if not uid:
                continue
            watched = watched_map.get(uid, set())
            personal = [a for a in alerts if a.stock_id in watched]
            to_send = personal[:3] if personal else [a for a in alerts if a.severity == "warning"][:2]
            if not to_send:
                continue
            msgs = [{"type": "text", "text": a.to_text()} for a in to_send]
            await push_line_messages(uid, msgs[:3], token=token, timeout=15, context="event_alert")
    except Exception as e:
        logger.error("[event_alert] push failed: %s", e)


async def run_event_scan(token: str = ""):
    """排程入口"""
    try:
        alerts = await scan_event_alerts()
        if alerts:
            logger.info(f"[event_alert] {len(alerts)} events found")
            await push_event_alerts_to_subscribers(alerts, token)
    except Exception as e:
        logger.error(f"[event_alert] run failed: {e}")
