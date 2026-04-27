"""自選股清單服務"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..models.models import Watchlist
from .twse_service import fetch_realtime_quote
from loguru import logger


async def get_watchlist(db: AsyncSession, user_id: str = "") -> list[dict]:
    result = await db.execute(
        select(Watchlist).where(Watchlist.user_id == user_id)
    )
    items = result.scalars().all()

    output = []
    for item in items:
        try:
            quote = await fetch_realtime_quote(item.stock_code)
        except Exception:
            quote = {}

        current_price = quote.get("price", 0)
        change_pct    = quote.get("change_pct", 0)
        change        = quote.get("change", 0)
        stock_name    = item.stock_name or quote.get("name", "")

        sl_triggered = bool(item.stop_loss and current_price > 0 and current_price <= item.stop_loss)
        tp_triggered = bool(item.target_price and current_price > 0 and current_price >= item.target_price)

        output.append({
            "id":            item.id,
            "stock_code":    item.stock_code,
            "stock_name":    stock_name,
            "current_price": current_price,
            "change":        change,
            "change_pct":    change_pct,
            "target_price":  item.target_price,
            "stop_loss":     item.stop_loss,
            "note":          item.note,
            "sl_triggered":  sl_triggered,
            "tp_triggered":  tp_triggered,
            "created_at":    item.created_at.isoformat() if item.created_at else "",
        })

    return output


async def add_to_watchlist(
    db: AsyncSession,
    user_id: str,
    stock_code: str,
    stock_name: str = "",
    target_price: float | None = None,
    stop_loss: float | None = None,
    note: str = "",
) -> dict:
    # 若已存在則更新
    result = await db.execute(
        select(Watchlist).where(
            Watchlist.user_id == user_id,
            Watchlist.stock_code == stock_code,
        )
    )
    item = result.scalar_one_or_none()

    if item:
        if stock_name:  item.stock_name   = stock_name
        if target_price is not None: item.target_price = target_price
        if stop_loss   is not None:  item.stop_loss    = stop_loss
        if note:        item.note         = note
    else:
        # 若 stock_name 未傳，嘗試從報價取
        if not stock_name:
            try:
                q = await fetch_realtime_quote(stock_code)
                stock_name = q.get("name", "")
            except Exception:
                pass

        item = Watchlist(
            user_id=user_id,
            stock_code=stock_code,
            stock_name=stock_name,
            target_price=target_price,
            stop_loss=stop_loss,
            note=note,
        )
        db.add(item)

    await db.commit()
    await db.refresh(item)
    return {"id": item.id, "stock_code": item.stock_code, "stock_name": item.stock_name}


async def remove_from_watchlist(db: AsyncSession, item_id: int, user_id: str = "") -> bool:
    result = await db.execute(
        select(Watchlist).where(Watchlist.id == item_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        return False
    await db.delete(item)
    await db.commit()
    return True
