"""Swing Service — 波段交易建議（ATR / MA / 支撐壓力）"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 3600  # 1 hour


async def get_swing(code: str) -> dict:
    """回傳波段交易建議，含入場區間 / 停損 / 目標價，附帶勝率估算。"""
    now = time.time()
    if code in _cache and now - _cache_ts.get(code, 0) < _TTL:
        return _cache[code]

    result = await _fetch_swing(code)
    _cache[code] = result
    _cache_ts[code] = now
    return result


async def _fetch_swing(code: str) -> dict:
    import asyncio
    import httpx

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
    params = {"interval": "1d", "range": "3mo"}
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"[swing] Yahoo Finance fetch failed for {code}: {e}")
        return _empty_swing(code)

    try:
        result_block = data["chart"]["result"][0]
        quotes = result_block["indicators"]["quote"][0]
        timestamps = result_block.get("timestamp", [])

        opens   = [float(v) if v is not None else None for v in quotes.get("open", [])]
        highs   = [float(v) if v is not None else None for v in quotes.get("high", [])]
        lows    = [float(v) if v is not None else None for v in quotes.get("low", [])]
        closes  = [float(v) if v is not None else None for v in quotes.get("close", [])]
        volumes = [float(v) if v is not None else 0.0  for v in quotes.get("volume", [])]

        # Strip trailing None rows
        valid_idx = [i for i, c in enumerate(closes) if c is not None]
        if len(valid_idx) < 20:
            logger.warning(f"[swing] Insufficient data for {code}: {len(valid_idx)} bars")
            return _empty_swing(code)

        highs  = [highs[i]  for i in valid_idx]
        lows   = [lows[i]   for i in valid_idx]
        closes = [closes[i] for i in valid_idx]

        n = len(closes)

        # --- ATR 14-day ---
        atr = _calc_atr(highs, lows, closes, period=14)

        # --- Moving averages ---
        ma20 = _ma(closes, 20)
        ma60 = _ma(closes, 60) if n >= 60 else None

        price = closes[-1]

        # --- Swing phase ---
        if ma60 is not None and price > ma20 > ma60:
            phase = "uptrend"
            win_rate = 0.65
        elif ma60 is not None and price < ma20 < ma60:
            phase = "downtrend"
            win_rate = 0.35
        else:
            phase = "sideways"
            win_rate = 0.45

        # --- Support / Resistance (last 10 days) ---
        recent_lows  = lows[-10:]
        recent_highs = highs[-10:]
        support    = min(recent_lows)
        resistance = max(recent_highs)

        # --- Entry zone: pullback to 20MA in uptrend ---
        entry_low  = round(ma20 - atr, 2)
        entry_high = round(ma20, 2)

        # --- Stop loss ---
        stop_loss = round(ma20 - 2 * atr, 2)

        # --- Targets ---
        target1 = round(resistance, 2)
        target2 = round(resistance + atr, 2)

        # --- Timeframe ---
        if phase == "uptrend":
            timeframe_suggestion = "短線：1 週內可操作；中線：持有 1 個月為目標"
        elif phase == "sideways":
            timeframe_suggestion = "區間震盪，建議觀察 1 週確認方向後再進場"
        else:
            timeframe_suggestion = "空頭結構，暫不建議做多；可等反轉信號後再評估（3 個月視角）"

        verdict = _gen_swing_verdict({
            "phase": phase,
            "price": price,
            "ma20": ma20,
            "ma60": ma60,
            "atr": atr,
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": stop_loss,
            "target1": target1,
            "target2": target2,
            "win_rate": win_rate,
            "support": support,
            "resistance": resistance,
        })

        return {
            "code":                code,
            "price":               round(price, 2),
            "atr":                 round(atr, 2),
            "ma20":                round(ma20, 2),
            "ma60":                round(ma60, 2) if ma60 is not None else None,
            "phase":               phase,
            "entry_zone":          {"low": entry_low, "high": entry_high},
            "stop_loss":           stop_loss,
            "target1":             target1,
            "target2":             target2,
            "support":             round(support, 2),
            "resistance":          round(resistance, 2),
            "timeframe_suggestion": timeframe_suggestion,
            "win_rate":            win_rate,
            "verdict":             verdict,
            "error":               None,
        }

    except Exception as e:
        logger.exception(f"[swing] Parse error for {code}: {e}")
        return _empty_swing(code, str(e))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _calc_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    trs = []
    for i in range(1, len(closes)):
        h = highs[i]
        l = lows[i]
        prev_c = closes[i - 1]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    if not trs:
        return 0.0
    recent = trs[-period:] if len(trs) >= period else trs
    return sum(recent) / len(recent)


def _ma(values: list[float], period: int) -> float:
    if len(values) < period:
        return sum(values) / len(values)
    return sum(values[-period:]) / period


def _empty_swing(code: str, error: str = "no data") -> dict:
    return {
        "code":                code,
        "price":               None,
        "atr":                 None,
        "ma20":                None,
        "ma60":                None,
        "phase":               "unknown",
        "entry_zone":          {"low": None, "high": None},
        "stop_loss":           None,
        "target1":             None,
        "target2":             None,
        "support":             None,
        "resistance":          None,
        "timeframe_suggestion": "資料不足，無法評估",
        "win_rate":            None,
        "verdict":             "無法取得資料，請稍後再試。",
        "error":               error,
    }


def _gen_swing_verdict(data: dict) -> str:
    phase = data["phase"]
    price = data["price"]
    ma20  = data["ma20"]
    atr   = data["atr"]
    win_rate = data["win_rate"]
    entry_low  = data["entry_low"]
    entry_high = data["entry_high"]

    pct = round(win_rate * 100)

    if phase == "uptrend":
        if entry_low <= price <= entry_high:
            return (
                f"目前股價 {price} 正好落在 20MA 回調入場區（{entry_low}–{entry_high}），"
                f"上升趨勢回踩支撐，為較佳的波段買點，歷史類似型態勝率約 {pct}%。"
            )
        elif price > entry_high:
            return (
                f"股價 {price} 已高於 20MA（{ma20:.2f}），追高風險偏大；"
                f"建議等待回踩 {entry_low}–{entry_high} 區間後再考慮進場（上升趨勢，勝率 {pct}%）。"
            )
        else:
            return (
                f"股價 {price} 跌至 20MA 以下，上升趨勢轉弱；"
                f"需觀察是否能在 {entry_low} 附近獲得支撐，否則回避操作（勝率降至 {pct}%）。"
            )
    elif phase == "downtrend":
        return (
            f"股價 {price} 處於空頭排列（價 < 20MA < 60MA），做多風險高，勝率僅 {pct}%。"
            f" 建議暫時觀望，等待均線翻多訊號或跌深反彈後再評估。"
        )
    else:
        return (
            f"股價 {price} 處於橫向整理，短期方向不明，勝率約 {pct}%。"
            f" 建議等待突破壓力（{data['resistance']}）或跌破支撐（{data['support']}）後順勢操作。"
        )


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

def format_swing_report(data: dict, code: str) -> str:
    if data.get("error") and data["price"] is None:
        return f"📉 [{code}] 波段分析\n⚠️ 無法取得資料：{data['error']}"

    phase_map = {
        "uptrend":   "📈 上升趨勢",
        "downtrend": "📉 下降趨勢",
        "sideways":  "↔️ 橫向整理",
        "unknown":   "❓ 未知",
    }
    phase_label = phase_map.get(data["phase"], data["phase"])

    win_rate = data.get("win_rate")
    win_bar  = ""
    if win_rate is not None:
        filled = round(win_rate * 10)
        win_bar = "🟩" * filled + "⬜" * (10 - filled) + f"  {round(win_rate*100)}%"

    entry = data.get("entry_zone", {})
    ez_str = (
        f"{entry.get('low', '—')} ~ {entry.get('high', '—')}"
        if entry.get("low") is not None else "—"
    )

    ma60_str = f"{data['ma60']}" if data.get("ma60") is not None else "資料不足"

    lines = [
        f"🌊 [{code}] 波段交易分析",
        f"━━━━━━━━━━━━━━━━━━",
        f"📌 現價：{data['price']}　ATR(14)：{data['atr']}",
        f"📊 趨勢：{phase_label}",
        f"📐 MA20：{data['ma20']}　MA60：{ma60_str}",
        f"",
        f"🎯 操作建議",
        f"  入場區：{ez_str}",
        f"  停損價：{data.get('stop_loss', '—')}",
        f"  目標①：{data.get('target1', '—')}",
        f"  目標②：{data.get('target2', '—')}",
        f"",
        f"🔖 支撐 / 壓力（近10日）",
        f"  支撐：{data.get('support', '—')}　壓力：{data.get('resistance', '—')}",
        f"",
        f"⏱ 持股時程",
        f"  {data.get('timeframe_suggestion', '—')}",
        f"",
        f"🎲 歷史勝率",
        f"  {win_bar}",
        f"",
        f"💡 {data.get('verdict', '')}",
    ]
    return "\n".join(lines)
