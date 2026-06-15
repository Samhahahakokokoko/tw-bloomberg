"""YouTube Alpha Engine — 抓取 YouTube 影片並用 Claude NLP 分析分析師觀點"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from loguru import logger
import asyncio

YOUTUBE_API_KEY  = os.getenv("YOUTUBE_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


async def fetch_transcript(video_id: str, max_chars: int = 3000) -> str:
    """用 youtube-transcript-api 抓取影片字幕，回傳純文字"""
    try:
        import asyncio as _asyncio
        from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

        def _get():
            try:
                segments = YouTubeTranscriptApi.get_transcript(
                    video_id, languages=["zh-TW", "zh-Hant", "zh", "zh-Hans", "en"]
                )
                return " ".join(s["text"] for s in segments)
            except (NoTranscriptFound, TranscriptsDisabled):
                return ""

        text = await _asyncio.to_thread(_get)
        return text[:max_chars] if text else ""
    except ImportError:
        logger.debug("[transcript] youtube-transcript-api not installed")
        return ""
    except Exception as e:
        logger.debug(f"[transcript] {video_id}: {e}")
        return ""

from ..utils.credit_guard import is_exhausted as _credit_exhausted, mark_exhausted as _mark_credit_exhausted

# 台股代碼正則（4-5碼數字，常見格式）
STOCK_CODE_RE = re.compile(r'\b([2-9]\d{3}[A-Z]?)\b')

# 情緒關鍵字
SENTIMENT_KEYWORDS = {
    "strong_bullish": ["強力看多", "大漲", "必買", "爆發", "強推", "all in", "百倍"],
    "bullish":        ["看多", "買進", "布局", "值得關注", "偏多", "有機會", "逢低買"],
    "bearish":        ["看空", "減碼", "謹慎", "風險高", "偏空", "可能回調", "注意"],
    "strong_bearish": ["強力看空", "大跌", "避開", "危險", "崩潰", "警告"],
}

TIMEFRAME_KEYWORDS = {
    "short":  ["短線", "今天", "本週", "這週", "超短", "當沖"],
    "medium": ["波段", "1個月", "季線", "中線", "這個月"],
    "long":   ["長期", "存股", "年線", "1年", "長線", "價值"],
}


@dataclass
class VideoAnalysis:
    video_id:    str
    title:       str
    channel_id:  str
    analyst_id:  str
    pub_date:    str
    stocks:      list[str]                    # 抽取到的股票代碼
    sentiment:   str = "neutral"
    timeframe:   str = "medium"
    key_points:  list[str] = field(default_factory=list)
    raw_text:    str = ""


async def fetch_channel_videos(channel_id: str, max_results: int = 5) -> list[dict]:
    """從 YouTube Data API 抓取最新影片"""
    if not YOUTUBE_API_KEY:
        logger.warning("[youtube] YOUTUBE_API_KEY not set, using mock data")
        return _mock_videos(channel_id)

    try:
        import httpx
        url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "key":        YOUTUBE_API_KEY,
            "channelId":  channel_id,
            "part":       "snippet",
            "type":       "video",
            "order":      "date",
            "maxResults": max_results,
        }
        async with httpx.AsyncClient(timeout=15) as c:
            r    = await c.get(url, params=params)
            data = r.json()

        videos = []
        for item in data.get("items", []):
            sn = item.get("snippet", {})
            videos.append({
                "video_id":   item["id"].get("videoId", ""),
                "title":      sn.get("title", ""),
                "description": sn.get("description", "")[:500],
                "pub_date":   sn.get("publishedAt", "")[:10],
            })
        return videos

    except Exception as e:
        logger.warning(f"[youtube] fetch failed for {channel_id}: {e}")
        return _mock_videos(channel_id)


def _mock_videos(channel_id: str) -> list[dict]:
    """無 API 時的 mock 影片資料"""
    today = datetime.now().strftime("%Y-%m-%d")
    return [
        {
            "video_id":    f"mock_{channel_id}_{today}",
            "title":       f"【今日分析】散熱族群 3443 3231 突破！AI伺服器題材持續發酵",
            "description": "今天重點分析散熱族群，3443 創意電子突破平台，外資連買3日，AI伺服器需求持續強勁，建議布局。",
            "pub_date":    today,
        }
    ]


async def analyze_with_claude(title: str, description: str, transcript: str = "") -> dict:
    """用 Claude API 做 NLP 分析，抽取股票和情緒（transcript 優先）"""
    if _credit_exhausted() or not ANTHROPIC_API_KEY:
        return _rule_based_analysis(title, description)

    content = transcript[:2500] if transcript else description[:500]
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        prompt = (
            f"你是台股分析助理，分析以下 YouTube 影片內容，以 JSON 格式回傳。\n\n"
            f"標題：{title}\n"
            f"{'字幕逐字稿：' + content if transcript else '影片描述：' + content}\n\n"
            f"請回傳 JSON，包含：\n"
            f"1. stocks: 提到的台股代碼列表（4碼數字，如 2330）\n"
            f"2. sentiment: strong_bullish/bullish/neutral/bearish/strong_bearish（今日整體看多/看空）\n"
            f"3. timeframe: short/medium/long（操作時間維度）\n"
            f"4. key_points: 最多3個關鍵論點（繁體中文，每點20字內）\n"
            f"5. summary: 200字以內的影片重點摘要（繁體中文）\n"
            f"6. operation: 操作建議一句話（20字內）\n\n"
            f"只回傳 JSON，不要 markdown 或其他說明。"
        )
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        text = re.sub(r"```json|```", "", text).strip()
        return json.loads(text)
    except Exception as e:
        if "credit balance is too low" in str(e):
            _mark_credit_exhausted()
            logger.warning("[youtube] Anthropic credit 耗盡，本 process 停止 AI 分析")
            try:
                from .system_monitor import log_module_status
                import asyncio
                asyncio.create_task(log_module_status(
                    "anthropic_ai", "error", "credit balance is too low — AI 分析已停用"
                ))
            except Exception as e:
                pass
        else:
            logger.warning(f"[youtube] claude analysis failed: {e}, using rule-based")
        return _rule_based_analysis(title, description)


def _rule_based_analysis(title: str, description: str) -> dict:
    """規則式分析（Claude API 不可用時的備用）"""
    text = (title + " " + description).lower()

    # 抽取股票代碼
    stocks = list(set(STOCK_CODE_RE.findall(title + " " + description)))[:5]

    # 情緒判斷
    sentiment = "neutral"
    for sent, keywords in SENTIMENT_KEYWORDS.items():
        if any(k in text for k in keywords):
            sentiment = sent
            break

    # 時間維度
    timeframe = "medium"
    for tf, keywords in TIMEFRAME_KEYWORDS.items():
        if any(k in text for k in keywords):
            timeframe = tf
            break

    # 關鍵論點（從標題抽取）
    key_points = []
    if "外資" in text:
        key_points.append("外資動向支撐")
    if any(k in text for k in ["突破", "創高", "漲停"]):
        key_points.append("技術面突破")
    if any(k in text for k in ["ai", "晶片", "伺服器"]):
        key_points.append("AI題材延燒")
    if not key_points:
        key_points = ["依影片標題判斷"]

    return {
        "stocks":     stocks,
        "sentiment":  sentiment,
        "timeframe":  timeframe,
        "key_points": key_points[:3],
    }


async def process_analyst_videos(analyst_id: str, channel_id: str,
                                  with_transcript: bool = False) -> list[VideoAnalysis]:
    """抓取並分析單一分析師的最新影片（可選擇是否抓字幕）"""
    videos  = await fetch_channel_videos(channel_id)
    results = []

    for v in videos:
        transcript = ""
        if with_transcript and v.get("video_id") and not v["video_id"].startswith("mock_"):
            transcript = await fetch_transcript(v["video_id"])

        analysis = await analyze_with_claude(v["title"], v.get("description", ""), transcript)
        stocks   = analysis.get("stocks", [])
        if not stocks:
            stocks = list(set(STOCK_CODE_RE.findall(v["title"] + " " + v.get("description", ""))))

        results.append(VideoAnalysis(
            video_id   = v["video_id"],
            title      = v["title"],
            channel_id = channel_id,
            analyst_id = analyst_id,
            pub_date   = v["pub_date"],
            stocks     = stocks[:5],
            sentiment  = analysis.get("sentiment", "neutral"),
            timeframe  = analysis.get("timeframe", "medium"),
            key_points = analysis.get("key_points", []),
            raw_text   = transcript[:500] if transcript else v["title"],
        ))
        # Attach extra fields from Claude
        results[-1].__dict__["summary"]   = analysis.get("summary", "")
        results[-1].__dict__["operation"] = analysis.get("operation", "")
        results[-1].__dict__["has_transcript"] = bool(transcript)

    return results


async def save_analyst_calls(analyses: list[VideoAnalysis]):
    """將分析結果存入資料庫（mock 影片不存入）"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import AnalystCall, Analyst
    from .twse_service import fetch_realtime_quote
    from sqlalchemy import select

    real_analyses = [va for va in analyses if not va.video_id.startswith("mock_")]
    if not real_analyses:
        logger.debug("[youtube] save_analyst_calls: all mock, skip DB write")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    async with AsyncSessionLocal() as db:
        for va in real_analyses:
            for stock_id in va.stocks:
                # 抓取當日收盤價
                entry_price = 0.0
                try:
                    q = await fetch_realtime_quote(stock_id)
                    entry_price = q.get("price", 0) if q else 0
                except Exception as e:
                    pass

                # 避免重複
                r = await db.execute(
                    select(AnalystCall)
                    .where(AnalystCall.date == today)
                    .where(AnalystCall.analyst_id == va.analyst_id)
                    .where(AnalystCall.stock_id == stock_id)
                )
                if r.scalar_one_or_none():
                    continue

                call = AnalystCall(
                    date        = today,
                    analyst_id  = va.analyst_id,
                    stock_id    = stock_id,
                    sentiment   = va.sentiment,
                    timeframe   = va.timeframe,
                    key_points  = json.dumps(va.key_points, ensure_ascii=False),
                    source_title = va.title[:200],
                    entry_price = entry_price,
                )
                db.add(call)

        # 更新分析師 total_calls
        r2 = await db.execute(select(Analyst).where(Analyst.analyst_id.in_(
            [a.analyst_id for a in real_analyses]
        )))
        for analyst in r2.scalars().all():
            analyst.total_calls += sum(len(a.stocks) for a in real_analyses if a.analyst_id == analyst.analyst_id)
            analyst.updated_at = datetime.utcnow()

        await db.commit()
    logger.info(f"[youtube] saved {len(real_analyses)} real video analyses (skipped {len(analyses)-len(real_analyses)} mock)")


async def run_daily_fetch():
    """每日 16:00 執行：抓取所有追蹤頻道的新影片"""
    from .analyst_tracker import get_all_analysts

    analysts = await get_all_analysts()
    total    = 0
    for a in analysts:
        cid = a.get("channel_id") or ""
        if not cid:
            logger.debug(f"[youtube] skip {a['name']} - no channel_id")
            continue
        try:
            analyses = await process_analyst_videos(a["analyst_id"], cid)
            await save_analyst_calls(analyses)
            total += len(analyses)
        except Exception as e:
            logger.warning(f"[youtube] {a['name']} failed: {e}")

    if total == 0:
        logger.warning("[youtube] daily fetch: no real analyses saved (API may have failed or no new videos)")

    logger.info(f"[youtube] daily fetch complete: {total} real analyses saved")
