"""Weekplan Service — 週策略報告（手動查詢 + 每週日20:00自動推播）"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 3600  # 1 hr (refresh Sunday)


async def get_weekplan(uid: str = "") -> dict:
    key = "weekplan"
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_weekplan(uid)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_weekplan(uid: str = "") -> dict:
    import asyncio
    from . import events_service, rotate_service, scorecard_service

    events_task   = events_service.get_events()
    rotate_task   = rotate_service.get_rotation()
    score_task    = scorecard_service.get_scorecard()
    watchlist_task= _get_watchlist(uid)

    events, rotation, scorecard, watchlist = await asyncio.gather(
        events_task, rotate_task, score_task, watchlist_task,
        return_exceptions=True
    )
    events    = events    if isinstance(events,    dict) else {}
    rotation  = rotation  if isinstance(rotation,  dict) else {}
    scorecard = scorecard if isinstance(scorecard, dict) else {}
    watchlist = watchlist if isinstance(watchlist, list) else []

    plan = _build_plan(events, rotation, scorecard, watchlist)
    return {
        "plan":       plan,
        "events":     events,
        "rotation":   rotation,
        "scorecard":  scorecard,
        "watchlist":  watchlist,
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


async def _get_watchlist(uid: str) -> list:
    try:
        from .stock_favorites import get_favorites
        favs = await get_favorites(uid)
        return favs[:5] if favs else []
    except Exception:
        return ["2330", "2454", "2317"]


def _build_plan(events: dict, rotation: dict, scorecard: dict, watchlist: list) -> dict:
    import datetime
    next_monday = datetime.date.today()
    while next_monday.weekday() != 0:
        next_monday += datetime.timedelta(days=1)
    week_str = f"{next_monday.month}/{next_monday.day} 週"

    # Key events next week
    all_ev  = events.get("events", [])
    next_wk = datetime.date.today() + datetime.timedelta(days=7)
    nw_events = [e for e in all_ev if datetime.date.today() <= datetime.date.fromisoformat(e["date"]) <= next_wk]
    high_ev   = [e for e in nw_events if e.get("impact") in ("高", "中高")]

    # Leading sectors from rotation
    rot_data  = rotation.get("rotation", {})
    leader    = rot_data.get("leader", "─")
    inflow    = rot_data.get("inflow", [])

    # Scorecard
    total = scorecard.get("total", 0)
    bias  = scorecard.get("bias", "中性")

    # Strategy direction
    if total >= 3:
        direction = "偏多布局"
        pos_advice= "建議維持七成倉位，重點布局領漲族群"
    elif total >= 1:
        direction = "中性偏多"
        pos_advice= "建議五成倉位，選擇強勢個股"
    elif total >= -1:
        direction = "觀望"
        pos_advice= "建議三成倉位，輕倉等待方向確立"
    elif total >= -3:
        direction = "中性偏空"
        pos_advice= "建議降至兩成倉位，以防守為主"
    else:
        direction = "偏空防守"
        pos_advice= "建議空倉或極低部位，等待市場穩定"

    # Focus groups
    focus = inflow[:3] if inflow else [leader] if leader != "─" else ["觀望"]

    return {
        "week":       week_str,
        "direction":  direction,
        "pos_advice": pos_advice,
        "bias":       bias,
        "score":      total,
        "focus":      focus,
        "leader":     leader,
        "key_events": high_ev[:3],
        "watchlist":  watchlist,
    }


def format_weekplan_report(data: dict) -> str:
    if not data or data.get("error"):
        return f"❌ {data.get('error', '無法生成週策略')}"

    plan = data["plan"]; ts = data["updated_at"]
    wl   = data.get("watchlist", [])

    DIR_ICON = {
        "偏多布局": "🔥", "中性偏多": "📈", "觀望": "⬜",
        "中性偏空": "📉", "偏空防守": "🔴",
    }
    icon    = DIR_ICON.get(plan.get("direction", "觀望"), "📊")
    score   = plan.get("score", 0)
    focus   = plan.get("focus", [])
    key_ev  = plan.get("key_events", [])
    wl_disp = plan.get("watchlist", wl)

    lines = [
        f"📋 下週策略報告  {plan.get('week', '')}",
        "─" * 36, "",
        f"📊 籌碼評分：{score:+.1f} / 10",
        f"多空方向：{icon} {plan.get('direction', '─')}（{plan.get('bias', '─')}）",
        "",
        "📌 倉位建議",
        f"  {plan.get('pos_advice', '─')}",
        "",
        "🎯 重點關注族群",
    ]
    for f in focus:
        lines.append(f"  ▶ {f}")

    if key_ev:
        lines += ["", "⚡ 下週重要事件"]
        for e in key_ev:
            impact_icon = {"高": "🔴", "中高": "🟠"}.get(e.get("impact", ""), "🟡")
            lines.append(f"  {impact_icon} {e['date']} {e['title']}")
            if e.get("tw_effect"):
                lines.append(f"     💡 {e['tw_effect'][:55]}")
    else:
        lines += ["", "⚡ 下週無重大財經事件，市場相對平穩"]

    if wl_disp:
        lines += ["", "👁️ 自選股技術面概況"]
        for code in wl_disp[:4]:
            lines.append(f"  📌 {code} — 請以 /exit {code} 查停利策略")

    lines += [
        "",
        "─" * 28,
        "🤖 AI 策略建議",
        f"下週方向【{plan.get('direction', '─')}】，資金流入族群為{'、'.join(focus[:2]) if focus else '─'}。",
        f"{'重點事件需提防波動，決議前降低槓桿。' if key_ev else '事件面平靜，以技術面為主要操作依據。'}",
        "",
        f"更新：{ts}",
        "⚠️ 週策略僅供參考，實際操作請結合個人風控",
    ]
    return "\n".join(lines)


async def push_weekplan_to_all() -> None:
    """Called by scheduler every Sunday 20:00."""
    try:
        from .line_push import push_to_all_users
        from .stock_favorites import get_all_user_ids
        uids = await get_all_user_ids()
        if not uids:
            logger.info("[weekplan] no users to push")
            return
        data   = await get_weekplan()
        report = format_weekplan_report(data)
        msg    = f"📋 下週策略報告自動推播\n\n{report[:4500]}\n\n輸入 /weekplan 隨時查詢"
        for uid in uids[:50]:
            try:
                await push_to_all_users(uid, msg)
            except Exception as e:
                logger.debug(f"[weekplan] push {uid}: {e}")
    except Exception as e:
        logger.error(f"[weekplan] push_weekplan_to_all: {e}")
