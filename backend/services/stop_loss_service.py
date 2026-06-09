"""stop_loss_service.py — 停損停利掃描與 LINE 推播"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional
from sqlalchemy import select
from loguru import logger


# ── CRUD ──────────────────────────────────────────────────────────────────────

async def set_stop(
    user_id: str,
    stock_code: str,
    stock_name: str = "",
    sl_price: Optional[float] = None,
    tp_price: Optional[float] = None,
) -> dict:
    """新增或更新停損停利設定。sl_price / tp_price 傳 None 表示不修改該欄位。"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import StopAlert

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(StopAlert).where(
                StopAlert.user_id   == user_id,
                StopAlert.stock_code == stock_code,
            )
        )
        alert = r.scalar_one_or_none()

        if alert:
            if sl_price is not None:
                alert.sl_price          = sl_price
                alert.sl_triggered_date = None   # 重設觸發記錄
            if tp_price is not None:
                alert.tp_price          = tp_price
                alert.tp_triggered_date = None
            if stock_name:
                alert.stock_name = stock_name
            alert.is_active  = True
            alert.updated_at = datetime.utcnow()
        else:
            alert = StopAlert(
                user_id    = user_id,
                stock_code = stock_code,
                stock_name = stock_name,
                sl_price   = sl_price,
                tp_price   = tp_price,
                is_active  = True,
            )
            db.add(alert)

        await db.commit()

    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "sl_price":   alert.sl_price,
        "tp_price":   alert.tp_price,
    }


async def get_stops(user_id: str) -> list[dict]:
    """取得用戶所有有效停損停利設定"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import StopAlert
    from .twse_service import fetch_realtime_quote

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(StopAlert).where(
                StopAlert.user_id  == user_id,
                StopAlert.is_active == True,
            ).order_by(StopAlert.stock_code)
        )
        alerts = r.scalars().all()

    result = []
    for a in alerts:
        try:
            q = await fetch_realtime_quote(a.stock_code)
            price = q.get("price", 0)
        except Exception as e:
            price = 0

        result.append({
            "stock_code": a.stock_code,
            "stock_name": a.stock_name or a.stock_code,
            "sl_price":   a.sl_price,
            "tp_price":   a.tp_price,
            "price":      price,
            "sl_status":  "🔴 已觸發" if (a.sl_price and price and price <= a.sl_price) else
                          ("🔻 未觸發" if a.sl_price else "—"),
            "tp_status":  "💰 已觸發" if (a.tp_price and price and price >= a.tp_price) else
                          ("🚀 未觸發" if a.tp_price else "—"),
        })
    return result


async def remove_stop(user_id: str, stock_code: str) -> bool:
    from ..models.database import AsyncSessionLocal
    from ..models.models import StopAlert

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(StopAlert).where(
                StopAlert.user_id    == user_id,
                StopAlert.stock_code == stock_code,
            )
        )
        alert = r.scalar_one_or_none()
        if not alert:
            return False
        await db.delete(alert)
        await db.commit()
    return True


# ── 掃描引擎 ──────────────────────────────────────────────────────────────────

async def scan_and_alert() -> int:
    """
    掃描所有停損停利設定，觸發時推播 LINE。
    每個觸發條件當天只推一次。
    回傳推播次數。
    """
    from ..models.database import AsyncSessionLocal
    from ..models.models import StopAlert, Portfolio
    from .twse_service import fetch_realtime_quote
    from .line_push import push_line_messages

    today = date.today().isoformat()
    pushed = 0

    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(StopAlert).where(StopAlert.is_active == True)
            )
            alerts = r.scalars().all()

        for alert in alerts:
            try:
                q = await fetch_realtime_quote(alert.stock_code)
                price = float(q.get("price", 0) or 0)
                if price <= 0:
                    continue

                name = alert.stock_name or q.get("name", alert.stock_code)

                # 查 portfolio 計算損益
                async with AsyncSessionLocal() as db:
                    pr = await db.execute(
                        select(Portfolio).where(
                            Portfolio.user_id    == alert.user_id,
                            Portfolio.stock_code == alert.stock_code,
                        )
                    )
                    holding = pr.scalar_one_or_none()

                cost   = float(holding.cost_price) if holding else 0
                shares = int(holding.shares)        if holding else 0
                pnl    = round((price - cost) * shares, 0) if cost and shares else 0
                pnl_pct = round((price - cost) / cost * 100, 1) if cost else 0

                # 停損觸發
                if (
                    alert.sl_price
                    and price <= alert.sl_price
                    and alert.sl_triggered_date != today
                ):
                    msg = _build_sl_msg(alert.stock_code, name, price, alert.sl_price, pnl, pnl_pct, shares)
                    qr  = _sl_qr(alert.stock_code, shares, price)
                    await push_line_messages(
                        alert.user_id,
                        [{"type": "text", "text": msg, "quickReply": qr}],
                        timeout=10, context="stop_loss",
                    )
                    async with AsyncSessionLocal() as db:
                        r2 = await db.execute(
                            select(StopAlert).where(StopAlert.id == alert.id)
                        )
                        a2 = r2.scalar_one_or_none()
                        if a2:
                            a2.sl_triggered_date = today
                            await db.commit()
                    pushed += 1
                    logger.info("[StopLoss] SL triggered: {} {} uid={}", alert.stock_code, price, alert.user_id[:8])

                # 停利觸發
                if (
                    alert.tp_price
                    and price >= alert.tp_price
                    and alert.tp_triggered_date != today
                ):
                    msg = _build_tp_msg(alert.stock_code, name, price, alert.tp_price, pnl, pnl_pct, shares)
                    qr  = _tp_qr(alert.stock_code, shares, price)
                    await push_line_messages(
                        alert.user_id,
                        [{"type": "text", "text": msg, "quickReply": qr}],
                        timeout=10, context="take_profit",
                    )
                    async with AsyncSessionLocal() as db:
                        r2 = await db.execute(
                            select(StopAlert).where(StopAlert.id == alert.id)
                        )
                        a2 = r2.scalar_one_or_none()
                        if a2:
                            a2.tp_triggered_date = today
                            await db.commit()
                    pushed += 1
                    logger.info("[TakeProfit] TP triggered: {} {} uid={}", alert.stock_code, price, alert.user_id[:8])

            except Exception as e:
                logger.warning("[StopLoss] scan error for {}: {}", alert.stock_code, e)

    except Exception as e:
        logger.error("[StopLoss] scan_and_alert failed: {}", e)

    return pushed


# ── 推播訊息格式 ──────────────────────────────────────────────────────────────

def _pnl_str(pnl: float, pnl_pct: float) -> str:
    sign = "+" if pnl >= 0 else ""
    return f"{sign}{pnl:,.0f} ({sign}{pnl_pct:.1f}%)"


def _build_sl_msg(code, name, price, sl, pnl, pnl_pct, shares) -> str:
    return (
        f"🚨 停損提醒\n"
        f"{code} {name}\n"
        f"─────────────\n"
        f"現價：{price:,.0f}　已跌破停損 {sl:,.0f}\n"
        f"建議：考慮停損出場\n"
        f"損益：{_pnl_str(pnl, pnl_pct)}\n"
        f"（持有 {shares:,} 股）"
    )


def _build_tp_msg(code, name, price, tp, pnl, pnl_pct, shares) -> str:
    return (
        f"💰 停利提醒\n"
        f"{code} {name}\n"
        f"─────────────\n"
        f"現價：{price:,.0f}　已達停利 {tp:,.0f}\n"
        f"建議：考慮分批出場\n"
        f"損益：{_pnl_str(pnl, pnl_pct)}\n"
        f"（持有 {shares:,} 股）"
    )


def _sl_qr(code: str, shares: int, price: float) -> dict:
    from line_webhook.flex_messages import qr_items
    sell_cmd = f"/sell {code} {shares} {price:.0f}"
    return qr_items(
        ("已停損", sell_cmd),
        ("繼續持有", f"/p"),
        ("查設定", "/stops"),
    )


def _tp_qr(code: str, shares: int, price: float) -> dict:
    from line_webhook.flex_messages import qr_items
    sell_cmd = f"/sell {code} {shares} {price:.0f}"
    return qr_items(
        ("已停利", sell_cmd),
        ("繼續持有", f"/p"),
        ("查設定", "/stops"),
    )
