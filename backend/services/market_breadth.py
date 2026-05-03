"""Market Breadth Engine — 市場廣度監控

指標：
  - 上漲/下跌家數比
  - 創20日新高比例
  - 跌停家數
  - 強勢股佔比（RS > 1 的比例）
  - 廣度警告：強勢股佔比 < 30% 推送警告
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from loguru import logger


@dataclass
class BreadthSnapshot:
    date:           str
    advances:       int   = 0     # 上漲家數
    declines:       int   = 0     # 下跌家數
    unchanged:      int   = 0     # 平盤家數
    new_high_pct:   float = 0.0   # 創20日新高比例 %
    limit_down:     int   = 0     # 跌停家數
    strong_pct:     float = 0.0   # 強勢股佔比 %（change > 0）
    breadth_score:  float = 50.0  # 綜合廣度評分 0~100
    warning:        bool  = False # 廣度惡化警告

    @property
    def advance_decline_ratio(self) -> float:
        total = self.advances + self.declines
        return self.advances / max(total, 1)

    def summary_text(self) -> str:
        arrow = "▲" if self.advance_decline_ratio >= 0.5 else "▼"
        warn  = "  ⚠️ 廣度惡化" if self.warning else ""
        return (
            f"📊 市場廣度{warn}\n"
            f"  上漲 {self.advances} / 下跌 {self.declines}\n"
            f"  強勢股佔比 {self.strong_pct:.0f}%\n"
            f"  創新高比例 {self.new_high_pct:.0f}%\n"
            f"  廣度評分 {self.breadth_score:.0f}/100"
        )


def calculate_breadth() -> BreadthSnapshot:
    """
    利用 screener pool 資料計算市場廣度快照。
    真實版本應接入 TWSE 全市場資料；目前用 pool 代理。
    """
    snap = BreadthSnapshot(date=datetime.now().strftime("%Y-%m-%d"))
    try:
        from .report_screener import all_screener
        rows = all_screener(200)
        if not rows:
            return snap

        advances   = sum(1 for r in rows if r.change_pct > 0)
        declines   = sum(1 for r in rows if r.change_pct < 0)
        unchanged  = len(rows) - advances - declines
        new_highs  = sum(1 for r in rows if r.breakout_pct >= 0)
        limit_down = sum(1 for r in rows if r.change_pct <= -9.5)
        strong     = sum(1 for r in rows if r.change_pct > 1.0)

        total      = len(rows)
        strong_pct = strong / max(total, 1) * 100
        nh_pct     = new_highs / max(total, 1) * 100

        # 廣度評分（0~100）
        ad_score  = (advances / max(advances + declines, 1)) * 40
        str_score = min(strong_pct, 60) / 60 * 40
        nh_score  = min(nh_pct, 50) / 50 * 20
        score     = ad_score + str_score + nh_score

        snap.advances      = advances
        snap.declines      = declines
        snap.unchanged     = unchanged
        snap.new_high_pct  = round(nh_pct, 1)
        snap.limit_down    = limit_down
        snap.strong_pct    = round(strong_pct, 1)
        snap.breadth_score = round(score, 1)
        snap.warning       = strong_pct < 30.0

    except Exception as e:
        logger.warning(f"[market_breadth] calculate failed: {e}")

    return snap


async def push_breadth_warning(snap: BreadthSnapshot):
    """廣度惡化時推送警告給訂閱者"""
    if not snap.warning:
        return

    from ..models.database import AsyncSessionLocal, settings
    from ..models.models import Subscriber
    from sqlalchemy import select
    import httpx

    async with AsyncSessionLocal() as db:
        r    = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
        subs = r.scalars().all()

    if not subs:
        return

    text = (
        "⚠️ 市場廣度警告\n\n"
        f"強勢股佔比降至 {snap.strong_pct:.0f}%\n"
        f"廣度評分：{snap.breadth_score:.0f}/100\n\n"
        "建議：市場轉弱，考慮降低倉位\n"
        "操作：優先保護獲利部位"
    )
    qr = {"items": [
        {"type": "action", "action": {
            "type": "postback", "label": "💼 看庫存",
            "data": "act=portfolio_view", "displayText": "看庫存",
        }},
        {"type": "action", "action": {
            "type": "postback", "label": "📊 大盤行情",
            "data": "act=market_card", "displayText": "大盤行情",
        }},
    ]}

    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    async with httpx.AsyncClient(timeout=20) as c:
        for sub in subs:
            try:
                await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": sub.line_user_id, "messages": [{
                        "type": "text", "text": text, "quickReply": qr,
                    }]},
                    headers=headers,
                )
            except Exception:
                pass

    logger.info(f"[market_breadth] warning pushed to {len(subs)} subscribers")


async def run_breadth_check():
    """排程入口：計算廣度 + 必要時推送警告"""
    try:
        snap = calculate_breadth()
        logger.info(
            f"[market_breadth] advances={snap.advances} declines={snap.declines}"
            f" strong={snap.strong_pct:.0f}% score={snap.breadth_score:.0f}"
            f" warning={snap.warning}"
        )
        if snap.warning:
            await push_breadth_warning(snap)
    except Exception as e:
        logger.error(f"[market_breadth] run failed: {e}")
