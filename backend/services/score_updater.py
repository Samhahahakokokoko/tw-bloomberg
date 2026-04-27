"""Agent B — 分析師：對所有有資料的股票計算三維度評分並存入 DB

每日 18:30 執行（Pipeline 完成後）
"""
import asyncio
from datetime import date, timedelta, datetime
from loguru import logger
from sqlalchemy import select

from ..models.database import AsyncSessionLocal
from ..models.models import StockScore, StockFinancials, MonthlyRevenue
from .indicator_engine import (
    score_fundamental, score_chip, score_technical,
    calc_total_score, calc_confidence,
)
from .finmind_service import fetch_adj_price, fetch_institutional_detail
from .twse_service import fetch_realtime_quote


async def _load_financials(stock_code: str) -> list[dict]:
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(StockFinancials)
            .where(
                StockFinancials.stock_code == stock_code,
                StockFinancials.is_anomaly == False,
            )
            .order_by(StockFinancials.year, StockFinancials.quarter)
            .limit(8)
        )
        rows = r.scalars().all()
    return [
        {
            "year":             h.year,
            "quarter":          h.quarter,
            "gross_margin":     h.gross_margin,
            "operating_margin": h.operating_margin,
            "net_margin":       h.net_margin,
            "eps":              h.eps,
        }
        for h in rows
    ]


async def _load_revenues(stock_code: str) -> list[dict]:
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(MonthlyRevenue)
            .where(MonthlyRevenue.stock_code == stock_code)
            .order_by(MonthlyRevenue.year, MonthlyRevenue.month)
            .limit(13)
        )
        rows = r.scalars().all()
    return [
        {
            "year":    h.year,
            "month":   h.month,
            "revenue": h.revenue,
            "yoy":     h.revenue_yoy,
            "mom":     h.revenue_mom,
        }
        for h in rows
    ]


async def calc_and_save_score(stock_code: str, today: str) -> bool:
    try:
        # 財務 + 月營收
        fin_task = asyncio.create_task(_load_financials(stock_code))
        rev_task = asyncio.create_task(_load_revenues(stock_code))

        # 抓取近 60 日調整後股價（技術指標）
        start_60 = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")
        price_task = asyncio.create_task(fetch_adj_price(stock_code, start_60))

        # 抓取近 30 日三大法人
        start_30 = (date.today() - timedelta(days=40)).strftime("%Y-%m-%d")
        chip_task = asyncio.create_task(fetch_institutional_detail(stock_code, start_30))

        financials, revenues, prices, chips = await asyncio.gather(
            fin_task, rev_task, price_task, chip_task
        )

        if not prices:
            return False

        # 技術面評分
        closes  = [p["close"]  for p in prices if p.get("close")]
        highs   = [p["high"]   for p in prices if p.get("high")]
        lows    = [p["low"]    for p in prices if p.get("low")]
        volumes = [p["volume"] for p in prices if p.get("volume") is not None]

        tech_score, tech_detail = score_technical(closes, highs, lows, volumes)
        fund_score, fund_detail = score_fundamental(revenues, financials)
        chip_score_val, chip_detail = score_chip(chips)

        all_detail = {**fund_detail, **chip_detail, **tech_detail}
        total = calc_total_score(fund_score, chip_score_val, tech_score)
        conf  = calc_confidence(total, all_detail)

        # 取股票名稱
        try:
            q = await fetch_realtime_quote(stock_code)
            stock_name = q.get("name", "")
        except Exception:
            stock_name = ""

        # Upsert
        async with AsyncSessionLocal() as db:
            existing = await db.execute(
                select(StockScore).where(
                    StockScore.stock_code == stock_code,
                    StockScore.score_date == today,
                )
            )
            rec = existing.scalar_one_or_none()

            vals = dict(
                stock_name         = stock_name,
                fundamental_score  = fund_score,
                chip_score         = chip_score_val,
                technical_score    = tech_score,
                total_score        = total,
                confidence         = conf,
                revenue_yoy        = all_detail.get("revenue_yoy"),
                gross_margin       = all_detail.get("gross_margin"),
                three_margins_up   = all_detail.get("three_margins_up", False),
                eps_growth_qtrs    = all_detail.get("eps_growth_qtrs", 0),
                foreign_consec_buy = all_detail.get("foreign_consec_buy", 0),
                trust_consec_buy   = all_detail.get("trust_consec_buy", 0),
                ma_aligned         = all_detail.get("ma_aligned", False),
                kd_golden_cross    = all_detail.get("kd_golden_cross", False),
                vol_breakout       = all_detail.get("vol_breakout", False),
                bb_breakout        = all_detail.get("bb_breakout", False),
                updated_at         = datetime.utcnow(),
            )

            if rec:
                for k, v in vals.items():
                    setattr(rec, k, v)
            else:
                db.add(StockScore(
                    stock_code = stock_code,
                    score_date = today,
                    **vals,
                ))
            await db.commit()

        logger.info(f"[Score] {stock_code}: F={fund_score} C={chip_score_val} T={tech_score} → {total}")
        return True

    except Exception as e:
        logger.error(f"[Score] {stock_code} error: {e}")
        return False


async def run_score_update():
    """對所有 DB 中有財務資料的股票重算評分"""
    today = date.today().strftime("%Y-%m-%d")
    logger.info("[Score] Agent B 啟動...")

    async with AsyncSessionLocal() as db:
        r = await db.execute(select(MonthlyRevenue.stock_code).distinct())
        codes = [row[0] for row in r.fetchall() if row[0]]

    logger.info(f"[Score] 處理 {len(codes)} 檔股票")
    success = fail = 0

    for i, code in enumerate(codes):
        ok = await calc_and_save_score(code, today)
        if ok: success += 1
        else:  fail += 1
        # rate limit 保護
        if (i + 1) % 5 == 0:
            await asyncio.sleep(2)

    logger.info(f"[Score] 完成：成功 {success} / 失敗 {fail}")
