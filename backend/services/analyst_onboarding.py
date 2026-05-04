"""
analyst_onboarding.py — YouTube 分析師入職流程

流程：
  1. 用戶貼上 YouTube URL（任何格式）
  2. 系統自動識別 channel_id + 抓取頻道資訊
  3. 生成審核摘要等待管理員核准
  4. 核准後進入 30 天沙盒追蹤期
  5. 沙盒期滿 + 達標 → 升級正式分析師
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from loguru import logger
from typing import Optional


# ── YouTube URL 格式解析 ──────────────────────────────────────────────────────

URL_PATTERNS = [
    # 標準頻道 ID: /channel/UCxxxxxxxx
    (r"youtube\.com/channel/(UC[\w-]{22})",      "channel_id"),
    # 自訂頻道名稱: /@handle 或 /c/name 或 /user/name
    (r"youtube\.com/@([\w.-]+)",                  "handle"),
    (r"youtube\.com/c/([\w.-]+)",                 "custom_name"),
    (r"youtube\.com/user/([\w.-]+)",              "username"),
    # 影片 URL（需再查 channel）
    (r"youtube\.com/watch\?v=([\w-]{11})",        "video_id"),
    (r"youtu\.be/([\w-]{11})",                    "video_id"),
    # 播放清單
    (r"youtube\.com/playlist\?list=(PL[\w-]+)",   "playlist_id"),
]


@dataclass
class ChannelPreview:
    """頻道預覽資訊（供審核用）"""
    channel_id:      str
    channel_url:     str
    title:           str
    description:     str
    subscriber_count: Optional[int]
    video_count:      Optional[int]
    view_count:       Optional[int]
    country:          str
    keywords:         list[str] = field(default_factory=list)
    recent_topics:    list[str] = field(default_factory=list)  # 從最近影片標題推斷
    auto_specialty:   str = ""    # 系統自動推斷的專長
    auto_style:       str = ""    # 系統自動推斷的風格
    raw_url:          str = ""
    fetched_at:       str = field(default_factory=lambda: datetime.now().isoformat())

    def format_review(self) -> str:
        subs = f"{self.subscriber_count:,}" if self.subscriber_count else "未知"
        vids = str(self.video_count)          if self.video_count     else "未知"
        lines = [
            f"📺 頻道審核摘要",
            f"─────────────────",
            f"頻道：{self.title}",
            f"Channel ID：{self.channel_id}",
            f"訂閱數：{subs}",
            f"影片數：{vids}",
            f"地區：{self.country or '未知'}",
            f"",
            f"系統推斷：",
            f"  專長：{self.auto_specialty or '待確認'}",
            f"  風格：{self.auto_style or '待確認'}",
        ]
        if self.recent_topics:
            lines.append(f"  近期話題：{'、'.join(self.recent_topics[:5])}")
        if self.keywords:
            lines.append(f"  關鍵字：{'、'.join(self.keywords[:5])}")
        lines += [
            f"",
            f"🔗 {self.channel_url}",
            f"",
            f"請輸入「/analyst approve {self.channel_id}」核准",
            f"或「/analyst reject {self.channel_id}」拒絕",
        ]
        return "\n".join(lines)


def parse_youtube_url(url: str) -> tuple[str, str]:
    """
    解析 YouTube URL，回傳 (identifier, id_type)
    id_type: channel_id / handle / custom_name / username / video_id
    """
    url = url.strip()
    for pattern, id_type in URL_PATTERNS:
        m = re.search(pattern, url, re.IGNORECASE)
        if m:
            return m.group(1), id_type
    return "", "unknown"


async def resolve_channel_id(identifier: str, id_type: str) -> str:
    """
    將 handle/video_id 轉換為真正的 channel_id（UCxxxxxxxx）。
    需要 YouTube Data API v3。
    """
    api_key = os.getenv("YOUTUBE_API_KEY", "")
    if not api_key:
        # 無 API key → 若已是 channel_id 直接回傳，否則用 handle 當 id
        if id_type == "channel_id":
            return identifier
        return f"handle_{identifier}"

    import httpx
    base = "https://www.googleapis.com/youtube/v3"

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            if id_type == "video_id":
                r = await c.get(f"{base}/videos", params={
                    "part": "snippet", "id": identifier, "key": api_key
                })
                items = r.json().get("items", [])
                if items:
                    return items[0]["snippet"]["channelId"]

            elif id_type == "handle":
                # YouTube API v3: 用 forHandle 查詢
                r = await c.get(f"{base}/channels", params={
                    "part": "id", "forHandle": identifier, "key": api_key
                })
                items = r.json().get("items", [])
                if items:
                    return items[0]["id"]

            elif id_type in ("custom_name", "username"):
                r = await c.get(f"{base}/channels", params={
                    "part": "id", "forUsername": identifier, "key": api_key
                })
                items = r.json().get("items", [])
                if items:
                    return items[0]["id"]

            elif id_type == "channel_id":
                return identifier

    except Exception as e:
        logger.warning("[onboarding] resolve_channel_id failed: %s", e)

    return identifier  # fallback


# ── 話題/專長自動推斷 ──────────────────────────────────────────────────────────

_TOPIC_KEYWORDS = {
    "半導體": ["tsmc", "台積電", "半導體", "晶片", "wafer", "fab"],
    "AI伺服器": ["ai", "server", "伺服器", "nvidia", "輝達", "算力"],
    "散熱": ["散熱", "液冷", "heat", "thermal", "cooling"],
    "PCB": ["pcb", "基板", "印刷電路板", "abf"],
    "存股": ["存股", "高股息", "定存", "etf", "殖利率"],
    "籌碼": ["籌碼", "外資", "法人", "主力", "分點"],
    "總經": ["聯準會", "fed", "利率", "通膨", "gdp", "景氣"],
    "航運": ["航運", "貨輪", "bdi", "散裝", "貨櫃"],
    "金融": ["金融", "銀行", "保險", "壽險", "金控"],
    "電動車": ["電動車", "ev", "tesla", "特斯拉", "電池"],
}

_STYLE_KEYWORDS = {
    "momentum": ["動能", "突破", "飆股", "強勢", "趨勢"],
    "value":    ["低估", "價值", "本益比", "便宜", "存股"],
    "chip":     ["籌碼", "外資", "法人", "主力分點"],
    "fundamental": ["財報", "營收", "eps", "獲利", "基本面"],
    "macro":    ["總經", "大盤", "市場", "景氣", "fed"],
}


def infer_specialty_and_style(
    titles: list[str], description: str = ""
) -> tuple[str, str]:
    """從影片標題 + 頻道描述推斷專長和風格"""
    text = " ".join(titles + [description]).lower()

    topic_hits: dict[str, int] = {}
    for topic, keywords in _TOPIC_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in text)
        if hits:
            topic_hits[topic] = hits

    specialty = ",".join(
        sorted(topic_hits, key=lambda t: -topic_hits[t])[:3]
    )

    style_hits: dict[str, int] = {}
    for style, keywords in _STYLE_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in text)
        if hits:
            style_hits[style] = hits

    best_style = max(style_hits, key=lambda s: style_hits[s]) if style_hits else ""
    return specialty, best_style


async def fetch_channel_preview(channel_id: str, raw_url: str = "") -> Optional[ChannelPreview]:
    """
    從 YouTube API 取得頻道資訊。
    若無 API key，回傳僅含 channel_id 的最小預覽。
    """
    api_key = os.getenv("YOUTUBE_API_KEY", "")
    title = ""
    description = ""
    subscriber_count = None
    video_count = None
    view_count = None
    country = ""
    keywords: list[str] = []
    recent_titles: list[str] = []

    if api_key:
        try:
            import httpx
            base = "https://www.googleapis.com/youtube/v3"
            async with httpx.AsyncClient(timeout=15) as c:
                # 頻道基本資訊
                r = await c.get(f"{base}/channels", params={
                    "part": "snippet,statistics,brandingSettings",
                    "id": channel_id, "key": api_key,
                })
                items = r.json().get("items", [])
                if items:
                    s = items[0]["snippet"]
                    st = items[0].get("statistics", {})
                    bs = items[0].get("brandingSettings", {}).get("channel", {})
                    title       = s.get("title", "")
                    description = s.get("description", "")[:300]
                    country     = s.get("country", "")
                    keywords    = [kw.strip() for kw in bs.get("keywords", "").split(",") if kw.strip()][:10]
                    subscriber_count = int(st.get("subscriberCount", 0) or 0)
                    video_count      = int(st.get("videoCount", 0) or 0)
                    view_count       = int(st.get("viewCount", 0) or 0)

                # 最近 10 部影片標題
                r2 = await c.get(f"{base}/search", params={
                    "part": "snippet", "channelId": channel_id,
                    "order": "date", "maxResults": 10, "type": "video",
                    "key": api_key,
                })
                for item in r2.json().get("items", []):
                    recent_titles.append(item["snippet"].get("title", ""))
        except Exception as e:
            logger.warning("[onboarding] fetch_channel_preview API failed: %s", e)

    # fallback：channel_id 就是名稱
    if not title:
        title = f"YouTube 頻道 {channel_id[:12]}..."

    auto_specialty, auto_style = infer_specialty_and_style(recent_titles, description)

    channel_url = raw_url or f"https://www.youtube.com/channel/{channel_id}"
    return ChannelPreview(
        channel_id       = channel_id,
        channel_url      = channel_url,
        title            = title,
        description      = description,
        subscriber_count = subscriber_count,
        video_count      = video_count,
        view_count       = view_count,
        country          = country,
        keywords         = keywords,
        recent_topics    = recent_titles[:5],
        auto_specialty   = auto_specialty,
        auto_style       = auto_style,
        raw_url          = raw_url,
    )


# ── 審核暫存（in-memory，等待管理員核准）──────────────────────────────────────

_pending_reviews: dict[str, ChannelPreview] = {}


async def start_review(url: str) -> tuple[Optional[ChannelPreview], str]:
    """
    主入口：接收 URL → 解析 → 抓頻道資訊 → 進入待審核佇列
    回傳 (ChannelPreview, error_msg)
    """
    identifier, id_type = parse_youtube_url(url)
    if not identifier or id_type == "unknown":
        return None, "無法識別 YouTube URL 格式"

    channel_id = await resolve_channel_id(identifier, id_type)
    if not channel_id:
        return None, "無法取得頻道 ID"

    # 檢查是否已在正式清單
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import Analyst
        from sqlalchemy import select
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(Analyst).where(Analyst.channel_id == channel_id)
            )
            if r.scalar_one_or_none():
                return None, f"此頻道已在追蹤清單中（{channel_id}）"
    except Exception:
        pass

    preview = await fetch_channel_preview(channel_id, raw_url=url)
    if not preview:
        return None, "頻道資訊取得失敗"

    _pending_reviews[channel_id] = preview
    logger.info("[onboarding] pending review: %s (%s)", preview.title, channel_id)
    return preview, ""


async def approve_channel(channel_id: str, override_name: str = "",
                          override_specialty: str = "") -> tuple[bool, str]:
    """
    管理員核准 → 以 sandbox 模式新增分析師（is_sandbox=True, tier=B）
    """
    preview = _pending_reviews.get(channel_id)
    if not preview:
        # 嘗試重新抓取
        preview = await fetch_channel_preview(channel_id)
        if not preview:
            return False, f"找不到待審核的頻道 {channel_id}"

    name      = override_name      or preview.title or channel_id
    specialty = override_specialty or preview.auto_specialty or ""
    style     = preview.auto_style

    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import Analyst, AnalystSandbox
        from sqlalchemy import select

        analyst_id = f"yt_{channel_id[:20]}"

        async with AsyncSessionLocal() as db:
            # 避免重複
            r = await db.execute(
                select(Analyst).where(Analyst.analyst_id == analyst_id)
            )
            if r.scalar_one_or_none():
                return False, f"分析師 {analyst_id} 已存在"

            # 正式 Analyst 記錄（tier=B, sandbox 模式）
            db.add(Analyst(
                analyst_id  = analyst_id,
                name        = name,
                channel_id  = channel_id,
                channel_url = preview.channel_url,
                specialty   = specialty,
                tier        = "B",
                style       = style,
                is_active   = True,
                notes       = f"sandbox_start={datetime.now().strftime('%Y-%m-%d')}",
                added_date  = datetime.now().strftime("%Y-%m-%d"),
            ))

            # Sandbox 追蹤記錄
            sandbox_end = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
            db.add(AnalystSandbox(
                analyst_id   = analyst_id,
                channel_id   = channel_id,
                channel_name = name,
                sandbox_start = datetime.now().strftime("%Y-%m-%d"),
                sandbox_end  = sandbox_end,
                status       = "active",
            ))
            await db.commit()

        _pending_reviews.pop(channel_id, None)
        logger.info("[onboarding] approved: %s → sandbox until %s", name, sandbox_end)
        return True, f"✅ 已核准「{name}」，進入 30 天沙盒追蹤（至 {sandbox_end}）"

    except Exception as e:
        logger.error("[onboarding] approve failed: %s", e)
        return False, f"核准失敗：{e}"


async def reject_channel(channel_id: str, reason: str = "") -> str:
    preview = _pending_reviews.pop(channel_id, None)
    name = preview.title if preview else channel_id
    logger.info("[onboarding] rejected: %s reason=%s", name, reason)
    return f"❌ 已拒絕「{name}」{('：' + reason) if reason else ''}"


def list_pending() -> list[ChannelPreview]:
    return list(_pending_reviews.values())
