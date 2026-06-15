"""Similarity Service — 個股走勢相似度搜尋"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 3600  # 1hr

# Universe of stocks to compare against
UNIVERSE = [
    "2330", "2454", "2317", "2303", "2882", "2881", "2891", "2886",
    "2412", "3711", "2308", "2382", "6669", "3231", "4938", "2357",
    "2379", "3034", "2395", "2912", "1301", "1303", "2002", "2006",
    "2603", "2609", "2615", "2301", "3045", "4904",
]


async def get_similar_stocks(code: str) -> dict:
    key = code.strip()
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _find_similar(key)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _find_similar(code: str) -> dict:
    import asyncio

    # Get target stock price series
    target_hist = await _get_returns(code)
    if not target_hist:
        return {"code": code, "error": "無法取得歷史資料", "similar": []}

    # Get comparison universe (exclude self)
    compare_codes = [c for c in UNIVERSE if c != code][:20]
    hists = await asyncio.gather(
        *[_get_returns(c) for c in compare_codes], return_exceptions=True
    )

    correlations = []
    for c, hist in zip(compare_codes, hists):
        if not isinstance(hist, list) or not hist:
            continue
        corr = _pearson(target_hist, hist)
        if corr is not None:
            correlations.append((c, corr))

    correlations.sort(key=lambda x: x[1], reverse=True)
    top5_codes = [c for c, _ in correlations[:5]]

    # Get current quotes for top 5
    quotes = await asyncio.gather(
        *[_get_quote(c) for c in top5_codes], return_exceptions=True
    )
    target_quote = await _get_quote(code)
    target_quote = target_quote if isinstance(target_quote, dict) else {}

    similar = []
    for (c, corr), q in zip(correlations[:5], quotes):
        q = q if isinstance(q, dict) else {}
        similar.append({
            "code": c,
            "name": q.get("name", c),
            "corr": round(corr, 3),
            "close": q.get("close", 0),
            "chg":   float(q.get("change_pct") or 0),
        })

    verdict = _gen_verdict(code, target_quote, similar, target_hist)

    return {
        "code":         code,
        "name":         target_quote.get("name", code),
        "price":        target_quote.get("close", 0),
        "similar":      similar,
        "verdict":      verdict,
        "days":         min(len(target_hist), 60),
        "updated_at":   time.strftime("%Y-%m-%d %H:%M"),
    }


async def _get_returns(code: str) -> list:
    try:
        import httpx
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
               f"?interval=1d&range=90d")
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as cl:
            r = await cl.get(url)
        js  = r.json()
        cls = js["chart"]["result"][0]["indicators"]["quote"][0].get("close", [])
        cls = [c for c in cls if c]
        if len(cls) < 20:
            return []
        # Convert to daily returns
        returns = [(cls[i] / cls[i - 1] - 1) for i in range(1, len(cls))]
        return returns[-60:]
    except Exception as e:
        logger.debug(f"[similar] returns {code}: {e}")
        return []


async def _get_quote(code: str) -> dict:
    try:
        from .twse_service import fetch_realtime_quote
        return await fetch_realtime_quote(code) or {}
    except Exception as e:
        logger.debug(f"[similar] quote {code}: {e}")
        return {}


def _pearson(x: list, y: list) -> float | None:
    n = min(len(x), len(y))
    if n < 10:
        return None
    x, y = x[-n:], y[-n:]
    mx, my = sum(x) / n, sum(y) / n
    num   = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    dx    = sum((xi - mx) ** 2 for xi in x) ** 0.5
    dy    = sum((yi - my) ** 2 for yi in y) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _gen_verdict(code: str, tq: dict, similar: list, target_hist: list) -> str:
    if not similar:
        return "相似股票資料不足，無法進行比較分析"

    top = similar[0]
    lines = []
    # Compare performance trend
    leader = [s for s in similar if s["chg"] > 1.0]
    lagger = [s for s in similar if s["chg"] < -1.0]
    if leader:
        names = "、".join(f"[{s['code']}]{s['name']}" for s in leader[:2])
        lines.append(f"相似股 {names} 今日上漲，若相關性持續，{code} 可能跟進")
    if lagger:
        names = "、".join(f"[{s['code']}]{s['name']}" for s in lagger[:2])
        lines.append(f"相似股 {names} 今日下跌，留意 {code} 連動風險")
    if not leader and not lagger:
        lines.append(f"相似股今日表現分歧，{code} 走勢需獨立判斷")

    lines.append(f"最相似為 [{top['code']}]{top['name']}（相關係數 {top['corr']:.2f}）")
    return "；".join(lines)


def format_similarity_report(data: dict) -> str:
    if data.get("error"):
        return f"❌ {data['error']}"
    if not data or not data.get("similar"):
        return "❌ 無法取得相似度資料"

    code    = data["code"]; name = data["name"]; price = data["price"]
    similar = data["similar"]; days = data["days"]
    verdict = data["verdict"]; ts = data["updated_at"]

    def _corr_bar(c, w=8):
        n = int((c + 1) / 2 * w)
        return "█" * n + "░" * (w - n)

    lines = [
        f"🔗 走勢相似度  [{code}] {name}",
        "─" * 32, "",
        f"分析區間：近 {days} 個交易日",
        f"現價：{price:,.1f} 元",
        "",
        "🏆 最相似股票 TOP 5",
        "",
    ]
    for i, s in enumerate(similar, 1):
        chg  = s["chg"]
        icon = "▲" if chg > 0 else ("▼" if chg < 0 else "─")
        corr_bar = _corr_bar(s["corr"])
        lines += [
            f"  {i}. [{s['code']}] {s['name']:<8}",
            f"     相關係數：{s['corr']:.3f}  [{corr_bar}]",
            f"     現價：{s['close']:,.1f}  今日：{icon}{abs(chg):.1f}%",
            "",
        ]

    lines += [
        "─" * 28,
        "🤖 AI 研判",
        verdict,
        "",
        f"更新：{ts}",
        "⚠️ 相關性為歷史統計，不保證未來同步",
    ]
    return "\n".join(lines)
