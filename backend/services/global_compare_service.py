"""global_compare_service.py — 台股與全球市場 YTD 比較"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 3600 * 2  # 2 hours

_MARKETS = {
    "台股加權": "^TWII",
    "S&P500":   "^GSPC",
    "那斯達克":  "^IXIC",
    "費城半導":  "^SOX",
    "日經225":   "^N225",
    "韓國綜合":  "^KS11",
    "恆生指數":  "^HSI",
    "上証指數":  "000001.SS",
}

_MARKET_PE: dict[str, float] = {
    "台股加權": 0.0,   # fetched dynamically
    "S&P500":  22.0,
    "那斯達克": 35.0,
    "日經225":  18.0,
    "韓國綜合": 11.0,
    "恆生指數": 9.0,
}

_FOREIGN_OWNERSHIP: dict[str, float] = {
    "2026": 42.5,
    "2025": 43.1,
    "2024": 41.8,
    "2023": 40.2,
    "2022": 38.5,
}


# ── 快取包裝 ──────────────────────────────────────────────────────────────────

async def get_global_compare() -> dict:
    """取得全球市場比較，TTL=2 小時快取。"""
    key = "global_compare"
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_global_compare()
    _cache[key] = result
    _cache_ts[key] = now
    return result


# ── 核心抓取 ──────────────────────────────────────────────────────────────────

async def _fetch_global_compare() -> dict:
    import asyncio
    import httpx

    headers = {"User-Agent": "Mozilla/5.0"}

    # ── 1. Fetch YTD data for all markets in parallel ────────────────────────
    async def _fetch_ytd(name: str, symbol: str, cl: httpx.AsyncClient) -> dict:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?range=ytd&interval=1mo"
        )
        try:
            resp = await cl.get(url)
            resp.raise_for_status()
            js = resp.json()
            result0  = js["chart"]["result"][0]
            closes   = result0["indicators"]["quote"][0].get("close", [])
            closes   = [c for c in closes if c is not None]
            if len(closes) < 2:
                return {"name": name, "symbol": symbol, "ytd_ret": 0.0, "current": 0.0}
            first   = closes[0]
            current = closes[-1]
            ytd_ret = (current - first) / first * 100 if first > 0 else 0.0
            return {
                "name":    name,
                "symbol":  symbol,
                "ytd_ret": round(ytd_ret, 2),
                "current": round(current, 2),
            }
        except Exception as e:
            logger.debug("[global_compare] {} {}: {}", name, symbol, e)
            return {"name": name, "symbol": symbol, "ytd_ret": 0.0, "current": 0.0}

    async with httpx.AsyncClient(timeout=15, headers=headers) as cl:
        tasks = [_fetch_ytd(n, s, cl) for n, s in _MARKETS.items()]
        market_results: list[dict] = await asyncio.gather(*tasks, return_exceptions=False)

    # ── 2. Try to fetch TWII trailing PE via 0050.TW quoteSummary ────────────
    tw_pe = 0.0
    try:
        async with httpx.AsyncClient(timeout=10, headers=headers) as cl:
            url_pe = (
                "https://query1.finance.yahoo.com/v10/finance/quoteSummary/0050.TW"
                "?modules=summaryDetail"
            )
            r = await cl.get(url_pe)
            r.raise_for_status()
            sd = r.json()["quoteSummary"]["result"][0]["summaryDetail"]
            tw_pe = float(sd.get("trailingPE", {}).get("raw", 0) or 0)
    except Exception as e:
        logger.debug("[global_compare] TWII PE fetch failed: {}", e)

    # Build final PE map
    pe_map = dict(_MARKET_PE)
    pe_map["台股加權"] = tw_pe if tw_pe > 0 else 18.0  # sane fallback

    # ── 3. Rank by YTD return (descending) ───────────────────────────────────
    sorted_markets = sorted(market_results, key=lambda x: x["ytd_ret"], reverse=True)
    tw_rank = next(
        (i + 1 for i, m in enumerate(sorted_markets) if m["name"] == "台股加權"),
        len(sorted_markets),
    )

    # ── 4. Global average PE (markets that have PE data) ─────────────────────
    pe_vals = [v for v in pe_map.values() if v > 0]
    global_avg_pe = round(sum(pe_vals) / len(pe_vals), 1) if pe_vals else 0.0

    # ── 5. Foreign ownership (current year) ──────────────────────────────────
    import datetime as _dt
    cur_year = str(_dt.date.today().year)
    foreign_ownership_pct = _FOREIGN_OWNERSHIP.get(cur_year, list(_FOREIGN_OWNERSHIP.values())[-1])

    data = {
        "markets":             sorted_markets,
        "tw_vs_world_rank":    tw_rank,
        "global_avg_pe":       global_avg_pe,
        "tw_pe":               round(pe_map.get("台股加權", 0.0), 1),
        "pe_map":              pe_map,
        "foreign_ownership_pct": foreign_ownership_pct,
        "foreign_ownership_history": _FOREIGN_OWNERSHIP,
        "updated_at":          time.strftime("%Y-%m-%d %H:%M"),
    }
    data["verdict"] = _gen_global_verdict(data)
    return data


# ── 結論生成 ──────────────────────────────────────────────────────────────────

def _gen_global_verdict(data: dict) -> str:
    rank     = data.get("tw_vs_world_rank", 4)
    n        = len(data.get("markets", [_MARKETS]))
    tw_pe    = data.get("tw_pe", 0)
    avg_pe   = data.get("global_avg_pe", 20)
    fo_pct   = data.get("foreign_ownership_pct", 42)

    parts: list[str] = []
    if rank <= 3:
        parts.append(f"台股今年表現強勢，排名全球第 {rank} / {n}，資金動能充沛")
    elif rank >= 6:
        parts.append(f"台股相對落後，排名全球第 {rank} / {n}，需留意資金外流風險")
    else:
        parts.append(f"台股表現中性，排名全球第 {rank} / {n}")

    if tw_pe > 0 and avg_pe > 0:
        if tw_pe < avg_pe * 0.85:
            parts.append(f"估值偏低（本益比 {tw_pe:.1f}x vs 全球均 {avg_pe:.1f}x），具補漲潛力")
        elif tw_pe > avg_pe * 1.15:
            parts.append(f"估值偏高（本益比 {tw_pe:.1f}x vs 全球均 {avg_pe:.1f}x），需謹慎追價")
        else:
            parts.append(f"估值合理（本益比 {tw_pe:.1f}x vs 全球均 {avg_pe:.1f}x）")

    if fo_pct >= 42:
        parts.append(f"外資持股比例 {fo_pct:.1f}%，外資仍為主力")
    else:
        parts.append(f"外資持股比例 {fo_pct:.1f}%，較高峰略有下降")

    return "；".join(parts) + "。"


# ── 報告格式化 ────────────────────────────────────────────────────────────────

def format_global_compare_report(data: dict) -> str:
    markets   = data.get("markets", [])
    tw_rank   = data.get("tw_vs_world_rank", "-")
    tw_pe     = data.get("tw_pe", 0.0)
    avg_pe    = data.get("global_avg_pe", 0.0)
    fo_pct    = data.get("foreign_ownership_pct", 0.0)
    fo_hist   = data.get("foreign_ownership_history", {})
    pe_map    = data.get("pe_map", {})

    lines = [
        "🌍 全球市場 YTD 比較",
        "─" * 30,
        "排名  市場        今年報酬",
    ]
    for i, m in enumerate(markets, 1):
        ytd = m["ytd_ret"]
        sign = "+" if ytd >= 0 else ""
        arrow = "▲" if ytd >= 0 else "▼"
        lines.append(
            f" {i:2d}.  {m['name']:<8}  {arrow} {sign}{ytd:.2f}%"
        )
    lines += [
        "",
        f"台股加權排名：第 {tw_rank} / {len(markets)} 名",
        "",
        "📊 本益比比較",
        "─" * 20,
    ]
    for market_name, pe_val in pe_map.items():
        if pe_val and pe_val > 0:
            lines.append(f"  {market_name:<8}  {pe_val:.1f}x")
    lines += [
        f"  全球均值   {avg_pe:.1f}x",
        "",
        "🏦 外資持股比例（台股）",
        "─" * 20,
    ]
    for yr in sorted(fo_hist.keys(), reverse=True):
        lines.append(f"  {yr}：{fo_hist[yr]:.1f}%")
    lines += [
        "",
        f"📋 AI 判斷：{data.get('verdict', '')}",
        "",
        f"更新：{data.get('updated_at', '')}",
    ]
    return "\n".join(lines)
