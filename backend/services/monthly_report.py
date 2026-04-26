"""每月1日績效報告 — 自動推送給所有訂閱者"""
from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from loguru import logger


async def push_monthly_reports():
    """每月1日 08:00 跑上月績效"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Subscriber, Portfolio
    from ..services.trade_log_service import get_monthly_stats, format_monthly_report
    from ..services.portfolio_service import get_portfolio
    from ..services.morning_report import _push_to_users
    from sqlalchemy import select

    last_month = date.today() - relativedelta(months=1)
    year, month = last_month.year, last_month.month

    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Subscriber))
        subscribers = r.scalars().all()

    for sub in subscribers:
        try:
            uid = sub.line_user_id
            async with AsyncSessionLocal() as db:
                stats     = await get_monthly_stats(db, uid, year, month)
                holdings  = await get_portfolio(db, uid)

            unrealized = sum(h["pnl"] for h in holdings)
            report     = format_monthly_report(stats, unrealized)
            await _push_to_users([uid], report)
        except Exception as e:
            logger.error(f"Monthly report push error for {sub.line_user_id[:8]}: {e}")

    logger.info(f"Monthly reports pushed for {year}/{month:02d} to {len(subscribers)} users")
