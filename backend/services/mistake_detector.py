"""AI Mistake Detector — 每週分析交易習慣，偵測常見錯誤"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import httpx
from loguru import logger


@dataclass
class MistakeReport:
    uid:           str
    period:        str
    mistakes:      list[dict] = field(default_factory=list)
    good_habits:   list[str]  = field(default_factory=list)
    ai_suggestion: str        = ""
    score:         int        = 80  # 0~100 本週交易評分

    def to_line_text(self) -> str:
        lines = [
            f"🔍 本週交易分析",
            f"評分：{self.score}/100  {self.period}",
            "─" * 20,
        ]
        if self.mistakes:
            lines.append(f"\n發現 {len(self.mistakes)} 個改善點：")
            for m in self.mistakes:
                lines.append(f"⚠️ {m['title']}")
                lines.append(f"   {m['detail']}")
        if self.good_habits:
            for g in self.good_habits:
                lines.append(f"✅ {g}")
        if self.ai_suggestion:
            lines.append(f"\nAI建議：{self.ai_suggestion}")
        return "\n".join(lines)


async def analyze_user(uid: str) -> MistakeReport:
    """分析單一用戶的交易習慣"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import TradeLog
    from sqlalchemy import select

    week_ago = datetime.utcnow() - timedelta(days=7)
    mistakes: list[dict] = []
    good_habits: list[str] = []
    score = 80

    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(TradeLog)
                .where(TradeLog.user_id == uid)
                .where(TradeLog.timestamp >= week_ago)
                .order_by(TradeLog.timestamp)
            )
            trades = list(r.scalars().all())
    except Exception as e:
        logger.warning(f"[mistake_detector] trade query failed: {e}")
        trades = []

    # ── 偵測 1：頻繁交易（單週 > 5筆）────────────────────────────────────────
    buys = [t for t in trades if t.action == "buy"]
    if len(buys) >= 5:
        mistakes.append({
            "title":  f"你本週有 {len(buys)} 次買進操作",
            "detail": "頻繁交易會增加手續費成本，建議精選交易",
        })
        score -= 10

    # ── 偵測 2：追高買進（買進後立即虧損 > 3%）──────────────────────────────
    buy_high_count = 0
    for t in buys:
        try:
            from .twse_service import fetch_realtime_quote
            q     = await fetch_realtime_quote(t.stock_code)
            curr  = q.get("price", t.cost_price) if q else t.cost_price
            chg   = (curr - t.cost_price) / t.cost_price if t.cost_price else 0
            if chg < -0.03:
                buy_high_count += 1
        except Exception:
            pass

    if buy_high_count >= 2:
        pct_str = f"{buy_high_count/max(len(buys),1)*100:.0f}%"
        mistakes.append({
            "title":  f"你本週有 {buy_high_count} 次追高買進",
            "detail": f"平均買在波段高點 {pct_str} 位置，建議設進場條件",
        })
        score -= 15

    # ── 偵測 3：族群集中度（若可取得庫存）──────────────────────────────────
    try:
        from .portfolio_service import get_holdings
        holdings = await get_holdings(uid)
        if holdings:
            sector_map: dict[str, float] = {}
            total_val = sum(h.get("market_value", 0) or 0 for h in holdings)
            for h in holdings:
                sec  = h.get("sector", "其他")
                val  = h.get("market_value", 0) or 0
                sector_map[sec] = sector_map.get(sec, 0) + val
            if total_val > 0:
                max_sec = max(sector_map, key=sector_map.get)
                max_pct = sector_map[max_sec] / total_val * 100
                if max_pct >= 50:
                    mistakes.append({
                        "title":  f"族群集中度偏高：{max_sec} 佔 {max_pct:.0f}%",
                        "detail": "建議分散到 3 個以上產業，降低單一族群風險",
                    })
                    score -= 10
                else:
                    good_habits.append("族群分散做得很好")
    except Exception:
        pass

    # ── 偵測 4：停損執行率 ──────────────────────────────────────────────────
    sells = [t for t in trades if t.action == "sell"]
    loss_sells = 0
    for t in sells:
        try:
            if t.cost_price and t.price and t.price < t.cost_price * 0.90:
                loss_sells += 1
        except Exception:
            pass

    if len(sells) > 0:
        sl_rate = loss_sells / len(sells) * 100
        if sl_rate < 60 and len(sells) >= 2:
            mistakes.append({
                "title":  f"停損執行率只有 {sl_rate:.0f}%",
                "detail": "建議設好自動停損警報，避免大幅虧損",
            })
            score -= 5
        elif sl_rate >= 80:
            good_habits.append("停損紀律執行得很好")

    if not mistakes:
        good_habits.append("本週交易習慣良好，繼續保持")

    suggestion = _generate_suggestion(mistakes)
    period = _this_week_str()

    return MistakeReport(
        uid           = uid,
        period        = period,
        mistakes      = mistakes,
        good_habits   = good_habits,
        ai_suggestion = suggestion,
        score         = max(0, min(100, score)),
    )


def _generate_suggestion(mistakes: list[dict]) -> str:
    if not mistakes:
        return "繼續保持良好的交易紀律"
    titles = " + ".join(m["title"].split("：")[0] for m in mistakes[:2])
    return f"重點改善：{titles}，設定交易規則後嚴格執行"


def _this_week_str() -> str:
    today  = datetime.now()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return f"{monday.strftime('%m/%d')}~{sunday.strftime('%m/%d')}"


async def push_weekly_mistake_reports():
    """每週五推送交易分析給所有訂閱者"""
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
                report = await analyze_user(sub.line_user_id)
                text   = report.to_line_text()
                qr     = {"items": [
                    {"type": "action", "action": {
                        "type": "message", "label": "📓 交易日誌", "text": "/journal"}},
                    {"type": "action", "action": {
                        "type": "postback", "label": "💼 看庫存",
                        "data": "act=portfolio_view", "displayText": "看庫存"}},
                ]}
                await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": sub.line_user_id, "messages": [
                        {"type": "text", "text": text, "quickReply": qr}
                    ]},
                    headers=headers,
                )
            except Exception as e:
                logger.warning(f"[mistake_detector] push failed: {e}")

    logger.info(f"[mistake_detector] pushed to {len(subs)} subscribers")
