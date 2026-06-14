"""Sector Flow Service — 產業資金流向文字熱力圖（LINE 適用）"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict | None = None
_cache_ts: float = 0.0
_TTL = 300  # 5 min

SECTOR_STOCKS: dict[str, list[str]] = {
    "半導體":   ["2330", "2303", "2344", "3711"],
    "IC設計":   ["2454", "3034", "4966", "6770"],
    "AI伺服器": ["3231", "6669", "4938", "3017"],
    "散熱電源": ["3443", "1590", "6285", "2313"],
    "PCB":      ["2382", "3037", "6269", "3376"],
    "電動車":   ["1590", "6223", "8054", "2355"],
    "金融":     ["2882", "2881", "2891", "2886"],
    "航運":     ["2603", "2609", "2615", "2623"],
    "生技":     ["4763", "6548", "4174", "1736"],
    "通訊":     ["2412", "3045", "4977", "6277"],
    "傳產鋼鐵": ["2002", "2006", "9910", "1301"],
    "電商零售": ["2912", "8046", "6183", "3088"],
}


async def get_sector_flow() -> dict:
    global _cache, _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < _TTL:
        return _cache

    result = await _calc_sector_flow()
    _cache = result
    _cache_ts = now
    return result


async def _calc_sector_flow() -> dict:
    import asyncio
    from .twse_service import fetch_realtime_quote

    async def _sector_score(sector: str, codes: list[str]) -> dict:
        tasks = [_safe_quote(c) for c in codes]
        quotes = await asyncio.gather(*tasks, return_exceptions=True)
        quotes = [q for q in quotes if isinstance(q, dict) and q]

        changes = [float(q.get("change_pct") or q.get("change_percent") or 0) for q in quotes]
        volumes = [float(q.get("volume") or q.get("trade_volume") or 0) for q in quotes]

        avg_chg  = sum(changes) / len(changes) if changes else 0.0
        tot_vol  = sum(volumes) if volumes else 0.0
        # score: combine price change + volume strength
        score = avg_chg * 0.6 + min(tot_vol / 1e7, 10) * 0.4

        return {
            "sector":   sector,
            "avg_chg":  round(avg_chg, 2),
            "volume":   round(tot_vol / 1e8, 2),  # 億元
            "score":    round(score, 2),
            "stocks":   len(quotes),
        }

    async def _safe_quote(code: str) -> dict:
        try:
            from .twse_service import fetch_realtime_quote
            return await fetch_realtime_quote(code) or {}
        except Exception:
            return {}

    tasks = [_sector_score(sec, codes) for sec, codes in SECTOR_STOCKS.items()]
    sectors = await asyncio.gather(*tasks, return_exceptions=True)
    sectors = [s for s in sectors if isinstance(s, dict)]
    sectors.sort(key=lambda x: x["score"], reverse=True)

    top3 = sectors[:3]
    return {
        "sectors":   sectors,
        "top3":      top3,
        "updated_at": time.strftime("%H:%M"),
    }


def format_sector_flow_text(data: dict) -> str:
    sectors = data.get("sectors", [])
    top3    = data.get("top3", [])
    updated = data.get("updated_at", "--")

    if not sectors:
        return "❌ 無法取得產業資料"

    # block-bar symbols
    def _bar(score: float) -> str:
        BAR = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
        mx = max((abs(s["score"]) for s in sectors), default=1) or 1
        idx = int((score + mx) / (2 * mx) * (len(BAR) - 1))
        idx = max(0, min(len(BAR) - 1, idx))
        return BAR[idx]

    def _icon(chg: float) -> str:
        if chg >= 2:  return "🔥"
        if chg >= 0.5: return "🟢"
        if chg > -0.5: return "⬜"
        if chg > -2:  return "🔴"
        return "❄️"

    lines = [
        f"📊 產業籌碼地圖 {updated}",
        "─" * 32,
        "",
    ]

    # 每行 2 個產業
    for i in range(0, len(sectors), 2):
        row = sectors[i:i+2]
        parts = []
        for s in row:
            bar  = _bar(s["score"])
            icon = _icon(s["avg_chg"])
            chg  = f"{s['avg_chg']:+.1f}%"
            vol  = f"{s['volume']:.1f}億"
            parts.append(f"{icon}{bar} {s['sector']:<5} {chg} {vol}")
        lines.append("  |  ".join(parts))

    lines += [
        "",
        "─" * 32,
        "🏆 今日資金集中前三名",
    ]
    for i, s in enumerate(top3, 1):
        medal = ["🥇", "🥈", "🥉"][i - 1]
        lines.append(
            f"{medal} {s['sector']}  {s['avg_chg']:+.1f}%  "
            f"成交 {s['volume']:.1f}億"
        )

    lines += [
        "",
        "▁=弱  ▄=中  █=強",
        "🔥強漲  🟢小漲  ⬜平盤  🔴小跌  ❄️強跌",
    ]
    return "\n".join(lines)
