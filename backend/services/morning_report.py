"""每日早報服務 v2 — 市場狀態 / 大盤 / 今日重點 / 操作建議 / 外資動向"""
import httpx
from datetime import datetime
from loguru import logger
from .twse_service import fetch_market_overview, fetch_stock_list


async def generate_morning_report() -> str:
    """組合早報文字 v3 — 含漲跌榜、量能標記、細化市場狀態"""
    today = datetime.now().strftime("%m/%d")
    lines = [f"📊 台股早報 {today}\n{'─'*22}"]

    market_state = "盤整"
    change_pct_val = 0.0

    # ── 大盤指數 + 市場狀態（細化 5 段）────────────────────────────────
    try:
        overview = await fetch_market_overview()
        if overview:
            sign = "▲" if overview["change"] >= 0 else "▼"
            change_pct_val = float(overview.get("change_pct") or 0)

            if change_pct_val > 1.5:
                market_state = "強多頭 🚀"
            elif change_pct_val > 0.5:
                market_state = "多頭 📈"
            elif change_pct_val < -1.5:
                market_state = "強空頭 🔻"
            elif change_pct_val < -0.5:
                market_state = "空頭 📉"
            else:
                market_state = "盤整 ↔️"

            lines.append(
                f"【市場狀態】{market_state}\n"
                f"大盤：{overview['value']:,.2f}  "
                f"{sign}{abs(overview['change']):.2f} ({change_pct_val:+.2f}%)"
            )
    except Exception as e:
        logger.error(f"Morning report TAIEX error: {e}")

    # ── 今日漲幅前 3 + 跌幅前 3 ──────────────────────────────────────
    try:
        stocks = await fetch_stock_list()
        with_change = [s for s in stocks if s.get("change_pct") is not None]

        gainers = sorted(with_change, key=lambda x: x.get("change_pct", 0), reverse=True)[:3]
        losers  = sorted(with_change, key=lambda x: x.get("change_pct", 0))[:3]

        def _vol_tag(s) -> str:
            vol   = s.get("volume", 0) or 0
            avg   = s.get("avg_volume", 0) or 0
            if avg > 0 and vol > avg * 3:
                return " 🔥爆量"
            if avg > 0 and vol > avg * 1.5:
                return " ⚡放量"
            return ""

        if gainers:
            lines.append("\n🟢 今日強勢（漲幅前 3）")
            for s in gainers:
                pct = s.get("change_pct", 0)
                lines.append(
                    f"  {s['code']} {s.get('name','')}  +{pct:.2f}%  "
                    f"{s.get('price','')}{_vol_tag(s)}"
                )

        if losers and losers[0].get("change_pct", 0) < 0:
            lines.append("\n🔴 今日弱勢（跌幅前 3）")
            for s in losers:
                pct = s.get("change_pct", 0)
                if pct >= 0:
                    break
                lines.append(
                    f"  {s['code']} {s.get('name','')}  {pct:.2f}%  "
                    f"{s.get('price','')}"
                )

        # 漲跌家數比
        up   = sum(1 for s in with_change if s.get("change_pct", 0) > 0)
        down = sum(1 for s in with_change if s.get("change_pct", 0) < 0)
        if up + down > 0:
            lines.append(f"\n📊 漲跌家數：上漲 {up} / 下跌 {down}")

    except Exception as e:
        logger.error(f"Morning report movers error: {e}")

    # ── 操作建議（對應細化市場狀態）──────────────────────────────────
    op_map = {
        "強多頭 🚀": "市場強勁，可積極布局動能股",
        "多頭 📈":   "多頭氛圍，選強勢股順勢操作",
        "強空頭 🔻": "市場重挫，建議空手觀望護本",
        "空頭 📉":   "偏空格局，輕倉避開弱勢個股",
        "盤整 ↔️":   "震盪整理，選擇性進場輕倉為主",
    }
    lines.append(f"\n【操作建議】{op_map.get(market_state, '謹慎操作')}")

    # ── 外資動向 ──────────────────────────────────────────────────────────
    try:
        inst = await _fetch_total_institutional()
        if inst:
            f_val = inst.get("foreign", 0)
            t_val = inst.get("trust", 0)
            d_val = inst.get("dealer", 0)
            direction = "買超" if f_val > 0 else "賣超"
            lines.append(
                f"\n【外資動向】{direction} {abs(f_val):,} 張\n"
                f"  外資：{f_val:+,}  投信：{t_val:+,}  自營：{d_val:+,}"
            )
    except Exception as e:
        logger.error(f"Morning report institutional error: {e}")

    # ── AI 簡評 ───────────────────────────────────────────────────────────
    try:
        body = "\n".join(lines)
        ai_comment = await _ai_summary(body)
        lines.append(f"\n🤖 {ai_comment}")
    except Exception as e:
        logger.error(f"Morning report AI error: {e}")

    return "\n".join(lines)


async def push_morning_report():
    """推送早報給所有訂閱者（含去重）"""
    from ..models.database import AsyncSessionLocal, settings
    from ..models.models import Subscriber
    from .push_dedup import check_and_record
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

    eligible = []
    for sub in subscribers:
        if await check_and_record(sub.line_user_id, "morning", report):
            eligible.append(sub.line_user_id)

    skipped = len(subscribers) - len(eligible)
    if skipped:
        logger.info(f"Morning report: {skipped} already pushed today, skipping")

    if eligible:
        await _push_to_users(eligible, report)
        logger.info(f"Morning report pushed to {len(eligible)} subscribers")


async def _fetch_total_institutional() -> dict:
    """全市場三大法人合計（BFI82U）"""
    url = "https://www.twse.com.tw/fund/BFI82U?response=json&type=day"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            data = resp.json()
            rows = data.get("data", [])
            total = next((r for r in rows if "合計" in str(r)), None)
            if not total and rows:
                total = rows[-1]
            if total and len(total) >= 4:
                def _n(v): return int(str(v).replace(",", "").replace("+", "") or 0)
                return {
                    "foreign": _n(total[2]) if len(total) > 2 else 0,
                    "trust":   _n(total[3]) if len(total) > 3 else 0,
                    "dealer":  _n(total[4]) if len(total) > 4 else 0,
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
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": (
                    "根據以下今日台股數據，用2句繁體中文簡評市場氣氛與操作方向：\n\n"
                    + report_body[:500]
                ),
            }],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        if "credit balance is too low" in str(e):
            logger.warning("[MorningReport] Anthropic API 額度不足")
            return "AI 簡評暫時無法使用（額度不足）"
        logger.error(f"AI summary error: {e}")
        return "AI 簡評暫時無法使用"


async def _push_to_users(user_ids: list[str], message: str):
    from ..models.database import settings
    from .line_push import multicast_line_messages
    if not settings.line_channel_access_token or not user_ids:
        return
    for i in range(0, len(user_ids), 500):
        batch = user_ids[i:i+500]
        async with httpx.AsyncClient(timeout=15) as client:
            ok = await multicast_line_messages(
                batch,
                [{"type": "text", "text": message}],
                client=client,
                context="morning_report",
            )
            if ok:
                logger.info(f"Multicast to {len(batch)} users")
