"""Agent A — 數據員：每日自動抓取並更新所有股票數據

執行順序（每日 18:00 盤後）：
  1. 從 TWSE 取得今日活躍股票清單
  2. 對每檔股票抓取 FinMind 調整後股價 / 月營收 / 財務報表 / 三大法人
  3. 清洗異常數據並標記
  4. 寫入 PostgreSQL（upsert）
  5. 觸發 Agent B 計算評分

節流策略（免費版 30 req/min）：
  - 同一批次最多處理 TOP_STOCKS_PER_RUN 檔
  - 優先處理：自選股 + 庫存持股 + 前次高分股
"""
import asyncio
from datetime import date, timedelta, datetime
from loguru import logger
from sqlalchemy import select

from ..models.database import AsyncSessionLocal
from ..models.models import (
    MonthlyRevenue, StockFinancials, StockScore, Watchlist, Portfolio,
)
from .finmind_service import (
    fetch_adj_price, fetch_monthly_revenue, fetch_financials,
    fetch_institutional_detail, fetch_tw_stock_info,
)
from .twse_service import fetch_stock_list

TOP_STOCKS_PER_RUN = 100   # 每次最多更新 100 檔（免費版限額）
PRIORITY_CODES: list[str] = []  # 動態填入優先股票

# ETF 代碼格式：台灣 ETF 以 "00" 開頭（0050, 0056, 00878…）
# FinMind TaiwanFinancialStatements / TaiwanStockMonthRevenue 不收錄 ETF，
# 送出這些代碼會得到 422，必須跳過。
def _is_etf(code: str) -> bool:
    # 台股普通股固定 4 位數；ETF（0050, 00878…）或可轉換公司債（020xxx）均跳過
    if len(code) != 4:
        return True
    return code.startswith("00")


async def _get_priority_codes() -> list[str]:
    """取得優先更新的股票代碼：庫存 + 自選股 + 前次高分"""
    codes: set[str] = set()
    async with AsyncSessionLocal() as db:
        # 庫存持股
        r = await db.execute(select(Portfolio.stock_code).distinct())
        codes.update(row[0] for row in r.fetchall() if row[0])
        # 自選股
        r = await db.execute(select(Watchlist.stock_code).distinct())
        codes.update(row[0] for row in r.fetchall() if row[0])
        # 前次高分前 50
        r = await db.execute(
            select(StockScore.stock_code)
            .order_by(StockScore.total_score.desc())
            .limit(50)
        )
        codes.update(row[0] for row in r.fetchall() if row[0])
    return list(codes)


async def _upsert_monthly_revenue(db, stock_code: str, rows: list[dict]):
    for r in rows:
        if not r.get("year") or not r.get("month"):
            continue
        existing = await db.execute(
            select(MonthlyRevenue).where(
                MonthlyRevenue.stock_code == stock_code,
                MonthlyRevenue.year == r["year"],
                MonthlyRevenue.month == r["month"],
            )
        )
        rec = existing.scalar_one_or_none()
        if rec:
            rec.revenue     = r.get("revenue")
            rec.revenue_mom = r.get("mom")
            rec.revenue_yoy = r.get("yoy")
            rec.cum_revenue = r.get("cum_revenue")
            rec.cum_revenue_yoy = r.get("cum_yoy")
            rec.updated_at  = datetime.utcnow()
        else:
            db.add(MonthlyRevenue(
                stock_code  = stock_code,
                year        = r["year"],
                month       = r["month"],
                revenue     = r.get("revenue"),
                revenue_mom = r.get("mom"),
                revenue_yoy = r.get("yoy"),
                cum_revenue = r.get("cum_revenue"),
                cum_revenue_yoy = r.get("cum_yoy"),
                updated_at  = datetime.utcnow(),
            ))
    await db.commit()


async def _upsert_financials(db, stock_code: str, rows: list[dict]):
    for r in rows:
        if not r.get("year") or not r.get("quarter"):
            continue
        # 異常值標記：毛利率或淨利率超出合理範圍
        is_anomaly = (
            abs(r.get("gross_margin", 0) or 0) > 200 or
            abs(r.get("net_margin", 0) or 0) > 200
        )
        existing = await db.execute(
            select(StockFinancials).where(
                StockFinancials.stock_code == stock_code,
                StockFinancials.year == r["year"],
                StockFinancials.quarter == r["quarter"],
            )
        )
        rec = existing.scalar_one_or_none()
        if rec:
            rec.revenue           = r.get("revenue")
            rec.gross_profit      = r.get("gross_profit")
            rec.operating_income  = r.get("operating_income")
            rec.net_income        = r.get("net_income")
            rec.eps               = r.get("eps")
            rec.gross_margin      = r.get("gross_margin")
            rec.operating_margin  = r.get("operating_margin")
            rec.net_margin        = r.get("net_margin")
            rec.is_anomaly        = is_anomaly
            rec.updated_at        = datetime.utcnow()
        else:
            db.add(StockFinancials(
                stock_code       = stock_code,
                year             = r["year"],
                quarter          = r["quarter"],
                revenue          = r.get("revenue"),
                gross_profit     = r.get("gross_profit"),
                operating_income = r.get("operating_income"),
                net_income       = r.get("net_income"),
                eps              = r.get("eps"),
                gross_margin     = r.get("gross_margin"),
                operating_margin = r.get("operating_margin"),
                net_margin       = r.get("net_margin"),
                is_anomaly       = is_anomaly,
                updated_at       = datetime.utcnow(),
            ))
    await db.commit()


async def update_single_stock(stock_code: str, force: bool = False) -> bool:
    """
    更新單一股票的所有 FinMind 資料。
    force=True 則忽略上次更新時間，強制重抓。
    ETF（以 "00" 開頭）跳過財務報表 / 月營收 API — FinMind 不收錄此類資料。
    """
    if _is_etf(stock_code):
        return True   # ETF 無需抓財務，直接視為成功

    today = date.today().strftime("%Y-%m-%d")

    # 檢查是否需要更新（今日已更新過則跳過）
    if not force:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(MonthlyRevenue.updated_at)
                .where(MonthlyRevenue.stock_code == stock_code)
                .order_by(MonthlyRevenue.updated_at.desc())
                .limit(1)
            )
            last = r.scalar()
            if last and last.date().isoformat() == today:
                return True  # 今日已更新

    start_date = (date.today() - timedelta(days=400)).strftime("%Y-%m-%d")

    try:
        # 並行抓取月營收 + 財務報表（價格在 Agent B 計算時再抓）
        rev_task = asyncio.create_task(fetch_monthly_revenue(stock_code, start_date))
        fin_task = asyncio.create_task(fetch_financials(stock_code, start_date))

        revenues, financials = await asyncio.gather(rev_task, fin_task)

        async with AsyncSessionLocal() as db:
            if revenues:
                await _upsert_monthly_revenue(db, stock_code, revenues)
            if financials:
                await _upsert_financials(db, stock_code, financials)

        logger.info(f"[Pipeline] {stock_code}: rev={len(revenues)} fin={len(financials)}")
        return True
    except Exception as e:
        logger.error(f"[Pipeline] {stock_code} error: {e}")
        return False


async def run_daily_pipeline(trigger_scoring: bool = True):
    """
    主排程入口（每日 18:00）
    1. 取優先股票清單
    2. 補充 TWSE 活躍股
    3. 逐一更新
    4. 觸發 Agent B 評分
    """
    logger.info("[Pipeline] Agent A 啟動...")

    priority = await _get_priority_codes()

    # 補充 TWSE 全市場清單到 quota 上限
    if len(priority) < TOP_STOCKS_PER_RUN:
        try:
            twse_list = await fetch_stock_list()
            all_codes = [
                s["code"] for s in twse_list
                if s.get("code", "").isdigit() and not _is_etf(s["code"])
            ]
            for code in all_codes:
                if code not in priority and len(priority) < TOP_STOCKS_PER_RUN:
                    priority.append(code)
        except Exception as e:
            logger.error(f"[Pipeline] TWSE list error: {e}")

    logger.info(f"[Pipeline] 準備更新 {len(priority)} 檔股票")

    success = fail = 0
    for i, code in enumerate(priority):
        ok = await update_single_stock(code)
        if ok:
            success += 1
        else:
            fail += 1
        # 每 10 檔休息一下，避免 rate limit
        if (i + 1) % 10 == 0:
            await asyncio.sleep(3)

    logger.info(f"[Pipeline] 完成：成功 {success} / 失敗 {fail}")

    if trigger_scoring:
        await asyncio.sleep(5)
        from .score_updater import run_score_update
        await run_score_update()
