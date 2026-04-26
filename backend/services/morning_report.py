"""每日早報服務 — 8:30 推送大盤、漲跌幅前三、三大法人、AI 簡評"""
import httpx
from datetime import datetime
from loguru import logger
from .twse_service import fetch_market_overview, fetch_stock_list


async def generate_morning_report() -> str:
    """組合早報文字"""
    today = datetime.now().strftime("%m/%d")
    lines = [f"📊 台股早報 {today}\n{'─'*22}"]

    # 大盤指數
    try:
        overview = await fetch_market_overview()
        if overview:
            sign = "▲" if overview["change"] >= 0 else "▼"
            lines.append(
                f"🏦 加權指數\n"
                f"  {overview['value']:,.2f}  {sign}{abs(overview['change']):.2f} ({overview['change_pct']:+.2f}%)"
            )
    except Exception as e:
        logger.error(f"Morning report TAIEX error: {e}")

    # 漲跌幅前三名
    try:
        stocks = await fetch_stock_list()
        with_change = [s for s in stocks if s.get("change_pct") is not None]
        top3_up = sorted(with_change, key=lambda x: x.get("change_pct", 0), reverse=True)[:3]
        top3_dn = sorted(with_change, key=lambda x: x.get("change_pct", 0))[:3]

        if top3_up:
            lines.append("\n📈 漲幅前三")
            for s in top3_up:
                lines.append(f"  {s['code']} {s['name']}  +{s.get('change_pct', 0):.2f}%  {s.get('price', '')}")
        if top3_dn:
            lines.append("\n📉 跌幅前三")
            for s in top3_dn:
                lines.append(f"  {s['code']} {s['name']}  {s.get('change_pct', 0):.2f}%  {s.get('price', '')}")
    except Exception as e:
        logger.error(f"Morning report movers error: {e}")

    # 三大法人合計
    try:
        inst = await _fetch_total_institutional()
        if inst:
            lines.append(
                f"\n🏛 三大法人合計\n"
                f"  外資 {inst['foreign']:+,}\n"
                f"  投信 {inst['trust']:+,}\n"
                f"  自營 {inst['dealer']:+,}"
            )
    except Exception as e:
        logger.error(f"Morning report institutional error: {e}")

    # AI 簡評
    try:
        body = "\n".join(lines)
        ai_comment = await _ai_summary(body)
        lines.append(f"\n🤖 AI 簡評\n{ai_comment}")
    except Exception as e:
        logger.error(f"Morning report AI error: {e}")

    return "\n".join(lines)


async def push_morning_report():
    """推送早報給所有訂閱者"""
    from ..models.database import AsyncSessionLocal, settings
    from ..models.models import Subscriber
    from sqlalchemy import select

    report = await generate_morning_report()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Subscriber).where(Subscriber.subscribed_morning == True)
        )
        subscribers = result.scalars().all()

    if not subscribers:
        logger.info("Morning report: no subscribers")
        return

    await _push_to_users([s.line_user_id for s in subscribers], report)
    logger.info(f"Morning report pushed to {len(subscribers)} subscribers")


async def _fetch_total_institutional() -> dict:
    """全市場三大法人合計（BFI82U）"""
    url = "https://www.twse.com.tw/fund/BFI82U?response=json&type=day"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            data = resp.json()
            rows = data.get("data", [])
            # 找「合計」列
            total = next((r for r in rows if "合計" in str(r)), None)
            if not total and rows:
                total = rows[-1]
            if total and len(total) >= 4:
                def _n(v): return int(str(v).replace(",", "").replace("+", "") or 0)
                return {
                    "foreign": _n(total[2]) if len(total) > 2 else 0,
                    "trust": _n(total[3]) if len(total) > 3 else 0,
                    "dealer": _n(total[4]) if len(total) > 4 else 0,
                }
    except Exception as e:
        logger.error(f"Total institutional error: {e}")
    return {}


async def _ai_summary(report_body: str) -> str:
    from ..models.database import settings
    if not settings.anthropic_api_key:
        return "（未設定 API Key）"
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    "根據以下今日台股數據，用2~3句繁體中文簡評市場氣氛與操作建議：\n\n"
                    + report_body[:600]
                )
            }],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.error(f"AI summary error: {e}")
        return "AI 簡評暫時無法使用"


async def _push_to_users(user_ids: list[str], message: str):
    from ..models.database import settings
    if not settings.line_channel_access_token or not user_ids:
        return
    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    # LINE multicast (一次最多 500 人)
    for i in range(0, len(user_ids), 500):
        batch = user_ids[i:i+500]
        payload = {
            "to": batch,
            "messages": [{"type": "text", "text": message}]
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.line.me/v2/bot/message/multicast",
                json=payload, headers=headers
            )
            logger.info(f"Multicast to {len(batch)} users: {r.status_code}")
