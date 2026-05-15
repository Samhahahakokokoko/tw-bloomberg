from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from ..models.database import get_db, AsyncSessionLocal
from ..models.models import Alert, NewsArticle, Subscriber
from ..services import twse_service, portfolio_service
from sqlalchemy import select
from loguru import logger

router = APIRouter()


# ── Schemas ────────────────────────────────────────────────────────────────────

class HoldingCreate(BaseModel):
    stock_code: str
    shares: int
    cost_price: float


class HoldingUpdate(BaseModel):
    shares: int
    cost_price: float


class AlertCreate(BaseModel):
    stock_code: str
    alert_type: str  # price_above / price_below
    threshold: float
    line_user_id: str = ""


# ── Market ─────────────────────────────────────────────────────────────────────

@router.get("/market/overview")
async def market_overview():
    return await twse_service.fetch_market_overview()


@router.get("/market/stocks")
async def list_stocks():
    return await twse_service.fetch_stock_list()


# ── Quote ──────────────────────────────────────────────────────────────────────

@router.get("/quote/{stock_code}")
async def get_quote(stock_code: str):
    data = await twse_service.fetch_realtime_quote(stock_code)
    if not data:
        raise HTTPException(404, f"No data for {stock_code}")
    return data


@router.get("/quote/{stock_code}/kline")
async def get_kline(stock_code: str, date: str = Query(None)):
    return await twse_service.fetch_kline(stock_code, date)


@router.get("/quote/{stock_code}/institutional")
async def get_institutional(stock_code: str):
    return await twse_service.fetch_institutional(stock_code)


# ── Portfolio ──────────────────────────────────────────────────────────────────

@router.get("/portfolio")
async def list_portfolio(db: AsyncSession = Depends(get_db)):
    return await portfolio_service.get_portfolio(db)


@router.post("/portfolio", status_code=201)
async def add_holding(payload: HoldingCreate, db: AsyncSession = Depends(get_db)):
    return await portfolio_service.add_holding(db, payload.stock_code, payload.shares, payload.cost_price)


@router.put("/portfolio/{holding_id}")
async def update_holding(holding_id: int, payload: HoldingUpdate, db: AsyncSession = Depends(get_db)):
    h = await portfolio_service.update_holding(db, holding_id, payload.shares, payload.cost_price)
    if not h:
        raise HTTPException(404, "Holding not found")
    return h


@router.delete("/portfolio/{holding_id}")
async def delete_holding(holding_id: int, db: AsyncSession = Depends(get_db)):
    ok = await portfolio_service.remove_holding(db, holding_id)
    if not ok:
        raise HTTPException(404, "Holding not found")
    return {"deleted": True}


@router.post("/portfolio/fix-names")
async def fix_portfolio_names(db: AsyncSession = Depends(get_db)):
    """重新從 TWSE/TPEX 抓取所有持股名稱，修正錯誤的股票名稱"""
    from ..models.models import Portfolio
    result = await db.execute(select(Portfolio))
    holdings = result.scalars().all()
    fixed = []
    for h in holdings:
        try:
            quote = await twse_service.fetch_realtime_quote(h.stock_code)
            new_name = quote.get("name", "")
            if new_name and new_name != h.stock_name:
                old_name = h.stock_name
                h.stock_name = new_name
                fixed.append({"code": h.stock_code, "old": old_name, "new": new_name})
        except Exception as e:
            logger.error(f"Fix name error {h.stock_code}: {e}")
    await db.commit()
    return {"fixed": fixed, "total": len(fixed)}


# ── Alerts ─────────────────────────────────────────────────────────────────────

@router.get("/alerts")
async def list_alerts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Alert).where(Alert.is_active == True))
    return result.scalars().all()


@router.post("/alerts", status_code=201)
async def create_alert(payload: AlertCreate, db: AsyncSession = Depends(get_db)):
    alert = Alert(**payload.model_dump())
    db.add(alert)
    await db.commit()
    await db.refresh(alert)
    return alert


@router.delete("/alerts/{alert_id}")
async def delete_alert(alert_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "Alert not found")
    await db.delete(a)
    await db.commit()
    return {"deleted": True}


# ── News ───────────────────────────────────────────────────────────────────────

@router.get("/news")
async def list_news(
    limit: int = Query(20, le=100),
    sentiment: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    q = select(NewsArticle).order_by(NewsArticle.published_at.desc()).limit(limit)
    if sentiment:
        q = q.where(NewsArticle.sentiment == sentiment)
    result = await db.execute(q)
    return result.scalars().all()


# ── AI ─────────────────────────────────────────────────────────────────────────

class AskRequest(BaseModel):
    question: str


@router.post("/ai/ask")
async def ai_ask(payload: AskRequest):
    from ..models.database import settings
    if not settings.anthropic_api_key:
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured")
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system="你是專業台股投資分析師，用繁體中文回答，條列重點，語氣專業但易懂。",
            messages=[{"role": "user", "content": payload.question}],
        )
        return {"answer": msg.content[0].text}
    except Exception as e:
        logger.error(f"AI ask error: {e}")
        raise HTTPException(500, str(e))


@router.get("/ai/portfolio-analysis")
async def ai_portfolio_analysis(db: AsyncSession = Depends(get_db)):
    from ..models.database import settings
    if not settings.anthropic_api_key:
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured")

    holdings = await portfolio_service.get_portfolio(db)
    if not holdings:
        raise HTTPException(422, "庫存為空，請先新增持股")

    total_mv = sum(h["market_value"] for h in holdings)
    total_pnl = sum(h["pnl"] for h in holdings)
    total_cost = sum(h["cost_price"] * h["shares"] for h in holdings)

    summary_lines = [f"## 目前庫存（共 {len(holdings)} 檔）\n"]
    for h in holdings:
        weight = h["market_value"] / total_mv * 100 if total_mv else 0
        summary_lines.append(
            f"- {h['stock_code']} {h['stock_name']}: "
            f"{h['shares']}股，成本 {h['cost_price']}，現價 {h['current_price']}，"
            f"損益 {h['pnl_pct']:+.1f}%，佔比 {weight:.1f}%"
        )
    summary_lines.append(f"\n總成本: {total_cost:,.0f} / 總市值: {total_mv:,.0f} / 總損益: {total_pnl:+,.0f} ({total_pnl/total_cost*100:+.1f}%)")

    prompt = "\n".join(summary_lines) + """

請針對此投資組合提供：
1. 集中度風險評估（是否過度集中於特定股票或產業）
2. 各持股目前走勢與技術面觀察
3. 建議的操作策略（停利、加碼、或調整比例）
4. 整體風險提示"""

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system="你是專業台股投資分析師，用繁體中文回答，條列重點，語氣專業但易懂。",
            messages=[{"role": "user", "content": prompt}],
        )
        return {"analysis": msg.content[0].text, "portfolio_summary": holdings}
    except Exception as e:
        logger.error(f"AI portfolio analysis error: {e}")
        raise HTTPException(500, str(e))


# ── Quote 擴充（本益比/殖利率）─────────────────────────────────────────────────

@router.get("/quote/{stock_code}/valuation")
async def get_valuation(stock_code: str):
    """本益比、殖利率、股價淨值比"""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_d")
            data = resp.json()
            item = next((x for x in data if x.get("Code") == stock_code), None)
            if item:
                return {
                    "stock_code": stock_code,
                    "pe_ratio": float(item.get("PEratio", 0) or 0),
                    "pb_ratio": float(item.get("PBratio", 0) or 0),
                    "dividend_yield": float(item.get("DividendYield", 0) or 0),
                    "close_price": float(item.get("ClosePrice", 0) or 0),
                    "date": item.get("Date", ""),
                }
    except Exception as e:
        logger.error(f"Valuation error {stock_code}: {e}")
    raise HTTPException(404, f"No valuation data for {stock_code}")


# ── Dividend ────────────────────────────────────────────────────────────────────

@router.get("/dividend/upcoming")
async def upcoming_dividends():
    from ..services.dividend_service import fetch_upcoming_dividends
    return await fetch_upcoming_dividends()


@router.get("/dividend/{stock_code}")
async def stock_dividends(stock_code: str):
    from ..services.dividend_service import fetch_dividend_by_code
    return await fetch_dividend_by_code(stock_code)


# ── Margin ──────────────────────────────────────────────────────────────────────

@router.get("/margin/{stock_code}")
async def get_margin(stock_code: str):
    from ..services.margin_service import fetch_margin_today
    data = await fetch_margin_today(stock_code)
    if not data:
        raise HTTPException(404, f"No margin data for {stock_code}")
    return data


@router.get("/margin/{stock_code}/history")
async def get_margin_history(stock_code: str, date: str = Query(None)):
    from ..services.margin_service import fetch_margin_history
    return await fetch_margin_history(stock_code, date)


# ── Subscriber ──────────────────────────────────────────────────────────────────

class SubscribeRequest(BaseModel):
    line_user_id: str
    display_name: str = ""
    subscribed_morning: bool = True
    subscribed_weekly: bool = True


@router.post("/subscribe", status_code=201)
async def subscribe(payload: SubscribeRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Subscriber).where(Subscriber.line_user_id == payload.line_user_id)
    )
    sub = result.scalar_one_or_none()
    if sub:
        sub.subscribed_morning = payload.subscribed_morning
        sub.subscribed_weekly = payload.subscribed_weekly
    else:
        sub = Subscriber(**payload.model_dump())
        db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub


@router.get("/subscribers")
async def list_subscribers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Subscriber))
    return result.scalars().all()


# ── Reports (manual trigger) ────────────────────────────────────────────────────

@router.post("/report/morning")
async def trigger_morning_report():
    from ..services.morning_report import generate_morning_report
    report = await generate_morning_report()
    return {"report": report}


@router.post("/report/weekly")
async def trigger_weekly_report():
    from ..services.weekly_report import generate_weekly_report
    report = await generate_weekly_report()
    return {"report": report}


# ── Earnings Reminders ──────────────────────────────────────────────────────────

class EarningsReminderCreate(BaseModel):
    user_id: str = ""
    stock_code: str
    period: str = ""              # e.g. "2025Q1"
    announce_date: str = ""       # YYYY-MM-DD，留空則自動估算
    remind_days_before: int = 3
    line_user_id: str = ""
    expected_eps: Optional[float] = None


class EarningsEpsUpdate(BaseModel):
    actual_eps: float


@router.get("/earnings")
async def list_earnings(user_id: str = Query(""), db: AsyncSession = Depends(get_db)):
    from ..services.earnings_service import list_reminders
    return await list_reminders(db, user_id)


@router.post("/earnings", status_code=201)
async def create_earnings_reminder(payload: EarningsReminderCreate, db: AsyncSession = Depends(get_db)):
    from ..services.earnings_service import add_reminder
    return await add_reminder(
        db,
        user_id=payload.user_id,
        stock_code=payload.stock_code,
        period=payload.period,
        announce_date=payload.announce_date,
        remind_days_before=payload.remind_days_before,
        line_user_id=payload.line_user_id,
        expected_eps=payload.expected_eps,
    )


@router.delete("/earnings/{reminder_id}")
async def delete_earnings_reminder(reminder_id: int, user_id: str = Query(""), db: AsyncSession = Depends(get_db)):
    from ..services.earnings_service import delete_reminder
    ok = await delete_reminder(db, reminder_id, user_id)
    if not ok:
        raise HTTPException(404, "Reminder not found")
    return {"deleted": True}


@router.put("/earnings/{reminder_id}/eps")
async def update_eps(reminder_id: int, payload: EarningsEpsUpdate, db: AsyncSession = Depends(get_db)):
    from ..services.earnings_service import update_actual_eps
    r = await update_actual_eps(db, reminder_id, payload.actual_eps)
    if not r:
        raise HTTPException(404, "Reminder not found")
    return r


@router.get("/earnings/{stock_code}/latest-eps")
async def get_latest_eps(stock_code: str):
    from ..services.earnings_service import fetch_latest_eps
    data = await fetch_latest_eps(stock_code)
    if not data:
        raise HTTPException(404, f"No EPS data for {stock_code}")
    return data


@router.post("/earnings/check-now")
async def trigger_earnings_check():
    from ..services.earnings_service import check_and_push_reminders
    await check_and_push_reminders()
    return {"status": "ok"}


# ── Watchlist ──────────────────────────────────────────────────────────────────

class WatchlistCreate(BaseModel):
    stock_code: str
    stock_name: str = ""
    target_price: Optional[float] = None
    stop_loss: Optional[float] = None
    note: str = ""
    user_id: str = ""


@router.get("/watchlist")
async def get_watchlist(user_id: str = Query(""), db: AsyncSession = Depends(get_db)):
    from ..services.watchlist_service import get_watchlist as _get
    return await _get(db, user_id)


@router.post("/watchlist", status_code=201)
async def add_watchlist(payload: WatchlistCreate, db: AsyncSession = Depends(get_db)):
    from ..services.watchlist_service import add_to_watchlist
    return await add_to_watchlist(
        db,
        payload.user_id,
        payload.stock_code,
        payload.stock_name,
        payload.target_price,
        payload.stop_loss,
        payload.note,
    )


@router.delete("/watchlist/{item_id}")
async def delete_watchlist(item_id: int, user_id: str = Query(""), db: AsyncSession = Depends(get_db)):
    from ..services.watchlist_service import remove_from_watchlist
    ok = await remove_from_watchlist(db, item_id, user_id)
    if not ok:
        raise HTTPException(404, "Watchlist item not found")
    return {"deleted": True}


# ── Chip Tracker ───────────────────────────────────────────────────────────────

@router.get("/chip/{stock_code}/history")
async def get_chip_history(stock_code: str, days: int = Query(20, ge=5, le=60)):
    from ..services.chip_service import fetch_chip_history
    return await fetch_chip_history(stock_code, days)


@router.get("/chip/{stock_code}/main-force-cost")
async def get_main_force_cost(stock_code: str):
    from ..services.chip_service import estimate_main_force_cost
    return await estimate_main_force_cost(stock_code)


# ── Stock Health ───────────────────────────────────────────────────────────────

@router.get("/health/{stock_code}")
async def stock_health(stock_code: str):
    from ..services.health_service import check_stock_health
    return await check_stock_health(stock_code)


# ── Market Anomaly ──────────────────────────────────────────────────────────────

@router.get("/market/anomaly")
async def market_anomaly():
    from ..services.market_anomaly_service import check_market_anomaly
    return await check_market_anomaly()


# ── Performance Leaderboard ─────────────────────────────────────────────────────

@router.get("/performance/leaderboard")
async def performance_leaderboard():
    from ..services.performance_service import get_leaderboard
    return await get_leaderboard()


@router.get("/performance/history")
async def performance_history(user_id: str = Query(""), days: int = Query(30, ge=7, le=90)):
    from ..services.performance_service import get_performance_history
    return await get_performance_history(user_id, days)


@router.post("/performance/snapshot")
async def trigger_snapshot():
    from ..services.performance_service import snapshot_all_users
    await snapshot_all_users()
    return {"status": "ok"}


# ── Weekly Stock Picks ──────────────────────────────────────────────────────────

@router.get("/picks/weekly")
async def weekly_stock_picks(top_n: int = Query(5, ge=3, le=10)):
    from ..services.stock_pick_service import generate_weekly_picks
    return await generate_weekly_picks(top_n)


# ── Copy Trade ──────────────────────────────────────────────────────────────────

class SharePortfolioRequest(BaseModel):
    user_id: str
    display_name: str = ""
    description: str = ""


class FollowRequest(BaseModel):
    follower_id: str
    share_code: str


class UnfollowRequest(BaseModel):
    follower_id: str
    leader_id: str


@router.post("/copytrade/publish")
async def publish_portfolio(payload: SharePortfolioRequest, db: AsyncSession = Depends(get_db)):
    from ..services.copy_trade_service import publish_portfolio as _publish
    return await _publish(db, payload.user_id, payload.display_name, payload.description)


@router.get("/copytrade/view/{share_code}")
async def view_shared_portfolio(share_code: str, db: AsyncSession = Depends(get_db)):
    from ..services.copy_trade_service import get_shared_portfolio
    data = await get_shared_portfolio(db, share_code)
    if not data:
        raise HTTPException(404, "Share code not found or portfolio is private")
    return data


@router.post("/copytrade/follow")
async def follow_trader(payload: FollowRequest, db: AsyncSession = Depends(get_db)):
    from ..services.copy_trade_service import follow_trader as _follow
    result = await _follow(db, payload.follower_id, payload.share_code)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.post("/copytrade/unfollow")
async def unfollow_trader(payload: UnfollowRequest, db: AsyncSession = Depends(get_db)):
    from ..services.copy_trade_service import unfollow_trader as _unfollow
    ok = await _unfollow(db, payload.follower_id, payload.leader_id)
    if not ok:
        raise HTTPException(404, "Follow relation not found")
    return {"unfollowed": True}


@router.get("/copytrade/following")
async def get_following(follower_id: str = Query(""), db: AsyncSession = Depends(get_db)):
    from ..services.copy_trade_service import get_following
    return await get_following(db, follower_id)


# ══════════════════════════════════════════════════════════════════════════════
# v2 升級 API
# ══════════════════════════════════════════════════════════════════════════════

# ── Screener — 多維度選股 ─────────────────────────────────────────────────────

class ScreenerRequest(BaseModel):
    preset:               Optional[str] = None
    revenue_yoy_min:      Optional[float] = None
    gross_margin_min:     Optional[float] = None
    three_margins_up:     Optional[bool]  = None
    eps_growth_qtrs_min:  Optional[int]   = None
    foreign_consec_buy_min: Optional[int] = None
    trust_consec_buy_min: Optional[int]   = None
    dual_signal:          Optional[bool]  = None
    ma_aligned:           Optional[bool]  = None
    kd_golden_cross:      Optional[bool]  = None
    vol_breakout:         Optional[bool]  = None
    bb_breakout:          Optional[bool]  = None
    fundamental_score_min: Optional[float] = None
    chip_score_min:       Optional[float] = None
    technical_score_min:  Optional[float] = None
    total_score_min:      Optional[float] = None
    sort_by:              str = "total_score"
    limit:                int = 20


class NLQueryRequest(BaseModel):
    query: str


@router.post("/screener")
async def run_screener_api(payload: ScreenerRequest):
    from ..services.screener_engine import run_screener, PRESETS, ScreenerFilter
    if payload.preset and payload.preset in PRESETS:
        f = PRESETS[payload.preset]
    else:
        f = ScreenerFilter(
            revenue_yoy_min       = payload.revenue_yoy_min,
            gross_margin_min      = payload.gross_margin_min,
            three_margins_up      = payload.three_margins_up,
            eps_growth_qtrs_min   = payload.eps_growth_qtrs_min,
            foreign_consec_buy_min= payload.foreign_consec_buy_min,
            trust_consec_buy_min  = payload.trust_consec_buy_min,
            dual_signal           = payload.dual_signal,
            ma_aligned            = payload.ma_aligned,
            kd_golden_cross       = payload.kd_golden_cross,
            vol_breakout          = payload.vol_breakout,
            bb_breakout           = payload.bb_breakout,
            fundamental_score_min = payload.fundamental_score_min,
            chip_score_min        = payload.chip_score_min,
            technical_score_min   = payload.technical_score_min,
            total_score_min       = payload.total_score_min,
            sort_by               = payload.sort_by,
            limit                 = payload.limit,
        )
    results = await run_screener(f)
    return {"results": results, "count": len(results)}


@router.get("/screener/presets")
async def list_presets():
    from ..services.screener_engine import PRESETS
    return {k: str(v) for k, v in PRESETS.items()}


@router.get("/screener/top")
async def top_scores(limit: int = Query(20, ge=1, le=100)):
    from ..services.screener_engine import get_top_scores
    return await get_top_scores(limit)


@router.post("/screener/nl")
async def nl_screener(payload: NLQueryRequest):
    from ..services.nl_query_parser import execute_nl_query
    return await execute_nl_query(payload.query)


# ── Scores — 評分查詢 ─────────────────────────────────────────────────────────

@router.get("/scores/{stock_code}")
async def get_score(stock_code: str):
    from ..services.screener_engine import get_stock_score
    data = await get_stock_score(stock_code)
    if not data:
        raise HTTPException(404, f"No score data for {stock_code}")
    return data


# ── Financials — 財務報表 ──────────────────────────────────────────────────────

@router.get("/financials/{stock_code}")
async def get_financials(stock_code: str, limit: int = Query(8, ge=1, le=20)):
    from sqlalchemy import select
    from ..models.models import StockFinancials
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(StockFinancials)
            .where(StockFinancials.stock_code == stock_code)
            .order_by(StockFinancials.year.desc(), StockFinancials.quarter.desc())
            .limit(limit)
        )
        rows = r.scalars().all()
    if not rows:
        # 嘗試從 FinMind 即時抓取
        try:
            from ..services.finmind_service import fetch_financials
            data = await fetch_financials(stock_code)
            return {"source": "finmind_live", "data": data[-limit:]}
        except Exception:
            raise HTTPException(404, f"No financial data for {stock_code}")
    return {
        "source": "cache",
        "data": [
            {
                "year":             r.year,
                "quarter":          r.quarter,
                "revenue":          r.revenue,
                "gross_margin":     r.gross_margin,
                "operating_margin": r.operating_margin,
                "net_margin":       r.net_margin,
                "eps":              r.eps,
            }
            for r in reversed(rows)
        ]
    }


# ── Revenue — 月營收 ──────────────────────────────────────────────────────────

@router.get("/revenue/{stock_code}")
async def get_revenue(stock_code: str, months: int = Query(13, ge=3, le=36)):
    from sqlalchemy import select
    from ..models.models import MonthlyRevenue
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(MonthlyRevenue)
            .where(MonthlyRevenue.stock_code == stock_code)
            .order_by(MonthlyRevenue.year.desc(), MonthlyRevenue.month.desc())
            .limit(months)
        )
        rows = r.scalars().all()
    if not rows:
        try:
            from ..services.finmind_service import fetch_monthly_revenue
            data = await fetch_monthly_revenue(stock_code)
            return {"source": "finmind_live", "data": data[-months:]}
        except Exception:
            raise HTTPException(404, f"No revenue data for {stock_code}")
    return {
        "source": "cache",
        "data": [
            {
                "year":    r.year,
                "month":   r.month,
                "revenue": r.revenue,
                "yoy":     r.revenue_yoy,
                "mom":     r.revenue_mom,
            }
            for r in reversed(rows)
        ]
    }


# ── Industry Sentiment ────────────────────────────────────────────────────────

@router.get("/industry/sentiment")
async def industry_sentiment():
    from ..services.industry_sentiment import get_all_sentiments
    return await get_all_sentiments()


@router.get("/industry/sentiment/{industry}")
async def single_industry_sentiment(industry: str):
    from ..services.industry_sentiment import analyze_industry
    return await analyze_industry(industry)


@router.post("/industry/sentiment/refresh")
async def refresh_industry_sentiment():
    from ..services.industry_sentiment import run_all_industries
    import asyncio
    asyncio.create_task(run_all_industries())
    return {"status": "started"}


# ── Pipeline — 手動觸發 ───────────────────────────────────────────────────────

@router.post("/pipeline/run")
async def trigger_pipeline(stock_code: Optional[str] = Query(None)):
    import asyncio
    if stock_code:
        from ..services.data_pipeline import update_single_stock
        from ..services.score_updater import calc_and_save_score
        from datetime import date
        ok = await update_single_stock(stock_code, force=True)
        today = date.today().strftime("%Y-%m-%d")
        if ok:
            await calc_and_save_score(stock_code, today)
        return {"status": "ok" if ok else "failed", "stock_code": stock_code}
    else:
        asyncio.create_task(_run_full_pipeline())
        return {"status": "started", "message": "全量更新已在背景啟動"}


async def _run_full_pipeline():
    from ..services.data_pipeline import run_daily_pipeline
    await run_daily_pipeline(trigger_scoring=True)


@router.post("/pipeline/score")
async def trigger_scoring():
    import asyncio
    from ..services.score_updater import run_score_update
    asyncio.create_task(run_score_update())
    return {"status": "started"}


# ── AI Trading Advisor ────────────────────────────────────────────────────────

@router.get("/advice/daily")
async def daily_advice():
    from ..services.ai_trading_advisor import generate_daily_trading_advice
    return {"advice": await generate_daily_trading_advice()}


@router.get("/advice/stock/{stock_code}")
async def stock_advice(stock_code: str):
    from ..services.ai_trading_advisor import analyze_stock_for_line, check_realtime_alerts
    analysis = await analyze_stock_for_line(stock_code)
    alerts   = await check_realtime_alerts(stock_code)
    return {"analysis": analysis, "alerts": alerts}


@router.get("/advice/alerts/{stock_code}")
async def realtime_alerts(stock_code: str):
    from ..services.ai_trading_advisor import check_realtime_alerts
    return {"alerts": await check_realtime_alerts(stock_code)}


# ── Backtest Feedback ─────────────────────────────────────────────────────────

@router.get("/backtest/feedback/summary")
async def feedback_summary():
    from backtest.feedback_engine import get_strategy_performance_summary
    return await get_strategy_performance_summary()


@router.post("/backtest/feedback/adjust-weights")
async def adjust_feature_weights():
    from backtest.feedback_engine import auto_adjust_feature_weights
    await auto_adjust_feature_weights()
    return {"status": "done"}


@router.get("/backtest/feedback/weights")
async def feedback_weights():
    from backtest.feedback_engine import get_feature_weights
    return await get_feature_weights()


# ── Market Regime ─────────────────────────────────────────────────────────────

@router.get("/market/regime")
async def market_regime_api():
    from backtest.market_regime import get_market_regime, REGIME_DESCRIPTION, REGIME_STRATEGY_TIPS
    regime = await get_market_regime()
    regime["description"] = REGIME_DESCRIPTION.get(regime.get("current", "unknown"), "")
    regime["strategy_tip"] = REGIME_STRATEGY_TIPS.get(regime.get("current", ""), "")
    return regime


# ══════════════════════════════════════════════════════════════════════════════
# v3 進階功能 API
# ══════════════════════════════════════════════════════════════════════════════

# ── Recommendation Tracker — 推薦績效 ─────────────────────────────────────────

@router.get("/accuracy")
async def get_accuracy(days: int = Query(30, ge=7, le=90)):
    from ..services.recommendation_tracker import get_accuracy_stats
    return await get_accuracy_stats(days)


@router.get("/accuracy/weights")
async def get_weight_history(limit: int = Query(20, ge=5, le=60)):
    from ..services.recommendation_tracker import get_weight_history
    return await get_weight_history(limit)


@router.get("/accuracy/weights/current")
async def current_weights():
    from ..services.recommendation_tracker import get_current_weights
    return await get_current_weights()


@router.post("/accuracy/backfill")
async def trigger_backfill():
    import asyncio
    from ..services.recommendation_tracker import backfill_prices
    asyncio.create_task(backfill_prices())
    return {"status": "started"}


@router.post("/accuracy/adjust-weights")
async def trigger_weight_adjustment():
    from ..services.recommendation_tracker import adjust_weights
    await adjust_weights()
    return {"status": "done"}


# ── Broker Tracker — 分點追蹤 ─────────────────────────────────────────────────

@router.get("/broker/{stock_code}")
async def get_top_brokers(stock_code: str, days: int = Query(10, ge=3, le=30)):
    from ..services.broker_tracker import get_top_brokers
    return await get_top_brokers(stock_code, days)


@router.get("/broker/track/{broker_name}")
async def track_broker(broker_name: str, days: int = Query(5, ge=3, le=20)):
    from ..services.broker_tracker import track_broker as _track
    return await _track(broker_name, days)


@router.get("/broker/smart-money/signals")
async def smart_money_signals():
    from ..services.broker_tracker import detect_smart_money
    return await detect_smart_money()


@router.post("/broker/{stock_code}/fetch")
async def fetch_broker_data(stock_code: str, days: int = Query(10, ge=3, le=30)):
    """手動觸發抓取並快取特定股票的分點資料"""
    from ..services.broker_tracker import fetch_broker_detail
    data = await fetch_broker_detail(stock_code, days)
    return {"stock_code": stock_code, "rows": len(data)}


# ── Portfolio Optimizer — 投組最佳化 ──────────────────────────────────────────

@router.get("/portfolio/optimize")
async def optimize_portfolio(user_id: str = Query("")):
    from ..services.portfolio_optimizer import full_portfolio_analysis
    result = await full_portfolio_analysis(user_id)
    if "error" in result:
        raise HTTPException(422, result["error"])
    return result


@router.get("/portfolio/var")
async def portfolio_var(user_id: str = Query(""), confidence: float = Query(0.95)):
    from ..services.portfolio_optimizer import (
        get_returns_matrix, calc_var, full_portfolio_analysis,
    )
    result = await full_portfolio_analysis(user_id)
    if "error" in result:
        raise HTTPException(422, result["error"])
    return result.get("var", {})


@router.get("/portfolio/correlation")
async def portfolio_correlation(user_id: str = Query("")):
    from ..services.portfolio_optimizer import full_portfolio_analysis
    result = await full_portfolio_analysis(user_id)
    if "error" in result:
        raise HTTPException(422, result["error"])
    return result.get("correlation", {})


# ── Data Source Status ─────────────────────────────────────────────────────────

@router.get("/data-status")
async def data_source_status():
    """
    回傳各資料來源的連線狀態，前端用來顯示資料可靠性警示。
    每個來源：{ ok: bool, latency_ms: int | null, note: str }
    """
    import time
    import httpx

    results: dict = {}

    # TWSE
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
            ok = r.status_code == 200 and len(r.json()) > 0
        results["twse"] = {"ok": ok, "latency_ms": int((time.monotonic() - t0) * 1000), "note": "上市即時報價"}
    except Exception as e:
        results["twse"] = {"ok": False, "latency_ms": None, "note": str(e)[:60]}

    # FinMind
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("https://api.finmindtrade.com/api/v4/info")
            ok = r.status_code == 200
        results["finmind"] = {"ok": ok, "latency_ms": int((time.monotonic() - t0) * 1000), "note": "歷史財務資料"}
    except Exception as e:
        results["finmind"] = {"ok": False, "latency_ms": None, "note": str(e)[:60]}

    # 本地 DB
    try:
        from ..models.database import AsyncSessionLocal
        from sqlalchemy import text
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        results["database"] = {"ok": True, "latency_ms": 0, "note": "本地資料庫"}
    except Exception as e:
        results["database"] = {"ok": False, "latency_ms": None, "note": str(e)[:60]}

    all_ok = all(v["ok"] for v in results.values())
    from quant.mock_isolation import env_info
    from quant.risk_kill_switch import status_dict
    return {
        "sources":      results,
        "all_ok":       all_ok,
        "env":          env_info(),
        "kill_switch":  status_dict(),
    }


# ── System Health ──────────────────────────────────────────────────────────────

@router.get("/system/health")
async def system_health():
    """完整系統健康儀表板（供前端 /system 頁面使用）"""
    from quant.system_health_dashboard import collect_health
    health = await collect_health()
    return health.to_dict()


@router.get("/system/kill-switch")
async def kill_switch_status():
    """Kill Switch 目前狀態"""
    from quant.risk_kill_switch import status_dict
    return status_dict()


@router.post("/system/kill-switch/activate")
async def activate_kill_switch(reason: str = "manual_admin"):
    """手動啟動 Kill Switch（管理員操作）"""
    from quant.risk_kill_switch import get_state
    get_state().activate(f"manual:{reason}")
    return {"activated": True, "reason": reason}


@router.post("/system/kill-switch/deactivate")
async def deactivate_kill_switch():
    """手動解除 Kill Switch"""
    from quant.risk_kill_switch import get_state
    get_state().deactivate("manual_admin_release")
    return {"deactivated": True}


@router.get("/system/audit-log")
async def get_audit_log(limit: int = Query(50), stock_id: str = Query("")):
    """查詢決策稽核日誌"""
    from ..models.models import AuditLog
    q = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if stock_id:
        q = q.where(AuditLog.stock_id == stock_id)
    result = await (await get_db().__anext__()).execute(q)
    rows = result.scalars().all()
    return [
        {
            "id":           r.id,
            "session_id":   r.session_id,
            "stock_id":     r.stock_id,
            "action":       r.action,
            "confidence":   r.confidence,
            "eligible":     r.eligible,
            "blocking":     r.blocking_reasons_json,
            "mock_count":   r.mock_count,
            "stale_count":  r.stale_count,
            "kill_switch":  r.kill_switch_on,
            "created_at":   str(r.created_at),
        }
        for r in rows
    ]


# ── Analysts ───────────────────────────────────────────────────────────────────

@router.get("/analysts")
async def list_analysts(tier: str = Query(None), active_only: bool = Query(True)):
    from ..services.analyst_tracker import get_all_analysts
    analysts = await get_all_analysts(active_only=active_only)
    if tier:
        analysts = [a for a in analysts if a.get("tier") == tier]
    return {"analysts": analysts, "total": len(analysts)}


@router.get("/analysts/consensus")
async def analyst_consensus(days: int = Query(7, ge=1, le=30), stock_code: str = Query(None)):
    from ..services.analyst_consensus_engine import calculate_daily_consensus
    from ..models.models import AnalystConsensusDaily
    results = await calculate_daily_consensus(days=days)
    if stock_code:
        results = [r for r in results if r.stock_id == stock_code]
    consensus = [
        {
            "stock":          r.stock_id,
            "name":           r.stock_name,
            "score":          round(r.consensus_score, 1),
            "bullish":        r.bullish_count,
            "bearish":        r.bearish_count,
            "total_analysts": r.total_analysts,
            "alpha":          r.consensus_score >= 60 and not r.is_divergent,
            "divergent":      r.is_divergent,
            "tier_str":       "S+A" if r.high_cred_count >= 2 else ("A" if r.high_cred_count >= 1 else "B"),
        }
        for r in sorted(results, key=lambda x: x.consensus_score, reverse=True)[:10]
    ]
    return {"consensus": consensus, "days": days}


@router.post("/analysts/fetch-youtube")
async def trigger_youtube_fetch():
    import asyncio
    from ..services.youtube_alpha_engine import run_daily_fetch
    asyncio.create_task(run_daily_fetch())
    return {"status": "started", "message": "YouTube 抓取已在背景啟動"}


@router.get("/analysts/{analyst_id}")
async def get_analyst(analyst_id: str):
    from ..services.analyst_tracker import get_analyst_stats
    data = await get_analyst_stats(analyst_id)
    if not data:
        raise HTTPException(404, f"Analyst {analyst_id} not found")
    return data
