"""Analyst Performance Engine — 追蹤並更新分析師歷史準確率"""
from __future__ import annotations

from datetime import datetime, timedelta
from loguru import logger
from sqlalchemy import select, and_


async def update_call_results():
    """更新 5 日前和 20 日前推薦的實際結果"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import AnalystCall
    from .twse_service import fetch_realtime_quote

    today    = datetime.now()
    date_5d  = (today - timedelta(days=7)).strftime("%Y-%m-%d")   # 約5個交易日
    date_20d = (today - timedelta(days=28)).strftime("%Y-%m-%d")  # 約20個交易日

    async with AsyncSessionLocal() as db:
        # 更新 5 日結果（7天前的推薦）
        r = await db.execute(
            select(AnalystCall)
            .where(AnalystCall.date == date_5d)
            .where(AnalystCall.result_5d == None)
        )
        calls_5d = r.scalars().all()

        for call in calls_5d:
            try:
                q = await fetch_realtime_quote(call.stock_id)
                if q and call.entry_price > 0:
                    curr  = q.get("price", 0) or 0
                    ret5d = (curr - call.entry_price) / call.entry_price
                    call.result_5d = round(ret5d, 4)
                    # 判斷是否正確（bullish 且漲 / bearish 且跌）
                    if call.sentiment in ("bullish", "strong_bullish"):
                        call.was_correct = ret5d > 0
                    elif call.sentiment in ("bearish", "strong_bearish"):
                        call.was_correct = ret5d < 0
                    else:
                        call.was_correct = abs(ret5d) < 0.02  # neutral → 小幅波動視為正確
            except Exception as e:
                logger.debug(f"[perf] result_5d update failed {call.stock_id}: {e}")

        # 更新 20 日結果
        r2 = await db.execute(
            select(AnalystCall)
            .where(AnalystCall.date == date_20d)
            .where(AnalystCall.result_20d == None)
        )
        calls_20d = r2.scalars().all()

        for call in calls_20d:
            try:
                q = await fetch_realtime_quote(call.stock_id)
                if q and call.entry_price > 0:
                    curr   = q.get("price", 0) or 0
                    ret20d = (curr - call.entry_price) / call.entry_price
                    call.result_20d = round(ret20d, 4)
            except Exception:
                pass

        await db.commit()
    logger.info(f"[perf] updated {len(calls_5d)} 5d results, {len(calls_20d)} 20d results")


async def recalculate_analyst_stats():
    """重新計算所有分析師的勝率和平均報酬"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Analyst, AnalystCall
    from sqlalchemy import func

    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Analyst).where(Analyst.is_active == True))
        analysts = r.scalars().all()

        for analyst in analysts:
            r2 = await db.execute(
                select(AnalystCall)
                .where(AnalystCall.analyst_id == analyst.analyst_id)
                .where(AnalystCall.was_correct != None)
            )
            calls = r2.scalars().all()

            if not calls:
                continue

            win_count  = sum(1 for c in calls if c.was_correct)
            win_rate   = win_count / len(calls)
            avg_return = sum(c.result_5d for c in calls if c.result_5d is not None) / max(1, len(calls))

            # 最大回撤（最差單次報酬）
            returns     = [c.result_5d for c in calls if c.result_5d is not None]
            max_drawdown = min(returns) if returns else 0.0

            # 可信度分數 = win_rate * 70 + avg_return * 30（最大100）
            reliability = min(100, win_rate * 70 + max(0, avg_return * 100) * 0.3)

            analyst.total_calls       = len(calls)
            analyst.win_rate          = round(win_rate, 4)
            analyst.avg_return        = round(avg_return, 4)
            analyst.max_drawdown      = round(max_drawdown, 4)
            analyst.reliability_score = round(reliability, 1)
            analyst.updated_at        = datetime.utcnow()

        await db.commit()
    logger.info("[perf] analyst stats recalculated")


async def run_daily_performance_update():
    """每日 17:00 執行績效更新"""
    try:
        await update_call_results()
        await recalculate_analyst_stats()
        logger.info("[perf] daily performance update complete")
    except Exception as e:
        logger.error(f"[perf] update failed: {e}")
