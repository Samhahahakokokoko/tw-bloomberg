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
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx
from loguru import logger

# ── 24 小時去重機制 ────────────────────────────────────────────────────────────
_SENT_ALERTS: dict[tuple[str, str], float] = {}  # (stock_id, alert_type) → sent_at
_DEDUP_TTL = 86400  # 24 小時（秒）


def _is_duplicate(stock_id: str, alert_type: str) -> bool:
    """同一 (stock_id, alert_type) 在 24h 內只發一次"""
    last = _SENT_ALERTS.get((stock_id, alert_type), 0.0)
    return (time.time() - last) < _DEDUP_TTL


def _mark_sent(stock_id: str, alert_type: str) -> None:
    _SENT_ALERTS[(stock_id, alert_type)] = time.time()


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
            except Exception as e:
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


async def _get_user_watched_codes(uid: str) -> set[str]:
    """取得用戶自選股 + 庫存代碼的聯集"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Watchlist, Portfolio
    from sqlalchemy import select
    codes: set[str] = set()
    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Watchlist.stock_code).where(Watchlist.user_id == uid))
            codes.update(row[0] for row in r.fetchall() if row[0])
            r = await db.execute(select(Portfolio.stock_code).where(Portfolio.user_id == uid))
            codes.update(row[0] for row in r.fetchall() if row[0])
    except Exception as e:
        logger.debug("[smart_alert] could not load watched codes for {}: {}", uid, e)
    return codes


async def push_alerts_to_subscribers(alerts: list[SmartAlert]):
    """把警報推送給訂閱者（個人化：優先推送自選股/庫存相關警報）"""
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

    # severity 排序：warning 優先於 opportunity/info
    all_sorted = sorted(alerts, key=lambda a: 0 if a.severity == "warning" else 1)

    from .line_push import push_line_messages
    pushed_count = 0

    async with httpx.AsyncClient(timeout=20) as c:
        for sub in subs:
            uid = sub.line_user_id

            # 1. 找出和此用戶相關的警報（自選股 + 庫存）
            watched = await _get_user_watched_codes(uid)
            if watched:
                personal = [a for a in all_sorted if a.stock_id in watched]
                # 2. 若無個人化警報，fallback 到 warning 級別的市場警報（最多1條）
                market_warn = [a for a in all_sorted if a.severity == "warning"][:1]
                to_send = (personal + [a for a in market_warn if a not in personal])[:3]
            else:
                # 未設自選股：只推最重要的 2 條
                to_send = all_sorted[:2]

            if not to_send:
                continue

            msgs = [
                {"type": "text", "text": a.to_line_text(), "quickReply": a.to_line_qr()}
                for a in to_send
            ]
            ok = await push_line_messages(uid, msgs[:5], client=c, context="smart_alert")
            if ok:
                pushed_count += 1

    logger.info("[smart_alert] pushed to {}/{} subscribers (personalized)", pushed_count, len(subs))


async def run_smart_alert_scan():
    """排程入口：掃描 + 去重 + 推送"""
    try:
        alerts = await scan_all_alerts()

        # 24h 去重：過濾掉已發過的同類警報
        new_alerts = [a for a in alerts if not _is_duplicate(a.stock_id, a.alert_type)]
        deduped = len(alerts) - len(new_alerts)
        if deduped:
            logger.debug(f"[smart_alert] deduped {deduped} repeat alerts (24h window)")

        if new_alerts:
            logger.info(f"[smart_alert] found {len(new_alerts)} new alerts ({deduped} deduped)")
            await push_alerts_to_subscribers(new_alerts)
            for a in new_alerts:
                _mark_sent(a.stock_id, a.alert_type)
        else:
            logger.debug(f"[smart_alert] no new alerts (checked {len(alerts)}, {deduped} deduped)")
    except Exception as e:
        logger.error(f"[smart_alert] run failed: {type(e).__name__}: {e}")
