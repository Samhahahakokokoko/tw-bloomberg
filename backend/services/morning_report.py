"""每日早報服務 v2 — 市場狀態 / 大盤 / 今日重點 / 操作建議 / 外資動向"""
import httpx
from datetime import datetime
from loguru import logger
from .twse_service import fetch_market_overview, fetch_stock_list
from ..utils.credit_guard import is_exhausted as _credit_exhausted, mark_exhausted as _mark_credit_exhausted


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

    # ── 情緒指數（豐富市場狀態描述）─────────────────────────────────
    try:
        from .market_sentiment import get_sentiment_score
        sent = await get_sentiment_score()
        s_score = sent["score"]
        s_desc  = sent.get("state_desc", sent["label"])
        s_reasons = sent.get("reasons", [])
        lines.append(
            f"\n【情緒指數】{sent['icon']} {s_score}/100  {s_desc}"
        )
        if s_reasons:
            for r in s_reasons[:2]:
                lines.append(f"  · {r}")
        vs = sent.get("vs_yesterday")
        if vs:
            lines.append(f"  {vs}")
    except Exception as e:
        logger.debug(f"Morning report sentiment error: {e}")
        # Fallback to simple map
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
        ai_comment = await _ai_summary(body, market_state, change_pct_val)
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


def _rule_based_summary(market_state: str, change_pct: float) -> str:
    """Anthropic 不可用時的規則式市場簡評"""
    if change_pct > 2.0:
        sentiment = "今日台股強勢上攻，市場多頭氣氛濃厚，動能股表現亮眼。"
    elif change_pct > 0.5:
        sentiment = "今日台股小幅走揚，多方佔優，短線可留意強勢個股。"
    elif change_pct < -2.0:
        sentiment = "今日台股重挫，市場賣壓沉重，建議保守觀望為宜。"
    elif change_pct < -0.5:
        sentiment = "今日台股偏弱，空方佔據主導，宜輕倉控制風險。"
    else:
        sentiment = "今日台股震盪整理，方向不明，短線宜觀望等待訊號。"

    if change_pct > 1.0:
        action = "可積極布局具基本面支撐的強勢股，但注意追高風險。"
    elif change_pct > 0:
        action = "選擇強於大盤的個股順勢操作，設好停損點。"
    elif change_pct > -1.0:
        action = "避免追空，等待支撐確立再考慮進場機會。"
    else:
        action = "建議空手觀望，待市場止穩再尋找反彈機會。"

    return f"{sentiment}{action}"


async def _ai_summary(report_body: str, market_state: str = "", change_pct: float = 0.0) -> str:
    from ..models.database import settings
    if not settings.anthropic_api_key or _credit_exhausted():
        return _rule_based_summary(market_state, change_pct)
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
            _mark_credit_exhausted()
            logger.warning("[MorningReport] Anthropic credit 耗盡，改用規則式摘要")
            return _rule_based_summary(market_state, change_pct)
        logger.error(f"AI summary error: {e}")
        return _rule_based_summary(market_state, change_pct)


async def _push_to_users(user_ids: list[str], message: str):
    from ..models.database import settings
    from .line_push import multicast_line_messages
    if not settings.line_channel_access_token or not user_ids:
        return
    qr = {"items": [
        {"type": "action", "action": {"type": "message", "label": "🌡️ 情緒指數", "text": "/sentiment"}},
        {"type": "action", "action": {"type": "message", "label": "🎯 今日選股", "text": "/r"}},
        {"type": "action", "action": {"type": "message", "label": "💼 庫存",     "text": "/p"}},
        {"type": "action", "action": {"type": "message", "label": "👁️ 自選股",   "text": "/watchlist"}},
        {"type": "action", "action": {"type": "message", "label": "📋 決策報告", "text": "/daily"}},
    ]}
    for i in range(0, len(user_ids), 500):
        batch = user_ids[i:i+500]
        async with httpx.AsyncClient(timeout=15) as client:
            ok = await multicast_line_messages(
                batch,
                [{"type": "text", "text": message, "quickReply": qr}],
                client=client,
                context="morning_report",
            )
            if ok:
                logger.info(f"Multicast to {len(batch)} users")
