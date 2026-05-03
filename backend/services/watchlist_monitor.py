"""Watchlist Monitor — 自選股每日掃描與日報推送"""
from __future__ import annotations

import httpx
from datetime import datetime
from loguru import logger


async def scan_user_watchlist(uid: str) -> list[dict]:
    """掃描單一用戶的自選股，回傳分析結果列表"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Watchlist
    from sqlalchemy import select
    from .twse_service import fetch_realtime_quote

    results = []
    async with AsyncSessionLocal() as db:
        r     = await db.execute(select(Watchlist).where(Watchlist.user_id == uid))
        items = r.scalars().all()

    for item in items:
        code = item.stock_code
        try:
            q     = await fetch_realtime_quote(code)
            price = q.get("price", 0) if q else 0
            chg   = q.get("change_pct", 0) if q else 0
            name  = item.stock_name or (q.get("name", code) if q else code)
            vol   = q.get("volume", 0) if q else 0
        except Exception:
            price, chg, name, vol = 0, 0, item.stock_name or code, 0

        # 判斷訊號
        signal = _evaluate_signal(price, chg, vol, item.target_price, item.stop_loss)

        results.append({
            "code":          code,
            "name":          name,
            "price":         price,
            "change_pct":    chg,
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

    lines = [f"👁️ 自選股日報 {today}", "─" * 18]
    for r in results:
        line = f"{r['signal_icon']} {r['code']} {r['name']}：{r['signal']}"
        if r["sl_triggered"]:
            line += "  🛑停損"
        elif r["tp_triggered"]:
            line += "  🎯目標"
        lines.append(line)
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

    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    async with httpx.AsyncClient(timeout=30) as c:
        for uid in uids:
            try:
                results = await scan_user_watchlist(uid)
                if not results:
                    continue
                text = format_watchlist_report(uid, results)
                qr   = _build_watchlist_qr(results)
                await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": uid, "messages": [{
                        "type": "text", "text": text, "quickReply": qr
                    }]},
                    headers=headers,
                )
            except Exception as e:
                logger.warning(f"[watchlist_monitor] push failed uid={uid[:8]}: {e}")

    logger.info(f"[watchlist_monitor] pushed to {len(uids)} users")


def _build_watchlist_qr(results: list[dict]) -> dict:
    items = []
    # 加入個股分析按鈕（最多 4 檔）
    for r in results[:4]:
        items.append({"type": "action", "action": {
            "type": "postback",
            "label": f"🔍{r['code']}",
            "data":  f"act=recommend_detail&code={r['code']}",
            "displayText": f"分析 {r['code']}",
        }})
    items.append({"type": "action", "action": {
        "type": "message", "label": "📋 完整清單", "text": "/watchlist",
    }})
    items.append({"type": "action", "action": {
        "type": "postback", "label": "💼 看庫存",
        "data": "act=portfolio_view", "displayText": "看庫存",
    }})
    return {"items": items[:13]}
