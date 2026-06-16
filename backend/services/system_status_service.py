"""System Status Service — 詳細系統健康監控"""
from __future__ import annotations

import time
import os
from loguru import logger
import asyncio

_cache: dict = {}
_cache_ts: float = 0.0
_TTL = 120  # 2 分鐘（狀態需要即時）

# 推播計數追蹤（模組級別）
_push_count_today: int = 0
_push_count_date: str = ""
_last_push_time: str = ""


def record_push(context: str = "") -> None:
    """每次推播後呼叫此函式記錄"""
    global _push_count_today, _push_count_date, _last_push_time
    today = time.strftime("%Y-%m-%d")
    if today != _push_count_date:
        _push_count_today = 0
        _push_count_date  = today
    _push_count_today += 1
    _last_push_time = time.strftime("%H:%M")


async def get_system_status() -> dict:
    global _cache, _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < _TTL:
        return _cache
    result = await _fetch_system_status()
    _cache = result
    _cache_ts = now
    return result


async def _fetch_system_status() -> dict:
    import httpx, asyncio

    # ── 1. API 連線狀態 ──────────────────────────────────────────────────────
    async def check_yahoo():
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/2330.TW"
            async with httpx.AsyncClient(timeout=6) as c:
                r = await c.get(url, params={"interval": "1d", "range": "1d"},
                                headers={"User-Agent": "Mozilla/5.0"})
            return {"name": "Yahoo Finance", "ok": r.is_success, "latency_ms": None}
        except Exception as e:
            return {"name": "Yahoo Finance", "ok": False, "error": str(e)[:50]}

    async def check_railway():
        base = os.getenv("RAILWAY_BACKEND_URL", "")
        if not base:
            return {"name": "Railway API", "ok": None, "note": "URL未設定"}
        try:
            t0 = time.monotonic()
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(f"{base}/health")
            ms = int((time.monotonic() - t0) * 1000)
            return {"name": "Railway API", "ok": r.is_success, "latency_ms": ms}
        except Exception as e:
            return {"name": "Railway API", "ok": False, "error": str(e)[:50]}

    async def check_line_api():
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
        if not token:
            return {"name": "LINE API", "ok": None, "note": "Token未設定"}
        try:
            t0 = time.monotonic()
            async with httpx.AsyncClient(timeout=6) as c:
                r = await c.get(
                    "https://api.line.me/v2/bot/info",
                    headers={"Authorization": f"Bearer {token}"},
                )
            ms = int((time.monotonic() - t0) * 1000)
            return {"name": "LINE API", "ok": r.is_success, "latency_ms": ms}
        except Exception as e:
            return {"name": "LINE API", "ok": False, "error": str(e)[:50]}

    async def check_anthropic():
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            return {"name": "Claude AI", "ok": None, "note": "Key未設定"}
        # 只檢查 key 格式，不實際呼叫 API 浪費費用
        ok = key.startswith("sk-ant-")
        return {"name": "Claude AI", "ok": ok, "note": "Key格式" + ("正確" if ok else "異常")}

    async def check_db():
        try:
            from ..models.database import AsyncSessionLocal
            from sqlalchemy import text
            t0 = time.monotonic()
            async with AsyncSessionLocal() as db:
                await db.execute(text("SELECT 1"))
            ms = int((time.monotonic() - t0) * 1000)
            return {"name": "資料庫", "ok": True, "latency_ms": ms}
        except Exception as e:
            return {"name": "資料庫", "ok": False, "error": str(e)[:50]}

    apis = await asyncio.gather(
        check_yahoo(), check_railway(), check_line_api(),
        check_anthropic(), check_db(),
        return_exceptions=True,
    )

    services = []
    for a in apis:
        if isinstance(a, dict):
            services.append(a)

    # ── 2. 資料庫統計 ────────────────────────────────────────────────────────
    db_stats = {}
    try:
        from ..models.database import AsyncSessionLocal
        from sqlalchemy import text
        async with AsyncSessionLocal() as db:
            tables = ["portfolio", "alerts", "journal_entries", "analyst_calls"]
            for table in tables:
                try:
                    result = await db.execute(text(f"SELECT COUNT(*) FROM {table}"))
                    cnt = result.scalar()
                    db_stats[table] = cnt
                except Exception as e:
                    db_stats[table] = "N/A"
    except Exception as e:
        logger.debug(f"[sysstatus] db_stats: {e}")

    # ── 3. 排程器狀態 ────────────────────────────────────────────────────────
    scheduler_info = {}
    try:
        from ..main import app
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler:
            jobs = scheduler.get_jobs()
            scheduler_info = {
                "running":   scheduler.running,
                "job_count": len(jobs),
            }
    except Exception as e:
        scheduler_info = {"running": None, "job_count": None}

    # ── 4. 記憶體快取統計 ────────────────────────────────────────────────────
    cache_stats = {}
    cache_services = [
        ("feargreed", "feargreed_service"),
        ("chiphealth", "chiphealth_service"),
        ("market_breadth", "market_breadth_service"),
        ("breaking_news", "breaking_news_service"),
    ]
    for label, module in cache_services:
        try:
            import importlib
            svc = importlib.import_module(f"..services.{module}", package=__name__)
            cache_cnt = len(getattr(svc, "_cache", {}))
            cache_stats[label] = cache_cnt
        except Exception as e:
            cache_stats[label] = "N/A"

    # ── 5. 整體健康評級 ──────────────────────────────────────────────────────
    ok_count  = sum(1 for s in services if s.get("ok") is True)
    err_count = sum(1 for s in services if s.get("ok") is False)
    if err_count == 0:
        overall = "ok"
        health_label = "✅ 所有系統正常"
    elif err_count == 1:
        overall = "degraded"
        health_label = "⚠️ 部分服務異常"
    else:
        overall = "error"
        health_label = "🔴 多項服務異常，請立即檢查"

    return {
        "overall":        overall,
        "health_label":   health_label,
        "services":       services,
        "db_stats":       db_stats,
        "scheduler":      scheduler_info,
        "cache_stats":    cache_stats,
        "push_today":     _push_count_today,
        "last_push":      _last_push_time,
        "env_check": {
            "LINE_TOKEN":     bool(os.getenv("LINE_CHANNEL_ACCESS_TOKEN")),
            "ANTHROPIC_KEY":  bool(os.getenv("ANTHROPIC_API_KEY")),
            "ADMIN_UID":      bool(os.getenv("ADMIN_LINE_UID")),
            "RAILWAY_URL":    bool(os.getenv("RAILWAY_BACKEND_URL")),
            "DATABASE_URL":   bool(os.getenv("DATABASE_URL")),
        },
        "updated_at":     time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def format_system_status_report(data: dict) -> str:
    if data.get("error"):
        return f"❌ {data['error']}"

    label    = data.get("health_label", "")
    services = data.get("services", [])
    db_stats = data.get("db_stats", {})
    sched    = data.get("scheduler", {})
    push     = data.get("push_today", 0)
    last_p   = data.get("last_push", "")
    env      = data.get("env_check", {})
    updated  = data.get("updated_at", "")

    lines = [
        "🖥️ 系統健康監控",
        "─" * 32,
        f"{label}",
        f"更新：{updated}",
        "",
        "── API 連線狀態 ──",
    ]

    for svc in services:
        if svc.get("ok") is True:
            icon = "✅"
        elif svc.get("ok") is False:
            icon = "❌"
        else:
            icon = "⚠️"
        lat = f"  {svc['latency_ms']}ms" if svc.get("latency_ms") else ""
        note = f"  {svc.get('note', '') or svc.get('error', '')}"
        lines.append(f"  {icon} {svc['name']}{lat}{note}")

    lines += ["", "── 資料庫統計 ──"]
    label_map = {
        "portfolio": "持倉記錄",
        "alerts": "警報設定",
        "journal_entries": "投資日記",
        "analyst_calls": "分析師記錄",
    }
    for tbl, cnt in db_stats.items():
        lines.append(f"  📊 {label_map.get(tbl, tbl)}：{cnt} 筆")

    lines += ["", "── 排程器狀態 ──"]
    if sched.get("running") is not None:
        run_icon = "✅" if sched.get("running") else "❌"
        lines.append(f"  {run_icon} 排程器{'運行中' if sched.get('running') else '已停止'}")
        lines.append(f"  📋 排程任務數：{sched.get('job_count', 0)} 個")
    else:
        lines.append("  ⚠️ 無法取得排程器狀態")

    lines += [
        "",
        "── 今日推播統計 ──",
        f"  📨 今日推送：{push} 則",
    ]
    if last_p:
        lines.append(f"  🕐 最後推送：{last_p}")

    lines += ["", "── 環境變數檢查 ──"]
    for var, ok in env.items():
        icon = "✅" if ok else "❌"
        lines.append(f"  {icon} {var}")

    lines += [
        "",
        "─" * 28,
        "輸入 /status 查簡易狀態 | /health 健康儀表板",
    ]
    return "\n".join(lines)


async def push_alert_if_unhealthy() -> None:
    """如果系統異常，推播警報給管理員"""
    import os
    from .line_push import push_line_messages
    admin_uid = os.getenv("ADMIN_LINE_UID", "")
    if not admin_uid:
        return
    try:
        data = await get_system_status()
        if data.get("overall") in ("error", "degraded"):
            report = format_system_status_report(data)
            await push_line_messages(
                admin_uid,
                [{"type": "text", "text": f"⚠️ 系統警報！\n\n{report[:3000]}"}],
                context="sysstatus.alert",
            )
            logger.warning(f"[sysstatus] alert pushed, overall={data.get('overall')}")
    except Exception as e:
        logger.error(f"[sysstatus] push_alert: {e}")
