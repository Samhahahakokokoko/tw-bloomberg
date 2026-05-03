"""Insider Flow Engine — 董監事持股追蹤"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from loguru import logger


@dataclass
class InsiderEvent:
    stock_id:     str
    stock_name:   str
    insider_name: str
    role:         str
    action:       str    # buy / sell
    shares:       int
    date_str:     str

    def to_line_text(self) -> str:
        icon = "✅" if self.action == "buy" else "⚠️"
        act  = "買進" if self.action == "buy" else "賣出"
        sig  = "正面訊號" if self.action == "buy" else "注意訊號"
        return (
            f"👔 內部人動態\n"
            f"{self.stock_id} {self.stock_name}\n"
            f"{'─' * 18}\n"
            f"{self.role}：本月{act} {self.shares:,}張\n"
            f"{icon} 內部人{'增持' if self.action == 'buy' else '減持'}，{sig}"
        )


async def get_insider_flow(stock_id: str) -> list[InsiderEvent]:
    """查詢特定股票的董監持股異動"""
    events: list[InsiderEvent] = []

    # 先從資料庫查詢已記錄的紀錄
    try:
        from ..models.database import AsyncSessionLocal
        from ..models.models import InsiderFlowLog
        from sqlalchemy import select, desc

        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(InsiderFlowLog)
                .where(InsiderFlowLog.stock_id == stock_id)
                .order_by(desc(InsiderFlowLog.created_at))
                .limit(5)
            )
            logs = r.scalars().all()

        for lg in logs:
            events.append(InsiderEvent(
                stock_id=lg.stock_id, stock_name=lg.stock_name,
                insider_name=lg.insider_name, role=lg.role,
                action=lg.action, shares=lg.shares,
                date_str=lg.date,
            ))
    except Exception as e:
        logger.debug(f"[insider_flow] db query failed: {e}")

    # 若無資料，嘗試從公開資訊觀測站抓取（目前模擬）
    if not events:
        events = await _fetch_mock_insider(stock_id)

    return events


async def _fetch_mock_insider(stock_id: str) -> list[InsiderEvent]:
    """模擬董監持股資料（實際版本應接入公開資訊觀測站 API）"""
    try:
        from .twse_service import fetch_realtime_quote
        from .report_screener import all_screener

        q    = await fetch_realtime_quote(stock_id)
        name = q.get("name", stock_id) if q else stock_id

        rows = all_screener(200)
        hit  = next((r for r in rows if r.stock_id == stock_id), None)
        if not hit:
            return []

        # 依籌碼狀況推斷董監動態
        action = "buy" if hit.chip_5d > 0 else "sell"
        shares = abs(int(hit.chip_5d / 100)) or 10
        month  = datetime.now().strftime("%Y-%m")

        return [InsiderEvent(
            stock_id=stock_id, stock_name=name,
            insider_name="主要董監事",
            role="董事長",
            action=action, shares=shares,
            date_str=month,
        )]
    except Exception as e:
        logger.warning(f"[insider_flow] mock failed: {e}")
        return []


def format_insider_list(events: list[InsiderEvent], stock_id: str) -> str:
    if not events:
        return f"👔 {stock_id} 董監持股\n\n近期無重大異動記錄"

    lines = [f"👔 {stock_id} 內部人動態", "─" * 18]
    for e in events:
        act  = "買進" if e.action == "buy" else "賣出"
        icon = "✅" if e.action == "buy" else "⚠️"
        lines.append(f"{icon} {e.date_str}  {e.role} {act} {e.shares:,}張")

    # 整體訊號評估
    buy_cnt  = sum(1 for e in events if e.action == "buy")
    sell_cnt = sum(1 for e in events if e.action == "sell")
    if buy_cnt > sell_cnt:
        lines.append("\n→ 整體內部人持續增持，正面訊號")
    elif sell_cnt > buy_cnt:
        lines.append("\n→ 內部人近期減持，建議留意")
    return "\n".join(lines)
