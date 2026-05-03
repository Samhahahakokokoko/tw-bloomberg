"""System Health Monitor — 系統健康監控"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

MODULES = [
    "morning_report", "ai_feed", "news_scraper", "alert_checker",
    "smart_alert_v2", "watchlist_daily", "market_breadth",
    "autonomous_research", "hedge_fund_agent", "sector_heatmap",
    "portfolio_manager_advice", "pipeline_movers",
]


@dataclass
class ModuleStatus:
    name:       str
    status:     str     # ok / warning / error / unknown
    last_run:   Optional[datetime]
    error_count: int = 0
    message:    str = ""

    @property
    def icon(self) -> str:
        return {"ok": "✅", "warning": "⚠️", "error": "❌", "unknown": "❓"}.get(self.status, "❓")


async def check_all_modules() -> list[ModuleStatus]:
    """檢查所有模組的健康狀態"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import SystemHealthLog
    from sqlalchemy import select, desc

    statuses: list[ModuleStatus] = []

    try:
        async with AsyncSessionLocal() as db:
            for module in MODULES:
                r    = await db.execute(
                    select(SystemHealthLog)
                    .where(SystemHealthLog.module == module)
                    .order_by(desc(SystemHealthLog.created_at))
                    .limit(1)
                )
                log = r.scalar_one_or_none()

                if log is None:
                    st = ModuleStatus(name=module, status="unknown", last_run=None)
                else:
                    age_hours = (datetime.utcnow() - log.created_at).total_seconds() / 3600
                    if log.status == "error":
                        status = "error"
                    elif age_hours > 26:
                        status = "warning"
                    else:
                        status = "ok"
                    st = ModuleStatus(
                        name=module, status=status,
                        last_run=log.last_run or log.created_at,
                        error_count=log.error_count,
                        message=log.message[:80],
                    )
                statuses.append(st)
    except Exception as e:
        logger.warning(f"[system_monitor] check failed: {e}")
        statuses = [ModuleStatus(name=m, status="unknown", last_run=None) for m in MODULES]

    return statuses


async def log_module_status(module: str, status: str, message: str = "", error_count: int = 0):
    """記錄模組健康狀態"""
    try:
        from ..models.database import AsyncSessionLocal
        from ..models.models import SystemHealthLog
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            r   = await db.execute(
                select(SystemHealthLog).where(SystemHealthLog.module == module)
            )
            rec = r.scalar_one_or_none()
            if rec is None:
                rec = SystemHealthLog(module=module)
                db.add(rec)
            rec.status      = status
            rec.message     = message[:500]
            rec.last_run    = datetime.utcnow()
            rec.error_count = error_count
            rec.created_at  = datetime.utcnow()
            await db.commit()
    except Exception as e:
        logger.debug(f"[system_monitor] log_module failed: {e}")


async def push_health_alert(module: str, error: str, admin_uid: str = None):
    """推送系統警告給管理員"""
    admin_id = admin_uid or os.getenv("ADMIN_LINE_UID", "")
    if not admin_id:
        return

    import os, httpx
    from ..models.database import settings

    text = (
        f"⚠️ 系統警告\n"
        f"{module} 執行失敗\n"
        f"時間：{datetime.now().strftime('%Y/%m/%d %H:%M')}\n"
        f"錯誤：{error[:100]}"
    )
    qr = {"items": [
        {"type": "action", "action": {
            "type": "message", "label": "🔄 重新執行", "text": f"/system restart {module}"}},
        {"type": "action", "action": {
            "type": "message", "label": "略過", "text": "ok"}},
    ]}

    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            await c.post(
                "https://api.line.me/v2/bot/message/push",
                json={"to": admin_id, "messages": [
                    {"type": "text", "text": text, "quickReply": qr}
                ]},
                headers=headers,
            )
        except Exception as e:
            logger.error(f"[system_monitor] alert push failed: {e}")


def format_health_dashboard(statuses: list[ModuleStatus]) -> str:
    """格式化系統健康儀表板文字"""
    ok_count  = sum(1 for s in statuses if s.status == "ok")
    err_count = sum(1 for s in statuses if s.status == "error")
    warn_count = sum(1 for s in statuses if s.status == "warning")

    lines = [
        f"🖥️ 系統健康監控",
        f"✅{ok_count} ⚠️{warn_count} ❌{err_count}",
        "─" * 18,
    ]
    for s in statuses:
        last = s.last_run.strftime("%m/%d %H:%M") if s.last_run else "從未執行"
        lines.append(f"{s.icon} {s.name[:20]}")
        lines.append(f"   └ {last}")
        if s.message and s.status != "ok":
            lines.append(f"   └ {s.message[:50]}")

    return "\n".join(lines)
