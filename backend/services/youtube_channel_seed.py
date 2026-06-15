"""YouTube Channel Seed — 初始化7大台股分析師頻道到資料庫"""
from __future__ import annotations

import os
from loguru import logger

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")

# 7 大追蹤頻道（handle → analyst info）
TRACKED_CHANNELS = [
    {
        "analyst_id":  "win16888",
        "name":        "老王不教股",
        "handle":      "win16888",
        "channel_url": "https://www.youtube.com/@win16888",
        "specialty":   "台股,大盤,短線,籌碼",
        "style":       "momentum",
        "tier":        "A",
    },
    {
        "analyst_id":  "s178",
        "name":        "股市阿水",
        "handle":      "s178",
        "channel_url": "https://www.youtube.com/@s178",
        "specialty":   "台股,波段,技術分析",
        "style":       "momentum",
        "tier":        "A",
    },
    {
        "analyst_id":  "ps1788",
        "name":        "散戶知識王",
        "handle":      "ps1788",
        "channel_url": "https://www.youtube.com/@ps1788",
        "specialty":   "台股,基本面,選股",
        "style":       "fundamental",
        "tier":        "A",
    },
    {
        "analyst_id":  "imoney168",
        "name":        "iMoney愛錢進",
        "handle":      "imoney168",
        "channel_url": "https://www.youtube.com/@imoney168",
        "specialty":   "台股,ETF,存股",
        "style":       "value",
        "tier":        "A",
    },
    {
        "analyst_id":  "we178",
        "name":        "WE Stock 財經",
        "handle":      "WE178",
        "channel_url": "https://www.youtube.com/@WE178",
        "specialty":   "台股,大盤,籌碼,外資",
        "style":       "chip",
        "tier":        "A",
    },
    {
        "analyst_id":  "oldwangstock",
        "name":        "老王說市",
        "handle":      "oldwangstock",
        "channel_url": "https://www.youtube.com/@oldwangstock",
        "specialty":   "台股,盤勢分析,操盤",
        "style":       "momentum",
        "tier":        "A",
    },
    {
        "analyst_id":  "remus_boss",
        "name":        "Remus老闆",
        "handle":      "remus_boss",
        "channel_url": "https://www.youtube.com/@remus_boss",
        "specialty":   "台股,選股,波段,AI題材",
        "style":       "momentum",
        "tier":        "A",
    },
]


async def ensure_channels_seeded() -> int:
    """確保7個頻道已存在於資料庫，回傳新增數量"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Analyst
    from sqlalchemy import select
    import datetime

    added = 0
    async with AsyncSessionLocal() as db:
        for ch in TRACKED_CHANNELS:
            r = await db.execute(
                select(Analyst).where(Analyst.analyst_id == ch["analyst_id"])
            )
            existing = r.scalar_one_or_none()
            if existing:
                # Update channel_url and specialty if changed
                if not existing.channel_url:
                    existing.channel_url = ch["channel_url"]
                if not existing.specialty:
                    existing.specialty = ch["specialty"]
                # Try to resolve real channel_id if missing
                if not existing.channel_id:
                    cid = await _resolve_channel_id(ch["handle"])
                    if cid:
                        existing.channel_id = cid
                continue

            # Resolve real YouTube channel_id via API
            channel_id = await _resolve_channel_id(ch["handle"])

            a = Analyst(
                analyst_id        = ch["analyst_id"],
                name              = ch["name"],
                channel_url       = ch["channel_url"],
                channel_id        = channel_id or f"handle_{ch['handle']}",
                specialty         = ch["specialty"],
                style             = ch.get("style", "momentum"),
                tier              = ch.get("tier", "A"),
                source_type       = "youtube",
                is_active         = True,
                win_rate          = 0.70,
                reliability_score = 70.0,
                quality_score     = 0.7,
                added_date        = datetime.date.today().isoformat(),
            )
            db.add(a)
            added += 1

        await db.commit()

    if added:
        logger.info(f"[channel_seed] seeded {added} new YouTube channels")
    return added


async def _resolve_channel_id(handle: str) -> str:
    """呼叫 YouTube Data API v3 將 handle 轉換為 UCxx channel_id"""
    if not YOUTUBE_API_KEY:
        return ""
    try:
        import httpx
        url = "https://www.googleapis.com/youtube/v3/channels"
        async with httpx.AsyncClient(timeout=10) as cl:
            r = await cl.get(url, params={
                "part":      "id",
                "forHandle": handle,
                "key":       YOUTUBE_API_KEY,
            })
        data  = r.json()
        items = data.get("items", [])
        if items:
            return items[0]["id"]
    except Exception as e:
        logger.debug(f"[channel_seed] resolve {handle}: {e}")
    return ""


async def get_channel_ids() -> dict[str, str]:
    """回傳 analyst_id → channel_id 對照表（DB中已有的）"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Analyst
    from sqlalchemy import select

    result = {}
    async with AsyncSessionLocal() as db:
        ids = [ch["analyst_id"] for ch in TRACKED_CHANNELS]
        r   = await db.execute(
            select(Analyst.analyst_id, Analyst.channel_id)
            .where(Analyst.analyst_id.in_(ids))
        )
        for row in r.all():
            if row.channel_id:
                result[row.analyst_id] = row.channel_id
    return result
