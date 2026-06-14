"""Personal Performance Service — 個人交易績效追蹤"""
from __future__ import annotations

import asyncio
from datetime import datetime, date, timedelta
from sqlalchemy import select
from loguru import logger

from ..models.database import AsyncSessionLocal


async def get_personal_performance(uid: str) -> dict:
    """取得個人績效：本月、本季、今年報酬率 + 最佳/最差交易 + 勝率"""
    async with AsyncSessionLocal() as db:
        from ..models.models import Portfolio, TradeLog
        # 持倉
        port_result = await db.execute(
            select(Portfolio).where(Portfolio.user_id == uid)
        )
        holdings = port_result.scalars().all()

        # 交易記錄
        try:
            hist_result = await db.execute(
                select(TradeLog).where(TradeLog.user_id == uid)
            )
            trades = hist_result.scalars().all()
        except Exception as e:
            trades = []

    # 取現價
    from .twse_service import fetch_realtime_quote
    prices = {}
    if holdings:
        results = await asyncio.gather(*[
            _safe_quote(h.stock_code) for h in holdings
        ], return_exceptions=True)
        for h, r in zip(holdings, results):
            if isinstance(r, dict):
                prices[h.stock_code] = float(r.get("close") or r.get("price") or h.cost_price)

    # 計算持倉市值和損益
    total_cost = total_mv = 0.0
    for h in holdings:
        cost = float(h.cost_price or 0) * int(h.shares or 0)
        mv   = prices.get(h.stock_code, float(h.cost_price or 0)) * int(h.shares or 0)
        total_cost += cost
        total_mv   += mv

    unrealized_pnl = total_mv - total_cost
    unrealized_pct = unrealized_pnl / total_cost * 100 if total_cost > 0 else 0.0

    # 已實現損益統計（從交易記錄）
    realized = _calc_realized(trades)

    # 月/季/年 報酬
    now = datetime.now()
    month_ret  = _period_return(trades, now - timedelta(days=30))
    quarter_ret = _period_return(trades, now - timedelta(days=90))
    year_ret   = _period_return(trades, date(now.year, 1, 1))

    # 對標 0050
    benchmark = await _get_benchmark_return()

    # 最佳/最差 交易
    best, worst = _best_worst(trades)

    # 勝率
    win_rate = _calc_win_rate(trades)

    return {
        "uid":            uid,
        "holdings_count": len(holdings),
        "total_cost":     total_cost,
        "total_mv":       total_mv,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pct": unrealized_pct,
        "realized_pnl":   realized.get("total", 0),
        "month_ret":      month_ret,
        "quarter_ret":    quarter_ret,
        "year_ret":       year_ret,
        "benchmark_ret":  benchmark,
        "best_trade":     best,
        "worst_trade":    worst,
        "win_rate":       win_rate,
        "total_trades":   len([t for t in trades if getattr(t, "action", "BUY") == "SELL"]),
    }


async def _safe_quote(code: str) -> dict:
    try:
        from .twse_service import fetch_realtime_quote
        return await fetch_realtime_quote(code) or {}
    except Exception as e:
        return {}


def _calc_realized(trades) -> dict:
    total = 0.0
    for t in trades:
        if getattr(t, "action", "BUY") == "SELL":
            pnl = float(getattr(t, "realized_pnl", 0) or 0)
            total += pnl
    return {"total": total}


def _period_return(trades, since) -> float | None:
    if not trades:
        return None
    since_dt = since if isinstance(since, datetime) else datetime.combine(since, datetime.min.time())
    pnl = 0.0
    cost = 0.0
    for t in trades:
        if getattr(t, "action", "BUY") != "SELL":
            continue
        trade_date = getattr(t, "trade_date", None) or getattr(t, "created_at", None)
        if not trade_date:
            continue
        if isinstance(trade_date, str):
            try: trade_date = datetime.fromisoformat(trade_date)
            except: continue
        if trade_date >= since_dt:
            p = float(getattr(t, "realized_pnl", 0) or 0)
            c = float(getattr(t, "trade_value", 0) or 0)
            pnl  += p
            cost += abs(c)
    if cost <= 0:
        return None
    return round(pnl / cost * 100, 2)


async def _get_benchmark_return() -> float:
    """取得 0050 近一個月報酬率"""
    try:
        from .twse_service import fetch_kline
        kl = await fetch_kline("0050")
        if kl and len(kl) >= 22:
            c0 = float(kl[-22].get("close") or 0)
            c1 = float(kl[-1].get("close")  or 0)
            if c0 > 0:
                return round((c1 - c0) / c0 * 100, 2)
    except Exception as e:
        pass
    return 0.0


def _best_worst(trades) -> tuple[dict | None, dict | None]:
    sell_trades = [t for t in trades if getattr(t, "action", "BUY") == "SELL"]
    if not sell_trades:
        return None, None

    def pnl_pct(t):
        p    = float(getattr(t, "realized_pnl", 0) or 0)
        cost = float(getattr(t, "trade_value", 0) or 1)
        return p / abs(cost) * 100 if cost != 0 else 0

    best  = max(sell_trades, key=pnl_pct, default=None)
    worst = min(sell_trades, key=pnl_pct, default=None)

    def to_dict(t):
        if t is None: return None
        return {
            "code":    getattr(t, "stock_code", ""),
            "pnl":     float(getattr(t, "realized_pnl", 0) or 0),
            "pnl_pct": pnl_pct(t),
        }

    return to_dict(best), to_dict(worst)


def _calc_win_rate(trades) -> float | None:
    sell_trades = [t for t in trades if getattr(t, "action", "BUY") == "SELL"]
    if not sell_trades:
        return None
    wins = sum(1 for t in sell_trades
               if float(getattr(t, "realized_pnl", 0) or 0) > 0)
    return round(wins / len(sell_trades) * 100, 1)


def format_performance_report(data: dict) -> str:
    def pct_str(v):
        if v is None: return "N/A"
        arrow = "▲" if v > 0 else ("▼" if v < 0 else "—")
        return f"{arrow} {abs(v):.2f}%"

    def pnl_str(v):
        if v is None: return "N/A"
        return f"{v:+,.0f} 元"

    lines = [
        "📈 個人交易績效報告",
        "─" * 28,
        "",
        "【持倉概況】",
        f"  持股：{data['holdings_count']} 支",
        f"  總成本：{data['total_cost']:,.0f} 元",
        f"  現值：{data['total_mv']:,.0f} 元",
        f"  未實現損益：{pnl_str(data['unrealized_pnl'])}（{pct_str(data['unrealized_pct'])}）",
        f"  已實現損益：{pnl_str(data['realized_pnl'])}",
        "",
        "【報酬率比較】",
        f"  本月：{pct_str(data['month_ret'])}  vs 0050: {pct_str(data['benchmark_ret'])}",
        f"  本季：{pct_str(data['quarter_ret'])}",
        f"  今年：{pct_str(data['year_ret'])}",
    ]

    if data.get("total_trades", 0) > 0:
        lines += [
            "",
            "【交易統計】",
            f"  總交易次數：{data['total_trades']} 次",
            f"  勝率：{data['win_rate']:.1f}%" if data.get("win_rate") is not None else "  勝率：N/A",
        ]
        if data.get("best_trade"):
            b = data["best_trade"]
            lines.append(f"  最佳交易：{b['code']}  {pnl_str(b['pnl'])}（{pct_str(b['pnl_pct'])}）")
        if data.get("worst_trade"):
            w = data["worst_trade"]
            lines.append(f"  最差交易：{w['code']}  {pnl_str(w['pnl'])}（{pct_str(w['pnl_pct'])}）")
    else:
        lines += ["", "尚無已完成交易記錄"]

    return "\n".join(lines)
