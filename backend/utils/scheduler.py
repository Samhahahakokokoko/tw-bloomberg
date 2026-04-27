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

    # 財報提醒檢查 — 每日 08:15
    scheduler.add_job(
        _check_earnings_reminders,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=15, timezone="Asia/Taipei"),
        id="earnings_reminder", replace_existing=True,
    )

    # 自選股停損停利檢查 — 交易時段每 5 分鐘
    scheduler.add_job(
        _check_watchlist_triggers,
        CronTrigger(day_of_week="mon-fri", hour="9-13", minute="*/5", timezone="Asia/Taipei"),
        id="watchlist_trigger", replace_existing=True,
    )

    # Agent A — 數據員：每日 18:00 抓取並更新 FinMind 數據
    scheduler.add_job(
        _run_agent_a,
        CronTrigger(day_of_week="mon-fri", hour=18, minute=0, timezone="Asia/Taipei"),
        id="agent_a_pipeline", replace_existing=True,
    )

    # Agent B — 分析師：每日 18:30 計算三維度評分
    scheduler.add_job(
        _run_agent_b,
        CronTrigger(day_of_week="mon-fri", hour=18, minute=30, timezone="Asia/Taipei"),
        id="agent_b_scoring", replace_existing=True,
    )

    # Agent C — 決策員：每日 19:00 產生 AI 推薦理由
    scheduler.add_job(
        _run_agent_c,
        CronTrigger(day_of_week="mon-fri", hour=19, minute=0, timezone="Asia/Taipei"),
        id="agent_c_decision", replace_existing=True,
    )

    # 產業情緒分析 — 每日 20:00（新聞累積後分析）
    scheduler.add_job(
        _run_industry_sentiment,
        CronTrigger(day_of_week="mon-fri", hour=20, minute=0, timezone="Asia/Taipei"),
        id="industry_sentiment", replace_existing=True,
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


async def _check_earnings_reminders():
    try:
        from ..services.earnings_service import check_and_push_reminders
        await check_and_push_reminders()
    except Exception as e:
        logger.error(f"Earnings reminder check failed: {e}")


async def _check_watchlist_triggers():
    try:
        from .alert_checker import check_watchlist_triggers
        await check_watchlist_triggers()
    except Exception as e:
        logger.error(f"Watchlist trigger check failed: {e}")


async def _run_agent_a():
    try:
        from ..services.data_pipeline import run_daily_pipeline
        await run_daily_pipeline(trigger_scoring=False)
    except Exception as e:
        logger.error(f"Agent A (pipeline) failed: {e}")


async def _run_agent_b():
    try:
        from ..services.score_updater import run_score_update
        await run_score_update()
    except Exception as e:
        logger.error(f"Agent B (scoring) failed: {e}")


async def _run_agent_c():
    try:
        from ..services.ai_decision_agent import run_ai_decision
        await run_ai_decision()
    except Exception as e:
        logger.error(f"Agent C (decision) failed: {e}")


async def _run_industry_sentiment():
    try:
        from ..services.industry_sentiment import run_all_industries
        await run_all_industries()
    except Exception as e:
        logger.error(f"Industry sentiment failed: {e}")
