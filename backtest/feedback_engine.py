"""Feedback Engine — 記錄回測交易，計算績效，自動調整 feature 權重

每次執行完回測後：
  1. 將所有交易存入 backtest_sessions + backtest_trade_records
  2. 計算整體績效：勝率、Sharpe、最大回撤
  3. 分析哪種 feature（技術/基本/籌碼）對應的訊號績效最好
  4. 更新 feature_weights 給 screener_engine 使用
"""
from __future__ import annotations
import json
from datetime import datetime, date
from loguru import logger
from sqlalchemy import select

from backend.models.database import AsyncSessionLocal
from backend.models.models import (
    BacktestSession, BacktestTradeRecord, FeatureWeight,
)
from .engine import BacktestResult


async def save_backtest_result(result: BacktestResult, stock_code: str) -> str:
    """
    將回測結果（BacktestResult）存入 DB。
    回傳 session_id。
    """
    session_id = result.session_id or stock_code + "_" + date.today().isoformat()

    async with AsyncSessionLocal() as db:
        # 存 session 摘要
        session = BacktestSession(
            session_id         = session_id,
            stock_code         = stock_code,
            strategy           = result.strategy,
            start_date         = result.start_date,
            end_date           = result.end_date,
            initial_capital    = result.initial_capital,
            final_capital      = result.final_capital,
            total_return       = result.total_return,
            annualized_return  = result.annualized_return,
            max_drawdown       = result.max_drawdown,
            sharpe_ratio       = result.sharpe_ratio,
            win_rate           = result.win_rate,
            total_trades       = result.total_trades,
            total_commission   = result.total_commission,
            total_tax          = result.total_tax,
            total_slippage     = result.total_slippage,
            cost_impact        = result.total_cost_impact,
            market_regime      = result.regime,
        )
        db.add(session)

        # 存交易明細（只存 SELL 紀錄，因為包含完整 round-trip 資訊）
        for t in result.trades:
            if t.get("action") != "SELL":
                continue
            db.add(BacktestTradeRecord(
                session_id    = session_id,
                stock_code    = stock_code,
                strategy      = result.strategy,
                entry_date    = t.get("entry_date", ""),
                exit_date     = t.get("date", ""),
                entry_price   = t.get("entry_price", 0),
                exit_price    = t.get("price", 0),
                shares        = t.get("shares", 0),
                gross_return  = t.get("gross_pnl", 0),
                net_return    = t.get("pnl", 0),
                commission    = t.get("commission", 0),
                tax           = t.get("tax", 0),
                slippage      = t.get("slippage", 0),
                holding_days  = t.get("holding_days", 0),
                is_winner     = (t.get("pnl", 0) or 0) > 0,
            ))

        await db.commit()

    logger.info(f"[Feedback] 回測結果已存 session={session_id}, trades={result.total_trades}")
    return session_id


async def get_strategy_performance_summary() -> list[dict]:
    """統計各策略在所有回測 session 中的平均績效"""
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(BacktestSession))
        sessions = r.scalars().all()

    by_strategy: dict[str, list] = {}
    for s in sessions:
        by_strategy.setdefault(s.strategy, []).append(s)

    summary = []
    for strat, items in sorted(by_strategy.items()):
        returns    = [i.total_return for i in items if i.total_return is not None]
        sharpes    = [i.sharpe_ratio for i in items if i.sharpe_ratio is not None]
        win_rates  = [i.win_rate     for i in items if i.win_rate     is not None]
        drawdowns  = [i.max_drawdown for i in items if i.max_drawdown is not None]
        summary.append({
            "strategy":         strat,
            "sessions":         len(items),
            "avg_return":       round(sum(returns)   / len(returns),   2) if returns   else 0,
            "avg_sharpe":       round(sum(sharpes)   / len(sharpes),   3) if sharpes   else 0,
            "avg_win_rate":     round(sum(win_rates) / len(win_rates), 1) if win_rates else 0,
            "avg_max_drawdown": round(sum(drawdowns) / len(drawdowns), 2) if drawdowns else 0,
        })

    summary.sort(key=lambda x: x["avg_sharpe"], reverse=True)
    return summary


async def get_session_detail(session_id: str) -> dict:
    """取得單一 session 的詳細資訊"""
    async with AsyncSessionLocal() as db:
        r_s = await db.execute(
            select(BacktestSession).where(BacktestSession.session_id == session_id)
        )
        session = r_s.scalar_one_or_none()

        r_t = await db.execute(
            select(BacktestTradeRecord).where(BacktestTradeRecord.session_id == session_id)
        )
        trades = r_t.scalars().all()

    if not session:
        return {}

    return {
        "session": {
            "session_id":       session.session_id,
            "stock_code":       session.stock_code,
            "strategy":         session.strategy,
            "start_date":       session.start_date,
            "end_date":         session.end_date,
            "total_return":     session.total_return,
            "annualized_return":session.annualized_return,
            "sharpe_ratio":     session.sharpe_ratio,
            "win_rate":         session.win_rate,
            "max_drawdown":     session.max_drawdown,
            "total_commission": session.total_commission,
            "total_tax":        session.total_tax,
            "cost_impact":      session.cost_impact,
            "market_regime":    session.market_regime,
        },
        "trades": [
            {
                "entry_date":  t.entry_date,
                "exit_date":   t.exit_date,
                "entry_price": t.entry_price,
                "exit_price":  t.exit_price,
                "shares":      t.shares,
                "net_return":  t.net_return,
                "holding_days":t.holding_days,
                "is_winner":   t.is_winner,
            }
            for t in trades
        ],
    }


async def get_feature_weights() -> dict:
    """取最新一組 feature 權重（給 screener_engine 使用）"""
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(FeatureWeight).order_by(FeatureWeight.updated_at.desc()).limit(1)
        )
        fw = r.scalar_one_or_none()

    if fw:
        return {
            "fundamental": fw.fundamental_weight,
            "chip":        fw.chip_weight,
            "technical":   fw.technical_weight,
        }
    return {"fundamental": 0.35, "chip": 0.35, "technical": 0.30}


async def auto_adjust_feature_weights():
    """
    根據回測績效自動調整三維度 feature 權重：
    - 在「基本面強」策略（ma_cross, macd）表現好時 → 提升基本面權重
    - 在「籌碼面」策略（institutional）表現好時 → 提升籌碼面權重
    - 在「技術面」策略（rsi, kd, bollinger, pvd）表現好時 → 提升技術面權重
    """
    summary = await get_strategy_performance_summary()
    if not summary:
        return

    FUNDAMENTAL_STRATEGIES = {"ma_cross", "macd", "momentum"}
    CHIP_STRATEGIES         = {"institutional"}
    TECHNICAL_STRATEGIES    = {"rsi", "kd", "bollinger", "pvd", "mean_reversion", "defensive"}

    def avg_sharpe(strats: set) -> float:
        items = [s for s in summary if s["strategy"] in strats]
        if not items:
            return 0
        return sum(s["avg_sharpe"] for s in items) / len(items)

    f_score = max(0, avg_sharpe(FUNDAMENTAL_STRATEGIES))
    c_score = max(0, avg_sharpe(CHIP_STRATEGIES))
    t_score = max(0, avg_sharpe(TECHNICAL_STRATEGIES))
    total   = f_score + c_score + t_score

    if total == 0:
        fw, cw, tw = 0.35, 0.35, 0.30
    else:
        fw = round(f_score / total, 4)
        cw = round(c_score / total, 4)
        tw = round(1 - fw - cw,    4)

    # 限制在合理範圍
    fw = max(0.10, min(0.60, fw))
    cw = max(0.10, min(0.60, cw))
    tw = max(0.10, min(0.60, tw))
    total2 = fw + cw + tw
    fw, cw, tw = fw / total2, cw / total2, tw / total2

    async with AsyncSessionLocal() as db:
        db.add(FeatureWeight(
            fundamental_weight = round(fw, 4),
            chip_weight        = round(cw, 4),
            technical_weight   = round(tw, 4),
            notes              = f"auto adjusted: F={fw:.2f} C={cw:.2f} T={tw:.2f}",
            updated_at         = datetime.utcnow(),
        ))
        await db.commit()

    logger.info(f"[Feedback] Feature weights adjusted: F={fw:.2f} C={cw:.2f} T={tw:.2f}")
