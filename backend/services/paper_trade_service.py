"""paper_trade_service.py — 模擬交易記錄"""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional
logger = logging.getLogger(__name__)


async def record_trade(uid: str, action: str, code: str, shares: int, price: float, note: str = "") -> dict:
    """記錄一筆模擬交易"""
    from backend.models.database import AsyncSessionLocal
    from backend.models.models import PaperTrade
    from backend.services.twse_service import fetch_realtime_quote

    name = code
    try:
        q = await fetch_realtime_quote(code)
        if q:
            name = q.get("name", code) or code
    except Exception:
        pass

    amount = shares * price * 1000  # 1張 = 1000股

    async with AsyncSessionLocal() as db:
        trade = PaperTrade(
            user_id=uid, stock_code=code, stock_name=name,
            action=action, shares=shares, price=price,
            amount=amount, note=note,
            traded_at=datetime.utcnow(),
        )
        db.add(trade)
        await db.commit()
        await db.refresh(trade)

    action_zh = "買入" if action == "buy" else "賣出"
    return {
        "id": trade.id,
        "message": f"✅ 已記錄{action_zh}：{name}（{code}）{shares}張 @ {price:.1f}\n金額：{amount:,.0f} 元"
    }


async def get_trade_history(uid: str, limit: int = 30) -> list:
    """取得交易歷史"""
    from backend.models.database import AsyncSessionLocal
    from backend.models.models import PaperTrade
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(PaperTrade).where(PaperTrade.user_id == uid)
            .order_by(PaperTrade.traded_at.desc()).limit(limit)
        )
        trades = r.scalars().all()
    return [
        {
            "id": t.id, "action": t.action, "code": t.stock_code, "name": t.stock_name,
            "shares": t.shares, "price": t.price, "amount": t.amount,
            "date": t.traded_at.strftime("%m/%d %H:%M") if t.traded_at else "",
        }
        for t in trades
    ]


async def get_pnl(uid: str) -> dict:
    """計算損益統計"""
    from backend.models.database import AsyncSessionLocal
    from backend.models.models import PaperTrade
    from backend.services.twse_service import fetch_realtime_quote
    from sqlalchemy import select
    import asyncio as _asyncio

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(PaperTrade).where(PaperTrade.user_id == uid)
            .order_by(PaperTrade.traded_at)
        )
        trades = r.scalars().all()

    if not trades:
        return {"positions": [], "realized_pnl": 0.0, "unrealized_pnl": 0.0, "total_pnl": 0.0}

    # 計算各股持倉（FIFO）
    positions: dict = {}
    realized_pnl = 0.0

    for t in trades:
        code = t.stock_code
        if t.action == "buy":
            if code not in positions:
                positions[code] = {"name": t.stock_name, "shares": 0, "cost": 0.0, "buys": []}
            positions[code]["shares"] += t.shares
            positions[code]["cost"]   += t.amount
            positions[code]["buys"].append({"shares": t.shares, "price": t.price})
        elif t.action == "sell":
            if code in positions and positions[code]["shares"] > 0:
                sold = min(t.shares, positions[code]["shares"])
                avg_cost = positions[code]["cost"] / positions[code]["shares"] / 1000
                realized_pnl += (t.price - avg_cost) * sold * 1000
                positions[code]["shares"] -= sold
                if positions[code]["shares"] > 0:
                    positions[code]["cost"] -= avg_cost * sold * 1000
                else:
                    positions[code]["cost"] = 0.0

    # 取得現價計算未實現損益
    open_codes = [c for c, p in positions.items() if p["shares"] > 0]

    async def _safe_price(c):
        try:
            q = await fetch_realtime_quote(c)
            return c, float(q.get("price", 0) or 0) if q else (c, 0.0)
        except Exception:
            return c, 0.0

    if open_codes:
        price_results = await _asyncio.gather(*[_safe_price(c) for c in open_codes])
        price_map = dict(price_results)
    else:
        price_map = {}

    pos_list = []
    unrealized_pnl = 0.0
    for code, pos in positions.items():
        if pos["shares"] <= 0:
            continue
        current = price_map.get(code, 0.0)
        avg_cost = pos["cost"] / pos["shares"] / 1000 if pos["shares"] > 0 else 0
        unreal = (current - avg_cost) * pos["shares"] * 1000 if current > 0 else 0
        unrealized_pnl += unreal
        pct = (current - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0
        pos_list.append({
            "code": code, "name": pos["name"], "shares": pos["shares"],
            "avg_cost": round(avg_cost, 1), "current": round(current, 1),
            "pnl": round(unreal, 0), "pnl_pct": round(pct, 2),
        })

    pos_list.sort(key=lambda x: x["pnl"], reverse=True)

    return {
        "positions": pos_list,
        "realized_pnl": round(realized_pnl, 0),
        "unrealized_pnl": round(unrealized_pnl, 0),
        "total_pnl": round(realized_pnl + unrealized_pnl, 0),
    }


def format_history(trades: list) -> str:
    if not trades:
        return "📜 模擬交易記錄\n\n尚無記錄"
    lines = [f"📜 模擬交易記錄（最近{len(trades)}筆）", "─" * 22]
    for t in trades[:15]:
        icon = "🟢" if t["action"] == "buy" else "🔴"
        action_zh = "買" if t["action"] == "buy" else "賣"
        lines.append(f"{icon} {t['date']} {action_zh} {t['name']}({t['code']}) {t['shares']}張 @{t['price']:.0f}")
    return "\n".join(lines)


def format_pnl(pnl: dict) -> str:
    lines = ["💰 模擬交易損益", "─" * 22]
    if pnl["positions"]:
        lines.append("📊 目前持倉：")
        for p in pnl["positions"]:
            icon = "▲" if p["pnl"] >= 0 else "▼"
            lines.append(f"  {icon} {p['name']}({p['code']}) {p['shares']}張  {p['pnl_pct']:+.1f}%  {p['pnl']:+,.0f}元")
    else:
        lines.append("目前無持倉")
    lines += [
        "",
        f"💼 已實現損益：{pnl['realized_pnl']:+,.0f} 元",
        f"📈 未實現損益：{pnl['unrealized_pnl']:+,.0f} 元",
        f"🎯 總損益：{pnl['total_pnl']:+,.0f} 元",
    ]
    return "\n".join(lines)
