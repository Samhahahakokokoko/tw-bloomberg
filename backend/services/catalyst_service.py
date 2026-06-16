"""Catalyst Service — 個股催化劑追蹤（財報/法說/展覽/客戶財報）"""
from __future__ import annotations

import time
from datetime import datetime, date, timedelta
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 3600 * 6

# 固定催化劑事件庫（未來3個月）
_CATALYST_DB: dict[str, list[dict]] = {
    "2330": [
        {"type": "earnings",  "date": "2026-07-17", "title": "2026 Q2 法說會",
         "impact": "high",   "detail": "台積電季度法說，預計討論 AI 晶片需求展望、CoWoS 擴產進度"},
        {"type": "industry",  "date": "2026-06-20", "title": "Computex 2026 AI 峰會",
         "impact": "medium", "detail": "輝達、AMD等客戶發表新品，直接帶動台積電先進製程需求預期"},
        {"type": "customer",  "date": "2026-07-24", "title": "Alphabet Q2 財報",
         "impact": "medium", "detail": "Google TPU 資本支出，影響台積電 N3/N2 訂單能見度"},
        {"type": "customer",  "date": "2026-07-30", "title": "Meta Q2 財報",
         "impact": "medium", "detail": "AI 資本支出計畫，影響台積電 AI 晶片訂單"},
        {"type": "customer",  "date": "2026-08-01", "title": "Apple Q3 FY2026 財報",
         "impact": "high",   "detail": "iPhone 下半年出貨展望，影響台積電 A18 Bionic 訂單"},
        {"type": "customer",  "date": "2026-07-23", "title": "Tesla Q2 財報",
         "impact": "low",    "detail": "FSD 晶片需求，間接影響台積電 AI 車用晶片佈局"},
    ],
    "2317": [
        {"type": "earnings",  "date": "2026-08-07", "title": "鴻海 Q2 法說會",
         "impact": "high",   "detail": "GB200/GB300 出貨量、AI 伺服器組裝進度"},
        {"type": "customer",  "date": "2026-07-29", "title": "NVIDIA Q2 財報",
         "impact": "high",   "detail": "Blackwell 出貨量直接影響鴻海 AI 伺服器訂單"},
        {"type": "industry",  "date": "2026-08-15", "title": "Hot Chips 2026",
         "impact": "low",    "detail": "AI 晶片架構峰會，影響下一代伺服器規格預期"},
    ],
    "2454": [
        {"type": "earnings",  "date": "2026-07-30", "title": "聯發科 Q2 法說會",
         "impact": "high",   "detail": "天璣系列出貨、AI 手機晶片市佔率進展"},
        {"type": "industry",  "date": "2026-09-09", "title": "Apple iPhone 17 發表",
         "impact": "medium", "detail": "若蘋果採用聯發科 5G Modem 消息確認，為重大催化劑"},
        {"type": "customer",  "date": "2026-07-24", "title": "三星 MX 業績發表",
         "impact": "medium", "detail": "Galaxy AI 手機銷量影響聯發科 Dimensity 系列出貨"},
    ],
    "2382": [
        {"type": "earnings",  "date": "2026-08-05", "title": "廣達 Q2 法說會",
         "impact": "high",   "detail": "AI 伺服器出貨量、NVL72/NVL36 機櫃交期"},
        {"type": "customer",  "date": "2026-07-29", "title": "NVIDIA Q2 財報",
         "impact": "high",   "detail": "Blackwell/Rubin 伺服器需求，直接影響廣達訂單能見度"},
    ],
}

# 行業展覽行事曆
_INDUSTRY_EVENTS: list[dict] = [
    {"date": "2026-06-20", "title": "Computex 2026 AI + HPC 峰會", "impact": "high",
     "affects": ["2330", "2317", "2382", "6669", "3443"]},
    {"date": "2026-08-15", "title": "Hot Chips 2026", "impact": "medium",
     "affects": ["2330", "2454", "2379"]},
    {"date": "2026-09-09", "title": "Apple 秋季發表會", "impact": "high",
     "affects": ["2330", "2454", "4938", "2357"]},
    {"date": "2026-09-22", "title": "IFA 2026（柏林消費電子展）", "impact": "medium",
     "affects": ["2454", "2317", "2379"]},
]

# 預設催化劑（通用）
_DEFAULT_CATALYSTS = [
    {"type": "earnings", "title": "季報公布期", "impact": "high",
     "detail": "台灣上市公司 Q2 財報陸續公布（7-8月），留意 EPS 超預期情況"},
    {"type": "industry", "title": "Fed 利率決策", "impact": "medium",
     "detail": "聯準會貨幣政策決議，影響資金成本與科技股估值"},
    {"type": "policy",   "title": "台灣央行理監事會", "impact": "medium",
     "detail": "台灣利率政策，影響金融股與高殖利率股"},
]

_IMPACT_ICON = {"high": "🔥", "medium": "🟡", "low": "🔵"}
_TYPE_LABEL  = {"earnings": "財報/法說", "customer": "客戶財報", "industry": "產業展覽",
                "policy": "政策事件", "macro": "總經事件"}


async def get_catalyst(code: str) -> dict:
    key = code.upper()
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = _build_catalyst(key)
    _cache[key] = result
    _cache_ts[key] = now
    return result


def _build_catalyst(code: str) -> dict:
    today_str = date.today().isoformat()
    cutoff    = (date.today() + timedelta(days=90)).isoformat()

    # 個股特定催化劑
    stock_cats = _CATALYST_DB.get(code, [])

    # 行業展覽中有影響到此股的
    industry_cats = []
    for ev in _INDUSTRY_EVENTS:
        if code in ev.get("affects", []) and today_str <= ev["date"] <= cutoff:
            industry_cats.append({
                "type":   "industry",
                "date":   ev["date"],
                "title":  ev["title"],
                "impact": ev["impact"],
                "detail": f"影響涉及 {', '.join(ev['affects'][:3])} 等",
            })

    # 合併並補齊
    all_cats = stock_cats + industry_cats
    all_cats = [c for c in all_cats if today_str <= c.get("date", "9999") <= cutoff]
    all_cats.sort(key=lambda x: x.get("date", "9999"))

    if not all_cats:
        all_cats = [dict(c, date=(date.today() + timedelta(days=i*14)).isoformat())
                    for i, c in enumerate(_DEFAULT_CATALYSTS)]

    # 評估未來30/60/90天高衝擊事件數
    high_30 = sum(1 for c in all_cats
                  if c.get("impact") == "high"
                  and c.get("date", "9999") <= (date.today() + timedelta(days=30)).isoformat())
    high_60 = sum(1 for c in all_cats if c.get("impact") == "high")

    # 整體催化劑評分（高衝擊×3 + 中衝擊×1）
    cat_score = sum(3 if c.get("impact") == "high" else 1 for c in all_cats)
    if cat_score >= 10:
        catalyst_rating, cat_icon = "催化劑密集（事件驅動機會大）", "🔥"
    elif cat_score >= 5:
        catalyst_rating, cat_icon = "催化劑適中", "🟡"
    else:
        catalyst_rating, cat_icon = "催化劑稀少", "🔵"

    return {
        "code":            code,
        "catalysts":       all_cats[:10],
        "high_30d":        high_30,
        "high_60d":        high_60,
        "cat_score":       cat_score,
        "catalyst_rating": catalyst_rating,
        "cat_icon":        cat_icon,
        "updated_at":      datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def format_catalyst_report(data: dict, code: str) -> str:
    if data.get("error"):
        return f"❌ {data['error']}"

    cats    = data.get("catalysts", [])
    rating  = data.get("catalyst_rating", "")
    icon    = data.get("cat_icon", "")
    h30     = data.get("high_30d", 0)
    score   = data.get("cat_score", 0)
    updated = data.get("updated_at", "")

    lines = [
        f"🎯 個股催化劑追蹤  {code}",
        "─" * 32, "",
        f"{icon} {rating}",
        f"催化劑評分：{score}  未來30天高衝擊事件：{h30} 件",
        f"更新：{updated}",
        "",
        "── 未來3個月重要催化劑 ──",
        "",
    ]

    if not cats:
        lines.append("  近期無顯著催化劑事件")
    else:
        for cat in cats:
            imp_icon = _IMPACT_ICON.get(cat.get("impact", "low"), "⬜")
            typ_lbl  = _TYPE_LABEL.get(cat.get("type", ""), "事件")
            lines += [
                f"  {imp_icon} {cat.get('date', '')}  [{typ_lbl}]",
                f"     {cat.get('title', '')}",
                f"     {cat.get('detail', '')}",
                "",
            ]

    lines += [
        "─" * 28,
        "🤖 AI 催化劑解讀",
        _gen_catalyst_verdict(data, code),
        "",
        f"輸入 /timeline {code} 查歷史事件 | /stress {code} 查壓力測試",
    ]
    return "\n".join(lines)


def _gen_catalyst_verdict(data: dict, code: str) -> str:
    cats  = data.get("catalysts", [])
    score = data.get("cat_score", 0)
    h30   = data.get("high_30d", 0)

    high_events = [c for c in cats if c.get("impact") == "high"]

    if h30 >= 2:
        ev_names = "、".join(c.get("title", "") for c in high_events[:2])
        return (f"未來30天內有 {h30} 個高衝擊事件（{ev_names}），"
                f"股價可能出現方向性突破，建議提前關注並設定目標價和停損。")
    elif score >= 5:
        return (f"未來3個月催化劑充足（評分 {score}），事件驅動行情可期。"
                f"建議在財報/法說前1-2週觀察量能變化，確認多空方向後再佈局。")
    else:
        return (f"{code} 近期催化劑較少，股價走勢可能以大盤連動為主。"
                f"此時適合評估基本面安全邊際，待明確催化劑出現再加碼。")
