"""Buzz Service — 個股社群討論熱度追蹤（PTT + 模擬）"""
from __future__ import annotations

import time
import re
from loguru import logger
import asyncio

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 1800  # 30 min

# Stock name mapping for PTT search
_STOCK_NAMES: dict = {
    "2330": "台積電", "2454": "聯發科", "2317": "鴻海", "2308": "台達電",
    "2303": "聯電",   "2002": "中鋼",   "1301": "台塑", "2882": "國泰金",
    "3008": "大立光", "2881": "富邦金", "2891": "中信金","2886": "兆豐金",
    "6505": "台塑化", "1303": "南亞",   "2412": "中華電","4938": "和碩",
}


async def get_buzz(code: str) -> dict:
    key = code.strip()
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_buzz(key)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_buzz(code: str) -> dict:
    import asyncio
    name       = _STOCK_NAMES.get(code, code)
    ptt_task   = _scrape_ptt(code, name)
    quote_task = _get_quote(code)

    ptt_data, quote = await asyncio.gather(ptt_task, quote_task, return_exceptions=True)
    ptt_data = ptt_data if isinstance(ptt_data, dict) else _fallback_ptt(code, name)
    quote    = quote    if isinstance(quote, dict)    else {}

    price    = float(quote.get("close") or 0)
    analysis = _analyze_buzz(ptt_data, price, name)

    return {
        "code":       code,
        "name":       name,
        "price":      price,
        "ptt":        ptt_data,
        "analysis":   analysis,
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


async def _scrape_ptt(code: str, name: str) -> dict:
    try:
        import httpx
        # PTT Stock board search
        search_term = name if name != code else code
        url = f"https://www.ptt.cc/bbs/Stock/search?q={search_term}"
        async with httpx.AsyncClient(
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0", "Cookie": "over18=1"},
            follow_redirects=True,
        ) as cl:
            r = await cl.get(url)
        html = r.text

        # Parse titles and push counts
        titles = re.findall(r'<div class="title">\s*<a[^>]*>([^<]+)</a>', html)
        pushes = re.findall(r'<div class="nrec"><span[^>]*>(\d+|爆|XX?)</span>', html)

        def _parse_push(p: str) -> int:
            if p == "爆":    return 100
            if p.startswith("X"): return -len(p) * 10
            try: return int(p)
            except: return 0

        push_vals  = [_parse_push(p) for p in pushes]
        push_count = sum(1 for p in push_vals if p > 0)
        push_down  = sum(1 for p in push_vals if p < 0)
        total_push = sum(push_vals)
        post_count = len(titles)

        # Sentiment from titles
        bull_kw  = ["多", "買", "漲", "看好", "大漲", "衝", "爆量", "飆"]
        bear_kw  = ["空", "賣", "跌", "看壞", "崩", "停損", "出場", "慘"]
        bull_cnt = sum(1 for t in titles if any(k in t for k in bull_kw))
        bear_cnt = sum(1 for t in titles if any(k in t for k in bear_kw))

        if post_count == 0:
            return _fallback_ptt(code, name)

        return {
            "post_count":  post_count,
            "push_count":  push_count,
            "push_down":   push_down,
            "total_push":  total_push,
            "bull_posts":  bull_cnt,
            "bear_posts":  bear_cnt,
            "titles":      titles[:5],
            "source":      "PTT Stock 版",
        }
    except Exception as e:
        logger.debug(f"[buzz] ptt {code}: {e}")
        return _fallback_ptt(code, name)


def _fallback_ptt(code: str, name: str) -> dict:
    import random
    post_count = random.randint(5, 60)
    bull       = random.randint(1, post_count // 2)
    bear       = random.randint(0, post_count // 3)
    return {
        "post_count":  post_count,
        "push_count":  random.randint(10, 200),
        "push_down":   random.randint(0, 30),
        "total_push":  random.randint(-50, 300),
        "bull_posts":  bull,
        "bear_posts":  bear,
        "titles":      [
            f"[討論] {name} 今天走勢分析",
            f"[標的] {name} 多方布局時機",
            f"[心得] 操作 {name} 心得分享",
        ],
        "source":      "PTT Stock 版（模擬）",
    }


async def _get_quote(code: str) -> dict:
    try:
        from .twse_service import fetch_realtime_quote
        return await fetch_realtime_quote(code) or {}
    except Exception as e:
        return {}


def _analyze_buzz(ptt: dict, price: float, name: str) -> dict:
    post_count = ptt.get("post_count", 0)
    bull_posts = ptt.get("bull_posts", 0)
    bear_posts = ptt.get("bear_posts", 0)
    total_push = ptt.get("total_push", 0)

    # Sentiment ratio
    total_opinion = bull_posts + bear_posts
    bull_ratio    = bull_posts / total_opinion if total_opinion else 0.5
    bear_ratio    = 1 - bull_ratio

    # Heat level
    if post_count >= 40:
        heat = "異常熱門"
        heat_warn = True
    elif post_count >= 20:
        heat = "熱度偏高"
        heat_warn = False
    elif post_count >= 8:
        heat = "正常討論"
        heat_warn = False
    else:
        heat = "冷門"
        heat_warn = False

    # Sentiment
    if bull_ratio >= 0.7:
        sentiment = "偏多"
        sent_icon = "📈"
    elif bull_ratio >= 0.55:
        sentiment = "略偏多"
        sent_icon = "📊"
    elif bull_ratio <= 0.3:
        sentiment = "偏空"
        sent_icon = "📉"
    elif bull_ratio <= 0.45:
        sentiment = "略偏空"
        sent_icon = "📊"
    else:
        sentiment = "中性"
        sent_icon = "⬜"

    verdict = (f"{name}在PTT討論熱度【{heat}】，近期 {post_count} 篇文章，"
               f"偏多：{bull_posts} 篇（{bull_ratio*100:.0f}%）"
               f"，偏空：{bear_posts} 篇（{bear_ratio*100:.0f}%）。")

    if heat_warn:
        verdict += f" ⚠️ 討論量異常飆升，可能是主力炒作或重大消息，需謹慎追高。"
    elif sentiment == "偏多" and total_push > 200:
        verdict += " 社群情緒高漲，短期有助於推升股價，但需留意獲利了結賣壓。"
    elif sentiment == "偏空":
        verdict += " 社群偏空氛圍濃厚，但散戶集體偏空時往往是底部訊號，注意反向操作機會。"
    else:
        verdict += " 社群討論中性，股價走勢以基本面技術面為主要驅動。"

    return {
        "heat":       heat,
        "heat_warn":  heat_warn,
        "sentiment":  sentiment,
        "sent_icon":  sent_icon,
        "bull_ratio": round(bull_ratio * 100, 1),
        "bear_ratio": round(bear_ratio * 100, 1),
        "verdict":    verdict,
    }


def format_buzz_report(data: dict) -> str:
    if not data or data.get("error"):
        return f"❌ {data.get('error', '無法取得社群熱度')}"

    code     = data["code"]; name = data["name"]; price = data["price"]
    ptt      = data["ptt"]; an = data["analysis"]; ts = data["updated_at"]

    heat_icon = {"異常熱門": "🔥", "熱度偏高": "📣", "正常討論": "💬", "冷門": "😴"}
    icon      = heat_icon.get(an["heat"], "💬")
    warn_tag  = "⚠️ 警示" if an.get("heat_warn") else ""

    bull_bar  = int(an["bull_ratio"] / 100 * 20)
    bear_bar  = 20 - bull_bar
    sentiment_bar = "📈" * (bull_bar // 4) + "📉" * (bear_bar // 4)

    lines = [
        f"💬 社群熱度追蹤  [{code}] {name}",
        "─" * 32, "",
        f"現價：{price:,.1f} 元",
        "",
        f"🔥 熱度：{icon} {an['heat']} {warn_tag}",
        f"   PTT 討論篇數：{ptt.get('post_count', 0)} 篇",
        f"   推文數：+{ptt.get('push_count', 0)} / -{ptt.get('push_down', 0)}",
        "",
        f"📊 情緒分析：{an['sent_icon']} {an['sentiment']}",
        f"   偏多：{an['bull_ratio']:.0f}%  偏空：{an['bear_ratio']:.0f}%",
        f"   [{sentiment_bar}]",
        "",
    ]

    titles = ptt.get("titles", [])
    if titles:
        lines.append("📝 近期熱門標題")
        for t in titles[:4]:
            lines.append(f"  • {t[:40]}")

    lines += [
        "",
        f"資料來源：{ptt.get('source', 'PTT Stock 版')}",
        "",
        "─" * 28,
        "🤖 AI 研判",
        an.get("verdict", ""),
        "",
        f"更新：{ts}",
        "⚠️ 社群情緒為反向指標參考，高度偏多時需謹慎",
    ]
    return "\n".join(lines)
