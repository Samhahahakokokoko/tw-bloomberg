"""YouTube Channel Seed — 初始化7大台股分析師頻道到資料庫"""
from __future__ import annotations

import os
from loguru import logger

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")

# 7 大追蹤頻道（handle → analyst info）
TRACKED_CHANNELS = [
    {
        "analyst_id":  "win16888",
        "name":        "林鈺凱分析師-摩爾證券投顧",
        "handle":      "win16888",
        "channel_id":  "UC9Pd7LN9potuHVafJCLX7Pw",
        "channel_url": "https://www.youtube.com/@win16888",
        "specialty":   "台股,大盤,短線,籌碼",
        "style":       "momentum",
        "tier":        "A",
    },
    {
        "analyst_id":  "s178",
        "name":        "郭哲榮分析師-摩爾證券投顧",
        "handle":      "s178",
        "channel_id":  "UChfl3auNxAxOR3wy8a8ysQQ",
        "channel_url": "https://www.youtube.com/@s178",
        "specialty":   "台股,波段,技術分析",
        "style":       "momentum",
        "tier":        "A",
    },
    {
        "analyst_id":  "ps1788",
        "name":        "林漢偉分析師-摩爾證券投顧",
        "handle":      "ps1788",
        "channel_id":  "UCleWOsRmPBhWPvQlSTy7fPw",
        "channel_url": "https://www.youtube.com/@ps1788",
        "specialty":   "台股,基本面,選股",
        "style":       "fundamental",
        "tier":        "A",
    },
    {
        "analyst_id":  "imoney168",
        "name":        "徐照興分析師-永誠國際投顧",
        "handle":      "imoney168",
        "channel_id":  "UC1Cj4kAK2fxOS23vELpq-Gg",
        "channel_url": "https://www.youtube.com/@imoney168",
        "specialty":   "台股,ETF,存股",
        "style":       "value",
        "tier":        "A",
    },
    {
        "analyst_id":  "we178",
        "name":        "鐘崑禎分析師-摩爾證券投顧",
        "handle":      "WE178",
        "channel_id":  "UCZn9BeImRq3SDLC8WVrVmUw",
        "channel_url": "https://www.youtube.com/@WE178",
        "specialty":   "台股,大盤,籌碼,外資",
        "style":       "chip",
        "tier":        "A",
    },
    {
        "analyst_id":  "oldwangstock",
        "name":        "老王愛說笑",
        "handle":      "oldwangstock",
        "channel_id":  "UCvnLmiWt_zIVIh0zUm_j4Hw",
        "channel_url": "https://www.youtube.com/@oldwangstock",
        "specialty":   "台股,盤勢分析,操盤",
        "style":       "momentum",
        "tier":        "A",
    },
    {
        "analyst_id":  "remus_boss",
        "name":        "雷老闆 Remus",
        "handle":      "remus_boss",
        "channel_id":  "UCFsyPpT525Fass_s7fA2qhg",
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
            # Use hardcoded channel_id from TRACKED_CHANNELS if available
            known_cid = ch.get("channel_id", "")
            if existing:
                # Always update to correct values
                if known_cid:
                    existing.channel_id = known_cid
                existing.channel_url = ch["channel_url"]
                existing.name        = ch["name"]
                existing.specialty   = ch["specialty"]
                existing.is_active   = True
                continue

            # Use hardcoded channel_id, fallback to API resolve
            channel_id = known_cid or await _resolve_channel_id(ch["handle"])

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
