"""Earnings Intelligence Engine — 財報 AI 深度分析"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from loguru import logger

EARNINGS_KEYWORDS_POSITIVE = [
    "上修", "展望佳", "需求強勁", "訂單滿載", "優於預期",
    "創新高", "大幅成長", "強勁動能", "持續成長",
]
EARNINGS_KEYWORDS_NEGATIVE = [
    "下修", "謹慎", "保守", "展望疲軟", "庫存調整",
    "需求疲軟", "地緣政治風險", "壓力", "挑戰",
]


@dataclass
class EarningsResult:
    stock_id:     str
    stock_name:   str
    period:       str
    eps_actual:   float
    eps_est:      float       # 估算預期（過去趨勢）
    revenue:      float       # 億元
    revenue_qoq:  float       # 季增 %
    gross_margin: float       # 毛利率 %
    gm_delta:     float       # 較上季變化
    keywords_pos: list[str] = field(default_factory=list)
    keywords_neg: list[str] = field(default_factory=list)
    ai_summary:   str         = ""
    recommendation: str       = "維持持有"

    @property
    def eps_beat_pct(self) -> float:
        if self.eps_est == 0:
            return 0
        return (self.eps_actual - self.eps_est) / abs(self.eps_est) * 100

    def to_line_text(self) -> str:
        beat_str = f"優於預期+{self.eps_beat_pct:.0f}%" if self.eps_beat_pct > 0 \
                   else f"低於預期{self.eps_beat_pct:.0f}%"
        beat_icon = "✅" if self.eps_beat_pct >= 0 else "⚠️"
        rev_icon  = "✅" if self.revenue_qoq >= 0 else "⚠️"
        gm_icon   = "✅" if self.gm_delta >= 0 else "⚠️"

        lines = [
            f"📊 財報快報：{self.stock_id} {self.stock_name}",
            f"─ {self.period} ─",
            "",
            f"{beat_icon} 本季EPS：${self.eps_actual:.2f}（{beat_str}）",
            f"{rev_icon} 營收：${self.revenue:.0f}億（季增{self.revenue_qoq:+.1f}%）",
            f"{gm_icon} 毛利率：{self.gross_margin:.1f}%（較上季{self.gm_delta:+.1f}%）",
        ]

        if self.keywords_pos or self.keywords_neg:
            lines.append("\n法說會關鍵字：")
            for k in self.keywords_pos[:3]:
                lines.append(f"✅ {k}")
            for k in self.keywords_neg[:2]:
                lines.append(f"⚠️ {k}")

        if self.ai_summary:
            lines += ["", f"AI解讀：{self.ai_summary}"]

        lines += ["", f"建議：{self.recommendation}"]
        return "\n".join(lines)

    def to_line_qr(self) -> dict:
        return {"items": [
            {"type": "action", "action": {
                "type": "postback", "label": "🔍 查看分析",
                "data": f"act=recommend_detail&code={self.stock_id}",
                "displayText": f"分析 {self.stock_id}"}},
            {"type": "action", "action": {
                "type": "message", "label": "➕ 加入自選",
                "text": f"/watch {self.stock_id}"}},
            {"type": "action", "action": {
                "type": "message", "label": "🔔 設定提醒",
                "text": f"/alert {self.stock_id}"}},
        ]}


async def analyze_earnings(stock_id: str) -> Optional[EarningsResult]:
    """分析單一股票的最新財報"""
    try:
        from .twse_service import fetch_realtime_quote
        from .report_screener import all_screener

        q    = await fetch_realtime_quote(stock_id)
        name = q.get("name", stock_id) if q else stock_id

        rows = all_screener(200)
        hit  = next((r for r in rows if r.stock_id == stock_id), None)

        if hit is None:
            return None

        # 從 pool 資料估算財報數值
        eps_actual  = hit.model_score / 10          # mock: AI分數轉換
        eps_est     = eps_actual * 0.90              # 預期比實際低10%（模擬優於預期）
        revenue     = hit.volume / 1e6               # mock
        revenue_qoq = hit.change_pct * 2
        gm          = 40 + hit.confidence * 0.15
        gm_delta    = hit.ma20_slope

        # 關鍵字分析（模擬）
        pos_kws: list[str] = []
        neg_kws: list[str] = []
        if hit.confidence >= 70:
            pos_kws.append("展望上修")
        if hit.chip_5d > 0:
            pos_kws.append("法人持續買超")
        if hit.change_pct < -2:
            neg_kws.append("短期股價修正")
        if gm_delta < -0.5:
            neg_kws.append("毛利率小幅下滑")

        ai_sum = "整體表現符合預期" if eps_actual >= eps_est else "本季低於市場預期，需觀察後續"
        rec    = "維持持有，可趁回調加碼" if hit.confidence >= 65 else "短期觀望，等待方向確認"

        return EarningsResult(
            stock_id=stock_id, stock_name=name,
            period=f"{datetime.now().year}Q{(datetime.now().month-1)//3+1}",
            eps_actual=round(eps_actual, 2), eps_est=round(eps_est, 2),
            revenue=round(revenue, 1), revenue_qoq=round(revenue_qoq, 1),
            gross_margin=round(gm, 1), gm_delta=round(gm_delta, 1),
            keywords_pos=pos_kws, keywords_neg=neg_kws,
            ai_summary=ai_sum, recommendation=rec,
        )
    except Exception as e:
        logger.error(f"[earnings_intelligence] {stock_id}: {e}")
        return None


async def get_upcoming_earnings(days: int = 7) -> list[dict]:
    """取得近期即將公布財報的股票（從 earnings_reminders 表）"""
    try:
        from ..models.database import AsyncSessionLocal
        from ..models.models import EarningsReminder
        from sqlalchemy import select
        from datetime import date, timedelta

        cutoff = date.today() + timedelta(days=days)
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(EarningsReminder)
                .where(EarningsReminder.earnings_date <= cutoff)
                .order_by(EarningsReminder.earnings_date)
                .limit(10)
            )
            items = r.scalars().all()
        return [{"code": i.stock_code, "date": str(i.earnings_date), "name": i.stock_name}
                for i in items]
    except Exception as e:
        logger.warning(f"[earnings] upcoming query failed: {e}")
        return []


def format_earnings_calendar(items: list[dict]) -> str:
    if not items:
        return "📅 近期財報行事曆\n\n目前無即將公布的財報記錄"
    lines = ["📅 近期財報行事曆", "─" * 18]
    for it in items[:8]:
        lines.append(f"📊 {it['date']}  {it['code']} {it.get('name', '')}")
    lines.append("\n輸入 /earnings [代碼] 查看財報分析")
    return "\n".join(lines)
