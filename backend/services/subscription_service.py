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

    from .line_push import push_line_messages
    icons = {"feedback": "💬", "bug": "🐛"}
    icon  = icons.get(kind, "📩")
    text  = (
        f"{icon} 用戶{'回饋' if kind == 'feedback' else '問題回報'}\n"
        f"用戶：{uid[:12]}\n"
        f"時間：{datetime.now().strftime('%m/%d %H:%M')}\n"
        f"{'─' * 18}\n{content[:300]}"
    )
    await push_line_messages(
        admin_uid,
        [{"type": "text", "text": text}],
        timeout=10,
        context="feedback",
    )
