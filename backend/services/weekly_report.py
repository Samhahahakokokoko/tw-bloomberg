"""週報服務 — 每週五 14:30 推送績效摘要"""
from datetime import datetime
from loguru import logger
from .twse_service import fetch_realtime_quote
from ..utils.credit_guard import is_exhausted as _credit_exhausted, mark_exhausted as _mark_credit_exhausted


async def generate_weekly_report() -> str:
    from ..models.database import AsyncSessionLocal
    from ..models.models import Portfolio
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Portfolio))
        holdings = result.scalars().all()

    if not holdings:
        return "📋 本週週報\n尚無持股資料"

    import asyncio as _asyncio

    async def _safe_price(h):
        try:
            q = await fetch_realtime_quote(h.stock_code)
            return h.stock_code, float(q.get("price", 0) or h.cost_price)
        except Exception:
            return h.stock_code, float(h.cost_price)

    price_results = await _asyncio.gather(*[_safe_price(h) for h in holdings])
    price_map = dict(price_results)

    rows = []
    total_mv = total_cost = 0.0
    for h in holdings:
        price = price_map.get(h.stock_code, h.cost_price)
        mv = price * h.shares
        cost = h.cost_price * h.shares
        pnl = mv - cost
        pnl_pct = pnl / cost * 100 if cost else 0
        total_mv += mv
        total_cost += cost
        rows.append((h.stock_code, h.stock_name or "", h.shares, h.cost_price, price, pnl, pnl_pct))

    total_pnl = total_mv - total_cost
    total_pnl_pct = total_pnl / total_cost * 100 if total_cost else 0

    week = datetime.now().strftime("%m/%d")
    lines = [
        f"📋 台股週報  W/E {week}",
        "─" * 24,
    ]
    for code, name, shares, cost, price, pnl, pct in sorted(rows, key=lambda x: x[6], reverse=True):
        icon = "▲" if pnl >= 0 else "▼"
        lines.append(f"{code} {name}\n  {shares:,}股 @{cost:.0f}→{price:.0f}  {icon}{abs(pnl):,.0f} ({pct:+.1f}%)")

    lines += [
        "─" * 24,
        f"總成本   {total_cost:>12,.0f}",
        f"總市值   {total_mv:>12,.0f}",
        f"總損益   {total_pnl:>+12,.0f} ({total_pnl_pct:+.1f}%)",
    ]

    # AI 一句話建議
    try:
        summary_text = "\n".join(lines)
        ai = await _weekly_ai_comment(summary_text)
        lines.append(f"\n🤖 {ai}")
    except Exception as e:
        pass

    return "\n".join(lines)


async def generate_enhanced_weekly_report(uid: str = "") -> str:
    """增強版週報：自選股排行 + 族群 + 事件 + AI展望"""
    now = datetime.now()
    week_str = now.strftime("W/E %m/%d")
    lines = [f"📋 週報  {week_str}", "─" * 24]

    # 1. 自選股週漲跌排行
    try:
        from ..models.database import AsyncSessionLocal
        from ..models.models import Watchlist
        from .twse_service import fetch_realtime_quote
        from sqlalchemy import select
        import asyncio as _asyncio

        if uid:
            async with AsyncSessionLocal() as db:
                r = await db.execute(select(Watchlist).where(Watchlist.user_id == uid))
                items = r.scalars().all()
        else:
            async with AsyncSessionLocal() as db:
                r = await db.execute(select(Watchlist))
                items = r.scalars().all()

        if items:
            async def _safe_q(code):
                try:
                    q = await fetch_realtime_quote(code)
                    return code, float(q.get("change_pct", 0) or 0), q.get("name", code) or code
                except Exception:
                    return code, 0.0, code

            codes = list({i.stock_code for i in items})[:20]
            results = await _asyncio.gather(*[_safe_q(c) for c in codes])
            results_sorted = sorted(results, key=lambda x: x[1], reverse=True)
            lines.append("👁️ 自選股週漲跌排行")
            for code, chg, name in results_sorted[:5]:
                icon = "▲" if chg >= 0 else "▼"
                lines.append(f"  {icon} {name} {chg:+.1f}%")
            lines.append("")
    except Exception as e:
        logger.warning(f"[weekly_report] watchlist section failed: {e}")

    # 2. 最強族群
    try:
        from backend.services.report_screener import _rt_cache
        prices = _rt_cache.get("prices", {})
        sector_changes: dict = {}
        SECTOR_KW = {
            "AI/雲端":  ["AI", "伺服器", "雲端"],
            "半導體":   ["半導體", "晶圓", "IC"],
            "金融":     ["金融", "銀行", "保險"],
            "航運":     ["航運", "貨櫃", "海運"],
            "生技":     ["生技", "醫療", "製藥"],
            "散熱":     ["散熱", "機殼"],
        }
        for code, data in list(prices.items())[:3000]:
            sector = str(data.get("sector", "") or "")
            chg = float(data.get("change_pct", 0) or 0)
            for sname, kws in SECTOR_KW.items():
                if any(kw in sector for kw in kws):
                    sector_changes.setdefault(sname, []).append(chg)
        if sector_changes:
            avg_changes = [(s, sum(cs)/len(cs)) for s, cs in sector_changes.items() if cs]
            avg_changes.sort(key=lambda x: x[1], reverse=True)
            lines.append("🔥 本週最強族群")
            for sname, chg in avg_changes[:4]:
                icon = "▲" if chg >= 0 else "▼"
                lines.append(f"  {icon} {sname} {chg:+.1f}%")
            lines.append("")
    except Exception as e:
        logger.warning(f"[weekly_report] sector section: {e}")

    # 3. 下週重要事件
    try:
        from datetime import timedelta
        next_week_start = now + timedelta(days=(7 - now.weekday()))
        next_week_end   = next_week_start + timedelta(days=4)
        lines.append(f"📅 下週重要事件 ({next_week_start.strftime('%m/%d')}–{next_week_end.strftime('%m/%d')})")
        try:
            from ..models.database import AsyncSessionLocal
            from ..models.models import DividendCalendar
            from sqlalchemy import select, and_
            async with AsyncSessionLocal() as db:
                r = await db.execute(
                    select(DividendCalendar).where(
                        and_(
                            DividendCalendar.ex_date >= next_week_start.strftime("%Y-%m-%d"),
                            DividendCalendar.ex_date <= next_week_end.strftime("%Y-%m-%d"),
                        )
                    ).limit(5)
                )
                events = r.scalars().all()
            if events:
                for ev in events:
                    lines.append(f"  💰 {ev.stock_name}({ev.stock_code}) 除息 {ev.ex_date}")
            else:
                lines.append("  （除息資料暫無）")
        except Exception:
            lines.append("  （除息資料暫無）")
        lines.append("  📊 注意：週五為最後交易日，留意結算風險")
        lines.append("")
    except Exception as e:
        logger.warning(f"[weekly_report] events section: {e}")

    # 4. AI 展望
    try:
        summary_for_ai = "\n".join(lines[:20])
        ai = await _weekly_ai_comment(summary_for_ai + "\n請給出下週市場展望和操作建議（30字內）")
        if ai:
            lines.append(f"🤖 AI展望：{ai}")
    except Exception:
        pass

    return "\n".join(lines)


async def push_weekly_report():
    from ..models.database import AsyncSessionLocal, settings
    from ..models.models import Subscriber
    from .push_dedup import check_and_record
    from .morning_report import _push_to_users
    from sqlalchemy import select

    try:
        report = await generate_enhanced_weekly_report()
    except Exception:
        report = await generate_weekly_report()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Subscriber).where(Subscriber.subscribed_weekly == True)
        )
        subs = result.scalars().all()

    if not subs:
        logger.info("Weekly report: no subscribers")
        return

    eligible = []
    for sub in subs:
        if await check_and_record(sub.line_user_id, "weekly", report):
            eligible.append(sub.line_user_id)

    skipped = len(subs) - len(eligible)
    if skipped:
        logger.info(f"Weekly report: {skipped} already pushed this week, skipping")

    if eligible:
        await _push_to_users(eligible, report)
        logger.info(f"Weekly report pushed to {len(eligible)} subscribers")


async def _weekly_ai_comment(summary: str) -> str:
    from ..models.database import settings
    if not settings.anthropic_api_key or _credit_exhausted():
        return ""
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": "根據以下週績效，用一句話給下週操作建議（繁中）：\n\n" + summary[:400]
            }],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        if "credit balance is too low" in str(e):
            _mark_credit_exhausted()
            logger.warning("[WeeklyReport] Anthropic credit 耗盡")
        return ""
