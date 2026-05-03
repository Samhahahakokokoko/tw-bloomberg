"""訂閱方案管理 + 推薦系統 + 客服"""
from __future__ import annotations

import random
import string
from datetime import datetime, timedelta

from loguru import logger

PLANS = {
    "free":     {"name": "免費版",     "price": 0,   "features": ["基本報價", "每日大盤", "3檔自選股"]},
    "standard": {"name": "標準版",     "price": 299, "features": ["所有選股", "AI個股分析", "無限自選股", "每日決策報告"]},
    "pro":      {"name": "專業版",     "price": 999, "features": ["所有功能", "聰明錢追蹤", "Fugle自動交易", "策略客製化"]},
}

PLAN_PERMISSIONS = {
    "free":     {"screener": False, "ai_analysis": True,  "smart_money": False, "auto_trade": False, "watchlist_limit": 3},
    "standard": {"screener": True,  "ai_analysis": True,  "smart_money": False, "auto_trade": False, "watchlist_limit": 999},
    "pro":      {"screener": True,  "ai_analysis": True,  "smart_money": True,  "auto_trade": True,  "watchlist_limit": 999},
}


async def get_user_plan(uid: str) -> dict:
    """取得用戶目前方案"""
    try:
        from ..models.database import AsyncSessionLocal
        from ..models.models import UserSubscription
        from sqlalchemy import select
        async with AsyncSessionLocal() as db:
            r   = await db.execute(select(UserSubscription).where(UserSubscription.user_id == uid))
            sub = r.scalar_one_or_none()

        if sub is None:
            return {"plan": "free", "expires_at": None, "active": True}

        active = sub.expires_at is None or sub.expires_at > datetime.utcnow()
        plan   = sub.plan if active else "free"
        return {"plan": plan, "expires_at": sub.expires_at, "active": active}
    except Exception:
        return {"plan": "free", "expires_at": None, "active": True}


async def check_permission(uid: str, feature: str) -> bool:
    """檢查用戶是否有功能權限"""
    info = await get_user_plan(uid)
    plan = info.get("plan", "free")
    return PLAN_PERMISSIONS.get(plan, PLAN_PERMISSIONS["free"]).get(feature, False)


async def upgrade_plan(uid: str, plan: str, months: int = 1):
    """升級用戶方案"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import UserSubscription
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        r   = await db.execute(select(UserSubscription).where(UserSubscription.user_id == uid))
        sub = r.scalar_one_or_none()
        if sub is None:
            sub = UserSubscription(user_id=uid)
            db.add(sub)
        sub.plan       = plan
        sub.expires_at = datetime.utcnow() + timedelta(days=30 * months)
        sub.updated_at = datetime.utcnow()
        await db.commit()


def format_plan_info(plan_key: str) -> str:
    plan = PLANS.get(plan_key, PLANS["free"])
    lines = [
        f"📦 {plan['name']}",
        ("免費" if plan["price"] == 0 else f"NT${plan['price']}/月"),
        "─" * 18,
        "包含功能：",
    ]
    for f in plan["features"]:
        lines.append(f"✅ {f}")
    return "\n".join(lines)


# ── 推薦系統 ──────────────────────────────────────────────────────────────────

def _generate_code() -> str:
    return "TW" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


async def get_or_create_referral(uid: str) -> str:
    """取得或建立用戶推薦碼"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import ReferralCode
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        r   = await db.execute(select(ReferralCode).where(ReferralCode.user_id == uid))
        rec = r.scalar_one_or_none()
        if rec is None:
            code = _generate_code()
            rec  = ReferralCode(user_id=uid, code=code)
            db.add(rec)
            await db.commit()
            await db.refresh(rec)
        return rec.code


async def apply_referral(uid: str, code: str) -> bool:
    """使用推薦碼（雙方各獲得1個月免費）"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import ReferralCode
    from sqlalchemy import select
    try:
        async with AsyncSessionLocal() as db:
            r     = await db.execute(select(ReferralCode).where(ReferralCode.code == code))
            ref   = r.scalar_one_or_none()
            if ref is None or ref.user_id == uid:
                return False
            ref.referrals    += 1
            ref.bonus_months += 1
            await db.commit()
        # 被推薦人升級1個月標準版
        await upgrade_plan(uid, "standard", months=1)
        # 推薦人也加一個月
        await upgrade_plan(ref.user_id, "standard", months=1)
        return True
    except Exception as e:
        logger.error(f"[referral] apply failed: {e}")
        return False


# ── 客服系統 ──────────────────────────────────────────────────────────────────

async def submit_feedback(uid: str, content: str, kind: str = "feedback"):
    """提交用戶回饋給管理員"""
    import os, httpx
    admin_uid = os.getenv("ADMIN_LINE_UID", "")
    if not admin_uid:
        logger.info(f"[feedback] uid={uid[:8]} kind={kind}: {content[:80]}")
        return

    from ..models.database import settings
    icon = {"feedback": "💬", "bug": "🐛", "help": "❓"}.get(kind, "📩")
    text = (
        f"{icon} 用戶回饋 [{kind.upper()}]\n"
        f"用戶：{uid[:12]}\n"
        f"時間：{datetime.now().strftime('%m/%d %H:%M')}\n"
        f"─" * 18 + f"\n{content[:300]}"
    )
    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            await c.post(
                "https://api.line.me/v2/bot/message/push",
                json={"to": admin_uid, "messages": [{"type": "text", "text": text}]},
                headers=headers,
            )
        except Exception as e:
            logger.warning(f"[feedback] push failed: {e}")
