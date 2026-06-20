"""Exit Strategy Service — 智慧停利停損策略"""
from __future__ import annotations

import time
from loguru import logger
import asyncio
import math

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 900  # 15 min


async def get_exit_strategy(code: str) -> dict:
    key = code.strip()
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_exit(key)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_exit(code: str) -> dict:
    import asyncio
    hist_task  = _get_hist(code)
    quote_task = _get_quote(code)

    hist, quote = await asyncio.gather(hist_task, quote_task, return_exceptions=True)
    hist  = hist  if isinstance(hist,  list) else []
    quote = quote if isinstance(quote, dict) else {}

    price = float(quote.get("close") or (hist[-1]["close"] if hist else 0))
    name  = quote.get("name", code)

    levels  = _calc_exit_levels(hist, price)
    verdict = _gen_verdict(levels, price, name)

    return {
        "code":     code,
        "name":     name,
        "price":    price,
        "levels":   levels,
        "verdict":  verdict,
        "hist":     hist[-20:] if hist else [],
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


async def _get_hist(code: str) -> list:
    try:
        import httpx
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
               f"?interval=1d&range=60d")
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as cl:
            r = await cl.get(url)
        js = r.json()
        q  = js["chart"]["result"][0]["indicators"]["quote"][0]
        bars = []
        for c, h, lo, v in zip(
            q.get("close",  []), q.get("high", []),
            q.get("low",    []), q.get("volume", [])
        ):
            if c:
                bars.append({"close": c, "high": h or c, "low": lo or c, "volume": v or 0})
        return bars
    except Exception as e:
        logger.debug(f"[exit] hist {code}: {e}")
        return []
        base = c
    return bars


async def _get_quote(code: str) -> dict:
    try:
        from .twse_service import fetch_realtime_quote
        return await fetch_realtime_quote(code) or {}
    except Exception as e:
        return {}


def _calc_atr(hist: list, n: int = 14) -> float:
    if len(hist) < n + 1:
        return hist[-1]["close"] * 0.02 if hist else 1.0
    trs = []
    for i in range(1, len(hist)):
        h  = hist[i]["high"]
        lo = hist[i]["low"]
        pc = hist[i-1]["close"]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    atr = sum(trs[-n:]) / n
    return round(atr, 2)


def _calc_exit_levels(hist: list, price: float) -> dict:
    if not hist or price <= 0:
        return _fallback_levels(price)

    atr    = _calc_atr(hist)
    closes = [b["close"] for b in hist]
    highs  = [b["high"]  for b in hist]

    # Support / resistance approximations
    recent_high = max(highs[-20:]) if len(highs) >= 20 else max(highs)
    ma20  = sum(closes[-20:]) / min(len(closes), 20)
    ma60  = sum(closes[-60:]) / min(len(closes), 60)

    # Trailing stop: 2×ATR below price
    trail_stop = round(price - 2 * atr, 1)
    hard_stop  = round(price - 3 * atr, 1)

    # Profit targets: 1×ATR, 2×ATR, 3×ATR (Fibonacci-like)
    target1 = round(price + 1.5 * atr, 1)
    target2 = round(price + 3.0 * atr, 1)
    target3 = round(price + 5.0 * atr, 1)

    # Exit plan: sell in 3 tranches
    exit_plan = [
        {"price": target1, "pct": 30, "note": "首批停利（1.5×ATR）"},
        {"price": target2, "pct": 50, "note": "主力停利（3×ATR）"},
        {"price": target3, "pct": 20, "note": "強勢留倉（5×ATR）"},
    ]

    # R/R ratio
    risk    = price - hard_stop
    reward  = target2 - price
    rr      = round(reward / risk, 2) if risk > 0 else 0

    return {
        "atr":        atr,
        "ma20":       round(ma20, 1),
        "ma60":       round(ma60, 1),
        "recent_high":round(recent_high, 1),
        "trail_stop": trail_stop,
        "hard_stop":  hard_stop,
        "target1":    target1,
        "target2":    target2,
        "target3":    target3,
        "exit_plan":  exit_plan,
        "rr_ratio":   rr,
    }


def _fallback_levels(price: float) -> dict:
    if price <= 0:
        price = 100.0
    atr = price * 0.025
    return {
        "atr":        round(atr, 2),
        "ma20":       round(price * 0.97, 1),
        "ma60":       round(price * 0.93, 1),
        "recent_high":round(price * 1.08, 1),
        "trail_stop": round(price - 2 * atr, 1),
        "hard_stop":  round(price - 3 * atr, 1),
        "target1":    round(price + 1.5 * atr, 1),
        "target2":    round(price + 3.0 * atr, 1),
        "target3":    round(price + 5.0 * atr, 1),
        "exit_plan": [
            {"price": round(price + 1.5 * atr, 1), "pct": 30, "note": "首批停利"},
            {"price": round(price + 3.0 * atr, 1), "pct": 50, "note": "主力停利"},
            {"price": round(price + 5.0 * atr, 1), "pct": 20, "note": "強勢留倉"},
        ],
        "rr_ratio": round(3 * atr / (2 * atr), 2),
    }


def _gen_verdict(levels: dict, price: float, name: str) -> str:
    rr     = levels.get("rr_ratio", 0)
    target2= levels.get("target2", 0)
    trail  = levels.get("trail_stop", 0)
    ma20   = levels.get("ma20", 0)

    if price > ma20:
        pos_desc = "目前價格在均線上方，趨勢偏多"
    else:
        pos_desc = "目前價格跌破均線，需謹慎操作"

    verdict = (f"{name}現價 {price:,.1f}，{pos_desc}。"
               f"建議分3批出場：首批 {levels['target1']:.1f}（出30%），"
               f"主力 {target2:.1f}（出50%），強勢留倉至 {levels['target3']:.1f}（最後20%）。"
               f"移動停利設 {trail:.1f}（2×ATR），強制停損 {levels['hard_stop']:.1f}（3×ATR）。"
               f"風報比：{rr:.1f}，{'優良' if rr >= 2 else '可接受' if rr >= 1.5 else '偏低，謹慎操作'}。")
    return verdict


def format_exit_report(data: dict) -> str:
    if not data or data.get("error"):
        return f"❌ {data.get('error', '無法取得停利策略')}"

    code    = data["code"]; name = data["name"]; price = data["price"]
    levels  = data["levels"]; verdict = data["verdict"]
    hist    = data["hist"]; ts = data["updated_at"]

    chars = "▁▂▃▄▅▆▇█"
    closes = [b["close"] for b in hist]
    if closes:
        mn, mx  = min(closes), max(closes)
        rng     = mx - mn or 0.01
        spark   = "".join(chars[int((c - mn) / rng * 7)] for c in closes[-16:])
    else:
        spark = "─"

    rr_icon = "✅" if levels.get("rr_ratio", 0) >= 2 else "⚠️"

    lines = [
        f"🎯 智慧停利策略  [{code}] {name}",
        "─" * 32, "",
        f"現價：{price:,.1f} 元  ATR：{levels.get('atr', 0):.1f}",
        f"MA20：{levels.get('ma20', 0):,.1f}  MA60：{levels.get('ma60', 0):,.1f}",
        f"近期高點：{levels.get('recent_high', 0):,.1f}",
        "",
        f"📈 價格走勢：{spark}",
        "",
        "🎯 分批出場計畫",
        f"  ├ 🥇 首批（30%）→ {levels.get('target1', 0):,.1f}",
        f"  ├ 🥈 主力（50%）→ {levels.get('target2', 0):,.1f}",
        f"  └ 🥉 留倉（20%）→ {levels.get('target3', 0):,.1f}",
        "",
        "🛡️ 風控設定",
        f"  移動停利：{levels.get('trail_stop', 0):,.1f}（跌破觸發，2×ATR）",
        f"  強制停損：{levels.get('hard_stop',  0):,.1f}（不得低於，3×ATR）",
        "",
        f"  {rr_icon} 風報比：{levels.get('rr_ratio', 0):.1f}（目標2以上）",
        "",
        "─" * 28,
        "🤖 AI 研判",
        verdict,
        "",
        f"更新：{ts}",
        "⚠️ 停利停損僅供參考，請依個人風險承受度調整",
    ]
    return "\n".join(lines)
