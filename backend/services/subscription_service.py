"""簡化版客服回饋系統（無商業邏輯，純個人工具）"""
from __future__ import annotations

import os
from datetime import datetime
from loguru import logger


async def submit_feedback(uid: str, content: str, kind: str = "feedback"):
    """提交用戶回饋給管理員 LINE"""
    admin_uid = os.getenv("ADMIN_LINE_UID", "")
    if not admin_uid:
        logger.info(f"[feedback] uid={uid[:8]} kind={kind}: {content[:80]}")
        return

    import httpx
    from ..models.database import settings
    icons = {"feedback": "💬", "bug": "🐛"}
    icon  = icons.get(kind, "📩")
    text  = (
        f"{icon} 用戶{'回饋' if kind == 'feedback' else '問題回報'}\n"
        f"用戶：{uid[:12]}\n"
        f"時間：{datetime.now().strftime('%m/%d %H:%M')}\n"
        f"{'─' * 18}\n{content[:300]}"
    )
    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            await c.post(
                "https://api.line.me/v2/bot/message/push",
                json={"to": admin_uid, "messages": [{"type": "text", "text": text}]},
                headers=headers,
            )
        except Exception as e:
            logger.warning(f"[feedback] push failed: {e}")
