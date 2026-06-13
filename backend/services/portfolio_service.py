"""庫存服務 — 完全 user_id 隔離，每個 LINE 用戶看到自己的資料"""
import asyncio
from datetime import date, datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..models.models import Portfolio
from ..services.twse_service import fetch_realtime_quote
from loguru import logger


async def get_portfolio(db: AsyncSession, user_id: str = "") -> list[dict]:
    q = select(Portfolio)
    if user_id:
        q = q.where(Portfolio.user_id == user_id)
    result = await db.execute(q)
    holdings = result.scalars().all()

    if not holdings:
        return []

    # Fetch all quotes in parallel
    quotes = await asyncio.gather(
        *[fetch_realtime_quote(h.stock_code) for h in holdings],
        return_exceptions=True,
    )

    output = []
    today = date.today()
    for h, quote in zip(holdings, quotes):
        if isinstance(quote, Exception) or not quote:
            quote = {}
        current_price = float(quote.get("price") or quote.get("close") or h.cost_price or 0)
        market_value  = current_price * h.shares
        cost          = h.cost_price * h.shares
        pnl           = market_value - cost
        pnl_pct       = round(pnl / cost * 100, 2) if cost else 0
        pnl_per_share = round(current_price - h.cost_price, 2)

        # Holding days from buy_date; fallback to created_at
        holding_days = 0
        raw_buy_date = getattr(h, "buy_date", None)
        if raw_buy_date:
            try:
                holding_days = (today - datetime.strptime(raw_buy_date, "%Y-%m-%d").date()).days
            except Exception as e:
                pass
        if holding_days == 0 and h.created_at:
            holding_days = max(0, (today - h.created_at.date()).days)

        output.append({
            "id":               h.id,
            "user_id":          h.user_id,
            "stock_code":       h.stock_code,
            "stock_name":       h.stock_name or quote.get("name", ""),
            "shares":           h.shares,
            "cost_price":       h.cost_price,
            "current_price":    current_price,
            "market_value":     round(market_value, 2),
            "pnl":              round(pnl, 2),
            "pnl_pct":          pnl_pct,
            "pnl_per_share":    pnl_per_share,
            "holding_days":     holding_days,
            "buy_date":         raw_buy_date or (h.created_at.strftime("%Y-%m-%d") if h.created_at else ""),
            "market_condition": getattr(h, "market_condition", "") or "",
        })
    return output


async def add_holding(
    db: AsyncSession,
    stock_code: str,
    shares: int,
    cost_price: float,
    user_id: str = "",
    buy_date: str = None,
    market_condition: str = None,
) -> Portfolio:
    quote = await fetch_realtime_quote(stock_code)
    from datetime import date as _date
    if not buy_date:
        buy_date = _date.today().strftime("%Y-%m-%d")

    # 同一 user 若已有此股，累加（加碼）
    result = await db.execute(
        select(Portfolio).where(
            Portfolio.user_id == user_id,
            Portfolio.stock_code == stock_code,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        total_shares = existing.shares + shares
        total_cost   = existing.cost_price * existing.shares + cost_price * shares
        existing.shares     = total_shares
        existing.cost_price = round(total_cost / total_shares, 4)
        existing.stock_name = quote.get("name", existing.stock_name)
        # Keep earliest buy_date
        existing_bd = getattr(existing, "buy_date", None)
        if not existing_bd or (buy_date and buy_date < existing_bd):
            existing.buy_date = buy_date
        if market_condition:
            existing.market_condition = market_condition
        await db.commit()
        await db.refresh(existing)
        return existing

    holding = Portfolio(
        user_id=user_id,
        stock_code=stock_code,
        stock_name=quote.get("name", ""),
        shares=shares,
        cost_price=cost_price,
        buy_date=buy_date,
        market_condition=market_condition or "unknown",
    )
    db.add(holding)
    await db.commit()
    await db.refresh(holding)
    return holding


async def remove_holding(db: AsyncSession, holding_id: int, user_id: str = "") -> bool:
    q = select(Portfolio).where(Portfolio.id == holding_id)
    if user_id:
        q = q.where(Portfolio.user_id == user_id)
    result = await db.execute(q)
    h = result.scalar_one_or_none()
    if not h:
        return False
    await db.delete(h)
    await db.commit()
    return True


async def adjust_shares(
    db: AsyncSession, holding_id: int, delta: int, user_id: str = ""
) -> Portfolio | None:
    """增減股數（delta 可為負數）"""
    q = select(Portfolio).where(Portfolio.id == holding_id)
    if user_id:
        q = q.where(Portfolio.user_id == user_id)
    result = await db.execute(q)
    h = result.scalar_one_or_none()
    if not h:
        return None
    new_shares = h.shares + delta
    if new_shares <= 0:
        await db.delete(h)
        await db.commit()
        return None
    h.shares = new_shares
    await db.commit()
    await db.refresh(h)
    return h


async def update_cost(
    db: AsyncSession, holding_id: int, new_cost: float, user_id: str = ""
) -> Portfolio | None:
    """更新成本"""
    q = select(Portfolio).where(Portfolio.id == holding_id)
    if user_id:
        q = q.where(Portfolio.user_id == user_id)
    result = await db.execute(q)
    h = result.scalar_one_or_none()
    if not h:
        return None
    h.cost_price = new_cost
    await db.commit()
    await db.refresh(h)
    return h


async def update_holding(
    db: AsyncSession, holding_id: int, shares: int, cost_price: float, user_id: str = ""
) -> Portfolio | None:
    q = select(Portfolio).where(Portfolio.id == holding_id)
    if user_id:
        q = q.where(Portfolio.user_id == user_id)
    result = await db.execute(q)
    h = result.scalar_one_or_none()
    if not h:
        return None
    h.shares     = shares
    h.cost_price = cost_price
    await db.commit()
    await db.refresh(h)
    return h
