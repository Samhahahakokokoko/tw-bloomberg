"""permission_service.py — 用戶角色與每日用量管控"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select, func
from loguru import logger

ADMIN_UID = "U54b6736befb5acc8bb350e5f085df5ff"

ROLE_LIMITS: dict[str, Optional[int]] = {
    "admin":   None,   # unlimited
    "premium": 50,
    "basic":   20,
    "blocked": 0,
}

# 指令 → 最低需要的角色
ADMIN_CMDS = {
    "/agent", "/redeploy", "/logs",
    "/adduser", "/removeuser", "/userlist", "/userstats",
}
PREMIUM_CMDS = {
    "/daily", "/report", "/analyst",
    "/analysis", "analysis", "/perf", "perf",
    "/chart", "chart", "/backtest",
    "/ai", "/rec", "/optimize", "/var",
    "/correlation", "/screener", "/pipeline",
    "/smart_money", "/smartmoney", "/morning",
    "/weekly", "/subscribe",
}
# basic 以上才能用：/p /buy /sell /alert /history /tax /quote /market /news
# 未列出的指令預設 basic 以上可用


async def get_role(user_id: str) -> str:
    """取得用戶角色，若不存在自動建立（admin 預設 admin，其餘 basic）"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import UserPermission

    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(UserPermission).where(UserPermission.user_id == user_id)
            )
            u = r.scalar_one_or_none()
            if not u:
                role = "admin" if user_id == ADMIN_UID else "basic"
                u = UserPermission(user_id=user_id, role=role)
                db.add(u)
                await db.commit()
                return role
            return u.role
    except Exception as e:
        logger.warning("[permission] get_role failed: %s", e)
        return "admin" if user_id == ADMIN_UID else "basic"


async def get_daily_count(user_id: str) -> int:
    """今日已使用次數"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import UsageLog

    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow    = today_start + timedelta(days=1)
    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(func.count()).where(
                    UsageLog.user_id == user_id,
                    UsageLog.created_at >= today_start,
                    UsageLog.created_at < tomorrow,
                )
            )
            return r.scalar() or 0
    except Exception as e:
        logger.warning("[permission] daily_count failed: %s", e)
        return 0


async def log_usage(user_id: str, action: str) -> None:
    from ..models.database import AsyncSessionLocal
    from ..models.models import UsageLog

    try:
        async with AsyncSessionLocal() as db:
            db.add(UsageLog(user_id=user_id, action=action[:50]))
            await db.commit()
    except Exception as e:
        logger.warning("[permission] log_usage failed: %s", e)


async def check_permission(user_id: str, cmd: str) -> tuple[bool, str]:
    """
    Returns (allowed, error_message).
    error_message is empty string when allowed.
    """
    role = await get_role(user_id)

    if role == "blocked":
        return False, "❌ 您的帳號已被停用\n如有疑問請聯繫管理員"

    if cmd in ADMIN_CMDS and role != "admin":
        return False, f"🔒 此功能僅限管理員使用"

    if cmd in PREMIUM_CMDS and role not in ("admin", "premium"):
        return False, (
            "⭐ 此功能需要 Premium 方案\n\n"
            f"指令：{cmd}\n"
            "目前方案：Basic（每日20次）\n\n"
            "如需升級請聯繫管理員"
        )

    # 每日用量（admin 跳過）
    if role != "admin":
        limit = ROLE_LIMITS.get(role, 20)
        if limit is not None:
            count = await get_daily_count(user_id)
            if count >= limit:
                return False, (
                    f"⏰ 今日使用次數已達上限（{limit}次）\n"
                    "明天再來！"
                )

    return True, ""


# ── 管理員操作 ────────────────────────────────────────────────────────────────

async def set_user_role(target_uid: str, role: str, admin_uid: str) -> dict:
    if admin_uid != ADMIN_UID:
        return {"ok": False, "error": "無管理員權限"}
    if role not in ROLE_LIMITS:
        return {"ok": False, "error": f"無效角色：{role}（可選 admin/premium/basic/blocked）"}

    from ..models.database import AsyncSessionLocal
    from ..models.models import UserPermission

    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(UserPermission).where(UserPermission.user_id == target_uid)
            )
            u = r.scalar_one_or_none()
            if u:
                u.role = role
                u.updated_at = datetime.utcnow()
            else:
                u = UserPermission(user_id=target_uid, role=role)
                db.add(u)
            await db.commit()
        return {"ok": True, "user_id": target_uid, "role": role}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def remove_user(target_uid: str, admin_uid: str) -> dict:
    if admin_uid != ADMIN_UID:
        return {"ok": False, "error": "無管理員權限"}

    from ..models.database import AsyncSessionLocal
    from ..models.models import UserPermission

    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(UserPermission).where(UserPermission.user_id == target_uid)
            )
            u = r.scalar_one_or_none()
            if not u:
                return {"ok": False, "error": "用戶不存在"}
            await db.delete(u)
            await db.commit()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def get_all_users() -> list[dict]:
    from ..models.database import AsyncSessionLocal
    from ..models.models import UserPermission

    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(UserPermission).order_by(UserPermission.role, UserPermission.created_at)
            )
            users = r.scalars().all()
        return [
            {
                "user_id":    u.user_id,
                "role":       u.role,
                "created_at": u.created_at.strftime("%Y-%m-%d") if u.created_at else "",
            }
            for u in users
        ]
    except Exception as e:
        logger.warning("[permission] get_all_users failed: %s", e)
        return []


async def get_usage_stats() -> list[dict]:
    from ..models.database import AsyncSessionLocal
    from ..models.models import UsageLog

    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow    = today_start + timedelta(days=1)
    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(UsageLog.user_id, func.count().label("cnt"))
                .where(
                    UsageLog.created_at >= today_start,
                    UsageLog.created_at < tomorrow,
                )
                .group_by(UsageLog.user_id)
                .order_by(func.count().desc())
                .limit(20)
            )
            return [{"user_id": row[0], "count": row[1]} for row in r.all()]
    except Exception as e:
        logger.warning("[permission] usage_stats failed: %s", e)
        return []
