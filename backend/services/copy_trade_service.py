"""跟單功能服務"""
import random
import string
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..models.database import AsyncSessionLocal
from ..models.models import CopyTradeRelation, SharedPortfolio, Portfolio
from .twse_service import fetch_realtime_quote
from loguru import logger


def _gen_share_code(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


async def publish_portfolio(db: AsyncSession, user_id: str, display_name: str = "", description: str = "") -> dict:
    """將投資組合公開分享，回傳分享碼"""
    result = await db.execute(
        select(SharedPortfolio).where(SharedPortfolio.user_id == user_id)
    )
    shared = result.scalar_one_or_none()

    if shared:
        if display_name: shared.display_name = display_name
        if description:  shared.description  = description
        shared.is_public = True
    else:
        code = _gen_share_code()
        # 確保 share_code 不重複
        while True:
            existing = await db.execute(
                select(SharedPortfolio).where(SharedPortfolio.share_code == code)
            )
            if not existing.scalar_one_or_none():
                break
            code = _gen_share_code()

        shared = SharedPortfolio(
            user_id=user_id,
            share_code=code,
            display_name=display_name or f"投資人_{user_id[:6]}",
            description=description,
        )
        db.add(shared)

    await db.commit()
    await db.refresh(shared)
    return {
        "share_code":   shared.share_code,
        "display_name": shared.display_name,
        "description":  shared.description,
    }


async def get_shared_portfolio(db: AsyncSession, share_code: str) -> dict | None:
    """用分享碼查看別人的公開持倉"""
    result = await db.execute(
        select(SharedPortfolio).where(
            SharedPortfolio.share_code == share_code,
            SharedPortfolio.is_public == True,
        )
    )
    shared = result.scalar_one_or_none()
    if not shared:
        return None

    # 取該用戶的持倉
    holdings_result = await db.execute(
        select(Portfolio).where(Portfolio.user_id == shared.user_id)
    )
    holdings = holdings_result.scalars().all()

    portfolio_items = []
    total_mv = total_cost = 0.0
    for h in holdings:
        try:
            quote = await fetch_realtime_quote(h.stock_code)
            price = quote.get("price", h.cost_price)
        except Exception:
            price = h.cost_price

        mv   = price * h.shares
        cost = h.cost_price * h.shares
        pnl  = mv - cost
        pnl_pct = pnl / cost * 100 if cost else 0

        total_mv   += mv
        total_cost += cost
        portfolio_items.append({
            "stock_code":    h.stock_code,
            "stock_name":    h.stock_name or "",
            "shares":        h.shares,
            "cost_price":    h.cost_price,
            "current_price": price,
            "pnl_pct":       round(pnl_pct, 2),
        })

    total_pnl     = total_mv - total_cost
    total_pnl_pct = total_pnl / total_cost * 100 if total_cost else 0

    return {
        "share_code":    share_code,
        "display_name":  shared.display_name,
        "description":   shared.description,
        "holdings":      portfolio_items,
        "total_mv":      round(total_mv),
        "total_pnl_pct": round(total_pnl_pct, 2),
    }


async def follow_trader(db: AsyncSession, follower_id: str, leader_share_code: str) -> dict:
    """跟單：追蹤某分享碼的交易者"""
    shared_result = await db.execute(
        select(SharedPortfolio).where(SharedPortfolio.share_code == leader_share_code)
    )
    shared = shared_result.scalar_one_or_none()
    if not shared:
        return {"error": "找不到此分享碼"}

    leader_id = shared.user_id
    if leader_id == follower_id:
        return {"error": "無法跟單自己"}

    existing = await db.execute(
        select(CopyTradeRelation).where(
            CopyTradeRelation.follower_id == follower_id,
            CopyTradeRelation.leader_id == leader_id,
        )
    )
    rel = existing.scalar_one_or_none()
    if rel:
        rel.is_active = True
    else:
        rel = CopyTradeRelation(follower_id=follower_id, leader_id=leader_id)
        db.add(rel)

    await db.commit()
    return {"leader_id": leader_id, "display_name": shared.display_name, "status": "已追蹤"}


async def unfollow_trader(db: AsyncSession, follower_id: str, leader_id: str) -> bool:
    result = await db.execute(
        select(CopyTradeRelation).where(
            CopyTradeRelation.follower_id == follower_id,
            CopyTradeRelation.leader_id == leader_id,
        )
    )
    rel = result.scalar_one_or_none()
    if not rel:
        return False
    rel.is_active = False
    await db.commit()
    return True


async def get_following(db: AsyncSession, follower_id: str) -> list[dict]:
    """取得我追蹤的所有交易者清單"""
    result = await db.execute(
        select(CopyTradeRelation, SharedPortfolio).join(
            SharedPortfolio, CopyTradeRelation.leader_id == SharedPortfolio.user_id, isouter=True
        ).where(
            CopyTradeRelation.follower_id == follower_id,
            CopyTradeRelation.is_active == True,
        )
    )
    rows = result.all()
    following = []
    for rel, shared in rows:
        entry = {
            "leader_id":    rel.leader_id,
            "display_name": shared.display_name if shared else rel.leader_id,
            "share_code":   shared.share_code if shared else None,
        }
        # 補充績效
        if shared and shared.share_code:
            try:
                portfolio_data = await get_shared_portfolio(db, shared.share_code)
                if portfolio_data:
                    entry["total_pnl_pct"] = portfolio_data["total_pnl_pct"]
                    entry["holdings_count"] = len(portfolio_data["holdings"])
            except Exception:
                pass
        following.append(entry)
    return following
