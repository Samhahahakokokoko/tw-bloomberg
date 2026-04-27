"""績效排行榜服務"""
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..models.database import AsyncSessionLocal
from ..models.models import Portfolio, PerformanceRecord
from .twse_service import fetch_realtime_quote
from loguru import logger


async def get_leaderboard() -> list[dict]:
    """取得各 user_id 的投資績效排行（按總損益% 排序）"""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Portfolio.user_id).distinct())
        user_ids = [row[0] for row in result.fetchall()]

    if not user_ids:
        return []

    board = []
    for uid in user_ids:
        try:
            perf = await _calc_user_performance(uid)
            if perf:
                board.append(perf)
        except Exception as e:
            logger.error(f"Performance calc error uid={uid}: {e}")

    board.sort(key=lambda x: x.get("total_pnl_pct", 0), reverse=True)
    for i, item in enumerate(board):
        item["rank"] = i + 1
    return board


async def _calc_user_performance(user_id: str) -> dict | None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Portfolio).where(Portfolio.user_id == user_id)
        )
        holdings = result.scalars().all()

    if not holdings:
        return None

    total_mv = total_cost = 0.0
    best_code = worst_code = ""
    best_pct  = float("-inf")
    worst_pct = float("inf")

    for h in holdings:
        try:
            quote = await fetch_realtime_quote(h.stock_code)
            price = quote.get("price", h.cost_price) or h.cost_price
        except Exception:
            price = h.cost_price

        mv   = price * h.shares
        cost = h.cost_price * h.shares
        pnl_pct = (price - h.cost_price) / h.cost_price * 100 if h.cost_price else 0

        total_mv   += mv
        total_cost += cost

        if pnl_pct > best_pct:
            best_pct  = pnl_pct
            best_code = h.stock_code
        if pnl_pct < worst_pct:
            worst_pct  = pnl_pct
            worst_code = h.stock_code

    total_pnl     = total_mv - total_cost
    total_pnl_pct = total_pnl / total_cost * 100 if total_cost else 0

    return {
        "user_id":        user_id or "匿名",
        "holdings_count": len(holdings),
        "total_cost":     round(total_cost),
        "total_mv":       round(total_mv),
        "total_pnl":      round(total_pnl),
        "total_pnl_pct":  round(total_pnl_pct, 2),
        "best_stock":     {"code": best_code, "pct": round(best_pct, 2)} if best_code else None,
        "worst_stock":    {"code": worst_code, "pct": round(worst_pct, 2)} if worst_code else None,
    }


async def snapshot_all_users():
    """每日 14:00 對所有用戶拍績效快照"""
    today = datetime.now().strftime("%Y-%m-%d")
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Portfolio.user_id).distinct())
        user_ids = [row[0] for row in result.fetchall()]

    for uid in user_ids:
        try:
            perf = await _calc_user_performance(uid)
            if not perf:
                continue
            async with AsyncSessionLocal() as db:
                existing = await db.execute(
                    select(PerformanceRecord).where(
                        PerformanceRecord.user_id == uid,
                        PerformanceRecord.record_date == today,
                    )
                )
                rec = existing.scalar_one_or_none()
                if rec:
                    rec.total_mv    = perf["total_mv"]
                    rec.total_cost  = perf["total_cost"]
                    rec.total_pnl   = perf["total_pnl"]
                    rec.daily_return = perf["total_pnl_pct"]
                else:
                    rec = PerformanceRecord(
                        user_id=uid,
                        record_date=today,
                        total_mv=perf["total_mv"],
                        total_cost=perf["total_cost"],
                        total_pnl=perf["total_pnl"],
                        daily_return=perf["total_pnl_pct"],
                    )
                    db.add(rec)
                await db.commit()
        except Exception as e:
            logger.error(f"Snapshot error uid={uid}: {e}")

    logger.info(f"Performance snapshot done for {len(user_ids)} users")


async def get_performance_history(user_id: str, days: int = 30) -> list[dict]:
    """取得單一用戶的歷史績效（用於趨勢圖）"""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PerformanceRecord)
            .where(PerformanceRecord.user_id == user_id)
            .order_by(PerformanceRecord.record_date.desc())
            .limit(days)
        )
        records = result.scalars().all()

    return [
        {
            "date":         r.record_date,
            "total_mv":     r.total_mv,
            "total_cost":   r.total_cost,
            "total_pnl":    r.total_pnl,
            "daily_return": r.daily_return,
        }
        for r in reversed(records)
    ]
