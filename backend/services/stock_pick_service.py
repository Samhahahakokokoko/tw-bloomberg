"""每週選股報告服務 — 法人籌碼 + AI 分析"""
import httpx
from datetime import datetime
from loguru import logger
from .twse_service import fetch_realtime_quote


def _parse_int(val) -> int:
    try:
        return int(str(val).replace(",", ""))
    except Exception as e:
        return 0


async def generate_weekly_picks(top_n: int = 5) -> dict:
    """
    選股邏輯：
    1. 抓全市場三大法人資料
    2. 篩選外資 + 投信雙買超
    3. AI 分析推薦
    """
    inst_map: dict[str, dict] = {}
    # Use DB price_history (populated by Agent B) for chip data — TWSE T86 API is broken (302)
    try:
        from datetime import date as _date, timedelta as _td
        from ..models.database import AsyncSessionLocal
        from ..models.models import PriceHistory
        from sqlalchemy import select, func
        cutoff = (_date.today() - _td(days=10)).isoformat()
        async with AsyncSessionLocal() as db:
            rows = await db.execute(
                select(
                    PriceHistory.stock_code,
                    func.sum(PriceHistory.foreign_net).label("f"),
                    func.sum(PriceHistory.investment_trust_net).label("t"),
                    func.sum(PriceHistory.dealer_net).label("d"),
                )
                .where(PriceHistory.date >= cutoff)
                .where(PriceHistory.foreign_net.isnot(None))
                .group_by(PriceHistory.stock_code)
            )
            for r in rows.fetchall():
                code = r.stock_code
                if not code or not code.isdigit():
                    continue
                f = int(r.f or 0)
                t = int(r.t or 0)
                d = int(r.d or 0)
                inst_map[code] = {
                    "name": "",
                    "foreign_net": f,
                    "trust_net": t,
                    "dealer_net": d,
                    "total_net": f + t + d,
                }
        logger.info(f"Weekly picks DB chip data: {len(inst_map)} stocks")
    except Exception as e:
        logger.error(f"Weekly picks DB chip error: {e}")

    # 篩選條件: 外資 > 500 張 且 法人合計 > 0
    candidates = [
        {"code": code, **info}
        for code, info in inst_map.items()
        if info["foreign_net"] > 500 and info["total_net"] > 0
    ]
    candidates.sort(key=lambda x: x["foreign_net"], reverse=True)
    top_candidates = candidates[:top_n * 3]

    # 補充報價
    picks = []
    for c in top_candidates:
        try:
            quote = await fetch_realtime_quote(c["code"])
            price = quote.get("price", 0)
            if price <= 0:
                continue
            picks.append({
                "code":        c["code"],
                "name":        c["name"] or quote.get("name", c["code"]),
                "price":       price,
                "change_pct":  quote.get("change_pct", 0),
                "foreign_net": c["foreign_net"],
                "trust_net":   c["trust_net"],
                "total_net":   c["total_net"],
            })
            if len(picks) >= top_n:
                break
        except Exception as e:
            continue

    # AI 分析
    ai_text = await _ai_pick_analysis(picks)

    return {
        "date":       datetime.now().strftime("%Y-%m-%d"),
        "picks":      picks,
        "ai_analysis": ai_text,
        "criteria":   "外資淨買超 > 500 張 且 三大法人合計買超",
    }


async def push_weekly_picks():
    """推播每週選股給訂閱者"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Subscriber
    from sqlalchemy import select
    from .morning_report import _push_to_users

    data = await generate_weekly_picks()
    picks = data["picks"]

    if not picks:
        logger.info("Weekly picks: no candidates found")
        return

    week = datetime.now().strftime("%Y/%m/%d")
    lines = [f"🎯 每週選股  {week}", "─" * 22]
    for p in picks:
        sign = "▲" if p["change_pct"] >= 0 else "▼"
        lines.append(
            f"\n{p['code']} {p['name']}\n"
            f"  現價 {p['price']}  {sign}{abs(p['change_pct']):.2f}%\n"
            f"  外資淨買 {p['foreign_net']:+,} 張"
        )

    if data.get("ai_analysis"):
        lines.append(f"\n🤖 AI 分析\n{data['ai_analysis']}")

    report = "\n".join(lines)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Subscriber).where(Subscriber.subscribed_weekly == True)
        )
        subs = result.scalars().all()

    if subs:
        await _push_to_users([s.line_user_id for s in subs], report)
        logger.info(f"Weekly picks pushed to {len(subs)} subscribers")


async def _ai_pick_analysis(picks: list[dict]) -> str:
    from ..models.database import settings
    if not picks or not settings.anthropic_api_key:
        return ""
    try:
        import anthropic
        text = "\n".join(
            f"{p['code']} {p['name']} 現價:{p['price']} 外資淨買:{p['foreign_net']}張"
            for p in picks
        )
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            messages=[{
                "role": "user",
                "content": (
                    "以下是本週台股外資偏好標的，請用繁體中文為每檔股票給出一句操作建議，"
                    "並說明選股邏輯（50字內每檔）：\n\n" + text
                ),
            }],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        if "credit balance is too low" in str(e):
            logger.warning("[StockPick] Anthropic API 額度不足")
        else:
            logger.error(f"AI pick analysis error: {e}")
        return ""
