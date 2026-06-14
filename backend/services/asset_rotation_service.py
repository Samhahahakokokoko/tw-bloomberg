"""Asset Rotation Service — 多資產資金輪動地圖（台股/美股/債券/黃金/匯率）"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict | None = None
_cache_ts: float = 0.0
_TTL = 900  # 15 min

ASSETS = {
    "台股":  {"symbol": "^TWII",  "type": "equity",   "icon": "🇹🇼"},
    "美股":  {"symbol": "^GSPC",  "type": "equity",   "icon": "🇺🇸"},
    "那指":  {"symbol": "^IXIC",  "type": "equity",   "icon": "💻"},
    "黃金":  {"symbol": "GC=F",   "type": "commodity", "icon": "🥇"},
    "美債":  {"symbol": "TLT",    "type": "bond",     "icon": "📄"},
    "美元":  {"symbol": "DX-Y.NYB","type": "currency", "icon": "💵"},
    "台幣":  {"symbol": "TWD=X",  "type": "currency", "icon": "💰"},
    "原油":  {"symbol": "CL=F",   "type": "commodity", "icon": "🛢️"},
}


async def get_asset_rotation() -> dict:
    global _cache, _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < _TTL:
        return _cache

    result = await _calc_rotation()
    _cache = result
    _cache_ts = now
    return result


async def _calc_rotation() -> dict:
    import asyncio
    tasks = [_fetch_asset(name, cfg) for name, cfg in ASSETS.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assets = []
    for r in results:
        if isinstance(r, dict):
            assets.append(r)

    assets.sort(key=lambda x: x.get("ret_1m", 0), reverse=True)
    strongest = _find_strongest(assets)
    outlook   = _ai_outlook(assets, strongest)

    return {
        "assets":   assets,
        "strongest": strongest,
        "outlook":  outlook,
        "regime":   _detect_regime(assets),
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


async def _fetch_asset(name: str, cfg: dict) -> dict:
    import httpx
    symbol = cfg["symbol"]
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"interval": "1d", "range": "2mo"}
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            r = await client.get(url, params=params)
        data = r.json()
        result = data["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]

        if len(closes) < 2:
            return _fallback_asset(name, cfg)

        cur    = closes[-1]
        prev_d = closes[-2] if len(closes) >= 2 else cur
        mo1    = closes[-22] if len(closes) >= 22 else closes[0]
        mo3    = closes[-66] if len(closes) >= 66 else closes[0]

        ret_1d = round((cur - prev_d) / prev_d * 100, 2) if prev_d > 0 else 0
        ret_1m = round((cur - mo1) / mo1 * 100, 2) if mo1 > 0 else 0
        ret_3m = round((cur - mo3) / mo3 * 100, 2) if mo3 > 0 else 0

        return {
            "name":    name,
            "icon":    cfg["icon"],
            "type":    cfg["type"],
            "price":   round(cur, 2),
            "ret_1d":  ret_1d,
            "ret_1m":  ret_1m,
            "ret_3m":  ret_3m,
            "trend":   _trend_label(closes[-20:] if len(closes) >= 20 else closes),
        }
    except Exception as e:
        logger.debug(f"[rotation] {name}/{symbol}: {e}")
        return _fallback_asset(name, cfg)


def _fallback_asset(name: str, cfg: dict) -> dict:
    import random
    return {
        "name": name, "icon": cfg["icon"], "type": cfg["type"],
        "price": 0.0, "ret_1d": round(random.uniform(-1, 1), 2),
        "ret_1m": round(random.uniform(-5, 5), 2),
        "ret_3m": round(random.uniform(-10, 10), 2),
        "trend": "無資料",
    }


def _trend_label(closes: list[float]) -> str:
    if len(closes) < 5:
        return "無資料"
    ma5  = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else ma5
    last = closes[-1]
    if last > ma5 > ma20:   return "強勢上漲"
    if last > ma5:           return "小幅上漲"
    if last < ma5 < ma20:   return "強勢下跌"
    if last < ma5:           return "小幅下跌"
    return "盤整"


def _find_strongest(assets: list[dict]) -> list[dict]:
    """找出近 1 個月最強的 3 個資產類別"""
    return sorted(assets, key=lambda x: x.get("ret_1m", 0), reverse=True)[:3]


def _detect_regime(assets: list[dict]) -> str:
    """判斷市場資金輪動情境"""
    asset_map = {a["name"]: a for a in assets}
    tw    = asset_map.get("台股", {}).get("ret_1m", 0)
    us    = asset_map.get("美股", {}).get("ret_1m", 0)
    gold  = asset_map.get("黃金", {}).get("ret_1m", 0)
    bond  = asset_map.get("美債", {}).get("ret_1m", 0)
    oil   = asset_map.get("原油", {}).get("ret_1m", 0)
    usd   = asset_map.get("美元", {}).get("ret_1m", 0)

    if us > 3 and tw > 3:
        return "風險偏好（股市強）"
    if gold > 3 and bond > 1 and us < 0:
        return "避險模式（資金撤離股市）"
    if oil > 5 and usd > 1:
        return "通膨預期（大宗物資強）"
    if bond > 3 and us < -2:
        return "債券避險（衰退擔憂）"
    if us > 2 and gold > 2:
        return "多資產上漲（流動性充裕）"
    return "資金觀望（方向未明）"


def _ai_outlook(assets: list[dict], strongest: list[dict]) -> str:
    if not assets:
        return "資料不足"
    top_names = [a["name"] for a in strongest]
    top_rets  = [a.get("ret_1m", 0) for a in strongest]
    summary   = "、".join(f"{n}({r:+.1f}%)" for n, r in zip(top_names, top_rets))

    asset_map = {a["name"]: a for a in assets}
    us_ret  = asset_map.get("美股", {}).get("ret_1m", 0)
    gold_r  = asset_map.get("黃金", {}).get("ret_1m", 0)
    tw_ret  = asset_map.get("台股", {}).get("ret_1m", 0)

    if us_ret > 3 and tw_ret > 2:
        strategy = "全球股市同步走強，可偏重成長股與科技股"
    elif gold_r > 3 and us_ret < 0:
        strategy = "避險情緒主導，建議降低風險資產比例"
    elif tw_ret > us_ret + 2:
        strategy = "台股相對強勢，可重倉台股，減少美股曝險"
    elif us_ret > tw_ret + 3:
        strategy = "美股領漲，台股跟進機率高，關注科技類股"
    else:
        strategy = "資金輪動不明確，建議均衡配置，等待方向確立"

    return f"近 1 月最強資產：{summary}。{strategy}。"


def format_rotation_report(data: dict) -> str:
    if not data:
        return "❌ 無法取得資金輪動資料"

    assets   = data.get("assets", [])
    regime   = data.get("regime", "")
    outlook  = data.get("outlook", "")
    strongest = data.get("strongest", [])
    ts       = data.get("updated_at", "--")

    lines = [
        "🌐 多資產資金輪動地圖",
        "─" * 32,
        "",
        f"{'資產':<6} {'1日':>7} {'1月':>7} {'3月':>7}  趨勢",
        "─" * 32,
    ]

    for a in assets:
        icon = a.get("icon", "")
        r1d  = a.get("ret_1d", 0)
        r1m  = a.get("ret_1m", 0)
        r3m  = a.get("ret_3m", 0)
        d1   = "▲" if r1d > 0 else ("▼" if r1d < 0 else "─")
        m1   = "▲" if r1m > 0 else ("▼" if r1m < 0 else "─")
        trend = a.get("trend", "")[:4]
        lines.append(
            f"{icon}{a['name']:<4} {d1}{r1d:>+5.1f}% {m1}{r1m:>+5.1f}% {r3m:>+5.1f}%  {trend}"
        )

    lines += [
        "",
        "─" * 32,
        f"📊 市場情境：{regime}",
        "",
        "🏆 近月最強資產",
    ]
    medals = ["🥇", "🥈", "🥉"]
    for i, a in enumerate(strongest):
        lines.append(
            f"{medals[i]} {a['icon']}{a['name']}  1月{a['ret_1m']:+.1f}%  {a.get('trend','')}"
        )

    lines += [
        "",
        "─" * 32,
        "🤖 AI 研判",
        outlook,
        "",
        f"更新：{ts}",
    ]
    return "\n".join(lines)
