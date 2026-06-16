"""Market Review Service — 盤後深度覆盤（16:30 推播）"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: float = 0.0
_TTL = 3600 * 4  # 4 hours

# Sector ETF universe for strongest/weakest detection
_SECTOR_ETFS = {
    "0050.TW":  "台灣50（大盤）",
    "0052.TW":  "科技類股",
    "0055.TW":  "金融類股",
    "0051.TW":  "中型100",
}


async def get_market_review() -> dict:
    """回傳今日大盤深度覆盤資料（快取 4 小時）。"""
    global _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < _TTL:
        return _cache

    result = await _fetch_market_review()
    _cache.clear()
    _cache.update(result)
    _cache_ts = now
    return result


async def _fetch_market_review() -> dict:
    import asyncio
    import httpx

    headers = {"User-Agent": "Mozilla/5.0"}

    async def _fetch_ohlcv(symbol: str, client: httpx.AsyncClient) -> dict | None:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"interval": "1d", "range": "5d"}
        try:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            result_block = data["chart"]["result"][0]
            q = result_block["indicators"]["quote"][0]
            opens   = [v for v in q.get("open", [])   if v is not None]
            highs   = [v for v in q.get("high", [])   if v is not None]
            lows    = [v for v in q.get("low", [])    if v is not None]
            closes  = [v for v in q.get("close", [])  if v is not None]
            volumes = [v for v in q.get("volume", []) if v is not None]
            if not closes:
                return None
            return {
                "symbol":  symbol,
                "opens":   opens,
                "highs":   highs,
                "lows":    lows,
                "closes":  closes,
                "volumes": volumes,
            }
        except Exception as e:
            logger.warning(f"[market_review] fetch failed for {symbol}: {e}")
            return None

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            tasks = [_fetch_ohlcv(sym, client) for sym in ["^TWII"] + list(_SECTOR_ETFS.keys())]
            results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        logger.exception(f"[market_review] AsyncClient error: {e}")
        return _empty_review()

    # Parse TWII
    twii_data = results[0] if not isinstance(results[0], BaseException) else None
    if twii_data is None:
        logger.warning("[market_review] TWII data unavailable")
        return _empty_review()

    twii_closes  = twii_data["closes"]
    twii_opens   = twii_data["opens"]
    twii_highs   = twii_data["highs"]
    twii_lows    = twii_data["lows"]
    twii_volumes = twii_data["volumes"]

    if len(twii_closes) < 2:
        return _empty_review()

    today_open   = float(twii_opens[-1])
    today_close  = float(twii_closes[-1])
    today_high   = float(twii_highs[-1])
    today_low    = float(twii_lows[-1])
    prev_close   = float(twii_closes[-2])
    today_range  = round(today_high - today_low, 2)
    chg_pct      = round((today_close - prev_close) / prev_close * 100, 2) if prev_close else 0.0

    # Volume vs 5-day average
    today_vol   = float(twii_volumes[-1]) if twii_volumes else 0.0
    avg_vol_5d  = (sum(float(v) for v in twii_volumes[-5:]) / max(len(twii_volumes[-5:]), 1))
    vol_ratio   = round(today_vol / avg_vol_5d, 2) if avg_vol_5d else 1.0

    # Trend determination
    if chg_pct > 0.3:
        trend = "上漲"
    elif chg_pct < -0.3:
        trend = "下跌"
    else:
        trend = "平盤"

    # Parse sector ETFs
    sector_returns: dict[str, float] = {}
    etf_symbols = list(_SECTOR_ETFS.keys())
    for i, sym in enumerate(etf_symbols):
        raw = results[1 + i]
        if isinstance(raw, BaseException) or raw is None:
            continue
        etf_closes = raw["closes"]
        if len(etf_closes) >= 2:
            ret = (float(etf_closes[-1]) - float(etf_closes[-2])) / float(etf_closes[-2]) * 100
            sector_returns[sym] = round(ret, 2)

    if sector_returns:
        strongest_sym = max(sector_returns, key=lambda s: sector_returns[s])
        weakest_sym   = min(sector_returns, key=lambda s: sector_returns[s])
        strongest_sector = f"{_SECTOR_ETFS[strongest_sym]}（{sector_returns[strongest_sym]:+.2f}%）"
        weakest_sector   = f"{_SECTOR_ETFS[weakest_sym]}（{sector_returns[weakest_sym]:+.2f}%）"
    else:
        strongest_sector = "資料不足"
        weakest_sector   = "資料不足"

    # Institutional flow estimate
    if trend == "上漲" and vol_ratio > 1.1:
        inst_flow_est = "外資買超可能性高（放量上漲）"
    elif trend == "下跌" and vol_ratio > 1.1:
        inst_flow_est = "外資賣超可能性高（放量下跌）"
    elif trend == "上漲" and vol_ratio < 0.9:
        inst_flow_est = "縮量上漲，外資態度觀望，需留意力道不足"
    elif trend == "下跌" and vol_ratio < 0.9:
        inst_flow_est = "縮量下跌，賣壓尚未完全釋出，明日需觀察"
    else:
        inst_flow_est = "量能正常，法人動向中性"

    # Tomorrow watch
    body = today_close - today_open
    upper_shadow = today_high - max(today_close, today_open)
    lower_shadow = min(today_close, today_open) - today_low
    range_ = today_high - today_low if today_high != today_low else 1.0

    if body > 0 and upper_shadow / range_ < 0.3:
        tomorrow_watch = "今日紅K實體飽滿，明日延續上漲機率較高，可持多觀察。"
    elif upper_shadow / range_ > 0.5:
        tomorrow_watch = "上影線偏長（上漲力道受壓），有高檔反轉風險，注意明日開盤方向。"
    elif body < 0 and lower_shadow / range_ > 0.4:
        tomorrow_watch = "下影線支撐有效，可能出現技術性反彈，明日觀察量能配合。"
    else:
        tomorrow_watch = "今日走勢中性，明日方向取決於外資及期貨夜盤，盤前留意。"

    return {
        "twii_open":        round(today_open, 2),
        "twii_close":       round(today_close, 2),
        "twii_high":        round(today_high, 2),
        "twii_low":         round(today_low, 2),
        "today_range":      today_range,
        "chg_pct":          chg_pct,
        "vol_ratio":        vol_ratio,
        "trend":            trend,
        "strongest_sector": strongest_sector,
        "weakest_sector":   weakest_sector,
        "inst_flow_est":    inst_flow_est,
        "tomorrow_watch":   tomorrow_watch,
        "verdict":          _gen_review_verdict(chg_pct, vol_ratio, trend),
        "error":            None,
    }


def _gen_review_verdict(chg_pct: float, vol_ratio: float, trend: str) -> str:
    abs_chg = abs(chg_pct)
    vol_desc = "放量" if vol_ratio > 1.1 else ("縮量" if vol_ratio < 0.9 else "量能平穩")
    if trend == "上漲":
        strength = "強勢" if abs_chg > 1.0 else "溫和"
        return f"大盤{vol_desc}{strength}收漲 {chg_pct:+.2f}%，短線多頭格局延續，持股可續抱，留意高點壓力。"
    elif trend == "下跌":
        strength = "急殺" if abs_chg > 1.0 else "小幅"
        return f"大盤{vol_desc}{strength}收跌 {chg_pct:+.2f}%，短線偏弱，操作上宜降低持股比例或緊縮停損。"
    else:
        return f"大盤{vol_desc}收平（{chg_pct:+.2f}%），多空拉鋸，等待方向確認後再積極操作。"


def _empty_review() -> dict:
    return {
        "twii_open":        None,
        "twii_close":       None,
        "twii_high":        None,
        "twii_low":         None,
        "today_range":      None,
        "chg_pct":          None,
        "vol_ratio":        None,
        "trend":            "未知",
        "strongest_sector": "—",
        "weakest_sector":   "—",
        "inst_flow_est":    "無法估算",
        "tomorrow_watch":   "資料不足，無法研判明日走勢。",
        "verdict":          "無法取得大盤資料，請稍後再試。",
        "error":            "no data",
    }


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

def format_review_report(data: dict) -> str:
    if data.get("error") and data["twii_close"] is None:
        return f"📋 盤後覆盤\n⚠️ {data['verdict']}"

    chg_pct   = data.get("chg_pct", 0.0) or 0.0
    arrow     = "▲" if chg_pct > 0 else ("▼" if chg_pct < 0 else "－")
    vol_ratio = data.get("vol_ratio", 1.0) or 1.0
    vol_label = "放量" if vol_ratio > 1.1 else ("縮量" if vol_ratio < 0.9 else "量平")
    vol_bar   = f"{vol_ratio:.2f}x 均量"

    lines = [
        f"📋 盤後深度覆盤",
        f"━━━━━━━━━━━━━━━━━━",
        f"",
        f"【走勢覆盤】",
        f"  加權指數：{data.get('twii_close', '—')}　{arrow} {chg_pct:+.2f}%",
        f"  開盤：{data.get('twii_open', '—')}　最高：{data.get('twii_high', '—')}　最低：{data.get('twii_low', '—')}",
        f"  震幅：{data.get('today_range', '—')} 點",
        f"  成交量：{vol_label}（{vol_bar}）",
        f"",
        f"【族群分析】",
        f"  🥇 最強：{data.get('strongest_sector', '—')}",
        f"  🥉 最弱：{data.get('weakest_sector', '—')}",
        f"",
        f"【法人解讀】",
        f"  {data.get('inst_flow_est', '—')}",
        f"",
        f"【明日注意】",
        f"  {data.get('tomorrow_watch', '—')}",
        f"",
        f"💡 {data.get('verdict', '')}",
    ]
    return "\n".join(lines)


async def push_daily_review() -> bool:
    """推播盤後覆盤至管理員 LINE（供排程呼叫）。"""
    import os
    try:
        from .line_push import push_line_messages
        admin_uid = os.getenv("ADMIN_LINE_UID", "")
        if not admin_uid:
            logger.warning("[market_review] ADMIN_LINE_UID not set, skip push")
            return False

        data   = await get_market_review()
        report = format_review_report(data)
        ok = await push_line_messages(
            admin_uid,
            [{"type": "text", "text": report[:4000]}],
            context="market_review.daily",
        )
        if ok:
            logger.info("[market_review] daily review pushed to admin")
        return ok
    except Exception as e:
        logger.error(f"[market_review] push_daily_review error: {e}")
        return False
