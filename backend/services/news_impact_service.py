"""News Impact Service — AI 新聞事件影響評估"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 600  # 10 min

# Industry → stock mapping for impact analysis
INDUSTRY_MAP: dict[str, list[str]] = {
    "台積電":    ["2330"],
    "晶圓":      ["2330", "2303", "2344"],
    "CoWoS":     ["2330", "3231", "6669", "4938"],
    "HBM":       ["2330", "4938", "3017"],
    "AI伺服器":  ["3231", "6669", "4938", "3017", "2382"],
    "半導體":    ["2330", "2303", "2344", "3711"],
    "電動車":    ["1590", "6223", "8054", "2355"],
    "航運":      ["2603", "2609", "2615", "2623"],
    "金融":      ["2882", "2881", "2891", "2886"],
    "聯發科":    ["2454"],
    "鴻海":      ["2317"],
    "散熱":      ["3443", "1590", "6285", "2313"],
    "PCB":       ["2382", "3037", "6269", "3376"],
    "5G":        ["2412", "3045", "4977", "6277"],
    "鋼鐵":      ["2002", "2006", "9910"],
    "生技":      ["4763", "6548", "4174", "1736"],
    "Fed":       ["2882", "2881", "2891", "2886"],
    "美元":      ["2882", "2603", "2609"],
    "油價":      ["2603", "2609", "1301", "1303"],
    "記憶體":    ["4938", "3017", "2330"],
}

NEGATIVE_KW = ["下滑", "下修", "砍單", "衰退", "庫存", "暴跌", "崩", "危機",
               "停滯", "虧損", "訴訟", "罰款", "制裁", "出走", "流失", "放緩"]
POSITIVE_KW = ["成長", "突破", "拿單", "旺季", "上調", "創高", "漲", "合作",
               "簽約", "擴產", "轉機", "獲利", "升評", "強勁", "需求強"]


async def get_news_impact(event_text: str) -> dict:
    key = event_text[:40].strip()
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _analyze_impact(event_text)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _analyze_impact(event_text: str) -> dict:
    import asyncio

    # Detect sentiment
    neg_hits = sum(1 for w in NEGATIVE_KW if w in event_text)
    pos_hits = sum(1 for w in POSITIVE_KW if w in event_text)
    is_negative = neg_hits > pos_hits

    # Find related stocks
    related_codes = _find_related_stocks(event_text)

    # Assess severity
    severity = _assess_severity(event_text, neg_hits + pos_hits)

    # Get quotes for related stocks
    quotes = await asyncio.gather(
        *[_get_quote(c) for c in related_codes[:8]], return_exceptions=True
    )
    stock_info = {}
    for code, q in zip(related_codes[:8], quotes):
        if isinstance(q, dict) and q:
            stock_info[code] = q

    # Generate beneficiaries and victims
    if is_negative:
        victims    = related_codes[:3]
        benefits   = _find_inverse_stocks(event_text, related_codes)[:3]
    else:
        benefits   = related_codes[:3]
        victims    = _find_inverse_stocks(event_text, related_codes)[:3]

    analysis = _gen_analysis(event_text, is_negative, severity, benefits, victims, stock_info)

    return {
        "event":       event_text,
        "sentiment":   "負面" if is_negative else "正面",
        "severity":    severity,
        "benefits":    [{"code": c, **stock_info.get(c, {"name": c, "close": 0, "chg": 0})}
                        for c in benefits],
        "victims":     [{"code": c, **stock_info.get(c, {"name": c, "close": 0, "chg": 0})}
                        for c in victims],
        "related":     related_codes[:6],
        "analysis":    analysis,
        "updated_at":  time.strftime("%Y-%m-%d %H:%M"),
    }


def _find_related_stocks(text: str) -> list:
    found = []
    for keyword, codes in INDUSTRY_MAP.items():
        if keyword in text:
            for c in codes:
                if c not in found:
                    found.append(c)
    return found or ["2330", "2454", "2317"]


def _find_inverse_stocks(text: str, main_codes: list) -> list:
    all_codes = []
    for codes in INDUSTRY_MAP.values():
        for c in codes:
            if c not in main_codes and c not in all_codes:
                all_codes.append(c)
    return all_codes[:3]


def _assess_severity(text: str, hit_count: int) -> str:
    strong = ["重大", "暴跌", "崩", "嚴重", "大幅", "顯著", "巨大", "翻倍"]
    if any(w in text for w in strong) or hit_count >= 3:
        return "重大影響"
    if hit_count >= 2:
        return "中等影響"
    return "輕微影響"


async def _get_quote(code: str) -> dict:
    try:
        from .twse_service import fetch_realtime_quote
        q = await fetch_realtime_quote(code)
        return q or {}
    except Exception:
        return {}


def _gen_analysis(event: str, is_neg: bool, severity: str,
                  benefits: list, victims: list, info: dict) -> str:
    dir_word = "利空" if is_neg else "利多"
    parts = [
        f"此事件屬於【{dir_word}】，影響程度評估為【{severity}】。",
    ]
    if is_neg:
        parts.append(f"主要受害股票：{', '.join(victims[:3])}，短線面臨賣壓，建議觀望或減碼。")
        if benefits:
            parts.append(f"潛在受益股：{', '.join(benefits[:3])}，若市場資金出逃可能轉向此類標的。")
    else:
        parts.append(f"主要受益股票：{', '.join(benefits[:3])}，短線有追漲動能，可觀察成交量確認。")
        if victims:
            parts.append(f"潛在競爭受害股：{', '.join(victims[:3])}，留意相對弱勢。")
    parts.append("⚠️ AI 分析基於關鍵字判斷，需結合完整新聞內容評估。")
    return " ".join(parts)


def format_impact_report(data: dict) -> str:
    if not data or data.get("error"):
        return f"❌ {data.get('error', '無法分析事件影響')}"

    event    = data["event"]
    sent     = data["sentiment"]
    severity = data["severity"]
    benefits = data["benefits"]
    victims  = data["victims"]
    analysis = data["analysis"]
    ts       = data["updated_at"]

    sent_icon = "📉" if sent == "負面" else "📈"
    sev_icon  = {"重大影響": "🔴", "中等影響": "🟡", "輕微影響": "🟢"}.get(severity, "⬜")

    lines = [
        "🎯 AI 新聞事件影響評估",
        "─" * 32, "",
        f"📰 事件：{event[:50]}",
        f"{sent_icon} 情緒：{sent}",
        f"{sev_icon} 影響：{severity}",
        "",
    ]

    if benefits:
        lines.append("📈 受益股（Top 3）")
        for s in benefits:
            chg  = float(s.get("chg", 0) or s.get("change_pct", 0) or 0)
            icon = "▲" if chg > 0 else ("▼" if chg < 0 else "─")
            name = s.get("name", s["code"])
            lines.append(f"  [{s['code']}] {name:<8} {icon}{abs(chg):.1f}%")
        lines.append("")

    if victims:
        lines.append("📉 受害股（Top 3）")
        for s in victims:
            chg  = float(s.get("chg", 0) or s.get("change_pct", 0) or 0)
            icon = "▲" if chg > 0 else ("▼" if chg < 0 else "─")
            name = s.get("name", s["code"])
            lines.append(f"  [{s['code']}] {name:<8} {icon}{abs(chg):.1f}%")
        lines.append("")

    lines += [
        "─" * 28,
        "🤖 AI 研判",
        analysis,
        "",
        f"更新：{ts}",
    ]
    return "\n".join(lines)
