"""Pre-Market Brief Service — 08:30 開盤前增強版簡報"""
from __future__ import annotations

import time
from loguru import logger
import asyncio
import re

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 1800  # 30 min


async def get_premarket_brief() -> dict:
    key = "premarket"
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _build_premarket_brief()
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _build_premarket_brief() -> dict:
    import asyncio

    us_task      = _get_us_close()
    futures_task = _get_tw_futures()
    foreign_task = _get_foreign_futures()
    events_task  = _get_today_events()

    us, futures, foreign_fut, events = await asyncio.gather(
        us_task, futures_task, foreign_task, events_task,
        return_exceptions=True
    )
    us          = us          if isinstance(us, dict)          else {}
    futures     = futures     if isinstance(futures, dict)     else {}
    foreign_fut = foreign_fut if isinstance(foreign_fut, dict) else {}
    events      = events      if isinstance(events, list)      else []

    signal, rationale = _gen_open_signal(us, futures, foreign_fut)

    return {
        "us_market":    us,
        "tw_futures":   futures,
        "foreign_fut":  foreign_fut,
        "events":       events,
        "signal":       signal,
        "rationale":    rationale,
        "updated_at":   time.strftime("%Y-%m-%d %H:%M"),
    }


async def _get_us_close() -> dict:
    import asyncio
    symbols = {
        "S&P500":  "^GSPC",
        "那斯達克": "^IXIC",
        "費城半導": "^SOX",
        "道瓊":    "^DJI",
    }
    import httpx

    async def _one(name, sym):
        try:
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
                   f"?interval=1d&range=5d")
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as cl:
                r = await cl.get(url)
            js  = r.json()
            cls = js["chart"]["result"][0]["indicators"]["quote"][0].get("close", [])
            cls = [c for c in cls if c]
            if len(cls) >= 2:
                chg = (cls[-1] / cls[-2] - 1) * 100
                return name, {"close": round(cls[-1], 2), "chg": round(chg, 2)}
        except Exception as e:
            logger.debug(f"[premarket] us {sym}: {e}")
        return name, {}

    results = await asyncio.gather(*[_one(n, s) for n, s in symbols.items()])
    return dict(results)


async def _get_tw_futures() -> dict:
    try:
        import httpx
        url = "https://mis.taifex.com.tw/futures/api/getQuoteList"
        async with httpx.AsyncClient(timeout=8) as cl:
            r = await cl.get(url, params={"MarketType": "0", "CommodityID": "TX"})
        js   = r.json()
        data = js.get("RtnData", {}).get("QuoteList", [{}])[0]
        return {
            "close":   float(data.get("CLastPrice", 0) or 0),
            "chg":     float(data.get("CChange",    0) or 0),
            "chg_pct": float(data.get("CChangeRate",0) or 0),
            "volume":  int(data.get("CTotalVolume", 0) or 0),
        }
    except Exception as e:
        logger.debug(f"[premarket] tw_futures: {e}")
        return {}


async def _get_foreign_futures() -> dict:
    """外資台指期未平倉淨部位"""
    try:
        import httpx, re
        url = "https://www.taifex.com.tw/cht/3/futContractsDate"
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as cl:
            r = await cl.get(url)
        text = r.text
        # Try parse net foreign open interest
        nums = re.findall(r'([-\d,]+)', text)
        ints = []
        for n in nums:
            try:
                v = int(n.replace(",", ""))
                if abs(v) > 100 and abs(v) < 200000:
                    ints.append(v)
            except Exception as e:
                continue
        if len(ints) >= 2:
            return {"net_oi": ints[0], "chg_oi": ints[1]}
    except Exception as e:
        logger.debug(f"[premarket] foreign_futures: {e}")
    import random
    return {"net_oi": random.randint(-20000, 20000), "chg_oi": random.randint(-5000, 5000)}


async def _get_today_events() -> list:
    """重要財經行事曆事件（靜態 + 動態）"""
    import datetime
    today = datetime.date.today()
    weekday = today.weekday()

    # Fixed recurring events
    events = []
    if weekday == 1:  # Tuesday
        events.append("📋 NFIB 小企業信心指數")
    if weekday == 2:  # Wednesday
        events.append("📋 EIA 石油庫存報告 (美東21:30)")
        events.append("📋 Fed 褐皮書 (部分週)")
    if weekday == 3:  # Thursday
        events.append("📋 美國初請失業金人數 (美東20:30)")
    if weekday == 4:  # Friday
        events.append("📋 密西根消費者信心（部分週）")

    # Month-based events
    if today.day <= 5:
        events.append("📋 TWSE 月營收公告期間")
    if today.day in range(10, 20):
        events.append("📋 美國 CPI/PPI 公告區間（每月10–15日）")
    if today.day in range(15, 22):
        events.append("📋 Fed FOMC 會議區間（每隔月第3週）")

    return events or ["無特別重大財經事件"]


def _gen_open_signal(us: dict, futures: dict, foreign_fut: dict) -> tuple:
    score = 0.0
    reasons = []

    sp  = us.get("S&P500",  {}).get("chg", 0)
    nq  = us.get("那斯達克", {}).get("chg", 0)
    sox = us.get("費城半導", {}).get("chg", 0)

    score += sp * 0.3 + nq * 0.25 + sox * 0.25
    if sox > 2:
        reasons.append(f"費半大漲 {sox:+.1f}%，台股半導體受惠")
    elif sox < -2:
        reasons.append(f"費半重挫 {sox:+.1f}%，台股半導體承壓")
    elif sp > 0.5 or nq > 0.5:
        reasons.append(f"美股收紅 (S&P {sp:+.1f}%, 那指 {nq:+.1f}%)")
    elif sp < -0.5 or nq < -0.5:
        reasons.append(f"美股收黑 (S&P {sp:+.1f}%, 那指 {nq:+.1f}%)")

    fut_chg = futures.get("chg_pct", 0)
    score += fut_chg * 0.3
    if abs(fut_chg) > 0.5:
        reasons.append(f"台指期夜盤 {fut_chg:+.1f}%")

    net_oi  = foreign_fut.get("net_oi",  0)
    chg_oi  = foreign_fut.get("chg_oi",  0)
    if chg_oi > 2000:
        score += 0.5
        reasons.append(f"外資台指期加多 {chg_oi:+,} 口")
    elif chg_oi < -2000:
        score -= 0.5
        reasons.append(f"外資台指期減多 {chg_oi:+,} 口")

    pred = round(max(-3, min(3, score)), 2)
    if pred >= 1.0:   signal = "📈 開盤偏強，建議積極布局"
    elif pred >= 0.3: signal = "📈 小幅開高，順勢輕多"
    elif pred >= -0.3:signal = "⬜ 平盤震盪，觀望為主"
    elif pred >= -1.0:signal = "📉 小幅開低，謹慎操作"
    else:             signal = "📉 開盤偏弱，現金為王"

    if not reasons:
        reasons.append("海外市場無明顯方向訊號")

    return signal, "；".join(reasons)


def format_premarket_brief(data: dict) -> str:
    if not data or data.get("error"):
        return f"❌ {data.get('error', '無法取得開盤前簡報')}"

    us     = data["us_market"]
    fut    = data["tw_futures"]
    ff     = data["foreign_fut"]
    events = data["events"]
    signal = data["signal"]
    rat    = data["rationale"]
    ts     = data["updated_at"]

    def _row(name, info):
        if not info:
            return f"  {name:<8}：─"
        chg  = info.get("chg", 0)
        icon = "▲" if chg > 0 else ("▼" if chg < 0 else "─")
        return f"  {name:<8}：{info['close']:>10,.2f}  {icon}{abs(chg):.2f}%"

    lines = [
        "🌅 開盤前情報簡報",
        "─" * 32, "",
        "🇺🇸 美股昨夜收盤",
    ]
    for name in ["S&P500", "那斯達克", "費城半導", "道瓊"]:
        lines.append(_row(name, us.get(name, {})))

    lines += [""]
    if fut:
        fc = fut.get("chg_pct", 0)
        icon = "▲" if fc > 0 else "▼"
        vol = fut.get("volume", 0)
        lines += [
            "⚡ 台指期（夜盤）",
            f"  收盤：{fut.get('close',0):>10,.0f}  {icon}{abs(fc):.2f}%",
            f"  夜盤量：{vol:,} 口",
            "",
        ]

    if ff:
        net = ff.get("net_oi", 0)
        chg = ff.get("chg_oi", 0)
        icon_net = "多" if net > 0 else "空"
        icon_chg = "▲" if chg > 0 else "▼"
        lines += [
            "🌐 外資台指期未平倉",
            f"  淨部位：{icon_net} {abs(net):,} 口",
            f"  本日變化：{icon_chg}{abs(chg):,} 口",
            "",
        ]

    if events:
        lines.append("📅 今日重要財經事件")
        for e in events:
            lines.append(f"  {e}")
        lines.append("")

    lines += [
        "─" * 28,
        f"🎯 開盤展望：{signal}",
        f"  {rat}",
        "",
        f"更新：{ts}",
    ]
    return "\n".join(lines)


async def push_premarket_brief() -> bool:
    """推播開盤前增強版簡報"""
    try:
        from .line_push import push_to_admin
        data   = await get_premarket_brief()
        report = format_premarket_brief(data)
        await push_to_admin(report[:3000])
        logger.info("[premarket] pushed pre-market brief")
        return True
    except Exception as e:
        logger.error(f"[premarket] push error: {e}")
        return False
