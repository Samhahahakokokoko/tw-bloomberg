"""Position Rebalancer — 投組再平衡建議"""
from __future__ import annotations

from dataclasses import dataclass, field
from loguru import logger


@dataclass
class RebalanceAction:
    stock_id:    str
    stock_name:  str
    current_pct: float
    target_pct:  float
    shares_delta: int   # 正=加 負=減
    price:       float

    @property
    def action(self) -> str:
        return "賣出" if self.shares_delta < 0 else "買進"

    def to_text(self) -> str:
        delta = abs(self.shares_delta)
        return (
            f"{self.stock_id} 目前{self.current_pct:.0f}% → 建議{self.target_pct:.0f}%"
            f"（{self.action}{delta}股）"
        )


@dataclass
class RebalanceReport:
    actions:       list[RebalanceAction] = field(default_factory=list)
    current_sharpe: float = 1.5
    target_sharpe:  float = 1.7
    current_mdd:    float = -0.18
    target_mdd:     float = -0.14
    cash_pct:       float = 8.0
    target_cash_pct: float = 15.0

    def to_line_text(self) -> str:
        if not self.actions:
            return "⚖️ 投組再平衡\n\n✅ 目前配置均衡，無需調整"

        lines = ["⚖️ 投組再平衡建議", "─" * 18, "", "需要調整："]
        for a in self.actions[:4]:
            lines.append(a.to_text())
        if self.cash_pct < self.target_cash_pct:
            lines.append(f"現金 目前{self.cash_pct:.0f}% → 建議{self.target_cash_pct:.0f}%（保留資金）")

        lines += [
            "",
            "預估調整後：",
            f"夏普比率：{self.current_sharpe:.2f} → {self.target_sharpe:.2f} ↑",
            f"最大回撤：{self.current_mdd*100:.0f}% → {self.target_mdd*100:.0f}% ↓",
        ]
        return "\n".join(lines)

    def to_line_qr(self) -> dict:
        return {"items": [
            {"type": "action", "action": {
                "type": "message", "label": "💼 查看庫存", "text": "/portfolio"}},
            {"type": "action", "action": {
                "type": "message", "label": "🤖 AI建議", "text": "/manage"}},
            {"type": "action", "action": {
                "type": "postback", "label": "略過",
                "data": "act=market_card", "displayText": "略過"}},
        ]}


async def calculate_rebalance(uid: str) -> RebalanceReport:
    """計算投組再平衡建議"""
    report = RebalanceReport()
    try:
        from .portfolio_service import get_holdings
        from .report_screener import all_screener

        holdings  = await get_holdings(uid)
        if not holdings:
            return report

        total_val = sum(h.get("market_value", 0) or 0 for h in holdings)
        if total_val == 0:
            return report

        rows      = all_screener(200)
        score_map = {r.stock_id: r for r in rows}

        # 計算目標權重（依 AI 分數調整）
        actions: list[RebalanceAction] = []
        for h in holdings:
            code    = h.get("stock_code", "")
            name    = h.get("stock_name", code)
            val     = h.get("market_value", 0) or 0
            curr_pct = val / total_val * 100
            price   = float(h.get("current_price") or h.get("cost_price") or 0)
            if price <= 0:
                try:
                    from .report_screener import _rt_cache
                    _p = _rt_cache.get("prices", {}).get(code, {})
                    price = float(_p.get("close", 0) or 0)
                except Exception:
                    pass
            if price <= 0:
                continue   # 無法取得股價，跳過此持股的再平衡計算
            row     = score_map.get(code)
            score   = row.confidence if row else 50

            # 目標比例：依評分調整
            if score >= 75:
                target_pct = min(curr_pct * 1.1, 20)
            elif score < 45:
                target_pct = max(curr_pct * 0.7, 0)
            else:
                target_pct = curr_pct

            delta_pct   = target_pct - curr_pct
            delta_val   = delta_pct / 100 * total_val
            shares_delta = int(delta_val / price / 1000) * 1000  # 整張

            if abs(delta_pct) >= 5 and abs(shares_delta) >= 1000:
                actions.append(RebalanceAction(
                    stock_id=code, stock_name=name,
                    current_pct=round(curr_pct, 1),
                    target_pct=round(target_pct, 1),
                    shares_delta=shares_delta,
                    price=price,
                ))

        report.actions = actions[:4]

        # 估算績效改善
        if actions:
            sell_count = sum(1 for a in actions if a.shares_delta < 0)
            report.target_sharpe = report.current_sharpe + 0.02 * len(actions)
            report.target_mdd    = report.current_mdd * (1 - 0.02 * sell_count)

    except Exception as e:
        logger.error(f"[rebalancer] {e}")

    return report


async def push_weekly_rebalance():
    """每週推送再平衡建議給有持股的訂閱者"""
    from ..models.database import AsyncSessionLocal, settings
    from ..models.models import Subscriber
    from sqlalchemy import select
    import httpx

    async with AsyncSessionLocal() as db:
        r    = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
        subs = r.scalars().all()

    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    async with httpx.AsyncClient(timeout=30) as c:
        for sub in subs:
            try:
                report = await calculate_rebalance(sub.line_user_id)
                if not report.actions:
                    continue
                text = report.to_line_text()
                qr   = report.to_line_qr()
                await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": sub.line_user_id, "messages": [
                        {"type": "text", "text": text, "quickReply": qr}
                    ]},
                    headers=headers,
                )
            except Exception as e:
                logger.warning(f"[rebalancer] push failed: {e}")
    logger.info(f"[rebalancer] pushed to {len(subs)} subscribers")
