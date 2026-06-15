"""Events Service — 財經行事曆（未來2週重要事件）"""
from __future__ import annotations

import time
import datetime
from loguru import logger
import asyncio

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 3600  # 1 hr


async def get_events() -> dict:
    key = "events"
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_events()
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_events() -> dict:
    import asyncio
    static_events = _get_static_calendar()
    tw_task = _get_tw_events()
    us_task = _get_us_events()

    tw_data, us_data = await asyncio.gather(tw_task, us_task, return_exceptions=True)
    tw_data = tw_data if isinstance(tw_data, list) else []
    us_data = us_data if isinstance(us_data, list) else []

    all_events = _merge_events(static_events, tw_data, us_data)
    all_events.sort(key=lambda x: x["date"])

    today  = datetime.date.today()
    cutoff = today + datetime.timedelta(days=14)
    events = [e for e in all_events if today <= datetime.date.fromisoformat(e["date"]) <= cutoff]

    verdict = _gen_verdict(events)
    return {
        "events":     events,
        "verdict":    verdict,
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


def _get_static_calendar() -> list:
    today  = datetime.date.today()
    year   = today.year
    month  = today.month

    # Static recurring events for near-term planning
    # Fed FOMC meeting dates (approximate for 2025-2026)
    fomc_dates = [
        f"{year}-01-29", f"{year}-03-19", f"{year}-05-07",
        f"{year}-06-18", f"{year}-07-30", f"{year}-09-17",
        f"{year}-11-05", f"{year}-12-10",
    ]
    # US CPI release (approx 2nd Wednesday each month)
    us_cpi_dates = _get_monthly_dates(year, month, 2, 2)  # 2nd Wednesday
    us_nfp_dates = _get_first_fridays(year, month)         # 1st Friday

    events = []
    for d in fomc_dates:
        try:
            dt = datetime.date.fromisoformat(d)
            if dt >= today:
                events.append({
                    "date": d, "type": "美國", "icon": "🇺🇸",
                    "title": "Fed 利率決議 (FOMC)",
                    "impact": "高",
                    "tw_effect": "影響台股走向，外資依利率差調整部位",
                })
        except ValueError:
            continue

    for d in us_cpi_dates[:3]:
        events.append({
            "date": d, "type": "美國", "icon": "🇺🇸",
            "title": "美國 CPI 通膨數據",
            "impact": "高",
            "tw_effect": "CPI高於預期→升息預期→外資賣台股；低於預期→降息預期→資金流入",
        })

    for d in us_nfp_dates[:3]:
        events.append({
            "date": d, "type": "美國", "icon": "🇺🇸",
            "title": "美國非農就業 (NFP)",
            "impact": "中高",
            "tw_effect": "就業強勁→通膨顧慮→對台股略空；就業偏弱→降息預期→利多",
        })

    # Taiwan events (approximate)
    tw_events_static = [
        {"month_offset": 0, "day": 23, "title": "台灣 CPI 通膨", "impact": "中",
         "tw_effect": "影響台灣央行利率決策，對出口股影響有限"},
        {"month_offset": 1, "day": 5,  "title": "台積電法說會",  "impact": "高",
         "tw_effect": "台積電法說決定台股科技族群走向"},
        {"month_offset": 0, "day": 28, "title": "台灣外銷訂單",  "impact": "中高",
         "tw_effect": "外銷訂單年增率影響出口概念股"},
    ]
    for t in tw_events_static:
        try:
            d = today.replace(day=t["day"])
            if d < today:
                d = (d.replace(day=1) + datetime.timedelta(days=32)).replace(day=t["day"])
            events.append({
                "date": d.isoformat(), "type": "台灣", "icon": "🇹🇼",
                "title": t["title"], "impact": t["impact"], "tw_effect": t["tw_effect"],
            })
        except Exception as e:
            continue

    return events


def _get_monthly_dates(year: int, month: int, weekday: int, nth: int) -> list:
    dates = []
    for m in range(month, month + 3):
        yr = year + (m - 1) // 12
        mo = ((m - 1) % 12) + 1
        first = datetime.date(yr, mo, 1)
        days_ahead = (weekday - first.weekday()) % 7
        first_wd   = first + datetime.timedelta(days=days_ahead)
        target     = first_wd + datetime.timedelta(weeks=nth - 1)
        dates.append(target.isoformat())
    return dates


def _get_first_fridays(year: int, month: int) -> list:
    dates = []
    for m in range(month, month + 3):
        yr = year + (m - 1) // 12
        mo = ((m - 1) % 12) + 1
        first = datetime.date(yr, mo, 1)
        days_ahead = (4 - first.weekday()) % 7
        dates.append((first + datetime.timedelta(days=days_ahead)).isoformat())
    return dates


async def _get_tw_events() -> list:
    # Scrape TWSE announcement calendar (simplified)
    return []


async def _get_us_events() -> list:
    # Could integrate investing.com calendar API here
    return []


def _merge_events(static: list, tw: list, us: list) -> list:
    seen = set()
    merged = []
    for e in static + tw + us:
        key = f"{e['date']}_{e['title']}"
        if key not in seen:
            seen.add(key)
            merged.append(e)
    return merged


def _gen_verdict(events: list) -> str:
    if not events:
        return "未來2週無重大財經事件，市場相對平靜。"

    high_impact = [e for e in events[:14] if e.get("impact") in ("高", "中高")]
    us_events   = [e for e in events if e.get("type") == "美國"]
    tw_events   = [e for e in events if e.get("type") == "台灣"]

    verdict = f"未來2週共 {len(events)} 個重要事件"
    if high_impact:
        verdict += f"，其中 {len(high_impact)} 個高影響事件。"
    verdict += " "

    if any("Fed" in e["title"] or "FOMC" in e["title"] for e in events):
        verdict += "本週期有 Fed 利率決議，為最高關注事件，建議決議前降低槓桿。"
    elif any("CPI" in e["title"] for e in us_events):
        verdict += "CPI 數據公布將影響降息預期，注意美股反應帶動台股波動。"

    return verdict


def format_events_report(data: dict) -> str:
    if not data or data.get("error"):
        return f"❌ {data.get('error', '無法取得行事曆')}"

    events  = data["events"]
    verdict = data["verdict"]
    ts      = data["updated_at"]

    IMPACT_ICON = {"高": "🔴", "中高": "🟠", "中": "🟡", "低": "⬜"}

    today = datetime.date.today()
    lines = [
        "📅 財經行事曆（未來2週）",
        "─" * 36, "",
    ]

    if not events:
        lines.append("  （無重大事件）")
    else:
        cur_week = None
        for e in events:
            try:
                dt   = datetime.date.fromisoformat(e["date"])
                week = dt.isocalendar()[1]
                if week != cur_week:
                    cur_week = week
                    diff = (dt - today).days
                    wlabel = "本週" if diff <= 7 else "下週"
                    lines += ["", f"── {wlabel} ──"]
                days_left = (dt - today).days
                day_tag   = f"（{days_left}天後）" if days_left > 0 else "（今天）"
                icon      = IMPACT_ICON.get(e.get("impact", "中"), "⬜")
                lines.append(
                    f"  {e['icon']} {e['date']} {day_tag}"
                )
                lines.append(
                    f"     {icon} {e['title']} [{e.get('impact','─')}影響]"
                )
                if e.get("tw_effect"):
                    lines.append(f"     💡 {e['tw_effect'][:60]}")
            except Exception as e:
                continue

    lines += [
        "",
        "─" * 28,
        "🤖 AI 研判",
        verdict,
        "",
        f"更新：{ts}",
        "🔴高  🟠中高  🟡中  ⬜低",
    ]
    return "\n".join(lines)
