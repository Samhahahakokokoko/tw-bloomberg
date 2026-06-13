"""AI 操盤日記服務 — 每日 21:00 推播或手動查詢"""
import os
from datetime import datetime
from loguru import logger
import asyncio


async def generate_diary(uid: str = "") -> str:
    """生成今日操盤日記，包含大盤、自選股、法人動向、明日建議。"""
    today = datetime.now().strftime("%Y/%m/%d")
    lines = [f"📔 {today} 操盤日記", "─" * 22, ""]

    # 1. 大盤數據
    market_idx = "N/A"
    market_chg_pct = 0.0
    sentiment_score = 50
    sentiment_label = "中性"
    try:
        from backend.services.twse_service import fetch_market_overview
        ov = await fetch_market_overview()
        market_idx = f"{float(ov.get('index', 0)):,.1f}" if ov.get("index") else "N/A"
        market_chg_pct = float(ov.get("change_pct", 0) or 0)
        sign = "+" if market_chg_pct >= 0 else ""
        lines.append(f"📈 大盤：{market_idx}  {sign}{market_chg_pct:.2f}%")
    except Exception as e:
        logger.debug(f"[diary] market overview error: {e}")
        lines.append("📈 大盤：資料取得失敗")

    # 2. 大盤情緒評分
    try:
        from backend.services.market_sentiment import get_sentiment_score
        sdata = await get_sentiment_score()
        sentiment_score = sdata.get("score", 50)
        sentiment_label = sdata.get("label", "中性")
        lines.append(f"🌡️ 市場情緒：{sentiment_score}/100（{sentiment_label}）")
    except Exception as e:
        logger.debug(f"[diary] sentiment error: {e}")

    lines.append("")

    # 3. 自選股表現（若有 uid）或全市場前3漲/跌
    try:
        if uid:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import Watchlist
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:
                r = await db.execute(select(Watchlist).where(Watchlist.user_id == uid).limit(20))
                watches = r.scalars().all()
            watch_codes = [w.stock_code for w in watches]
        else:
            watch_codes = []

        if watch_codes:
            import asyncio
            from backend.services.twse_service import fetch_realtime_quote
            quotes = await asyncio.gather(
                *[fetch_realtime_quote(c) for c in watch_codes],
                return_exceptions=True,
            )
            perf = []
            for code, q in zip(watch_codes, quotes):
                if isinstance(q, Exception) or not q:
                    continue
                pct = float(q.get("change_pct", 0) or 0)
                name = q.get("name", code)
                perf.append((code, name, pct))
            perf.sort(key=lambda x: x[2], reverse=True)
            if perf:
                lines.append("📊 自選股表現（今日）：")
                for code, name, pct in perf[:3]:
                    sign = "+" if pct >= 0 else ""
                    lines.append(f"  {code} {name}  {sign}{pct:.2f}%")
                lines.append("")
        else:
            # Fallback: top movers from rt_cache
            from backend.services.report_screener import _rt_cache
            prices = _rt_cache.get("prices", {})
            if prices:
                movers = sorted(
                    [(c, v) for c, v in prices.items()],
                    key=lambda x: float(x[1].get("change_pct", 0) or 0),
                    reverse=True,
                )
                lines.append("🏆 今日強勢股：")
                for code, v in movers[:3]:
                    pct = float(v.get("change_pct", 0) or 0)
                    name = v.get("name", code)
                    lines.append(f"  {code} {name}  +{pct:.2f}%")
                lines.append("")
                lines.append("💔 今日弱勢股：")
                for code, v in reversed(movers[-3:]):
                    pct = float(v.get("change_pct", 0) or 0)
                    name = v.get("name", code)
                    lines.append(f"  {code} {name}  {pct:.2f}%")
                lines.append("")
    except Exception as e:
        logger.debug(f"[diary] watchlist/movers error: {e}")

    # 4. 法人動向（外資合計）
    try:
        import httpx
        import json as _json
        url = "https://www.twse.com.tw/fund/BFI82U?response=json&type=day"
        async with httpx.AsyncClient(timeout=10) as c:
            raw = await c.get(url)
            data = _json.loads(raw.content)
        rows = data.get("data", [])

        def _n(v):
            try:
                return int(str(v).replace(",", "").replace("+", "") or 0)
            except Exception as e:
                return 0

        foreign_net = None
        for row in rows:
            if len(row) >= 4 and "外資" in str(row[0]) and "陸資" in str(row[0]):
                foreign_net = _n(row[3])
                break
        if foreign_net is not None:
            sign = "+" if foreign_net >= 0 else ""
            trend = "買超" if foreign_net > 0 else "賣超"
            lines.append(f"🏦 外資今日：{sign}{foreign_net / 1e8:.1f}億（{trend}）")
            lines.append("")
    except Exception as e:
        logger.debug(f"[diary] institutional error: {e}")

    # 5. 明日操作建議
    lines.append("📌 明日重點：")
    if sentiment_score >= 70:
        lines.append("  情緒偏多，可維持七~九成倉")
        lines.append("  注意強勢股是否有追高風險")
    elif sentiment_score >= 50:
        lines.append("  情緒中性偏多，維持五~七成倉")
        lines.append("  選擇外資買超個股佈局")
    elif sentiment_score >= 30:
        lines.append("  情緒偏空，控制倉位於五成以下")
        lines.append("  避免追漲，等待回測支撐再買")
    else:
        lines.append("  市場極度恐慌，建議三成倉或空手")
        lines.append("  等待恐慌消化後再考慮進場")

    lines.append("")

    # 6. AI 點評（若有 Anthropic API key）
    try:
        import anthropic as _anthropic
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if api_key:
            client = _anthropic.AsyncAnthropic(api_key=api_key)
            prompt = (
                f"今日台股大盤漲跌{market_chg_pct:+.2f}%，"
                f"市場情緒{sentiment_score}/100（{sentiment_label}）。"
                "請用繁體中文給出2-3行今日台股操盤點評，簡潔直接。"
            )
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            )
            ai_text = resp.content[0].text.strip()
            lines.append("🤖 AI 點評：")
            lines.append(f"  {ai_text}")
    except Exception as e:
        logger.debug(f"[diary] AI comment error: {e}")

    return "\n".join(lines)
