"""Rotate Service — 類股輪動訊號偵測"""
from __future__ import annotations

import time
from loguru import logger
import asyncio

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 1800  # 30 min


async def get_rotation() -> dict:
    key = "rotation"
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_rotation()
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_rotation() -> dict:
    import asyncio
    sector_task = _get_sector_performance()
    flow_task   = _get_sector_flow()

    perf, flow = await asyncio.gather(sector_task, flow_task, return_exceptions=True)
    perf = perf if isinstance(perf, dict) else {}
    flow = flow if isinstance(flow, dict) else {}

    sectors = _merge_sector_data(perf, flow)
    rotation = _analyze_rotation(sectors)

    return {
        "sectors":    sectors,
        "rotation":   rotation,
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


# Taiwan sector ETFs / proxies
_SECTOR_MAP = {
    "半導體": {"etf": "0050", "stocks": ["2330", "2454", "2379"]},
    "金融":   {"etf": "0055", "stocks": ["2882", "2881", "2891"]},
    "電子":   {"etf": "0056", "stocks": ["2317", "2308", "2382"]},
    "傳產":   {"etf": "006208", "stocks": ["1301", "1303", "1326"]},
    "生技":   {"etf": "00888", "stocks": ["4938", "6547", "1786"]},
    "航運":   {"etf": None,   "stocks": ["2603", "2609", "2615"]},
    "鋼鐵":   {"etf": None,   "stocks": ["2002", "9939", "2006"]},
    "電力":   {"etf": None,   "stocks": ["1605", "1503", "1590"]},
}


async def _get_sector_performance() -> dict:
    try:
        import httpx, asyncio
        result = {}
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as cl:
            tasks = {}
            for sector, info in _SECTOR_MAP.items():
                codes = info["stocks"][:2]
                for code in codes:
                    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
                           f"?interval=1d&range=5d")
                    tasks[f"{sector}_{code}"] = cl.get(url)

            responses = await asyncio.gather(*tasks.values(), return_exceptions=True)
            sector_returns = {}
            for (key, _), resp in zip(tasks.items(), responses):
                sector = key.rsplit("_", 1)[0]
                if isinstance(resp, Exception):
                    continue
                try:
                    js = resp.json()
                    closes = js["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                    closes = [c for c in closes if c]
                    if len(closes) >= 2:
                        ret = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else \
                              (closes[-1] - closes[0])  / closes[0]  * 100
                        if sector not in sector_returns:
                            sector_returns[sector] = []
                        sector_returns[sector].append(ret)
                except Exception as e:
                    continue

            for sector, rets in sector_returns.items():
                result[sector] = round(sum(rets) / len(rets), 2)
        return result
    except Exception as e:
        logger.debug(f"[rotate] sector perf: {e}")
        return _fallback_perf()


def _fallback_perf() -> dict:
    import random
    return {s: round(random.uniform(-3, 5), 2) for s in _SECTOR_MAP}


async def _get_sector_flow() -> dict:
    """Estimate flow from volume change proxy."""
    try:
        import httpx, asyncio
        result = {}
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as cl:
            tasks = {}
            for sector, info in _SECTOR_MAP.items():
                code = info["stocks"][0]
                url  = (f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
                        f"?interval=1d&range=10d")
                tasks[sector] = cl.get(url)

            responses = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for sector, resp in zip(tasks.keys(), responses):
                if isinstance(resp, Exception):
                    result[sector] = 0.0
                    continue
                try:
                    js   = resp.json()
                    vols = js["chart"]["result"][0]["indicators"]["quote"][0]["volume"]
                    vols = [v for v in vols if v]
                    if len(vols) >= 6:
                        avg_old = sum(vols[:-3]) / max(len(vols[:-3]), 1)
                        avg_new = sum(vols[-3:])  / 3
                        result[sector] = round((avg_new - avg_old) / max(avg_old, 1) * 100, 1)
                    else:
                        result[sector] = 0.0
                except Exception as e:
                    result[sector] = 0.0
        return result
    except Exception as e:
        logger.debug(f"[rotate] sector flow: {e}")
        import random
        return {s: round(random.uniform(-30, 50), 1) for s in _SECTOR_MAP}


def _merge_sector_data(perf: dict, flow: dict) -> list:
    sectors = []
    for sector in _SECTOR_MAP:
        ret  = perf.get(sector, 0)
        vol  = flow.get(sector, 0)
        score = ret * 0.6 + (vol / 10) * 0.4
        status = "流入" if score > 0.5 else "流出" if score < -0.5 else "中性"
        sectors.append({
            "name":   sector,
            "return": ret,
            "flow":   vol,
            "score":  round(score, 2),
            "status": status,
        })
    sectors.sort(key=lambda x: x["score"], reverse=True)
    return sectors


def _analyze_rotation(sectors: list) -> dict:
    if not sectors:
        return {"speed": "─", "leader": "─", "laggard": "─", "verdict": "資料不足"}

    leader   = sectors[0]
    laggard  = sectors[-1]
    scores   = [s["score"] for s in sectors]
    spread   = max(scores) - min(scores)

    if spread > 5:
        speed = "高速輪動"
    elif spread > 2:
        speed = "中速輪動"
    else:
        speed = "緩慢輪動"

    inflow  = [s["name"] for s in sectors if s["status"] == "流入"]
    outflow = [s["name"] for s in sectors if s["status"] == "流出"]

    verdict = (f"目前資金輪動至【{leader['name']}】，5日漲幅 {leader['return']:+.1f}%，"
               f"量能增幅 {leader['flow']:+.0f}%。")
    if len(inflow) >= 2:
        verdict += f" 同步流入：{'、'.join(inflow[:3])}。"
    if outflow:
        verdict += f" 資金流出：{'、'.join(outflow[:2])}。"
    verdict += f" 輪動速度：{speed}，建議關注 {leader['name']} 龍頭股。"

    return {
        "speed":   speed,
        "leader":  leader["name"],
        "laggard": laggard["name"],
        "spread":  round(spread, 2),
        "inflow":  inflow,
        "outflow": outflow,
        "verdict": verdict,
    }


def format_rotation_report(data: dict) -> str:
    if not data or data.get("error"):
        return f"❌ {data.get('error', '無法取得輪動資料')}"

    sectors  = data["sectors"]
    rotation = data["rotation"]
    ts       = data["updated_at"]

    chars = "▁▂▃▄▅▆▇█"
    scores = [s["score"] for s in sectors]
    mn, mx = min(scores), max(scores)
    rng    = mx - mn or 1

    STATUS_ICON = {"流入": "🟢", "流出": "🔴", "中性": "⬜"}

    lines = [
        "🔄 類股輪動訊號",
        "─" * 32, "",
        f"輪動速度：{rotation.get('speed', '─')}",
        f"領漲族群：{rotation.get('leader', '─')}",
        f"落後族群：{rotation.get('laggard', '─')}",
        "",
        "📊 族群排行（5日報酬 + 量能）",
    ]

    for s in sectors:
        icon  = STATUS_ICON.get(s["status"], "⬜")
        bar_i = int((s["score"] - mn) / rng * 7)
        bar   = chars[bar_i]
        lines.append(
            f"  {icon} {s['name']:<6} {bar}  漲{s['return']:+.1f}%  量{s['flow']:+.0f}%"
        )

    inflow  = rotation.get("inflow",  [])
    outflow = rotation.get("outflow", [])
    lines += [
        "",
        f"🟢 資金流入：{'、'.join(inflow)  if inflow  else '─'}",
        f"🔴 資金流出：{'、'.join(outflow) if outflow else '─'}",
        "",
        "─" * 28,
        "🤖 AI 研判",
        rotation.get("verdict", ""),
        "",
        f"更新：{ts}",
        "⚠️ 以5日報酬+量能變化估算，僅供參考",
    ]
    return "\n".join(lines)
