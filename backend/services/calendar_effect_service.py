"""Calendar Effect Service — 台股月曆效應分析"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 3600  # 1hr

# Historical monthly return averages (Taiwan market, approximate long-term stats)
MONTHLY_RETURNS: dict[int, dict] = {
    1:  {"avg": 1.8,  "positive_rate": 62, "label": "1月",  "note": "元月效應，外資回補"},
    2:  {"avg": 0.9,  "positive_rate": 55, "label": "2月",  "note": "春節前後震盪，農曆效應"},
    3:  {"avg": 1.2,  "positive_rate": 58, "label": "3月",  "note": "第一季法說旺季"},
    4:  {"avg": 0.7,  "positive_rate": 54, "label": "4月",  "note": "除息旺季開始，量能觀察"},
    5:  {"avg": -0.5, "positive_rate": 45, "label": "5月",  "note": "Sell in May 效應"},
    6:  {"avg": 0.2,  "positive_rate": 51, "label": "6月",  "note": "半年結算，法人調倉"},
    7:  {"avg": 1.5,  "positive_rate": 60, "label": "7月",  "note": "Q2 財報效應，科技旺季"},
    8:  {"avg": -0.8, "positive_rate": 44, "label": "8月",  "note": "暑期量縮，美股Jackson Hole"},
    9:  {"avg": -1.1, "positive_rate": 42, "label": "9月",  "note": "9月效應最弱，聯準會會議"},
    10: {"avg": 0.6,  "positive_rate": 53, "label": "10月", "note": "萬聖節效應，Q3法說"},
    11: {"avg": 1.9,  "positive_rate": 63, "label": "11月", "note": "年底作帳行情啟動"},
    12: {"avg": 2.3,  "positive_rate": 65, "label": "12月", "note": "聖誕行情，作帳尾聲"},
}

# Day-of-week effect
DOW_RETURNS: dict[int, dict] = {
    0: {"avg": 0.12, "label": "週一", "note": "週末效應消化"},
    1: {"avg": 0.18, "label": "週二", "note": "全週最強單日"},
    2: {"avg": 0.05, "label": "週三", "note": "週中整理"},
    3: {"avg": 0.08, "label": "週四", "note": "法人布局"},
    4: {"avg": -0.06, "label": "週五", "note": "週末效應，獲利了結"},
}

# Special calendar events
SPECIAL_EVENTS = [
    {"month": 1,  "week": 1,  "name": "元月效應",     "strength": "強",   "direction": "多"},
    {"month": 2,  "week": -1, "name": "春節前效應",   "strength": "中",   "direction": "震盪"},
    {"month": 5,  "week": 1,  "name": "Sell in May", "strength": "中",   "direction": "空"},
    {"month": 7,  "week": 2,  "name": "科技財報季",   "strength": "強",   "direction": "多"},
    {"month": 9,  "week": 1,  "name": "9月效應",      "strength": "強",   "direction": "空"},
    {"month": 11, "week": 1,  "name": "年底作帳",     "strength": "強",   "direction": "多"},
    {"month": 12, "week": 3,  "name": "聖誕行情",     "strength": "中",   "direction": "多"},
]


async def get_calendar_effect() -> dict:
    key = "calendar"
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _build_calendar_data()
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _build_calendar_data() -> dict:
    import datetime
    today = datetime.date.today()
    month = today.month
    dow   = today.weekday()   # 0=Mon … 4=Fri
    week_of_month = (today.day - 1) // 7 + 1

    current_month = MONTHLY_RETURNS[month]
    current_dow   = DOW_RETURNS.get(dow, DOW_RETURNS[0])

    # Rank months
    ranked = sorted(MONTHLY_RETURNS.items(), key=lambda x: x[1]["avg"], reverse=True)

    # Next 3 months outlook
    outlook = []
    for i in range(1, 4):
        m = (month - 1 + i) % 12 + 1
        outlook.append({"month": m, **MONTHLY_RETURNS[m]})

    # Active special events
    active_events = [e for e in SPECIAL_EVENTS
                     if e["month"] == month and abs(e["week"] - week_of_month) <= 1]

    # Composite signal
    score = current_month["avg"] + current_dow["avg"]
    if score > 1.5:
        signal = "積極偏多"
    elif score > 0:
        signal = "中性偏多"
    elif score > -0.5:
        signal = "中性觀望"
    else:
        signal = "偏空謹慎"

    return {
        "today":         today.strftime("%Y-%m-%d"),
        "month":         month,
        "dow":           dow,
        "week_of_month": week_of_month,
        "current_month": current_month,
        "current_dow":   current_dow,
        "ranked_months": ranked,
        "outlook":       outlook,
        "active_events": active_events,
        "composite_score": round(score, 2),
        "signal":        signal,
        "updated_at":    time.strftime("%Y-%m-%d %H:%M"),
    }


def format_calendar_report(data: dict) -> str:
    if not data:
        return "❌ 無法取得月曆效應資料"

    today    = data["today"]
    cm       = data["current_month"]
    cdow     = data["current_dow"]
    signal   = data["signal"]
    score    = data["composite_score"]
    events   = data["active_events"]
    outlook  = data["outlook"]
    ranked   = data["ranked_months"]

    # Monthly strength bar (normalize -1.5 ~ 2.5 to 0-10)
    def _bar(avg):
        w = int((avg + 1.5) / 4.0 * 8)
        w = max(0, min(8, w))
        color = "🟢" if avg > 0 else "🔴"
        return color * w + "⬜" * (8 - w)

    lines = [
        f"📅 台股月曆效應  {today}",
        "─" * 32, "",
        f"📊 當月效應（{cm['label']}）",
        f"  歷史均報：{cm['avg']:+.1f}%  上漲機率：{cm['positive_rate']}%",
        f"  {_bar(cm['avg'])}",
        f"  特性：{cm['note']}",
        "",
        f"📆 今日星期（{cdow['label']}）",
        f"  歷史均報：{cdow['avg']:+.2f}%",
        f"  特性：{cdow['note']}",
        "",
    ]

    if events:
        lines.append("⚡ 當前特殊效應")
        for e in events:
            icon = "📈" if e["direction"] == "多" else ("📉" if e["direction"] == "空" else "↔️")
            lines.append(f"  {icon} {e['name']}（強度：{e['strength']}，方向：{e['direction']}）")
        lines.append("")

    lines += [
        "─" * 28,
        f"🎯 綜合評分：{score:+.2f}",
        f"📌 操作訊號：{signal}",
        "",
        "📈 未來 3 個月展望",
    ]
    for o in outlook:
        trend = "▲" if o["avg"] > 0 else "▼"
        lines.append(f"  {o['label']}：{trend}{abs(o['avg']):.1f}%  ({o['note'][:10]})")

    lines += ["", "─" * 28, "🏆 歷史最強月份"]
    for rank, (m, info) in enumerate(ranked[:3], 1):
        medal = ["🥇", "🥈", "🥉"][rank - 1]
        lines.append(f"  {medal} {info['label']}：{info['avg']:+.1f}%（上漲率 {info['positive_rate']}%）")

    lines += ["", f"⚠️ 統計為歷史均值，不保證未來走勢", f"更新：{data['updated_at']}"]
    return "\n".join(lines)
