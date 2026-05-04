"""
system_health_dashboard.py — 完整系統健康監控

Web 頁面 /dashboard/health 顯示所有模組狀態
任何模組變紅 → 立即 LINE 通知
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

STATUS_ICON = {"green": "🟢", "yellow": "🟡", "red": "🔴", "unknown": "⚪"}


@dataclass
class ModuleStatus:
    name:          str
    status:        str = "unknown"    # green / yellow / red / unknown
    last_run_at:   Optional[str] = None
    latency_ms:    Optional[int] = None
    error_rate:    float = 0.0        # 0-1
    data_quality:  float = 0.0        # 0-1
    last_error:    str = ""
    note:          str = ""

    @property
    def icon(self) -> str:
        return STATUS_ICON.get(self.status, "⚪")

    @property
    def age_minutes(self) -> Optional[float]:
        if not self.last_run_at:
            return None
        try:
            dt = datetime.fromisoformat(self.last_run_at)
            return (datetime.now() - dt).total_seconds() / 60
        except Exception:
            return None

    def format_row(self) -> str:
        age = f"{self.age_minutes:.0f}分前" if self.age_minutes is not None else "—"
        lat = f"{self.latency_ms}ms" if self.latency_ms is not None else "—"
        err = f"{self.error_rate:.0%}" if self.error_rate >= 0 else "—"
        dq  = f"{self.data_quality:.0%}" if self.data_quality > 0 else "—"
        return f"{self.icon} {self.name:<20} | {age:<8} | {lat:<8} | {err:<6} | {dq}"

    def to_dict(self) -> dict:
        return {
            "name":          self.name,
            "status":        self.status,
            "icon":          self.icon,
            "last_run_at":   self.last_run_at,
            "latency_ms":    self.latency_ms,
            "error_rate":    self.error_rate,
            "data_quality":  self.data_quality,
            "last_error":    self.last_error,
            "note":          self.note,
            "age_minutes":   self.age_minutes,
        }


@dataclass
class SystemHealth:
    modules:              list[ModuleStatus] = field(default_factory=list)
    global_data_quality:  float = 0.0
    mock_ratio:           float = 0.0
    stale_ratio:          float = 0.0
    api_success_rate:     float = 1.0
    kill_switch_active:   bool  = False
    kill_switch_reason:   str   = ""
    ts:                   str   = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def overall_status(self) -> str:
        if self.kill_switch_active:
            return "red"
        reds    = sum(1 for m in self.modules if m.status == "red")
        yellows = sum(1 for m in self.modules if m.status == "yellow")
        if reds > 0:
            return "red"
        if yellows > 2:
            return "yellow"
        return "green"

    def format_dashboard(self) -> str:
        icon = STATUS_ICON.get(self.overall_status, "⚪")
        lines = [
            f"{icon} 系統健康儀表板  {self.ts[:16]}",
            "",
            f"{'模組':<20} | {'上次執行':<8} | {'延遲':<8} | {'錯誤率':<6} | 資料品質",
            "─" * 65,
        ]
        for m in self.modules:
            lines.append(m.format_row())
        lines += [
            "",
            f"全系統資料可信度：{self.global_data_quality:.0%}",
            f"Mock 資料比例：{self.mock_ratio:.0%}（生產環境應為 0%）",
            f"Stale 資料比例：{self.stale_ratio:.0%}",
            f"API 成功率：{self.api_success_rate:.0%}",
        ]
        if self.kill_switch_active:
            lines.append(f"\n⛔ Kill Switch 啟動中：{self.kill_switch_reason}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "overall_status":     self.overall_status,
            "modules":            [m.to_dict() for m in self.modules],
            "global_data_quality": self.global_data_quality,
            "mock_ratio":         self.mock_ratio,
            "stale_ratio":        self.stale_ratio,
            "api_success_rate":   self.api_success_rate,
            "kill_switch_active": self.kill_switch_active,
            "kill_switch_reason": self.kill_switch_reason,
            "ts":                 self.ts,
        }


async def collect_health() -> SystemHealth:
    """收集所有模組狀態"""
    modules: list[ModuleStatus] = []

    # ── TWSE API ──────────────────────────────────────────────────────────────
    t0 = time.monotonic()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
            ok = r.status_code == 200
        lat = int((time.monotonic() - t0) * 1000)
        modules.append(ModuleStatus(
            name="TWSE API", status="green" if ok else "red",
            latency_ms=lat, error_rate=0.0 if ok else 1.0, data_quality=0.99,
        ))
    except Exception as e:
        modules.append(ModuleStatus(name="TWSE API", status="red", last_error=str(e)[:60]))

    # ── FinMind API ───────────────────────────────────────────────────────────
    t0 = time.monotonic()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("https://api.finmindtrade.com/api/v4/info")
            ok = r.status_code == 200
        lat = int((time.monotonic() - t0) * 1000)
        modules.append(ModuleStatus(
            name="FinMind API", status="green" if ok else "yellow",
            latency_ms=lat, error_rate=0.0 if ok else 0.5, data_quality=0.92,
        ))
    except Exception as e:
        modules.append(ModuleStatus(name="FinMind API", status="red", last_error=str(e)[:60]))

    # ── Database ──────────────────────────────────────────────────────────────
    t0 = time.monotonic()
    try:
        from backend.models.database import AsyncSessionLocal
        from sqlalchemy import text
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        lat = int((time.monotonic() - t0) * 1000)
        modules.append(ModuleStatus(
            name="Database", status="green", latency_ms=lat, data_quality=1.0,
        ))
    except Exception as e:
        modules.append(ModuleStatus(name="Database", status="red", last_error=str(e)[:60]))

    # ── Kill Switch ───────────────────────────────────────────────────────────
    from quant.risk_kill_switch import status_dict
    ks = status_dict()
    modules.append(ModuleStatus(
        name        = "Kill Switch",
        status      = "red" if ks["kill_switch_active"] else "green",
        note        = ks.get("reason", "OFF") if ks["kill_switch_active"] else "OFF",
        data_quality = 0.0 if ks["kill_switch_active"] else 1.0,
    ))

    # ── Decision Engine（最後一次執行時間從 DB 取）────────────────────────────
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import AuditLog
        from sqlalchemy import select, func
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(func.max(AuditLog.created_at))
            )
            last_run = r.scalar()
        modules.append(ModuleStatus(
            name="Decision Engine",
            status="green" if last_run else "yellow",
            last_run_at=str(last_run) if last_run else None,
            data_quality=0.90,
        ))
    except Exception:
        modules.append(ModuleStatus(name="Decision Engine", status="unknown"))

    # ── 整體指標 ──────────────────────────────────────────────────────────────
    alive = [m for m in modules if m.status != "unknown"]
    avg_dq = sum(m.data_quality for m in alive) / len(alive) if alive else 0.0

    health = SystemHealth(
        modules              = modules,
        global_data_quality  = round(avg_dq, 3),
        mock_ratio           = 0.0,   # 從 DB 查詢可補充
        stale_ratio          = 0.0,
        api_success_rate     = sum(1 for m in modules if m.status == "green") / max(len(modules), 1),
        kill_switch_active   = ks["kill_switch_active"],
        kill_switch_reason   = ks.get("reason", ""),
    )

    # 寫 DB 日誌
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import SystemHealthLog
        import json
        async with AsyncSessionLocal() as db:
            db.add(SystemHealthLog(
                overall_status      = health.overall_status,
                modules_json        = json.dumps([m.to_dict() for m in modules], ensure_ascii=False),
                global_data_quality = health.global_data_quality,
                mock_ratio          = health.mock_ratio,
                stale_ratio         = health.stale_ratio,
                kill_switch_active  = health.kill_switch_active,
            ))
            await db.commit()
    except Exception:
        pass

    # 若有紅色模組 → 推送 LINE
    red_modules = [m for m in modules if m.status == "red" and m.name != "Kill Switch"]
    if red_modules:
        _push_health_alert(red_modules)

    return health


def _push_health_alert(red_modules: list[ModuleStatus]):
    """推送模組異常警告"""
    names = ", ".join(m.name for m in red_modules)
    text  = (
        f"🔴 系統模組異常\n\n"
        f"異常模組：{names}\n"
        f"時間：{datetime.now().strftime('%m/%d %H:%M')}\n\n"
        f"請檢查系統日誌"
    )
    try:
        import asyncio

        async def _send():
            try:
                import httpx
                from backend.models.database import settings, AsyncSessionLocal
                from backend.models.models import Subscriber
                from sqlalchemy import select
                token = settings.line_channel_access_token
                if not token:
                    return
                async with AsyncSessionLocal() as db:
                    r = await db.execute(
                        select(Subscriber).where(Subscriber.subscribed_morning == True)
                    )
                    subs = r.scalars().all()
                headers = {"Authorization": f"Bearer {token}"}
                async with httpx.AsyncClient(timeout=15) as c:
                    for sub in subs:
                        try:
                            await c.post(
                                "https://api.line.me/v2/bot/message/push",
                                json={"to": sub.line_user_id, "messages": [{"type": "text", "text": text}]},
                                headers=headers,
                            )
                        except Exception:
                            pass
            except Exception as e:
                logger.warning("[Health] LINE push failed: %s", e)

        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_send())
    except Exception:
        pass
