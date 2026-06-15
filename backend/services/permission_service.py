"""permission_service.py — 用戶角色與每日用量管控"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select, func
from loguru import logger

ADMIN_UID = "U54b6736befb5acc8bb350e5f085df5ff"

ROLE_LIMITS: dict[str, Optional[int]] = {
    "admin":   None,   # unlimited
    "premium": None,   # unlimited
    "basic":   None,   # unlimited — 所有用戶無使用次數限制
    "blocked": 0,
}

# 指令 → 最低需要的角色（僅保留系統管理指令限制）
ADMIN_CMDS = {
    "/agent", "/redeploy", "/logs",
    "/adduser", "/removeuser", "/userlist", "/userstats",
}
PREMIUM_CMDS: set = set()  # 已移除 Premium 限制，所有功能對所有用戶開放


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
        logger.warning("[permission] get_role failed: {}", e)
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
        logger.warning("[permission] daily_count failed: {}", e)
        return 0


async def log_usage(user_id: str, action: str) -> None:
    from ..models.database import AsyncSessionLocal
    from ..models.models import UsageLog

    try:
        async with AsyncSessionLocal() as db:
            db.add(UsageLog(user_id=user_id, action=action[:50]))
            await db.commit()
    except Exception as e:
        logger.warning("[permission] log_usage failed: {}", e)


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

    # Premium 限制已移除，所有用戶均可使用全部功能
    # 每日用量（blocked 為 0，其餘皆 None = 無限制）
    if role != "admin":
        limit = ROLE_LIMITS.get(role)
        if limit is not None and limit == 0:
            return False, "❌ 您的帳號已被停用\n如有疑問請聯繫管理員"

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
        logger.warning("[permission] get_all_users failed: {}", e)
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
        logger.warning("[permission] usage_stats failed: {}", e)
        return []
