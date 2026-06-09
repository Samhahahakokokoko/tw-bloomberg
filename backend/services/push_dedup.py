"""LINE 推送去重模組

各類型週期限制：
  morning        → 每天 1 次（period_key = YYYY-MM-DD）
  daily          → 每天 1 次
  weekly         → 每週 1 次（period_key = YYYY-WNN）
  analyst        → 每天 1 次
  alert          → 每天同 content_hash 只推 1 次（stock+condition 相同才算重複）
  default        → 每天 1 次
"""
import hashlib
from datetime import datetime, timezone
from loguru import logger

_WEEKLY_TYPES = {"weekly"}


def _period_key(message_type: str) -> str:
    now = datetime.now()
    if message_type in _WEEKLY_TYPES:
        return now.strftime("%Y-W%W")
    return now.strftime("%Y-%m-%d")


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:64]


async def check_and_record(
    user_id: str,
    message_type: str,
    content: str,
) -> bool:
    """
    檢查是否可以推送；若可以則寫入記錄並回傳 True，
    若已推過則回傳 False（呼叫方應跳過此次推送）。
    """
    from ..models.database import AsyncSessionLocal
    from ..models.models import PushLog
    from sqlalchemy import select

    period = _period_key(message_type)
    chash  = _hash(content)

    try:
        async with AsyncSessionLocal() as db:
            existing = await db.execute(
                select(PushLog).where(
                    PushLog.user_id      == user_id,
                    PushLog.message_type == message_type,
                    PushLog.period_key   == period,
                    PushLog.content_hash == chash,
                )
            )
            if existing.scalar_one_or_none():
                return False

            db.add(PushLog(
                user_id      = user_id,
                message_type = message_type,
                content_hash = chash,
                period_key   = period,
            ))
            await db.commit()
            return True
    except Exception as e:
        logger.warning("[push_dedup] check_and_record error (allow push): {}", e)
        return True  # fail-open: 有問題時允許推送，不阻塞


async def get_today_log(user_id: str | None = None) -> list[dict]:
    """取得今日（或指定用戶今日）的推送記錄，供 /pushlog 指令使用。"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import PushLog
    from sqlalchemy import select

    today = datetime.now().strftime("%Y-%m-%d")
    try:
        async with AsyncSessionLocal() as db:
            q = select(PushLog).where(PushLog.period_key == today)
            if user_id:
                q = q.where(PushLog.user_id == user_id)
            q = q.order_by(PushLog.pushed_at.desc()).limit(50)
            rows = (await db.execute(q)).scalars().all()
            return [
                {
                    "user_id":      r.user_id,
                    "message_type": r.message_type,
                    "period_key":   r.period_key,
                    "pushed_at":    str(r.pushed_at or "")[:16],
                }
                for r in rows
            ]
    except Exception as e:
        logger.warning("[push_dedup] get_today_log error: {}", e)
        return []
