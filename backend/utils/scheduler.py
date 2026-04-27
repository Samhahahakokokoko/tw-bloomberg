"""APScheduler — 排程任務"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger


def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Asia/Taipei")

    # 每日早報 — 週一到週五 08:30
    scheduler.add_job(
        _run_morning_report,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=30, timezone="Asia/Taipei"),
        id="morning_report", replace_existing=True,
    )

    # 週報 — 週五 14:30（收盤後）
    scheduler.add_job(
        _run_weekly_report,
        CronTrigger(day_of_week="fri", hour=14, minute=30, timezone="Asia/Taipei"),
        id="weekly_report", replace_existing=True,
    )

    # 新聞爬蟲 — 週一到週五 08:00~18:00，每 30 分鐘
    scheduler.add_job(
        _run_scraper,
        CronTrigger(day_of_week="mon-fri", hour="8-18", minute="*/30", timezone="Asia/Taipei"),
        id="news_scraper", replace_existing=True,
    )

    # 警報檢查 — 交易時段每 3 分鐘
    scheduler.add_job(
        _check_alerts,
        CronTrigger(day_of_week="mon-fri", hour="9-13", minute="*/3", timezone="Asia/Taipei"),
        id="alert_checker", replace_existing=True,
    )

    # 除權息資料更新 — 每日 07:00
    scheduler.add_job(
        _refresh_dividends,
        CronTrigger(day_of_week="mon-fri", hour=7, minute=0, timezone="Asia/Taipei"),
        id="dividend_refresh", replace_existing=True,
    )

    # 每月績效報告 — 每月1日 08:00
    scheduler.add_job(
        _run_monthly_report,
        CronTrigger(day=1, hour=8, minute=0, timezone="Asia/Taipei"),
        id="monthly_report", replace_existing=True,
    )

    # 大盤異常偵測 — 交易時段每 5 分鐘
    scheduler.add_job(
        _check_market_anomaly,
        CronTrigger(day_of_week="mon-fri", hour="9-13", minute="*/5", timezone="Asia/Taipei"),
        id="market_anomaly", replace_existing=True,
    )

    # 績效快照 — 週一到週五 14:00
    scheduler.add_job(
        _snapshot_performance,
        CronTrigger(day_of_week="mon-fri", hour=14, minute=0, timezone="Asia/Taipei"),
        id="perf_snapshot", replace_existing=True,
    )

    # 每週選股推播 — 週五 15:00
    scheduler.add_job(
        _push_weekly_picks,
        CronTrigger(day_of_week="fri", hour=15, minute=0, timezone="Asia/Taipei"),
        id="weekly_picks", replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler started (morning report 08:30 / weekly report Fri 14:30)")
    return scheduler


async def _run_morning_report():
    try:
        from ..services.morning_report import push_morning_report
        await push_morning_report()
    except Exception as e:
        logger.error(f"Morning report job failed: {e}")


async def _run_weekly_report():
    try:
        from ..services.weekly_report import push_weekly_report
        await push_weekly_report()
    except Exception as e:
        logger.error(f"Weekly report job failed: {e}")


async def _run_scraper():
    try:
        from scraper.news_scraper import scrape_all
        await scrape_all()
    except Exception as e:
        logger.error(f"Scraper job failed: {e}")


async def _check_alerts():
    try:
        from .alert_checker import check_all_alerts
        await check_all_alerts()
    except Exception as e:
        logger.error(f"Alert check failed: {e}")


async def _run_monthly_report():
    try:
        from ..services.monthly_report import push_monthly_reports
        await push_monthly_reports()
    except Exception as e:
        logger.error(f"Monthly report job failed: {e}")


async def _refresh_dividends():
    try:
        from ..services.dividend_service import fetch_upcoming_dividends
        await fetch_upcoming_dividends()
        logger.info("Dividend data refreshed")
    except Exception as e:
        logger.error(f"Dividend refresh failed: {e}")


async def _check_market_anomaly():
    try:
        from ..services.market_anomaly_service import check_market_anomaly, push_anomaly_alert
        anomaly = await check_market_anomaly()
        if anomaly and anomaly.get("has_anomaly"):
            await push_anomaly_alert(anomaly)
    except Exception as e:
        logger.error(f"Market anomaly check failed: {e}")


async def _snapshot_performance():
    try:
        from ..services.performance_service import snapshot_all_users
        await snapshot_all_users()
    except Exception as e:
        logger.error(f"Performance snapshot failed: {e}")


async def _push_weekly_picks():
    try:
        from ..services.stock_pick_service import push_weekly_picks
        await push_weekly_picks()
    except Exception as e:
        logger.error(f"Weekly picks push failed: {e}")
