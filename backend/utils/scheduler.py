"""APScheduler — 排程任務"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from ..models.database import AsyncSessionLocal


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

    # Alpha Pipeline — 18:00 Layer 1: 動能啟動掃描 + 資金流向
    scheduler.add_job(
        _run_pipeline_movers,
        CronTrigger(day_of_week="mon-fri", hour=18, minute=0, timezone="Asia/Taipei"),
        id="pipeline_movers", replace_existing=True,
    )

    # Alpha Pipeline — 18:15 Layer 2+3: 三層分類 + 六大過濾 + 族群輪動
    scheduler.add_job(
        _run_pipeline_scanner_filter,
        CronTrigger(day_of_week="mon-fri", hour=18, minute=15, timezone="Asia/Taipei"),
        id="pipeline_scanner_filter", replace_existing=True,
    )

    # Alpha Pipeline — 18:30 Layer 4: Research + Alpha 衰退檢查
    scheduler.add_job(
        _run_pipeline_research,
        CronTrigger(day_of_week="mon-fri", hour=18, minute=30, timezone="Asia/Taipei"),
        id="pipeline_research", replace_existing=True,
    )

    # Alpha Pipeline — 18:45 Layer 5: Portfolio Overlay + Conviction 計算
    scheduler.add_job(
        _run_pipeline_overlay_prep,
        CronTrigger(day_of_week="mon-fri", hour=18, minute=45, timezone="Asia/Taipei"),
        id="pipeline_overlay_prep", replace_existing=True,
    )

    # Meta Alpha 週報 — 每週五 18:30
    scheduler.add_job(
        _run_meta_alpha_weekly,
        CronTrigger(day_of_week="fri", hour=18, minute=30, timezone="Asia/Taipei"),
        id="meta_alpha_weekly", replace_existing=True,
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

    # 持倉健康報告 — 每日 19:00 推播（portfolio_overlay）
    scheduler.add_job(
        _push_portfolio_overlay,
        CronTrigger(day_of_week="mon-fri", hour=19, minute=0, timezone="Asia/Taipei"),
        id="portfolio_overlay", replace_existing=True,
    )

    # 產業情緒分析 — 每日 20:00（新聞累積後分析）
    scheduler.add_job(
        _run_industry_sentiment,
        CronTrigger(day_of_week="mon-fri", hour=20, minute=0, timezone="Asia/Taipei"),
        id="industry_sentiment", replace_existing=True,
    )

    # AI 日報操作建議 — 每日 19:30 推播
    scheduler.add_job(
        _push_daily_advice,
        CronTrigger(day_of_week="mon-fri", hour=19, minute=30, timezone="Asia/Taipei"),
        id="daily_advice", replace_existing=True,
    )

    # 每日決策報告 — 19:30 推播（decision_engine）
    scheduler.add_job(
        _push_daily_decision,
        CronTrigger(day_of_week="mon-fri", hour=19, minute=30, timezone="Asia/Taipei"),
        id="daily_decision", replace_existing=True,
    )

    # Feedback 自動調整 feature 權重 — 每週日 22:00
    scheduler.add_job(
        _auto_adjust_feature_weights,
        CronTrigger(day_of_week="sun", hour=22, minute=0, timezone="Asia/Taipei"),
        id="feature_weight_adjust", replace_existing=True,
    )

    # 推薦結果回填 — 每日 15:30 盤後回填 5d/10d 股價
    scheduler.add_job(
        _backfill_recommendation_prices,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=30, timezone="Asia/Taipei"),
        id="rec_backfill", replace_existing=True,
    )

    # 08:30 盤前選股表（動能 + 全維度）
    scheduler.add_job(
        _push_morning_picks,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=30, timezone="Asia/Taipei"),
        id="morning_picks", replace_existing=True,
    )

    # 19:30 收盤後選股表（今日收盤後三張圖）
    scheduler.add_job(
        _push_group_report,
        CronTrigger(day_of_week="mon-fri", hour=19, minute=30, timezone="Asia/Taipei"),
        id="group_report", replace_existing=True,
    )

    # 週五 15:00 本週績效 + 下週潛力股
    scheduler.add_job(
        _push_friday_summary,
        CronTrigger(day_of_week="fri", hour=15, minute=0, timezone="Asia/Taipei"),
        id="friday_summary", replace_existing=True,
    )

    # 評分權重自動調整 — 每週一 08:00
    scheduler.add_job(
        _adjust_scoring_weights,
        CronTrigger(day_of_week="mon", hour=8, minute=0, timezone="Asia/Taipei"),
        id="weight_adjust", replace_existing=True,
    )

    # 聰明錢訊號推播 — 每日 18:30（盤後）
    scheduler.add_job(
        _push_smart_money,
        CronTrigger(day_of_week="mon-fri", hour=18, minute=30, timezone="Asia/Taipei"),
        id="smart_money", replace_existing=True,
    )

    # ── 新功能排程（第一批）──────────────────────────────────────────────────

    scheduler.add_job(
        _push_ai_feed,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=31, timezone="Asia/Taipei"),
        id="ai_feed", replace_existing=True,
    )
    scheduler.add_job(
        _run_smart_alert,
        CronTrigger(day_of_week="mon-fri", hour="9-13", minute="*/30", timezone="Asia/Taipei"),
        id="smart_alert_v2", replace_existing=True,
    )
    scheduler.add_job(
        _push_watchlist_daily,
        CronTrigger(day_of_week="mon-fri", hour=19, minute=0, timezone="Asia/Taipei"),
        id="watchlist_daily", replace_existing=True,
    )
    scheduler.add_job(
        _run_breadth_check,
        CronTrigger(day_of_week="mon-fri", hour="9-13", minute="*/15", timezone="Asia/Taipei"),
        id="market_breadth", replace_existing=True,
    )

    # ── Analyst Intelligence System 排程 ─────────────────────────────────

    # 16:00 抓取 YouTube 新影片並分析
    scheduler.add_job(
        _run_youtube_fetch,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=0, timezone="Asia/Taipei"),
        id="youtube_fetch", replace_existing=True,
    )
    # 16:30 觀點轉變偵測（抓片完成後）
    scheduler.add_job(
        _run_analyst_alert_check,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=30, timezone="Asia/Taipei"),
        id="analyst_alert_check", replace_existing=True,
    )
    # 17:00 更新分析師績效 + 計算共識 + 話題統計
    scheduler.add_job(
        _run_analyst_performance,
        CronTrigger(day_of_week="mon-fri", hour=17, minute=0, timezone="Asia/Taipei"),
        id="analyst_performance", replace_existing=True,
    )
    # 每月1日 00:00 重新評定 Tier
    scheduler.add_job(
        _run_monthly_tier_update,
        CronTrigger(day=1, hour=0, minute=0, timezone="Asia/Taipei"),
        id="monthly_tier_update", replace_existing=True,
    )
    # 20:00 推送共識報告
    scheduler.add_job(
        _push_analyst_consensus,
        CronTrigger(day_of_week="mon-fri", hour=20, minute=0, timezone="Asia/Taipei"),
        id="analyst_consensus_push", replace_existing=True,
    )

    # ── 新功能排程（第三批/最終批）──────────────────────────────────────────

    # 17:30 Autonomous Daily Research
    scheduler.add_job(
        _push_autonomous_research,
        CronTrigger(day_of_week="mon-fri", hour=17, minute=30, timezone="Asia/Taipei"),
        id="autonomous_research", replace_existing=True,
    )
    # 18:00 完整 AI Hedge Fund Agent 流程
    scheduler.add_job(
        _run_hedge_fund_agent,
        CronTrigger(day_of_week="mon-fri", hour=18, minute=0, timezone="Asia/Taipei"),
        id="hedge_fund_agent_run", replace_existing=True,
    )
    # 19:00 Smart Money 推送
    scheduler.add_job(
        _push_smart_money_v2,
        CronTrigger(day_of_week="mon-fri", hour=19, minute=0, timezone="Asia/Taipei"),
        id="smart_money_v2", replace_existing=True,
    )
    # 19:30 AI Agent 決策報告推送
    scheduler.add_job(
        _push_agent_report,
        CronTrigger(day_of_week="mon-fri", hour=19, minute=30, timezone="Asia/Taipei"),
        id="agent_report", replace_existing=True,
    )
    # 週五 公開投組排行更新
    scheduler.add_job(
        _update_public_rankings,
        CronTrigger(day_of_week="fri", hour=15, minute=30, timezone="Asia/Taipei"),
        id="public_rankings", replace_existing=True,
    )

    # ── 新功能排程（第二批）──────────────────────────────────────────────────

    # 18:00 RS Ranking + Breadth 收盤計算
    scheduler.add_job(
        _run_post_market_breadth,
        CronTrigger(day_of_week="mon-fri", hour=18, minute=0, timezone="Asia/Taipei"),
        id="post_market_breadth", replace_existing=True,
    )
    # 18:30 Sector Heatmap 生成推送
    scheduler.add_job(
        _push_sector_heatmap,
        CronTrigger(day_of_week="mon-fri", hour=18, minute=30, timezone="Asia/Taipei"),
        id="sector_heatmap", replace_existing=True,
    )
    # 19:30 AI Portfolio Manager 建議推送
    scheduler.add_job(
        _push_portfolio_manager,
        CronTrigger(day_of_week="mon-fri", hour=19, minute=30, timezone="Asia/Taipei"),
        id="portfolio_manager_advice", replace_existing=True,
    )
    # 每週五 18:00 Mistake Detector 週報
    scheduler.add_job(
        _push_mistake_detector,
        CronTrigger(day_of_week="fri", hour=18, minute=0, timezone="Asia/Taipei"),
        id="mistake_detector_weekly", replace_existing=True,
    )

    # ── 市場情報作戰系統排程 ─────────────────────────────────────────────────

    # 15:30 盤後：週期/領先/法人足跡掃描
    scheduler.add_job(
        _run_market_intel_scan,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=30, timezone="Asia/Taipei"),
        id="market_intel_scan", replace_existing=True,
    )
    # 16:30 分析師觀點飄移偵測（YouTube 抓完後）
    scheduler.add_job(
        _run_drift_detection_job,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=30, timezone="Asia/Taipei"),
        id="drift_detection", replace_existing=True,
    )
    # 17:00 過熱/壓力計算 + 推送
    scheduler.add_job(
        _push_euphoria_stress,
        CronTrigger(day_of_week="mon-fri", hour=17, minute=0, timezone="Asia/Taipei"),
        id="euphoria_stress_push", replace_existing=True,
    )
    # 20:30 AI 多空辯論（重點個股）
    scheduler.add_job(
        _push_ai_debate,
        CronTrigger(day_of_week="mon-fri", hour=20, minute=30, timezone="Asia/Taipei"),
        id="ai_debate_push", replace_existing=True,
    )
    # 每週五 19:00 預測市場快照 + 上週結算
    scheduler.add_job(
        _run_prediction_market_weekly,
        CronTrigger(day_of_week="fri", hour=19, minute=0, timezone="Asia/Taipei"),
        id="prediction_market_weekly", replace_existing=True,
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


async def _backfill_recommendation_prices():
    try:
        from ..services.recommendation_tracker import backfill_prices
        await backfill_prices()
    except Exception as e:
        logger.error(f"Recommendation backfill failed: {e}")


async def _adjust_scoring_weights():
    try:
        from ..services.recommendation_tracker import adjust_weights
        await adjust_weights()
    except Exception as e:
        logger.error(f"Weight adjustment failed: {e}")


async def _push_smart_money():
    try:
        from ..services.broker_tracker import push_smart_money_alerts
        await push_smart_money_alerts()
    except Exception as e:
        logger.error(f"Smart money push failed: {e}")


async def _push_daily_advice():
    try:
        from ..services.ai_trading_advisor import generate_daily_trading_advice
        from ..models.models import Subscriber
        from sqlalchemy import select
        from ..services.morning_report import _push_to_users
        advice = await generate_daily_trading_advice()
        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
            subs = r.scalars().all()
        if subs:
            await _push_to_users([s.line_user_id for s in subs], advice)
            logger.info(f"Daily advice pushed to {len(subs)} subscribers")
    except Exception as e:
        logger.error(f"Daily advice push failed: {e}")


async def _auto_adjust_feature_weights():
    try:
        from backtest.feedback_engine import auto_adjust_feature_weights
        await auto_adjust_feature_weights()
    except Exception as e:
        logger.error(f"Feature weight adjustment failed: {e}")


async def _push_morning_picks():
    """08:30 盤前：動能 + 全維度 選股圖推送"""
    try:
        import os
        from sqlalchemy import select
        from ..models.models import Subscriber
        from ..models.database import settings as cfg
        from backend.services.report_screener import momentum_screener, all_screener, paginate
        from backend.services.generate_report_image import generate_report_image, push_report_image

        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
            subs = r.scalars().all()
        if not subs:
            return
        user_ids = [s.line_user_id for s in subs]
        base_url = os.getenv("BASE_URL", "")

        for fn, label in [(momentum_screener, "動能選股"), (all_screener, "全維度排名")]:
            rows = fn()
            page_rows, total = paginate(rows, 1)
            path = generate_report_image(
                stocks=page_rows, group=f"盤前重點｜{label}",
                market_state=os.getenv("MARKET_STATE", "unknown"),
                page=1, total_pages=total,
            )
            if base_url:
                await push_report_image(path, user_ids, cfg.line_channel_access_token, base_url, alt_text=label)
            logger.info(f"[MorningPicks] {label} 推送 {len(user_ids)} 人")
    except Exception as e:
        logger.error(f"Morning picks push failed: {e}")


async def _push_friday_summary():
    """週五 15:00：本週績效 + 下週潛力股"""
    try:
        import os
        from sqlalchemy import select
        from ..models.models import Subscriber
        from ..models.database import settings as cfg
        from backend.services.report_screener import breakout_screener, value_screener, paginate
        from backend.services.generate_report_image import generate_report_image, push_report_image

        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
            subs = r.scalars().all()
        if not subs:
            return
        user_ids = [s.line_user_id for s in subs]
        base_url = os.getenv("BASE_URL", "")

        for fn, label in [(breakout_screener, "下週潛力突破"), (value_screener, "存股精選")]:
            rows = fn()
            page_rows, total = paginate(rows, 1)
            path = generate_report_image(
                stocks=page_rows, group=f"週五精選｜{label}",
                market_state=os.getenv("MARKET_STATE", "unknown"),
            )
            if base_url:
                await push_report_image(path, user_ids, cfg.line_channel_access_token, base_url, alt_text=label)
            logger.info(f"[FridaySummary] {label} 推送 {len(user_ids)} 人")
    except Exception as e:
        logger.error(f"Friday summary push failed: {e}")


async def _push_group_report():
    """每日 19:30：產生族群連動選股表圖片 → 推送給所有訂閱者"""
    try:
        import os
        from sqlalchemy import select
        from ..models.models import Subscriber
        from ..models.database import settings as cfg
        from backend.services.generate_report_image import generate_and_push

        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(Subscriber).where(Subscriber.subscribed_morning == True)
            )
            subs = r.scalars().all()

        if not subs:
            logger.info("[GroupReport] 無訂閱者，略過推送")
            return

        user_ids = [s.line_user_id for s in subs]
        base_url  = os.getenv("BASE_URL", "")

        from backend.services.report_screener import (
            ai_screener, momentum_screener, chip_screener, paginate
        )
        from backend.services.generate_report_image import generate_report_image, push_report_image

        tasks = [
            (ai_screener,       "AI族群"),
            (momentum_screener, "動能選股"),
            (chip_screener,     "籌碼選股"),
        ]
        for fn, label in tasks:
            rows = fn()
            page_rows, total = paginate(rows, 1)
            path = generate_report_image(
                stocks=page_rows, group=f"收盤後｜{label}",
                market_state=os.getenv("MARKET_STATE", "unknown"),
                page=1, total_pages=total,
            )
            if base_url:
                await push_report_image(path, user_ids, cfg.line_channel_access_token, base_url, alt_text=label)
            logger.info(f"[GroupReport] {label} 圖片: {path}, 推送 {len(user_ids)} 人")

    except Exception as e:
        logger.error(f"Group report push failed: {e}")


async def _push_portfolio_overlay():
    """每日 19:00 — 持倉健康報告推送給所有訂閱者"""
    try:
        from quant.portfolio_overlay import PortfolioOverlay
        from ..models.database import settings
        overlay = PortfolioOverlay()
        n = await overlay.push_all_subscribers(settings.line_channel_access_token)
        logger.info(f"[PortfolioOverlay] pushed to {n} subscribers")
    except Exception as e:
        logger.error(f"Portfolio overlay job failed: {e}")


async def _push_daily_decision():
    """每日 19:30 — 決策報告推送給所有訂閱者"""
    try:
        from quant.decision_engine import DecisionEngine
        from ..models.database import settings
        engine = DecisionEngine()
        n = await engine.push_all_subscribers(settings.line_channel_access_token)
        logger.info(f"[DecisionEngine] pushed to {n} subscribers")
    except Exception as e:
        logger.error(f"Daily decision job failed: {e}")


# ── Alpha Pipeline 四段排程 ───────────────────────────────────────────────────

async def _run_pipeline_movers():
    """18:00 — Layer 1: 動能啟動掃描 + 資金流向"""
    try:
        from quant.movers_engine import MoversEngine
        engine  = MoversEngine()
        results = await engine.scan()
        if not results:
            results = engine.scan_mock(20)
        logger.info(f"[Pipeline 18:00] movers scan: {len(results)} 檔動能股")
    except Exception as e:
        logger.error(f"[Pipeline 18:00] movers failed: {e}")
    try:
        from quant.capital_flow_engine import CapitalFlowEngine
        from ..models.database import settings
        engine   = CapitalFlowEngine()
        snapshot = await engine.scan()
        logger.info(f"[Pipeline 18:00] capital flow: {snapshot.top_inflow_sector} 流入")
        if snapshot.rotation_warning:
            await engine.push_rotation_warning(snapshot, settings.line_channel_access_token)
    except Exception as e:
        logger.error(f"[Pipeline 18:00] capital_flow failed: {e}")


async def _run_pipeline_scanner_filter():
    """18:15 — Layer 2+3: 三層分類 + 六大過濾 + 族群輪動"""
    try:
        from quant.movers_engine import MoversEngine
        from quant.scanner_engine import ScannerEngine
        from quant.filter_engine import FilterEngine

        movers      = await MoversEngine().scan()
        if not movers:
            movers = MoversEngine().scan_mock(20)

        scan_result = ScannerEngine().classify(movers)
        all_recs    = scan_result.core + scan_result.medium + scan_result.satellite
        filter_res  = FilterEngine().filter(all_recs)
        passed      = filter_res["passed"]

        logger.info(
            "[Pipeline 18:15] scanner: core=%d medium=%d sat=%d | filter pass=%d",
            len(scan_result.core), len(scan_result.medium),
            len(scan_result.satellite), len(passed),
        )
    except Exception as e:
        logger.error(f"[Pipeline 18:15] scanner/filter failed: {e}")
    try:
        from quant.sector_rotation_engine import SectorRotationEngine
        engine    = SectorRotationEngine()
        strengths = await engine.scan()
        signal    = engine.detect_rotation(strengths)
        await engine.save_snapshot(strengths)
        logger.info("[Pipeline 18:15] sector: main=%s rotation=%s",
                    ",".join(signal.mainstream[:2]), signal.rotation_alert)
    except Exception as e:
        logger.error(f"[Pipeline 18:15] sector_rotation failed: {e}")


async def _run_pipeline_research():
    """18:30 — Layer 4: Research 自動核查 + Alpha 衰退檢查"""
    try:
        from quant.movers_engine import MoversEngine
        from quant.scanner_engine import ScannerEngine
        from quant.filter_engine import FilterEngine
        from quant.research_checklist import ResearchChecklist

        movers     = await MoversEngine().scan() or MoversEngine().scan_mock(20)
        scan_res   = ScannerEngine().classify(movers)
        all_recs   = scan_res.core + scan_res.medium + scan_res.satellite
        filter_res = FilterEngine().filter(all_recs)
        passed     = filter_res["passed"][:5]

        checker = ResearchChecklist()
        results = []
        for rec in passed:
            code = rec.stock_id if hasattr(rec, "stock_id") else rec.get("stock_id", "")
            r    = await checker.check(code)
            results.append(f"{code}:{r.overall}({r.auto_pass}/6)")

        logger.info("[Pipeline 18:30] research: %s", " ".join(results))
    except Exception as e:
        logger.error(f"[Pipeline 18:30] research failed: {e}")
    try:
        from quant.alpha_decay_engine import AlphaDecayEngine
        from quant.meta_alpha_engine import KNOWN_ALPHAS
        engine  = AlphaDecayEngine()
        dead    = []
        for alpha in KNOWN_ALPHAS:
            state   = await engine._load_state(alpha)
            history = state.get("ic_history", [])
            if history:
                health = await engine.update_ic(alpha, history[-1])
                if health.status == "DEAD":
                    dead.append(alpha)
        logger.info("[Pipeline 18:30] alpha_decay: %d dead factors=%s",
                    len(dead), dead)
    except Exception as e:
        logger.error(f"[Pipeline 18:30] alpha_decay failed: {e}")


async def _run_pipeline_overlay_prep():
    """18:45 — Layer 5: Portfolio Overlay 預熱 + Conviction 批量計算"""
    try:
        from quant.portfolio_overlay import PortfolioOverlay
        from ..models.database import AsyncSessionLocal
        from ..models.models import Subscriber
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            r    = await db.execute(select(Subscriber))
            subs = r.scalars().all()

        overlay = PortfolioOverlay()
        total_signals = 0
        for sub in subs[:10]:
            uid = sub.line_user_id
            if uid:
                signals = await overlay.scan(uid)
                total_signals += len(signals)

        logger.info("[Pipeline 18:45] overlay prep: %d 檔持倉已掃描", total_signals)
    except Exception as e:
        logger.error(f"[Pipeline 18:45] overlay prep failed: {e}")
    try:
        from quant.conviction_engine import ConvictionEngine
        from quant.movers_engine import MoversEngine
        from quant.scanner_engine import ScannerEngine
        engine   = ConvictionEngine()
        movers   = await MoversEngine().scan() or MoversEngine().scan_mock(15)
        scan_res = ScannerEngine().classify(movers)
        all_recs = scan_res.core + scan_res.medium
        results  = engine.batch_compute([
            {"mover": m, "scan_rec": r, "research": None, "regime": {"regime": "UNKNOWN", "confidence": 0.5}}
            for m, r in zip(movers[:10], all_recs[:10])
        ])
        logger.info("[Pipeline 18:45] conviction: %d 檔達交易門檻", len(results))
    except Exception as e:
        logger.error(f"[Pipeline 18:45] conviction failed: {e}")


async def _run_youtube_fetch():
    try:
        from ..services.youtube_alpha_engine import run_daily_fetch
        await run_daily_fetch()
    except Exception as e:
        logger.error(f"YouTube fetch failed: {e}")


async def _run_analyst_alert_check():
    try:
        from ..services.analyst_alert_engine import run_daily_alert_check
        await run_daily_alert_check()
    except Exception as e:
        logger.error(f"Analyst alert check failed: {e}")


async def _run_monthly_tier_update():
    try:
        from ..services.analyst_quality_engine import run_monthly_tier_update, generate_monthly_report
        from ..services.analyst_performance_engine import run_daily_performance_update
        await run_daily_performance_update()
        await run_monthly_tier_update()
        report = await generate_monthly_report()
        # 推送月度評比給所有訂閱者
        from ..models.database import AsyncSessionLocal, settings
        from ..models.models import Subscriber
        from sqlalchemy import select
        import httpx
        async with AsyncSessionLocal() as db:
            r    = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
            subs = r.scalars().all()
        headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
        async with httpx.AsyncClient(timeout=30) as c:
            for sub in subs:
                try:
                    await c.post(
                        "https://api.line.me/v2/bot/message/push",
                        json={"to": sub.line_user_id, "messages": [{"type": "text", "text": report}]},
                        headers=headers,
                    )
                except Exception:
                    pass
        logger.info(f"[monthly_tier] pushed report to {len(subs)} subscribers")
    except Exception as e:
        logger.error(f"Monthly tier update failed: {e}")


async def _run_analyst_performance():
    try:
        from ..services.analyst_performance_engine import run_daily_performance_update
        from ..services.analyst_consensus_engine import run_daily_consensus
        from ..services.analyst_topic_engine import update_topics_from_calls
        await run_daily_performance_update()
        await run_daily_consensus()
        await update_topics_from_calls()
    except Exception as e:
        logger.error(f"Analyst performance update failed: {e}")


async def _push_analyst_consensus():
    try:
        from ..services.analyst_heatmap import push_consensus_report
        await push_consensus_report()
    except Exception as e:
        logger.error(f"Analyst consensus push failed: {e}")


async def _push_autonomous_research():
    try:
        from ..services.autonomous_research import push_daily_research
        await push_daily_research()
    except Exception as e:
        logger.error(f"Autonomous research push failed: {e}")


async def _run_hedge_fund_agent():
    try:
        from ..services.hedge_fund_agent import run_agent_pipeline
        report = await run_agent_pipeline("system")
        logger.info(f"[HedgeFundAgent] decisions={len(report.decisions)} health={report.health_score}")
    except Exception as e:
        logger.error(f"Hedge fund agent run failed: {e}")


async def _push_agent_report():
    try:
        from ..services.hedge_fund_agent import push_agent_report
        await push_agent_report()
    except Exception as e:
        logger.error(f"Agent report push failed: {e}")


async def _push_smart_money_v2():
    try:
        from ..services.broker_tracker import push_smart_money_alerts
        await push_smart_money_alerts()
    except Exception as e:
        logger.error(f"Smart money v2 push failed: {e}")


async def _update_public_rankings():
    try:
        from ..services.public_portfolio_service import update_weekly_returns
        await update_weekly_returns()
        logger.info("[PublicRankings] weekly returns updated")
    except Exception as e:
        logger.error(f"Public rankings update failed: {e}")


async def _run_post_market_breadth():
    try:
        from ..services.market_breadth import run_breadth_check
        from ..services.rs_engine import get_top20, format_rs_ranking
        await run_breadth_check()
        records = get_top20()
        logger.info(f"[PostMarket] RS top={records[0].stock_id if records else 'N/A'}")
    except Exception as e:
        logger.error(f"Post-market breadth/RS failed: {e}")


async def _push_sector_heatmap():
    try:
        from ..services.sector_heatmap import push_heatmap
        await push_heatmap()
    except Exception as e:
        logger.error(f"Sector heatmap push failed: {e}")


async def _push_portfolio_manager():
    try:
        from ..services.portfolio_manager import push_daily_portfolio_advice
        await push_daily_portfolio_advice()
    except Exception as e:
        logger.error(f"Portfolio manager push failed: {e}")


async def _push_mistake_detector():
    try:
        from ..services.mistake_detector import push_weekly_mistake_reports
        await push_weekly_mistake_reports()
    except Exception as e:
        logger.error(f"Mistake detector push failed: {e}")


async def _push_ai_feed():
    try:
        from ..services.ai_feed import push_ai_feed
        await push_ai_feed()
    except Exception as e:
        logger.error(f"AI Feed push failed: {e}")


async def _run_smart_alert():
    try:
        from ..services.smart_alert_v2 import run_smart_alert_scan
        await run_smart_alert_scan()
    except Exception as e:
        logger.error(f"Smart Alert scan failed: {e}")


async def _push_watchlist_daily():
    try:
        from ..services.watchlist_monitor import push_daily_watchlist_reports
        await push_daily_watchlist_reports()
    except Exception as e:
        logger.error(f"Watchlist daily push failed: {e}")


async def _run_breadth_check():
    try:
        from ..services.market_breadth import run_breadth_check
        await run_breadth_check()
    except Exception as e:
        logger.error(f"Market breadth check failed: {e}")


async def _run_meta_alpha_weekly():
    """週五 18:30 — Meta Alpha 週排名 + 推送報告"""
    try:
        from quant.meta_alpha_engine import MetaAlphaEngine
        from ..models.database import settings
        engine = MetaAlphaEngine()
        await engine.push_weekly_report(settings.line_channel_access_token)
        logger.info("[MetaAlpha] weekly report pushed")
    except Exception as e:
        logger.error(f"[MetaAlpha] weekly report failed: {e}")


# ── 市場情報作戰系統 job handlers ────────────────────────────────────────────

async def _run_market_intel_scan():
    """15:30 — 市場週期/領先滯後/主題擴散/法人足跡掃描"""
    try:
        from quant.timeline_engine import run_market_timeline
        from quant.lead_lag_engine import run_lead_lag_scan
        from quant.theme_propagation_engine import run_theme_propagation
        from quant.institutional_footprint_engine import scan_institutional_footprint

        results_tl = await run_market_timeline()
        logger.info("[Intel 15:30] timeline: %d stocks scanned", len(results_tl))

        result_ll = await run_lead_lag_scan()
        logger.info("[Intel 15:30] lead_lag: %d triggered", len(result_ll.triggered_signals))

        results_theme = await run_theme_propagation()
        logger.info("[Intel 15:30] theme: top=%s %.0f%%",
                    results_theme[0].theme if results_theme else "N/A",
                    results_theme[0].total_score if results_theme else 0)

        results_fp = await scan_institutional_footprint()
        smart = sum(1 for r in results_fp if r.is_smart_money)
        logger.info("[Intel 15:30] footprint: %d smart money", smart)
    except Exception as e:
        logger.error(f"[Intel 15:30] market intel scan failed: {e}")


async def _run_drift_detection_job():
    """16:30 — 分析師觀點飄移偵測並推送高嚴重度警報"""
    try:
        from quant.analyst_drift_detector import get_drift_from_db
        from ..models.database import settings
        import httpx

        report = await get_drift_from_db()
        if not report.high_severity:
            logger.info("[drift 16:30] no high-severity drift alerts")
            return

        from ..models.database import AsyncSessionLocal
        from ..models.models import Subscriber
        from sqlalchemy import select

        text = report.to_line_text()
        async with AsyncSessionLocal() as db:
            r    = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
            subs = r.scalars().all()

        headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
        async with httpx.AsyncClient(timeout=30) as c:
            for sub in subs:
                try:
                    await c.post(
                        "https://api.line.me/v2/bot/message/push",
                        json={"to": sub.line_user_id, "messages": [{"type": "text", "text": text}]},
                        headers=headers,
                    )
                except Exception:
                    pass
        logger.info("[drift 16:30] pushed %d alerts to %d subscribers",
                    len(report.high_severity), len(subs))
    except Exception as e:
        logger.error(f"[drift 16:30] drift detection failed: {e}")


async def _push_euphoria_stress():
    """17:00 — 計算過熱/壓力指數並推送"""
    try:
        from quant.euphoria_engine import compute_euphoria
        from quant.stress_engine import compute_stress
        from ..models.database import settings, AsyncSessionLocal
        from ..models.models import Subscriber
        from sqlalchemy import select
        import httpx

        euphoria = await compute_euphoria()
        stress   = await compute_stress()

        text = (
            f"{euphoria.to_line_text()}\n\n"
            f"─────────────────\n\n"
            f"{stress.to_line_text()}"
        )

        async with AsyncSessionLocal() as db:
            r    = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
            subs = r.scalars().all()

        if not subs:
            return

        headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
        async with httpx.AsyncClient(timeout=30) as c:
            for sub in subs:
                try:
                    await c.post(
                        "https://api.line.me/v2/bot/message/push",
                        json={"to": sub.line_user_id, "messages": [{"type": "text", "text": text}]},
                        headers=headers,
                    )
                except Exception:
                    pass
        logger.info("[intel 17:00] euphoria=%.1f stress=%.1f pushed to %d",
                    euphoria.euphoria_score, stress.stress_score, len(subs))
    except Exception as e:
        logger.error(f"[intel 17:00] euphoria/stress push failed: {e}")


async def _push_ai_debate():
    """20:30 — AI 多空辯論（今日重點個股）"""
    try:
        from quant.ai_debate_engine import run_ai_debate
        from quant.movers_engine import MoversEngine
        from ..models.database import settings, AsyncSessionLocal
        from ..models.models import Subscriber
        from sqlalchemy import select
        import httpx

        movers = await MoversEngine().scan()
        if not movers:
            movers = MoversEngine().scan_mock(5)

        target = movers[:2] if movers else []
        if not target:
            return

        texts = []
        for m in target:
            sid   = m.stock_id if hasattr(m, "stock_id") else m.get("stock_id", "")
            sname = m.name     if hasattr(m, "name")     else m.get("name", sid)
            result = await run_ai_debate(sid, sname)
            texts.append(result.to_line_text())

        full_text = "\n\n─────────────────\n\n".join(texts)

        async with AsyncSessionLocal() as db:
            r    = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
            subs = r.scalars().all()

        if not subs:
            return

        headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
        async with httpx.AsyncClient(timeout=30) as c:
            for sub in subs:
                try:
                    await c.post(
                        "https://api.line.me/v2/bot/message/push",
                        json={"to": sub.line_user_id, "messages": [{"type": "text", "text": full_text}]},
                        headers=headers,
                    )
                except Exception:
                    pass
        logger.info("[intel 20:30] debate pushed %d stocks to %d subscribers",
                    len(target), len(subs))
    except Exception as e:
        logger.error(f"[intel 20:30] ai debate push failed: {e}")


async def _run_prediction_market_weekly():
    """週五 19:00 — 預測市場快照 + 過期命題自動結算"""
    try:
        from quant.prediction_market_engine import get_snapshot
        from datetime import datetime

        snapshot = await get_snapshot()

        expired = [p for p in snapshot.predictions if p.days_left <= 0]
        logger.info("[predict Fri19:00] active=%d resolved=%d expired_today=%d acc=%.0f%%",
                    snapshot.total_active, snapshot.total_resolved,
                    len(expired), snapshot.accuracy_30d * 100)
    except Exception as e:
        logger.error(f"[predict Fri19:00] prediction market weekly failed: {e}")
