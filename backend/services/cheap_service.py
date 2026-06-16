"""Cheap Service — 個股便宜/合理/昂貴評估（PE / 52週位階）"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 3600 * 4  # 4 hours

# Peer PE reference table
_PEER_PE: dict[str, dict] = {
    "2330": {"peers": ["2303", "2454"], "sector": "半導體",   "sector_avg_pe": 18},
    "2454": {"peers": ["2303", "2330"], "sector": "半導體",   "sector_avg_pe": 18},
    "2317": {"peers": ["2382", "4938"], "sector": "電子製造", "sector_avg_pe": 14},
    "2382": {"peers": ["2317", "6669"], "sector": "伺服器",   "sector_avg_pe": 16},
    "2881": {"peers": ["2882", "2886"], "sector": "金融",     "sector_avg_pe": 12},
    "2882": {"peers": ["2881", "2886"], "sector": "金融",     "sector_avg_pe": 12},
}
_DEFAULT_SECTOR_PE = 15


async def get_cheap(code: str) -> dict:
    """評估個股目前是否便宜，回傳 PE / PB / 52週位階 / 評級。"""
    now = time.time()
    if code in _cache and now - _cache_ts.get(code, 0) < _TTL:
        return _cache[code]

    result = await _fetch_cheap(code)
    _cache[code] = result
    _cache_ts[code] = now
    return result


async def _fetch_cheap(code: str) -> dict:
    import asyncio
    import httpx

    headers = {"User-Agent": "Mozilla/5.0"}
    ticker  = f"{code}.TW"

    async def _fetch_summary(client: httpx.AsyncClient) -> dict:
        url    = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
        params = {"modules": "defaultKeyStatistics,summaryDetail"}
        try:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"[cheap] quoteSummary fetch failed for {code}: {e}")
            return {}

    async def _fetch_history(client: httpx.AsyncClient) -> dict:
        url    = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"interval": "1wk", "range": "1y"}
        try:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"[cheap] history fetch failed for {code}: {e}")
            return {}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            summary_raw, history_raw = await asyncio.gather(
                _fetch_summary(client),
                _fetch_history(client),
            )
    except Exception as e:
        logger.exception(f"[cheap] AsyncClient error for {code}: {e}")
        return _empty_cheap(code)

    # --- Parse quoteSummary ---
    pe_trailing:  float | None = None
    pe_forward:   float | None = None
    pb:           float | None = None
    current_price: float | None = None

    try:
        res_block    = summary_raw.get("quoteSummary", {}).get("result", [{}])[0] or {}
        key_stats    = res_block.get("defaultKeyStatistics", {})
        summary_det  = res_block.get("summaryDetail", {})

        def _raw(d: dict, key: str) -> float | None:
            v = d.get(key)
            if isinstance(v, dict):
                v = v.get("raw")
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        pe_trailing   = _raw(summary_det, "trailingPE")
        pe_forward    = _raw(key_stats,   "forwardPE")
        pb            = _raw(key_stats,   "priceToBook")
        current_price = _raw(summary_det, "regularMarketPrice") or _raw(summary_det, "previousClose")
    except Exception as e:
        logger.warning(f"[cheap] summary parse error for {code}: {e}")

    # --- Parse 1-year weekly price history ---
    wk52_high:   float | None = None
    wk52_low:    float | None = None
    price_percentile: float | None = None

    try:
        result_block = history_raw.get("chart", {}).get("result", [None])[0]
        if result_block:
            q      = result_block.get("indicators", {}).get("quote", [{}])[0]
            closes = [float(v) for v in q.get("close", []) if v is not None]
            hi     = [float(v) for v in q.get("high", [])  if v is not None]
            lo     = [float(v) for v in q.get("low", [])   if v is not None]
            if hi:
                wk52_high = max(hi)
            if lo:
                wk52_low  = min(lo)
            # Use last weekly close as current price if summary didn't give one
            if closes and current_price is None:
                current_price = closes[-1]
    except Exception as e:
        logger.warning(f"[cheap] history parse error for {code}: {e}")

    if current_price and wk52_high is not None and wk52_low is not None:
        rng = wk52_high - wk52_low
        price_percentile = round((current_price - wk52_low) / rng * 100, 1) if rng > 0 else 50.0
    else:
        price_percentile = None

    # --- Peer PE lookup ---
    peer_info      = _PEER_PE.get(code, {})
    sector         = peer_info.get("sector", "一般")
    sector_avg_pe  = peer_info.get("sector_avg_pe", _DEFAULT_SECTOR_PE)

    # --- PE Verdict ---
    pe_to_use = pe_trailing or pe_forward
    if pe_to_use is None:
        pe_verdict = "無PE資料"
    elif pe_to_use < sector_avg_pe * 0.8:
        pe_verdict = "便宜"
    elif pe_to_use < sector_avg_pe * 1.2:
        pe_verdict = "合理"
    else:
        pe_verdict = "昂貴"

    # --- Percentile Verdict ---
    if price_percentile is None:
        percentile_verdict = "無位階資料"
    elif price_percentile < 20:
        percentile_verdict = "歷史低位"
    elif price_percentile < 40:
        percentile_verdict = "偏低位置"
    elif price_percentile < 60:
        percentile_verdict = "中間位置"
    elif price_percentile < 80:
        percentile_verdict = "偏高位置"
    else:
        percentile_verdict = "歷史高位"

    # --- Overall Verdict ---
    if pe_verdict == "便宜" and (price_percentile is None or price_percentile < 50):
        overall_verdict = "便宜"
    elif pe_verdict == "昂貴" and (price_percentile is None or price_percentile > 60):
        overall_verdict = "昂貴"
    elif pe_verdict in ("無PE資料",):
        overall_verdict = percentile_verdict if price_percentile is not None else "資料不足"
    else:
        overall_verdict = "合理"

    return {
        "code":             code,
        "price":            round(current_price, 2) if current_price is not None else None,
        "pe_trailing":      round(pe_trailing,   2) if pe_trailing  is not None else None,
        "pe_forward":       round(pe_forward,    2) if pe_forward   is not None else None,
        "pb":               round(pb,            2) if pb           is not None else None,
        "wk52_high":        round(wk52_high,     2) if wk52_high    is not None else None,
        "wk52_low":         round(wk52_low,      2) if wk52_low     is not None else None,
        "price_percentile": price_percentile,
        "sector":           sector,
        "sector_avg_pe":    sector_avg_pe,
        "pe_verdict":       pe_verdict,
        "percentile_verdict": percentile_verdict,
        "overall_verdict":  overall_verdict,
        "updated_at":       time.strftime("%Y-%m-%d %H:%M"),
        "error":            None,
    }


def _empty_cheap(code: str, error: str = "no data") -> dict:
    return {
        "code":             code,
        "price":            None,
        "pe_trailing":      None,
        "pe_forward":       None,
        "pb":               None,
        "wk52_high":        None,
        "wk52_low":         None,
        "price_percentile": None,
        "sector":           "—",
        "sector_avg_pe":    _DEFAULT_SECTOR_PE,
        "pe_verdict":       "無資料",
        "percentile_verdict": "無資料",
        "overall_verdict":  "無資料",
        "updated_at":       time.strftime("%Y-%m-%d %H:%M"),
        "error":            error,
    }


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

def format_cheap_report(data: dict, code: str) -> str:
    if data.get("error") and data["price"] is None:
        return f"💰 [{code}] 估值分析\n⚠️ 無法取得資料，請稍後再試。"

    overall = data.get("overall_verdict", "合理")
    verdict_icon = {"便宜": "🟢", "合理": "🟡", "昂貴": "🔴"}.get(overall, "⚪")

    # 52-week percentile bar
    pct = data.get("price_percentile")
    if pct is not None:
        filled  = round(pct / 10)
        pct_bar = "█" * filled + "░" * (10 - filled) + f"  {pct:.0f}%"
        pct_str = f"{pct:.0f}%（{data.get('percentile_verdict', '—')}）"
    else:
        pct_bar = "—"
        pct_str = "—"

    # PE comparison
    pe_t = data.get("pe_trailing")
    pe_f = data.get("pe_forward")
    pe_display = (
        f"本益比(TTM)：{pe_t if pe_t is not None else '—'} "
        f"| 預估(Fwd)：{pe_f if pe_f is not None else '—'}"
    )
    sector_pe_display = (
        f"產業平均：{data.get('sector_avg_pe', '—')} → "
        f"評級：{data.get('pe_verdict', '—')}"
    )

    lines = [
        f"💰 [{code}] 估值分析",
        f"━━━━━━━━━━━━━━━━━━",
        f"📌 現價：{data.get('price', '—')}　產業：{data.get('sector', '—')}",
        f"",
        f"【本益比分析】",
        f"  {pe_display}",
        f"  {sector_pe_display}",
        f"  股價淨值比(PB)：{data.get('pb', '—')}",
        f"",
        f"【52週位階】",
        f"  最高：{data.get('wk52_high', '—')}　最低：{data.get('wk52_low', '—')}",
        f"  位階：{pct_str}",
        f"  {pct_bar}",
        f"  ↑ 低 {'':>20} 高 ↑",
        f"",
        f"【綜合評級】",
        f"  {verdict_icon} {overall}",
        f"",
        f"🕐 更新：{data.get('updated_at', '—')}",
    ]
    return "\n".join(lines)
