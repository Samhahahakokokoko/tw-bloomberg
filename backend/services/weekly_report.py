"""週報服務 — 每週五 14:30 推送績效摘要"""
from datetime import datetime
from loguru import logger
from .twse_service import fetch_realtime_quote


async def generate_weekly_report() -> str:
    from ..models.database import AsyncSessionLocal
    from ..models.models import Portfolio
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Portfolio))
        holdings = result.scalars().all()

    if not holdings:
        return "📋 本週週報\n尚無持股資料"

    rows = []
    total_mv = total_cost = 0.0
    for h in holdings:
        quote = await fetch_realtime_quote(h.stock_code)
        price = quote.get("price", h.cost_price)
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
    except Exception:
        pass

    return "\n".join(lines)


async def push_weekly_report():
    from ..models.database import AsyncSessionLocal, settings
    from ..models.models import Subscriber
    from sqlalchemy import select
    from .morning_report import _push_to_users

    report = await generate_weekly_report()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Subscriber).where(Subscriber.subscribed_weekly == True)
        )
        subs = result.scalars().all()

    if subs:
        await _push_to_users([s.line_user_id for s in subs], report)
        logger.info(f"Weekly report pushed to {len(subs)} subscribers")
    else:
        logger.info("Weekly report: no subscribers")


async def _weekly_ai_comment(summary: str) -> str:
    from ..models.database import settings
    if not settings.anthropic_api_key:
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
    except Exception:
        return ""
