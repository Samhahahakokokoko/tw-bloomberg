"""AI Trade Journal — 交易日誌自動記錄與查詢"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from loguru import logger
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.models import TradeJournal
from ..models.database import AsyncSessionLocal


# ── AI 原因生成（規則式，可升級為 Claude）──────────────────────────────────────

async def _generate_entry_reason(stock_id: str, action: str) -> tuple[str, str, float, float]:
    """
    為進出場生成 AI 原因與風險備註。
    回傳 (reason, risk_notes, stop_loss_pct, target_pct)
    """
    reasons: list[str] = []
    risks:   list[str] = []

    try:
        from .twse_service import fetch_realtime_quote, fetch_institutional
        q = await fetch_realtime_quote(stock_id)
        if q:
            chg = q.get("change_pct", 0) or 0
            if action == "buy":
                if chg > 0:
                    reasons.append(f"今日上漲 +{chg:.1f}%，動能正向")
                if chg > 3:
                    risks.append(f"短線漲幅已達 {chg:.1f}%，注意追高風險")
            else:
                if chg < 0:
                    reasons.append(f"今日下跌 {chg:.1f}%，停損出場")

        inst = await fetch_institutional(stock_id)
        if inst:
            fn = inst.get("foreign_net", 0) or 0
            if fn > 0 and action == "buy":
                reasons.append(f"外資買超 {fn/1e6:.0f}萬，籌碼支撐")
            elif fn < 0 and action == "buy":
                risks.append(f"外資賣超 {abs(fn)/1e6:.0f}萬，留意籌碼壓力")
    except Exception as e:
        logger.debug(f"[journal] reason gen failed: {e}")

    try:
        from .report_screener import momentum_screener
        rows = momentum_screener(50)
        hit  = next((r for r in rows if r.stock_id == stock_id), None)
        if hit:
            if hit.confidence >= 70 and action == "buy":
                reasons.append(f"AI 評分 {hit.confidence:.0f}分，系統評估正面")
            if hit.breakout_pct >= 3:
                reasons.append(f"突破 {hit.breakout_pct:.1f}%，技術面突破")
            if hit.chip_5d > 0 and action == "buy":
                reasons.append("外資法人5日買超，籌碼健康")
            if hit.change_pct < -2 and action == "buy":
                risks.append(f"今日跌 {hit.change_pct:.1f}%，確認非逢低買進反彈")
    except Exception as e:
        logger.debug(f"[journal] screener reason failed: {e}")

    if not reasons:
        reasons = ["手動操作，自行判斷進場"] if action == "buy" else ["手動出場"]
    if not risks:
        risks = ["注意倉位控制，勿重押單一個股"]

    stop_pct   = -0.08   # 預設 8% 停損
    target_pct = +0.15   # 預設 15% 目標

    return "\n".join(f"✅ {r}" for r in reasons[:4]), \
           "\n".join(f"⚠️ {r}" for r in risks[:3]), \
           stop_pct, target_pct


# ── 核心 CRUD ─────────────────────────────────────────────────────────────────

async def log_trade(
    uid: str,
    stock_id: str,
    stock_name: str,
    action: str,
    price: float,
    shares: int,
) -> TradeJournal:
    """記錄一筆交易並生成 AI 日誌"""
    reason, risk_notes, sl_pct, tp_pct = await _generate_entry_reason(stock_id, action)

    stop_loss    = round(price * (1 + sl_pct), 1)
    target_price = round(price * (1 + tp_pct), 1)

    entry = TradeJournal(
        user_id      = uid,
        date         = date.today(),
        stock_id     = stock_id,
        stock_name   = stock_name,
        action       = action,
        price        = price,
        shares       = shares,
        reason       = reason,
        risk_notes   = risk_notes,
        stop_loss    = stop_loss,
        target_price = target_price,
        outcome      = "holding",
    )
    async with AsyncSessionLocal() as db:
        db.add(entry)
        await db.commit()
        await db.refresh(entry)
    return entry


async def get_journal(uid: str, stock_id: Optional[str] = None,
                      limit: int = 10) -> list[TradeJournal]:
    """查詢交易日誌"""
    async with AsyncSessionLocal() as db:
        q = select(TradeJournal).where(TradeJournal.user_id == uid)
        if stock_id:
            q = q.where(TradeJournal.stock_id == stock_id)
        q = q.order_by(desc(TradeJournal.created_at)).limit(limit)
        r = await db.execute(q)
        return list(r.scalars().all())


def format_journal_entry(entry: TradeJournal) -> str:
    """格式化單筆交易日誌訊息"""
    action_str = "買進" if entry.action == "buy" else "賣出"
    date_str   = entry.date.strftime("%Y/%m/%d") if entry.date else "--"
    lines = [
        f"📓 交易日誌",
        f"{date_str} {action_str} {entry.stock_id} {entry.stock_name}",
        "─" * 20,
        "",
        "進場原因：" if entry.action == "buy" else "出場原因：",
        entry.reason or "（無記錄）",
        "",
    ]
    if entry.risk_notes:
        lines += ["風險注意：", entry.risk_notes, ""]
    if entry.stop_loss:
        lines.append(f"建議停損：${entry.stop_loss:.0f}")
    if entry.target_price:
        lines.append(f"建議目標：${entry.target_price:.0f}")
    if entry.outcome != "holding":
        pnl_str = f"+{entry.pnl:.0f}" if (entry.pnl or 0) >= 0 else f"{entry.pnl:.0f}"
        lines.append(f"結果：{entry.outcome}  損益：{pnl_str}")
    return "\n".join(lines)


def format_journal_list(entries: list[TradeJournal]) -> str:
    """格式化多筆日誌摘要"""
    if not entries:
        return "📓 交易日誌\n\n尚無記錄\n每次買賣後自動記錄"

    lines = [f"📓 交易日誌（最近 {len(entries)} 筆）", "─" * 18]
    for e in entries:
        date_str   = e.date.strftime("%m/%d") if e.date else "--"
        action_str = "買" if e.action == "buy" else "賣"
        outcome    = "⏳" if e.outcome == "holding" else ("✅" if e.outcome == "profit" else "❌")
        lines.append(f"{outcome} {date_str} {action_str} {e.stock_id} {e.stock_name} @{e.price:.0f}")
    lines.append("\n輸入 /journal 代碼 查看詳情")
    return "\n".join(lines)
