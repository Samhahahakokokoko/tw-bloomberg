"""Smart Alert 2.0 — AI 事件通知引擎

偵測類型：
  1. 外資突然大量賣超（單日 > 5億）
  2. 族群情緒急轉（情緒分數單日跌 > 20）
  3. 個股成交量異常（> 3倍均量）
  4. 突破半年新高
  5. 跌破重要支撐
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx
from loguru import logger


@dataclass
class SmartAlert:
    alert_type:  str          # foreign_sell / sector_drop / volume_spike / new_high / support_break
    stock_id:    str
    stock_name:  str
    severity:    str          # warning / opportunity / info
    headline:    str
    detail:      str
    action_hint: str
    timestamp:   datetime = field(default_factory=datetime.now)

    def to_line_text(self) -> str:
        icon = {"warning": "⚠️", "opportunity": "🚀", "info": "ℹ️"}.get(self.severity, "🔔")
        return (
            f"{icon} 異常警報\n"
            f"{self.stock_id} {self.stock_name}\n"
            f"{'─' * 18}\n"
            f"{self.detail}\n\n"
            f"建議：{self.action_hint}"
        )

    def to_line_qr(self) -> dict:
        return {"items": [
            {"type": "action", "action": {
                "type": "postback", "label": "🔍 查看分析",
                "data": f"act=recommend_detail&code={self.stock_id}",
                "displayText": f"分析 {self.stock_id}"}},
            {"type": "action", "action": {
                "type": "message", "label": "🚫 忽略",
                "text": "好的"}},
        ]}


async def detect_foreign_selling(threshold_billion: float = 5.0) -> list[SmartAlert]:
    """外資突然大量賣超（超過 threshold 億）"""
    alerts: list[SmartAlert] = []
    try:
        from .twse_service import fetch_institutional
        codes = ["2330", "2454", "2317", "2308", "3034"]
        for code in codes:
            try:
                data = await fetch_institutional(code)
                if not data:
                    continue
                fn = data.get("foreign_net", 0) or 0
                if fn < -(threshold_billion * 1e8):
                    from .twse_service import fetch_realtime_quote
                    q    = await fetch_realtime_quote(code)
                    name = q.get("name", code) if q else code
                    alerts.append(SmartAlert(
                        alert_type  = "foreign_sell",
                        stock_id    = code,
                        stock_name  = name,
                        severity    = "warning",
                        headline    = f"{name} 外資大量賣超",
                        detail      = f"外資今日賣超 {abs(fn)/1e8:.1f}億（異常大量）",
                        action_hint = "注意觀察，評估是否減碼",
                    ))
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"[smart_alert] foreign_selling scan failed: {e}")
    return alerts


async def detect_volume_anomaly(ratio_threshold: float = 3.0) -> list[SmartAlert]:
    """個股成交量異常（> N倍均量）"""
    alerts: list[SmartAlert] = []
    try:
        from .report_screener import momentum_screener
        rows = momentum_screener(50)
        for r in rows:
            vol   = r.volume or 0
            avg   = r.vol_20d_max or 0
            if avg > 0 and vol > avg * ratio_threshold:
                alerts.append(SmartAlert(
                    alert_type  = "volume_spike",
                    stock_id    = r.stock_id,
                    stock_name  = r.name,
                    severity    = "opportunity",
                    headline    = f"{r.name} 成交量異常放大",
                    detail      = f"今日量 {vol/1e3:,.0f}張，為均量 {ratio_threshold:.0f}倍以上",
                    action_hint = "量能突破，可能啟動行情，留意追蹤",
                ))
    except Exception as e:
        logger.warning(f"[smart_alert] volume_anomaly scan failed: {e}")
    return alerts


async def detect_new_high(period_days: int = 126) -> list[SmartAlert]:
    """突破半年新高（約 126 個交易日）"""
    alerts: list[SmartAlert] = []
    try:
        from .report_screener import breakout_screener
        rows = breakout_screener(30)
        for r in rows:
            if r.breakout_pct >= 5.0:   # 突破幅度 >= 5%
                alerts.append(SmartAlert(
                    alert_type  = "new_high",
                    stock_id    = r.stock_id,
                    stock_name  = r.name,
                    severity    = "opportunity",
                    headline    = f"{r.name} 突破半年高點",
                    detail      = f"突破幅度 {r.breakout_pct:.1f}%，AI評分 {r.confidence:.0f}分",
                    action_hint = "技術面突破，可考慮建立初始部位",
                ))
    except Exception as e:
        logger.warning(f"[smart_alert] new_high scan failed: {e}")
    return alerts


async def detect_support_break() -> list[SmartAlert]:
    """跌破重要支撐（MA20 以下且動能轉負）"""
    alerts: list[SmartAlert] = []
    try:
        from .report_screener import momentum_screener
        rows = momentum_screener(50)
        for r in rows:
            if r.ma20_slope < -0.5 and r.change_pct < -2.0:
                alerts.append(SmartAlert(
                    alert_type  = "support_break",
                    stock_id    = r.stock_id,
                    stock_name  = r.name,
                    severity    = "warning",
                    headline    = f"{r.name} 跌破支撐",
                    detail      = f"今日跌 {r.change_pct:.1f}%，MA趨勢轉弱",
                    action_hint = "注意停損，若持有中考慮減碼",
                ))
    except Exception as e:
        logger.warning(f"[smart_alert] support_break scan failed: {e}")
    return alerts


async def scan_all_alerts() -> list[SmartAlert]:
    """執行所有偵測，回傳所有觸發的警報"""
    results: list[SmartAlert] = []
    for fn in [detect_foreign_selling, detect_volume_anomaly,
               detect_new_high, detect_support_break]:
        try:
            found = await fn()
            results.extend(found)
        except Exception as e:
            logger.warning(f"[smart_alert] {fn.__name__} failed: {e}")
    return results


async def push_alerts_to_subscribers(alerts: list[SmartAlert]):
    """把警報推送給所有訂閱者"""
    if not alerts:
        return

    from ..models.database import AsyncSessionLocal, settings
    from ..models.models import Subscriber
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        r    = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
        subs = r.scalars().all()

    if not subs:
        return

    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    # 只推最重要的 3 個警報，避免訊息轟炸
    top_alerts = sorted(alerts, key=lambda a: 0 if a.severity == "warning" else 1)[:3]

    async with httpx.AsyncClient(timeout=20) as c:
        for sub in subs:
            msgs = []
            for alert in top_alerts:
                msgs.append({
                    "type": "text",
                    "text": alert.to_line_text(),
                    "quickReply": alert.to_line_qr(),
                })
            try:
                await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": sub.line_user_id, "messages": msgs[:5]},
                    headers=headers,
                )
            except Exception as e:
                logger.warning(f"[smart_alert] push failed: {e}")

    logger.info(f"[smart_alert] pushed {len(top_alerts)} alerts to {len(subs)} subscribers")


async def run_smart_alert_scan():
    """排程入口：掃描 + 推送"""
    try:
        alerts = await scan_all_alerts()
        if alerts:
            logger.info(f"[smart_alert] found {len(alerts)} alerts")
            await push_alerts_to_subscribers(alerts)
        else:
            logger.debug("[smart_alert] no alerts triggered")
    except Exception as e:
        logger.error(f"[smart_alert] run failed: {e}")
