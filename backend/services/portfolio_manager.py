"""AI Portfolio Manager — 每日自動分析庫存並給出調整建議"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import httpx
from loguru import logger


@dataclass
class HoldingSignal:
    stock_id:   str
    stock_name: str
    action:     Literal["add", "reduce", "hold", "exit"]
    current_pct: float   # 目前佔投組比例 %
    target_pct:  float   # 建議目標比例 %
    reason:      str
    ai_score:    float = 50.0


@dataclass
class PortfolioAdvice:
    uid:             str
    health_score:    int
    main_risk:       str
    signals:         list[HoldingSignal] = field(default_factory=list)
    add_list:        list[HoldingSignal] = field(default_factory=list)
    reduce_list:     list[HoldingSignal] = field(default_factory=list)
    hold_list:       list[HoldingSignal] = field(default_factory=list)

    def to_line_text(self) -> str:
        today = datetime.now().strftime("%m/%d")
        lines = [f"🤖 AI 投組管理建議  {today}", "─" * 20, "", "今日建議操作："]

        for s in self.add_list[:2]:
            lines.append(f"➕ 加碼：{s.stock_id} {s.stock_name}（{s.reason}）")
        for s in self.reduce_list[:2]:
            lines.append(f"➖ 減碼：{s.stock_id} {s.stock_name}（{s.reason}）")
        for s in self.hold_list[:3]:
            lines.append(f"⚖️ 維持：{s.stock_id} {s.stock_name}（{s.reason}）")

        lines += [
            "",
            f"投組健康分：{self.health_score}/100",
            f"主要風險：{self.main_risk}",
        ]
        return "\n".join(lines)


async def analyze_portfolio(uid: str) -> PortfolioAdvice:
    """分析用戶庫存並生成 AI 投組建議"""
    advice = PortfolioAdvice(uid=uid, health_score=75, main_risk="資料不足，無法評估")

    try:
        from .portfolio_service import get_holdings
        holdings = await get_holdings(uid)
        if not holdings:
            advice.main_risk = "尚無持股"
            return advice

        # 計算各持股比例
        total_val = sum(h.get("market_value", 0) or 0 for h in holdings)
        if total_val == 0:
            return advice

        # 族群集中度分析
        sector_map: dict[str, float] = {}
        for h in holdings:
            sec = h.get("sector", "其他")
            val = h.get("market_value", 0) or 0
            sector_map[sec] = sector_map.get(sec, 0) + val

        max_sector     = max(sector_map, key=sector_map.get) if sector_map else "其他"
        max_sector_pct = sector_map.get(max_sector, 0) / total_val * 100

        health = 85
        if max_sector_pct > 70:
            health -= 15
            advice.main_risk = f"{max_sector}集中度偏高（{max_sector_pct:.0f}%）"
        elif max_sector_pct > 50:
            health -= 5
            advice.main_risk = f"{max_sector}集中度略高（{max_sector_pct:.0f}%）"
        else:
            advice.main_risk = "族群分散，風險可控"

        # 各持股評分
        from .report_screener import all_screener
        screener_rows = all_screener(200)
        score_map = {r.stock_id: r for r in screener_rows}

        signals: list[HoldingSignal] = []
        for h in holdings:
            code  = h.get("stock_code", "")
            name  = h.get("stock_name", code)
            val   = h.get("market_value", 0) or 0
            pct   = val / total_val * 100
            pnl   = h.get("unrealized_pnl_pct", 0) or 0
            row   = score_map.get(code)
            score = row.confidence if row else 50

            if score >= 70 and pct < 15:
                action = "add"
                reason = f"趨勢最強，建議加至{min(pct+5,15):.0f}%"
                health += 2
            elif score < 40 or pnl < -0.10:
                action = "reduce"
                reason = f"動能轉弱，降至{max(pct-5,0):.0f}%"
                health -= 3
            else:
                action = "hold"
                reason = "持續觀察"

            sig = HoldingSignal(
                stock_id=code, stock_name=name,
                action=action, current_pct=round(pct, 1),
                target_pct=round(pct, 1),
                reason=reason, ai_score=score,
            )
            signals.append(sig)

        advice.signals    = signals
        advice.add_list   = [s for s in signals if s.action == "add"]
        advice.reduce_list = [s for s in signals if s.action == "reduce"]
        advice.hold_list  = [s for s in signals if s.action == "hold"]
        advice.health_score = max(0, min(100, health))

    except Exception as e:
        logger.error(f"[portfolio_manager] analyze failed: {e}")

    return advice


async def push_daily_portfolio_advice():
    """每日 19:30 推送 AI 投組建議"""
    from ..models.database import AsyncSessionLocal, settings
    from ..models.models import Subscriber
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        r    = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
        subs = r.scalars().all()

    if not subs:
        return

    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    async with httpx.AsyncClient(timeout=30) as c:
        for sub in subs:
            try:
                advice = await analyze_portfolio(sub.line_user_id)
                if not advice.signals:
                    continue
                text = advice.to_line_text()
                qr   = {"items": [
                    {"type": "action", "action": {
                        "type": "postback", "label": "💼 看庫存",
                        "data": "act=portfolio_view", "displayText": "看庫存"}},
                    {"type": "action", "action": {
                        "type": "postback", "label": "📊 今日選股",
                        "data": "act=screener_qr", "displayText": "今日選股"}},
                ]}
                await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": sub.line_user_id, "messages": [
                        {"type": "text", "text": text, "quickReply": qr}
                    ]},
                    headers=headers,
                )
            except Exception as e:
                logger.warning(f"[portfolio_manager] push failed: {e}")

    logger.info(f"[portfolio_manager] pushed to {len(subs)} subscribers")
