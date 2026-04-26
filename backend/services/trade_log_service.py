"""交易日誌服務 — 記錄每筆買賣、計算手續費/稅、統計損益"""
from __future__ import annotations
from datetime import datetime, date
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, extract
from ..models.models import TradeLog, Portfolio
from loguru import logger

COMMISSION_RATE = 0.001425   # 0.1425%
TAX_RATE_SELL   = 0.003      # 0.3% 證交稅（賣出才有）
MIN_COMMISSION  = 20         # 最低手續費 20 元


async def log_trade(
    db: AsyncSession,
    user_id: str,
    stock_code: str,
    stock_name: str,
    action: str,           # "BUY" | "SELL"
    price: float,
    shares: int,
    avg_cost: float = 0,   # 均成本（SELL 才需要）
) -> TradeLog:
    value      = price * shares
    commission = max(round(value * COMMISSION_RATE), MIN_COMMISSION)
    tax        = round(value * TAX_RATE_SELL) if action == "SELL" else 0

    if action == "BUY":
        net_amount   = -(value + commission)     # 付出
        realized_pnl = 0.0
    else:
        net_amount   = value - commission - tax  # 收入
        realized_pnl = (price - avg_cost) * shares - commission - tax

    entry = TradeLog(
        user_id=user_id,
        trade_date=date.today().isoformat(),
        stock_code=stock_code,
        stock_name=stock_name,
        action=action,
        price=price,
        shares=shares,
        trade_value=round(value, 2),
        commission=commission,
        tax=tax,
        net_amount=round(net_amount, 2),
        realized_pnl=round(realized_pnl, 2),
        avg_cost_at_trade=avg_cost,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    logger.info(f"TradeLog: {user_id[:8]} {action} {stock_code} x{shares} @{price}")
    return entry


async def get_history(
    db: AsyncSession,
    user_id: str,
    limit: int = 20,
    stock_code: str = None,
) -> list[TradeLog]:
    q = (
        select(TradeLog)
        .where(TradeLog.user_id == user_id)
        .order_by(TradeLog.trade_date.desc(), TradeLog.id.desc())
        .limit(limit)
    )
    if stock_code:
        q = q.where(TradeLog.stock_code == stock_code)
    r = await db.execute(q)
    return r.scalars().all()


async def get_ytd_tax(db: AsyncSession, user_id: str) -> dict:
    """今年度稅務試算"""
    year = datetime.now().year
    q = (
        select(
            func.sum(TradeLog.tax).label("total_tax"),
            func.sum(TradeLog.commission).label("total_commission"),
            func.sum(TradeLog.realized_pnl).label("total_realized"),
            func.count(TradeLog.id).label("trade_count"),
        )
        .where(
            TradeLog.user_id == user_id,
            TradeLog.trade_date >= f"{year}-01-01",
        )
    )
    r = await db.execute(q)
    row = r.one()
    return {
        "year": year,
        "total_tax":        round(float(row.total_tax or 0)),
        "total_commission": round(float(row.total_commission or 0)),
        "total_realized":   round(float(row.total_realized or 0)),
        "trade_count":      int(row.trade_count or 0),
        "total_cost":       round(float((row.total_tax or 0) + (row.total_commission or 0))),
    }


async def get_monthly_stats(
    db: AsyncSession, user_id: str, year: int, month: int
) -> dict:
    """指定月份的交易統計"""
    prefix = f"{year}-{month:02d}"
    q = (
        select(TradeLog)
        .where(
            TradeLog.user_id == user_id,
            TradeLog.trade_date.startswith(prefix),
        )
        .order_by(TradeLog.trade_date)
    )
    r = await db.execute(q)
    logs = r.scalars().all()

    if not logs:
        return {"year": year, "month": month, "trade_count": 0}

    sells = [t for t in logs if t.action == "SELL"]
    wins  = [t for t in sells if t.realized_pnl > 0]

    best  = max(sells, key=lambda t: t.realized_pnl, default=None)
    worst = min(sells, key=lambda t: t.realized_pnl, default=None)

    return {
        "year":    year,
        "month":   month,
        "trade_count": len(logs),
        "sell_count":  len(sells),
        "total_realized": round(sum(t.realized_pnl for t in sells), 2),
        "total_commission": round(sum(t.commission for t in logs), 2),
        "total_tax": round(sum(t.tax for t in logs), 2),
        "win_rate":  round(len(wins) / len(sells) * 100, 1) if sells else 0,
        "best_trade":  {
            "code": best.stock_code, "pnl": round(best.realized_pnl),
            "date": best.trade_date,
        } if best else None,
        "worst_trade": {
            "code": worst.stock_code, "pnl": round(worst.realized_pnl),
            "date": worst.trade_date,
        } if worst else None,
    }


def format_trade_history(logs: list[TradeLog]) -> str:
    if not logs:
        return "尚無交易紀錄"
    lines = ["📋 交易紀錄\n" + "─"*22]
    for t in logs:
        icon = "🟢" if t.action == "BUY" else "🔴"
        pnl_str = f"  損益 {t.realized_pnl:+,.0f}" if t.action == "SELL" else ""
        lines.append(
            f"{icon} {t.trade_date}  {t.stock_code}\n"
            f"   {t.action} {t.shares:,}股 @{t.price:,.1f}"
            f"  手續費 {t.commission:,.0f}"
            + (f"  稅 {t.tax:,.0f}" if t.tax else "")
            + pnl_str
        )
    return "\n".join(lines)


def format_monthly_report(stats: dict, unrealized_pnl: float = 0) -> str:
    if stats.get("trade_count", 0) == 0:
        return f"📊 {stats['year']}/{stats['month']:02d} 月報\n本月無交易紀錄"

    best  = stats.get("best_trade")
    worst = stats.get("worst_trade")
    lines = [
        f"📊 {stats['year']}/{stats['month']:02d} 月度績效報告",
        "─" * 24,
        f"交易筆數：{stats['trade_count']} 筆（賣出 {stats['sell_count']} 筆）",
        f"已實現損益：{stats['total_realized']:+,.0f}",
        f"未實現損益：{unrealized_pnl:+,.0f}",
        f"勝率：{stats['win_rate']:.1f}%",
        "─" * 24,
        f"最佳交易：{best['code']} +{best['pnl']:,.0f} ({best['date']})" if best else "最佳：無",
        f"最差交易：{worst['code']} {worst['pnl']:+,.0f} ({worst['date']})" if worst else "最差：無",
        "─" * 24,
        f"手續費支出：{stats['total_commission']:,.0f}",
        f"證交稅支出：{stats['total_tax']:,.0f}",
        f"交易成本合計：{stats['total_commission']+stats['total_tax']:,.0f}",
    ]
    return "\n".join(lines)


def format_tax_report(stats: dict) -> str:
    return (
        f"🧾 {stats['year']} 年度稅務試算\n"
        f"─" * 22 + "\n"
        f"總交易筆數：{stats['trade_count']}\n"
        f"已實現損益：{stats['total_realized']:+,.0f}\n"
        f"─" * 22 + "\n"
        f"證交稅合計：{stats['total_tax']:,.0f}\n"
        f"手續費合計：{stats['total_commission']:,.0f}\n"
        f"交易成本合計：{stats['total_cost']:,.0f}\n"
        f"─" * 22 + "\n"
        f"台灣現行無證券交易所得稅\n"
        f"（2016年停徵後尚未復徵）"
    )
