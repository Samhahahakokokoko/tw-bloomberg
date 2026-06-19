"""收盤總結 — 15:00 推播
整合：自選股今日表現 + 法人動向 + 明日注意事項
設計原則：LINE 一則可讀完
"""
from __future__ import annotations
from datetime import datetime, date, timedelta
from loguru import logger


async def generate_closing_summary(uid: str) -> str:
    """生成單一使用者的收盤總結"""
    today = datetime.now().strftime("%m/%d")
    parts: list[str] = [f"📉 {today} 收盤總結"]

    # ── 1. 自選股今日表現 ──────────────────────────────────────────
    try:
        from .watchlist_monitor import scan_user_watchlist
        items = await scan_user_watchlist(uid)
        if items:
            parts.append("─" * 16)
            parts.append("📊 自選股表現")
            for it in items[:5]:
                chg   = it.get("change_pct", 0)
                price = it.get("price", 0)
                icon  = "🔴" if chg <= -2 else ("🟠" if chg < 0 else ("🟢" if chg >= 2 else "⚪"))
                sign  = "+" if chg >= 0 else ""
                sl    = " 🛑停損!" if it.get("sl_triggered") else ""
                tp    = " 🎯目標!" if it.get("tp_triggered") else ""
                parts.append(
                    f"{icon} {it['code']} {it['name']}  "
                    f"{price:,.0f}元({sign}{chg:.1f}%){sl}{tp}"
                )
    except Exception as e:
        logger.debug(f"[closing_summary] watchlist: {e}")

    # ── 2. 法人動向重點（從 TWSE T86 取今日三大法人買賣超前3名）────
    try:
        import httpx as _hx
        from datetime import datetime as _dt
        _url = (f"https://www.twse.com.tw/rwd/zh/fund/TWT38U"
                f"?type=ALLBUT0999&date={_dt.now().strftime('%Y%m%d')}&response=json")
        async with _hx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as _c:
            _r = await _c.get(_url)
        _js = _r.json()
        _rows = _js.get("data", [])
        if _rows:
            # col0=代號, col1=名稱, col2=外資買, col3=外資賣, col4=外資淨
            def _to_int(v): return int(str(v).replace(",", "")) if str(v).replace(",", "").lstrip("-").isdigit() else 0
            _items = [{"code": r[0], "name": r[1], "net": _to_int(r[4])} for r in _rows if len(r) > 4]
            _buy  = sorted([x for x in _items if x["net"] > 0], key=lambda x: -x["net"])[:3]
            _sell = sorted([x for x in _items if x["net"] < 0], key=lambda x: x["net"])[:2]
            if _buy or _sell:
                parts.append("─" * 16)
                parts.append("🏦 外資動向")
            if _buy:
                parts.append("買超：" + "、".join(f"{x['code']}{x['name']}" for x in _buy))
            if _sell:
                parts.append("賣超：" + "、".join(f"{x['code']}{x['name']}" for x in _sell))
    except Exception as e:
        pass

    # ── 3. 明日注意事項 ────────────────────────────────────────────
    tomorrow_notes: list[str] = []
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    try:
        from .dividend_service import fetch_upcoming_dividends
        divs = await fetch_upcoming_dividends(days_ahead=2)
        tmr_divs = [d for d in divs if str(d.get("ex_date", "")).startswith(tomorrow[:10])]
        if tmr_divs:
            names = "、".join(f"{d.get('code','')} {d.get('name','')}" for d in tmr_divs[:3])
            tomorrow_notes.append(f"💰 除息：{names}")
    except Exception as e:
        pass

    try:
        from .conference_service import get_conferences
        confs = await get_conferences(days_ahead=2)
        tmr_confs = [c for c in confs if str(c.get("date", "")).startswith(tomorrow[:10])]
        if tmr_confs:
            names = "、".join(c.get("company_name") or c.get("name", "") for c in tmr_confs[:2])
            tomorrow_notes.append(f"🏢 法說：{names}")
    except Exception as e:
        pass

    try:
        from .earnings_service import check_and_push_reminders
        # just check for upcoming earnings, don't push here
        pass
    except Exception as e:
        pass

    if tomorrow_notes:
        parts.append("─" * 16)
        parts.append("📌 明日注意")
        for note in tomorrow_notes[:2]:
            parts.append(note)

    parts.append("─" * 16)
    parts.append("/today 查今日狀態 • /p 庫存 • /risk 風控")
    return "\n".join(parts)


async def push_closing_summary_all() -> None:
    """15:00 推播收盤總結給所有訂閱者（安靜模式時跳過）"""
    from .notify_config import is_quiet_mode
    if is_quiet_mode():
        logger.info("[closing_summary] 安靜模式中，跳過推播")
        return
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
        logger.error(f"[closing_summary] DB query failed: {e}")
        return

    if not subs:
        return

    qr = {"items": [
        {"type": "action", "action": {"type": "message", "label": "📊 今日總覽", "text": "/today"}},
        {"type": "action", "action": {"type": "message", "label": "💼 我的庫存", "text": "/p"}},
        {"type": "action", "action": {"type": "message", "label": "🔍 選股",   "text": "/screen"}},
    ]}

    async with httpx.AsyncClient(timeout=30) as c:
        for sub in subs:
            try:
                text = await generate_closing_summary(sub.line_user_id)
                await push_line_messages(
                    sub.line_user_id,
                    [{"type": "text", "text": text[:4800], "quickReply": qr}],
                    client=c, context="closing_summary",
                )
            except Exception as e:
                logger.warning(f"[closing_summary] push failed {sub.line_user_id[:8]}: {e}")

    logger.info(f"[closing_summary] 完成推播，{len(subs)} 位訂閱者")
