"""精簡早報 — 08:45 盤前推播
整合：自選股重點 + 大盤情緒 + 今日重大事件
設計原則：LINE 一則可讀完，控制在 300 字以內
"""
from __future__ import annotations
from datetime import datetime, date
from loguru import logger

# 只有達到以下任一條件的股票才完整顯示，其餘只列代號+持平
_CHG_THRESHOLD  = 2.0   # 漲跌幅 >= 2%
_RSI_HIGH       = 70    # RSI 超買
_RSI_LOW        = 30    # RSI 超賣


def _is_notable(it: dict) -> bool:
    """判斷股票是否「有變化」，值得完整顯示"""
    chg = abs(it.get("change_pct", 0) or 0)
    rsi = it.get("rsi")
    sl  = it.get("sl_triggered", False)
    tp  = it.get("tp_triggered", False)
    if sl or tp:
        return True
    if chg >= _CHG_THRESHOLD:
        return True
    if rsi is not None and (rsi >= _RSI_HIGH or rsi <= _RSI_LOW):
        return True
    return False


async def generate_compact_morning(uid: str) -> str:
    """生成單一使用者的精簡盤前摘要（< 300 字）"""
    today = datetime.now().strftime("%m/%d")
    parts: list[str] = [f"🌅 {today} 盤前摘要"]

    # ── 1. 自選股（有變化才完整顯示）────────────────────────────
    try:
        from .watchlist_monitor import scan_user_watchlist
        items = await scan_user_watchlist(uid)
        if items:
            parts.append("─" * 16)
            notable   = [it for it in items if _is_notable(it)]
            flat_codes = [it["code"] for it in items if not _is_notable(it)]

            for it in notable[:4]:
                price = it.get("price", 0)
                chg   = it.get("change_pct", 0)
                rsi   = it.get("rsi")
                icon  = it.get("signal_icon", "📊")
                sign  = "+" if chg >= 0 else ""
                rsi_s = ""
                if rsi is not None:
                    if rsi >= _RSI_HIGH:
                        rsi_s = f" RSI{rsi:.0f}⚡"
                    elif rsi <= _RSI_LOW:
                        rsi_s = f" RSI{rsi:.0f}💧"
                    else:
                        rsi_s = f" RSI{rsi:.0f}"
                sl_s = " 🛑" if it.get("sl_triggered") else ""
                tp_s = " 🎯" if it.get("tp_triggered") else ""
                parts.append(
                    f"{icon} {it['code']}  "
                    f"{price:,.0f}({sign}{chg:.1f}%){rsi_s}{sl_s}{tp_s}"
                )

            if flat_codes:
                parts.append(f"⚪ 持平：{'、'.join(flat_codes)}")
    except Exception as e:
        logger.debug(f"[compact_morning] watchlist: {e}")

    # ── 2. 大盤情緒（一句話）────────────────────────────────────
    try:
        from .market_sentiment import get_sentiment_score
        data  = await get_sentiment_score()
        score = data.get("score") or data.get("total_score") or 0
        if score >= 70:
            mood = f"🟢 情緒 {score:.0f}/100 偏多，可積極布局"
        elif score >= 40:
            mood = f"⚪ 情緒 {score:.0f}/100 中性，持盈保泰"
        else:
            mood = f"🔴 情緒 {score:.0f}/100 偏空，控制倉位"
        parts.append("─" * 16)
        parts.append(mood)
    except Exception as e:
        logger.debug(f"[compact_morning] sentiment: {e}")

    # ── 3. 今日重大事件（最多 2 件）──────────────────────────────
    events: list[str] = []
    try:
        from .dividend_service import fetch_upcoming_dividends
        divs = await fetch_upcoming_dividends(days_ahead=1)
        today_iso = date.today().isoformat()
        today_divs = [d for d in divs
                      if str(d.get("ex_date", "")).startswith(today_iso[:10])]
        if today_divs:
            codes = "、".join(str(d.get("code", "")) for d in today_divs[:3])
            events.append(f"💰 除息：{codes}")
    except Exception as e:
        pass

    try:
        from .conference_service import get_conferences
        confs = await get_conferences(days_ahead=1)
        if confs:
            name = confs[0].get("company_name") or confs[0].get("name", "")
            extra = f"等 {len(confs)} 場" if len(confs) > 1 else ""
            events.append(f"🏢 法說：{name}{extra}")
    except Exception as e:
        pass

    if events:
        parts.append("─" * 16)
        for ev in events[:2]:
            parts.append(ev)

    parts.append("─" * 16)
    parts.append("/today 完整 • /screen 選股 • /quiet 安靜模式")
    return "\n".join(parts)


async def push_compact_morning_all() -> None:
    """08:45 推播精簡早報給所有訂閱者（安靜模式時跳過）"""
    from .notify_config import is_quiet_mode
    if is_quiet_mode():
        logger.info("[compact_morning] 安靜模式中，跳過推播")
        return

    from ..models.database import AsyncSessionLocal
    from ..models.models import Subscriber
    from sqlalchemy import select
    import httpx
    from .line_push import push_line_messages

    try:
        async with AsyncSessionLocal() as db:
            r    = await db.execute(
                select(Subscriber).where(Subscriber.subscribed_morning == True)
            )
            subs = r.scalars().all()
    except Exception as e:
        logger.error(f"[compact_morning] DB query failed: {e}")
        return

    if not subs:
        logger.info("[compact_morning] 無訂閱者，跳過")
        return

    qr = {"items": [
        {"type": "action", "action": {"type": "message", "label": "📊 今日總覽", "text": "/today"}},
        {"type": "action", "action": {"type": "message", "label": "🔍 選股",     "text": "/screen"}},
        {"type": "action", "action": {"type": "message", "label": "📈 大盤",     "text": "/market"}},
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
                logger.warning(
                    f"[compact_morning] push failed {sub.line_user_id[:8]}: {e}"
                )

    logger.info(f"[compact_morning] 完成推播，{len(subs)} 位訂閱者")
