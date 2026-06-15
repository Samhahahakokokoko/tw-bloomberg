"""YouTube Analyst Push Service — 每日08:00抓取新影片、分析並推播"""
from __future__ import annotations

import json
import time
from datetime import datetime, date, timedelta
from loguru import logger

_last_seen: dict[str, str] = {}   # analyst_id → last video_id (in-memory)

# 共識股快取（1小時TTL）
_consensus_cache: dict | None = None
_consensus_ts: float = 0.0
_CONSENSUS_TTL = 3600

SENTIMENT_LABEL = {
    "strong_bullish": "🔥 強力看多",
    "bullish":        "📈 看多",
    "neutral":        "⬜ 中性",
    "bearish":        "📉 看空",
    "strong_bearish": "💀 強力看空",
}


async def run_morning_check() -> dict:
    """每日 08:00 執行：檢查7大頻道是否有新影片並推播"""
    from .youtube_channel_seed import ensure_channels_seeded, TRACKED_CHANNELS
    from .youtube_alpha_engine import (
        fetch_channel_videos, fetch_transcript, analyze_with_claude, save_analyst_calls,
        process_analyst_videos, STOCK_CODE_RE, VideoAnalysis
    )

    # 確保頻道已入庫
    await ensure_channels_seeded()

    today_str = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    new_videos = []   # list of (analyst_info, video_dict, analysis_dict)

    for ch in TRACKED_CHANNELS:
        try:
            channel_id = await _get_channel_id(ch["analyst_id"])
            videos     = await fetch_channel_videos(channel_id, max_results=3)
            if not videos:
                continue

            latest = videos[0]
            vid_id = latest.get("video_id", "")
            pub    = latest.get("pub_date", "")

            # Only process if video is within the last 3 days (more lenient than 1 day)
            cutoff_date = (date.today() - timedelta(days=3)).isoformat()
            if pub < cutoff_date:
                logger.debug(f"[yt_push] {ch['name']} latest video too old: {pub}")
                continue

            # Check if we already processed this video
            if _last_seen.get(ch["analyst_id"]) == vid_id:
                logger.debug(f"[yt_push] {ch['name']} no new video")
                continue

            _last_seen[ch["analyst_id"]] = vid_id

            # Fetch transcript and analyze
            transcript = ""
            if not vid_id.startswith("mock_"):
                transcript = await fetch_transcript(vid_id)

            analysis = await analyze_with_claude(
                latest["title"], latest.get("description", ""), transcript
            )
            stocks = analysis.get("stocks", [])
            if not stocks:
                stocks = list(set(STOCK_CODE_RE.findall(
                    latest["title"] + " " + latest.get("description", "")
                )))

            va = VideoAnalysis(
                video_id   = vid_id,
                title      = latest["title"],
                channel_id = channel_id,
                analyst_id = ch["analyst_id"],
                pub_date   = pub,
                stocks     = stocks[:5],
                sentiment  = analysis.get("sentiment", "neutral"),
                timeframe  = analysis.get("timeframe", "medium"),
                key_points = analysis.get("key_points", []),
                raw_text   = transcript[:300] if transcript else latest["title"],
            )
            va.__dict__["summary"]        = analysis.get("summary", "")
            va.__dict__["operation"]      = analysis.get("operation", "")
            va.__dict__["analyst_name"]   = ch["name"]
            va.__dict__["has_transcript"] = bool(transcript)

            new_videos.append((ch, latest, va))

        except Exception as e:
            logger.warning(f"[yt_push] {ch['analyst_id']} error: {e}")

    if not new_videos:
        logger.info("[yt_push] no new videos today")
        return {"pushed": 0, "new_videos": 0}

    # Save to DB (only real videos, skip mocks)
    try:
        from .youtube_alpha_engine import save_analyst_calls
        real_videos = [nv for nv in new_videos if not nv[2].video_id.startswith("mock_")]
        if real_videos:
            await save_analyst_calls([nv[2] for nv in real_videos])
            # Invalidate consensus cache so next query reflects new data
            global _consensus_cache
            _consensus_cache = None
        else:
            logger.debug("[yt_push] all new_videos are mock, skip DB write")
    except Exception as e:
        logger.warning(f"[yt_push] save_analyst_calls: {e}")

    # Push notifications
    pushed = await _push_notifications(new_videos)
    logger.info(f"[yt_push] morning check done: {len(new_videos)} new videos, {pushed} pushes")
    return {"pushed": pushed, "new_videos": len(new_videos)}


async def _get_channel_id(analyst_id: str) -> str:
    """從DB取得分析師的channel_id"""
    try:
        from ..models.database import AsyncSessionLocal
        from ..models.models import Analyst
        from sqlalchemy import select
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(Analyst.channel_id).where(Analyst.analyst_id == analyst_id)
            )
            row = r.scalar_one_or_none()
            return row or f"handle_{analyst_id}"
    except Exception:
        return f"handle_{analyst_id}"


async def _push_notifications(new_videos: list) -> int:
    """推播新影片通知給所有用戶"""
    try:
        from .line_push import push_to_admin, push_to_all_users
        from .stock_favorites import get_all_user_ids
    except Exception as e:
        logger.warning(f"[yt_push] import push services: {e}")
        return 0

    msgs = [format_video_notification(ch, video, va) for ch, video, va in new_videos]
    combined = "\n\n".join(msgs[:4])[:4500]  # LINE message limit

    pushed = 0
    try:
        await push_to_admin(f"📺 YouTube 早報\n\n{combined}")
        pushed += 1
    except Exception as e:
        logger.warning(f"[yt_push] admin push: {e}")

    try:
        uids = await get_all_user_ids()
        for uid in uids[:50]:
            try:
                await push_to_all_users(uid, f"📺 分析師早報\n\n{combined[:2000]}")
                pushed += 1
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"[yt_push] user push: {e}")

    return pushed


def format_video_notification(ch: dict, video: dict, va) -> str:
    """格式化單一影片通知"""
    sent_label = SENTIMENT_LABEL.get(va.sentiment, "⬜ 中性")
    stocks_str = "  ".join(va.stocks) if va.stocks else "─"
    kp_str     = "\n".join(f"• {kp}" for kp in (va.key_points or [])[:3])
    summary    = getattr(va, "summary", "") or ""
    operation  = getattr(va, "operation", "") or ""
    has_trans  = getattr(va, "has_transcript", False)
    vid_url    = f"https://youtu.be/{va.video_id}" if not va.video_id.startswith("mock_") else ""

    lines = [
        f"📺 {ch['name']} 新影片",
        f"📌 {video['title'][:50]}",
        f"",
        f"方向：{sent_label}",
        f"點名股：{stocks_str}",
    ]
    if kp_str:
        lines += ["", "重點：", kp_str]
    if summary:
        lines += ["", f"摘要：{summary[:120]}"]
    if operation:
        lines += [f"建議：{operation}"]
    if vid_url:
        lines += ["", f"🔗 {vid_url}"]
    if has_trans:
        lines += ["（✅ 已分析字幕逐字稿）"]
    return "\n".join(lines)


async def get_latest_analyst_views() -> list[dict]:
    """取得7大分析師最新影片觀點（用於 /analyst 指令）"""
    from .youtube_channel_seed import TRACKED_CHANNELS
    from ..models.database import AsyncSessionLocal
    from ..models.models import AnalystCall, Analyst
    from sqlalchemy import select, desc

    results = []
    async with AsyncSessionLocal() as db:
        for ch in TRACKED_CHANNELS:
            try:
                r = await db.execute(
                    select(AnalystCall)
                    .where(AnalystCall.analyst_id == ch["analyst_id"])
                    .order_by(desc(AnalystCall.created_at))
                    .limit(1)
                )
                call = r.scalar_one_or_none()
                if call:
                    kp = []
                    try:
                        kp = json.loads(call.key_points or "[]")
                    except Exception:
                        pass
                    results.append({
                        "analyst_id":  ch["analyst_id"],
                        "name":        ch["name"],
                        "date":        call.date,
                        "sentiment":   call.sentiment,
                        "stocks":      [call.stock_id] if call.stock_id else [],
                        "key_points":  kp,
                        "source_title": call.source_title or "",
                        "channel_url": ch["channel_url"],
                    })
                else:
                    results.append({
                        "analyst_id":  ch["analyst_id"],
                        "name":        ch["name"],
                        "date":        "─",
                        "sentiment":   "neutral",
                        "stocks":      [],
                        "key_points":  [],
                        "source_title": "尚無記錄",
                        "channel_url": ch["channel_url"],
                    })
            except Exception as e:
                logger.debug(f"[yt_views] {ch['analyst_id']}: {e}")

    return results


def format_analyst_summary(views: list[dict]) -> str:
    """格式化所有分析師觀點彙整報告"""
    if not views:
        return "📺 YouTube 分析師追蹤\n\n尚無資料，請等待每日 08:00 自動抓取"

    bull_count  = sum(1 for v in views if "bullish" in v.get("sentiment", ""))
    bear_count  = sum(1 for v in views if "bearish" in v.get("sentiment", ""))
    neut_count  = len(views) - bull_count - bear_count

    # Most mentioned stocks
    from collections import Counter
    all_stocks = []
    for v in views:
        all_stocks.extend(v.get("stocks", []))
    top_stocks = Counter(all_stocks).most_common(5)

    lines = [
        "📺 YouTube 分析師觀點彙整",
        "─" * 32, "",
        f"📊 多空統計：看多 {bull_count} / 中性 {neut_count} / 看空 {bear_count}",
        "",
    ]

    if top_stocks:
        lines.append(f"🔥 被最多分析師點名：{'  '.join(f'{s}({c}人)' for s, c in top_stocks[:3])}")
        lines.append("")

    for v in views:
        sent  = SENTIMENT_LABEL.get(v["sentiment"], "⬜ 中性")
        title = v.get("source_title", "")[:35]
        kp    = v.get("key_points", [])
        stocks_str = "  ".join(v.get("stocks", [])[:3]) or "─"

        lines += [
            f"━━ {v['name']} ({v['date']})",
            f"方向：{sent}",
            f"股票：{stocks_str}",
        ]
        if kp:
            lines.append(f"重點：{kp[0][:30]}")
        if title:
            lines.append(f"影片：{title}")
        lines.append("")

    # AI verdict
    if bull_count > bear_count + 1:
        verdict = f"多位分析師偏多，市場情緒樂觀，建議關注被多人點名標的：{'、'.join(s for s, _ in top_stocks[:2])}。"
    elif bear_count > bull_count + 1:
        verdict = "多位分析師偏空，建議保守操作，降低持倉比例，等待明確訊號。"
    else:
        verdict = f"分析師看法分歧，多空均衡，建議以個股基本面和技術面為操作依據。"

    lines += [
        "─" * 28,
        "🤖 AI 綜合研判",
        verdict,
        "",
        "輸入 /analyst yt 更新 | /consensus 看共識股",
    ]
    return "\n".join(lines)


async def get_consensus_debug() -> str:
    """Debug: 顯示 analyst_calls 表狀態、快取資訊、最新資料"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import AnalystCall
    from sqlalchemy import select, func, desc
    import time as _time

    lines = ["🔍 /consensus debug", "─" * 30]
    try:
        async with AsyncSessionLocal() as db:
            # 總筆數
            r_count = await db.execute(select(func.count()).select_from(AnalystCall))
            total = r_count.scalar() or 0
            lines.append(f"analyst_calls 總筆數：{total}")
            lines.append("")

            if total == 0:
                lines.append("⚠️ 資料庫無任何分析師預測記錄")
                lines.append("可能原因：YouTube API 失敗或排程未執行")
            else:
                # 每日筆數（近7天）
                r_by_date = await db.execute(
                    select(AnalystCall.date, func.count().label("cnt"))
                    .group_by(AnalystCall.date)
                    .order_by(desc(AnalystCall.date))
                    .limit(7)
                )
                lines.append("每日筆數（近7天）：")
                for row in r_by_date.all():
                    lines.append(f"  {row.date}：{row.cnt} 筆")
                lines.append("")

                # 最新10筆
                r_latest = await db.execute(
                    select(AnalystCall)
                    .order_by(desc(AnalystCall.created_at))
                    .limit(10)
                )
                latest_rows = r_latest.scalars().all()
                lines.append("最新10筆記錄：")
                for c in latest_rows:
                    is_mock = "(mock)" if (c.source_title or "").startswith("【今日分析】散熱") else ""
                    lines.append(f"  {c.date} {c.analyst_id} {c.stock_id} {c.sentiment} {is_mock}")
                lines.append("")

                # 非mock筆數（今天）
                from datetime import date as _date
                today_str = _date.today().isoformat()
                r_today = await db.execute(
                    select(func.count()).select_from(AnalystCall)
                    .where(AnalystCall.date == today_str)
                )
                today_cnt = r_today.scalar() or 0
                lines.append(f"今日（{today_str}）筆數：{today_cnt}")

    except Exception as e:
        lines.append(f"❌ DB查詢失敗：{e}")

    # 快取狀態
    global _consensus_cache, _consensus_ts
    cache_age = int(_time.time() - _consensus_ts) if _consensus_ts else -1
    lines += ["", "快取狀態："]
    if _consensus_cache is not None and cache_age >= 0:
        lines.append(f"  有快取 / {cache_age}秒前更新 / TTL={_CONSENSUS_TTL}s")
        lines.append(f"  快取共識股：{len(_consensus_cache.get('stocks',[]))} 個")
    else:
        lines.append("  無快取（下次查詢會直接讀DB）")

    lines += ["", "排程：08:00 晨間推播 / 16:00 每日抓取"]
    lines.append("輸入 /consensus 看實際共識結果")
    return "\n".join(lines)


async def get_analyst_accuracy_by_id(analyst_id: str) -> dict:
    """取得特定分析師準確率（分析師代號或handle）"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import AnalystCall, Analyst
    from sqlalchemy import select, desc, func

    async with AsyncSessionLocal() as db:
        # Find analyst
        r = await db.execute(
            select(Analyst).where(Analyst.analyst_id == analyst_id)
        )
        a = r.scalar_one_or_none()
        if not a:
            return {"error": f"找不到分析師：{analyst_id}"}

        # Get all calls
        r2 = await db.execute(
            select(AnalystCall)
            .where(AnalystCall.analyst_id == analyst_id)
            .order_by(desc(AnalystCall.created_at))
            .limit(50)
        )
        calls = r2.scalars().all()

    total  = len(calls)
    scored = [c for c in calls if c.was_correct is not None]
    wins   = sum(1 for c in scored if c.was_correct)
    win_rate = wins / len(scored) * 100 if scored else a.win_rate * 100

    recent = calls[:10]
    recent_data = []
    for c in recent:
        correct_tag = "✅" if c.was_correct else "❌" if c.was_correct is False else "⏳"
        recent_data.append({
            "date":     c.date,
            "stock":    c.stock_id,
            "sentiment": c.sentiment,
            "correct":  correct_tag,
            "result_5d": c.result_5d,
        })

    return {
        "analyst_id": analyst_id,
        "name":       a.name,
        "total":      total,
        "scored":     len(scored),
        "wins":       wins,
        "win_rate":   round(win_rate, 1),
        "tier_label": _tier_label(win_rate / 100),
        "recent":     recent_data,
        "channel_url": a.channel_url,
    }


def _tier_label(win_rate: float) -> str:
    if win_rate >= 0.65: return "⭐⭐⭐ 高可信"
    if win_rate >= 0.50: return "⭐⭐ 中可信"
    if win_rate >= 0.35: return "⭐ 低可信"
    return "🔄 反向指標"


def format_accuracy_report(data: dict) -> str:
    if data.get("error"):
        return f"❌ {data['error']}"

    name     = data["name"]
    total    = data["total"]
    win_rate = data["win_rate"]
    tier     = data["tier_label"]
    recent   = data.get("recent", [])

    lines = [
        f"🎯 分析師準確率  {name}",
        "─" * 32, "",
        f"評級：{tier}",
        f"總紀錄：{total} 筆  已驗證：{data['scored']} 筆",
        f"勝率：{win_rate:.1f}%  （達標：65%以上為高可信）",
        "",
        "📋 最近 10 筆",
    ]
    for r in recent[:10]:
        r5d = f"{r['result_5d']:+.1f}%" if r.get("result_5d") else ""
        lines.append(
            f"  {r['correct']} {r['date']}  {r['stock']}  "
            f"{r['sentiment'][:8]}  {r5d}"
        )

    lines += [
        "",
        f"🔗 頻道：{data.get('channel_url', '─')}",
        "",
        "指令：/accuracy [分析師代號] 查看特定分析師",
        "例：/accuracy win16888",
    ]
    return "\n".join(lines)


async def get_consensus_stocks(days: int = 7, min_analysts: int = 2, force: bool = False) -> dict:
    """找出近N天被多位分析師同時點名的股票（1小時TTL快取）"""
    global _consensus_cache, _consensus_ts
    if not force and _consensus_cache is not None and time.time() - _consensus_ts < _CONSENSUS_TTL:
        logger.debug("[consensus] returning cached result")
        return _consensus_cache

    from ..models.database import AsyncSessionLocal
    from ..models.models import AnalystCall
    from sqlalchemy import select, func
    from datetime import date, timedelta
    from collections import defaultdict

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    logger.info(f"[consensus] querying DB: date >= {cutoff}, min_analysts={min_analysts}")

    stock_analysts: dict = defaultdict(set)
    stock_sentiments: dict = defaultdict(list)

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(AnalystCall.stock_id, AnalystCall.analyst_id,
                   AnalystCall.sentiment, AnalystCall.date)
            .where(AnalystCall.date >= cutoff)
            .where(AnalystCall.stock_id != "")
        )
        for row in r.all():
            stock_analysts[row.stock_id].add(row.analyst_id)
            stock_sentiments[row.stock_id].append(row.sentiment)

    consensus = []
    for stock, analysts in stock_analysts.items():
        if len(analysts) < min_analysts:
            continue
        sents     = stock_sentiments[stock]
        bull_pct  = sum(1 for s in sents if "bullish" in s) / len(sents) * 100
        bear_pct  = sum(1 for s in sents if "bearish" in s) / len(sents) * 100
        agreement = "偏多" if bull_pct > 60 else "偏空" if bear_pct > 60 else "分歧"
        consensus.append({
            "stock":       stock,
            "analyst_cnt": len(analysts),
            "analysts":    list(analysts),
            "bull_pct":    round(bull_pct, 0),
            "bear_pct":    round(bear_pct, 0),
            "agreement":   agreement,
        })

    consensus.sort(key=lambda x: x["analyst_cnt"], reverse=True)
    result = {"stocks": consensus, "days": days, "min_analysts": min_analysts, "cutoff": cutoff}
    _consensus_cache = result
    _consensus_ts = time.time()
    logger.info(f"[consensus] found {len(consensus)} consensus stocks, cache updated")
    return result


def format_consensus_stocks(data: dict) -> str:
    stocks = data.get("stocks", [])
    days   = data.get("days", 7)

    lines = [
        f"🤝 分析師共識股票（近{days}天）",
        "─" * 32, "",
    ]

    if not stocks:
        lines.append("  尚無被多位分析師同時點名的股票")
    else:
        AGREE_ICON = {"偏多": "📈", "偏空": "📉", "分歧": "⬜"}
        for s in stocks[:8]:
            icon = AGREE_ICON.get(s["agreement"], "⬜")
            from .youtube_channel_seed import TRACKED_CHANNELS
            ch_names = {ch["analyst_id"]: ch["name"] for ch in TRACKED_CHANNELS}
            analysts_str = "、".join(ch_names.get(a, a) for a in s["analysts"][:3])
            lines += [
                f"  {icon} {s['stock']}  {s['analyst_cnt']}位分析師  {s['agreement']}",
                f"     點名者：{analysts_str}",
                f"     多空比：📈{s['bull_pct']:.0f}% / 📉{s['bear_pct']:.0f}%",
                "",
            ]

    top3 = stocks[:3]
    if top3:
        top_names = "、".join(s["stock"] for s in top3)
        verdict = f"共識度最高：{top_names}，建議結合技術面確認進場時機。"
    else:
        verdict = "目前無明確共識股，分析師觀點分散，建議等待訊號集中再操作。"

    lines += [
        "─" * 28,
        "🤖 AI 研判",
        verdict,
        "",
        "輸入 /analyst — 查看各分析師詳細觀點",
    ]
    return "\n".join(lines)
