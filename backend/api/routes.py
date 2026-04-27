from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from ..models.database import get_db
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
