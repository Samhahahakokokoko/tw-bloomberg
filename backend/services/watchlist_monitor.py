"""Watchlist Monitor — 自選股每日掃描與日報推送"""
from __future__ import annotations

import httpx
from datetime import datetime
from loguru import logger


async def _fetch_rsi(code: str) -> float | None:
    """取得個股 RSI(14)，失敗回傳 None"""
    try:
        from .twse_service import fetch_kline
        from ..services.health_service import _calc_rsi
        klines = await fetch_kline(code)
        if not klines or len(klines) < 15:
            return None
        closes = [float(k.get("close", 0) or 0) for k in klines if k.get("close")]
        if len(closes) < 15:
            return None
        return round(_calc_rsi(closes), 1)
    except Exception as e:
        logger.debug(f"[watchlist_monitor] RSI fetch failed for {code}: {e}")
        return None


async def scan_user_watchlist(uid: str) -> list[dict]:
    """掃描單一用戶的自選股，回傳分析結果列表"""
    import asyncio as _asyncio
    from ..models.database import AsyncSessionLocal
    from ..models.models import Watchlist
    from sqlalchemy import select
    from .twse_service import fetch_realtime_quote

    async with AsyncSessionLocal() as db:
        r     = await db.execute(select(Watchlist).where(Watchlist.user_id == uid))
        items = r.scalars().all()

    if not items:
        return []

    # Fetch quotes and RSI for all stocks in parallel
    async def _fetch_quote_safe(code):
        try:
            return code, await fetch_realtime_quote(code)
        except Exception:
            return code, {}

    quote_results, rsi_results = await _asyncio.gather(
        _asyncio.gather(*[_fetch_quote_safe(item.stock_code) for item in items]),
        _asyncio.gather(*[_fetch_rsi(item.stock_code) for item in items]),
    )
    quotes = {c: q for c, q in quote_results}
    rsi_map = {item.stock_code: rsi for item, rsi in zip(items, rsi_results)}

    results = []
    for item in items:
        code  = item.stock_code
        q     = quotes.get(code) or {}
        price = q.get("price", 0) or 0
        chg   = q.get("change_pct", 0) or 0
        name  = item.stock_name or q.get("name", code) or code
        vol   = q.get("volume", 0) or 0
        rsi   = rsi_map.get(code)
        if isinstance(rsi, Exception):
            rsi = None

        signal = _evaluate_signal(price, chg, vol, item.target_price, item.stop_loss)

        results.append({
            "code":          code,
            "name":          name,
            "price":         price,
            "change_pct":    chg,
            "rsi":           rsi,
            "signal":        signal["label"],
            "signal_icon":   signal["icon"],
            "detail":        signal["detail"],
            "sl_triggered":  bool(item.stop_loss and price > 0 and price <= item.stop_loss),
            "tp_triggered":  bool(item.target_price and price > 0 and price >= item.target_price),
        })

    return results


def _evaluate_signal(price: float, chg: float, vol: float,
                     target: float | None, stop: float | None) -> dict:
    """根據技術指標判斷訊號"""
    if stop and price > 0 and price <= stop:
        return {"label": "停損觸發", "icon": "🛑", "detail": f"跌破停損 {stop:.1f}"}
    if target and price > 0 and price >= target:
        return {"label": "目標達成", "icon": "🎯", "detail": f"達到目標 {target:.1f}"}
    if chg >= 3.0:
        return {"label": "強勢上攻", "icon": "🔥", "detail": f"漲幅 +{chg:.1f}%"}
    if chg <= -3.0:
        return {"label": "下跌注意", "icon": "⚠️", "detail": f"跌幅 {chg:.1f}%"}
    if chg >= 1.0:
        return {"label": "趨勢持續", "icon": "✅", "detail": f"+{chg:.1f}%"}
    if chg <= -1.0:
        return {"label": "量縮注意", "icon": "⚠️", "detail": f"{chg:.1f}%"}
    return {"label": "盤整觀察", "icon": "👁️", "detail": f"{chg:+.1f}%"}


def format_watchlist_report(uid: str, results: list[dict]) -> str:
    """格式化自選股日報文字"""
    today = datetime.now().strftime("%m/%d")
    if not results:
        return f"👁️ 自選股日報 {today}\n\n尚未加入自選股\n輸入 /watch 代碼 加入"

    lines = [f"👁️ 自選股日報 {today}（{len(results)} 檔）", "─" * 18]
    for r in results:
        price = r.get("price", 0)
        chg   = r.get("change_pct", 0)
        rsi   = r.get("rsi")
        sign  = "+" if chg >= 0 else ""
        price_str = f"{price:,.0f}元 ({sign}{chg:.1f}%)" if price else "--"

        # RSI 標籤
        if rsi is not None:
            if rsi <= 30:
                rsi_str = f"  RSI:{rsi:.0f}（超賣）"
            elif rsi >= 70:
                rsi_str = f"  RSI:{rsi:.0f}（超買）"
            else:
                rsi_str = f"  RSI:{rsi:.0f}"
        else:
            rsi_str = "  RSI:--"

        status = ""
        if r["sl_triggered"]:
            status = "  🛑停損！"
        elif r["tp_triggered"]:
            status = "  🎯目標達！"

        lines.append(f"{r['signal_icon']} {r['code']} {r['name']}  {price_str}{rsi_str}{status}")
        lines.append(f"   └ {r['detail']}")
    return "\n".join(lines)


async def push_daily_watchlist_reports():
    """收盤後推送每個有自選股用戶的日報"""
    from ..models.database import AsyncSessionLocal, settings
    from ..models.models import Watchlist
    from sqlalchemy import select, distinct

    async with AsyncSessionLocal() as db:
        r    = await db.execute(select(distinct(Watchlist.user_id)))
        uids = [row[0] for row in r.fetchall() if row[0]]

    if not uids:
        logger.info("[watchlist_monitor] no watchlist users")
        return

    async with httpx.AsyncClient(timeout=30) as c:
        for uid in uids:
            try:
                results = await scan_user_watchlist(uid)
                if not results:
                    continue
                text = format_watchlist_report(uid, results)
                qr   = _build_watchlist_qr(results)
                from .line_push import push_line_messages
                await push_line_messages(
                    uid,
                    [{"type": "text", "text": text, "quickReply": qr}],
                    client=c,
                    context="watchlist_monitor",
                )
            except Exception as e:
                logger.warning(f"[watchlist_monitor] push failed uid={uid[:8]}: {e}")

    logger.info(f"[watchlist_monitor] pushed to {len(uids)} users")


def _build_watchlist_qr(results: list[dict]) -> dict:
    items = []
    # 加入個股分析按鈕（最多 3 檔，留空間給工具按鈕）
    for r in results[:3]:
        items.append({"type": "action", "action": {
            "type": "postback",
            "label": f"🔍{r['code']}",
            "data":  f"act=recommend_detail&code={r['code']}",
            "displayText": f"分析 {r['code']}",
        }})
    items.append({"type": "action", "action": {
        "type": "message", "label": "🌡️ 情緒指數", "text": "/sentiment",
    }})
    items.append({"type": "action", "action": {
        "type": "message", "label": "🔔 警報清單", "text": "/alerts",
    }})
    items.append({"type": "action", "action": {
        "type": "postback", "label": "💼 看庫存",
        "data": "act=portfolio_view", "displayText": "看庫存",
    }})
    return {"items": items[:13]}
