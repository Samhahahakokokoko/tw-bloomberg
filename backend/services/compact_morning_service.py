"""精簡早報 — 08:45 盤前推播
整合：自選股重點 + 大盤情緒 + 今日重大事件
設計原則：LINE 一則可讀完，不超過 500 字
"""
from __future__ import annotations
from datetime import datetime, date
from loguru import logger


async def generate_compact_morning(uid: str) -> str:
    """生成單一使用者的精簡盤前摘要"""
    today = datetime.now().strftime("%m/%d")
    parts: list[str] = [f"🌅 {today} 盤前摘要"]

    # ── 1. 自選股狀態（最多 5 檔）────────────────────────────────
    try:
        from .watchlist_monitor import scan_user_watchlist
        items = await scan_user_watchlist(uid)
        if items:
            parts.append("─" * 16)
            for it in items[:5]:
                price = it.get("price", 0)
                chg   = it.get("change_pct", 0)
                rsi   = it.get("rsi")
                icon  = it.get("signal_icon", "📊")
                sign  = "+" if chg >= 0 else ""
                rsi_s = f" RSI{rsi:.0f}" if rsi is not None else ""
                parts.append(
                    f"{icon} {it['code']} {it['name']}  "
                    f"{price:,.0f}({sign}{chg:.1f}%){rsi_s}"
                )
    except Exception as e:
        logger.debug(f"[compact_morning] watchlist: {e}")

    # ── 2. 大盤情緒分數 + 建議倉位 ──────────────────────────────
    try:
        from .market_sentiment import get_sentiment_score
        data  = await get_sentiment_score()
        score = data.get("score") or data.get("total_score") or 0
        if score >= 70:
            mood = "偏多 ↑ 可增持"
        elif score >= 40:
            mood = "中性 → 持盈保泰"
        else:
            mood = "偏空 ↓ 控制倉位"
        parts.append("─" * 16)
        parts.append(f"🎯 情緒分數 {score:.0f}/100  {mood}")
    except Exception as e:
        logger.debug(f"[compact_morning] sentiment: {e}")

    # ── 3. 今日重大事件（最多 2 件）──────────────────────────────
    events: list[str] = []
    try:
        from .dividend_service import fetch_upcoming_dividends
        divs = await fetch_upcoming_dividends(days_ahead=1)
        today_iso = date.today().isoformat()
        today_divs = [d for d in divs if str(d.get("ex_date", "")).startswith(today_iso[:10])]
        if today_divs:
            names = "、".join(f"{d.get('code','')} {d.get('name','')}" for d in today_divs[:3])
            events.append(f"💰 除息：{names}")
    except Exception:
        pass

    try:
        from .conference_service import get_conferences
        confs = await get_conferences(days_ahead=1)
        if confs:
            first = confs[0]
            name  = first.get("company_name") or first.get("name", "")
            events.append(f"🏢 法說：{name}" + ("等" if len(confs) > 1 else ""))
    except Exception:
        pass

    try:
        from .news_pipeline import get_latest_news
        news_list = await get_latest_news(limit=1, importance="high")
        if news_list:
            headline = news_list[0].get("title", "")[:40]
            events.append(f"📰 {headline}")
    except Exception:
        pass

    if events:
        parts.append("─" * 16)
        parts.append("📌 今日重點")
        for ev in events[:2]:
            parts.append(ev)

    parts.append("─" * 16)
    parts.append("/today 完整查詢 • /screen 選股")
    return "\n".join(parts)


async def push_compact_morning_all() -> None:
    """08:45 推播精簡早報給所有訂閱者"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Subscriber
    from sqlalchemy import select
    import httpx
    from .line_push import push_line_messages

    try:
        async with AsyncSessionLocal() as db:
            r    = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
            subs = r.scalars().all()
    except Exception as e:
        logger.error(f"[compact_morning] DB query failed: {e}")
        return

    if not subs:
        logger.info("[compact_morning] 無訂閱者，跳過")
        return

    qr = {"items": [
        {"type": "action", "action": {"type": "message", "label": "📊 今日總覽", "text": "/today"}},
        {"type": "action", "action": {"type": "message", "label": "🔍 選股",   "text": "/screen"}},
        {"type": "action", "action": {"type": "message", "label": "📈 大盤",   "text": "/market"}},
    ]}

    async with httpx.AsyncClient(timeout=30) as c:
        for sub in subs:
            try:
                text = await generate_compact_morning(sub.line_user_id)
                await push_line_messages(
                    sub.line_user_id,
                    [{"type": "text", "text": text[:4800], "quickReply": qr}],
                    client=c, context="compact_morning",
                )
            except Exception as e:
                logger.warning(f"[compact_morning] push failed {sub.line_user_id[:8]}: {e}")

    logger.info(f"[compact_morning] 完成推播，{len(subs)} 位訂閱者")
