"""Fear & Greed Service — 台股專屬恐慌貪婪指數（0-100）"""
from __future__ import annotations

import asyncio
import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 1800

# Rolling history for percentile calculation (last 30 readings)
_history: list[float] = []
_MAX_HISTORY = 30


async def get_feargreed() -> dict:
    key = "feargreed"
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_feargreed()
    _cache[key] = result
    _cache_ts[key] = now
    return result


# ---------------------------------------------------------------------------
# Sub-fetchers
# ---------------------------------------------------------------------------

async def _fetch_yahoo_last_close(ticker: str, range_: str = "5d", interval: str = "1d") -> float | None:
    """Return most recent close for a Yahoo Finance ticker."""
    import httpx
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?range={range_}&interval={interval}"
    )
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            data = resp.json()
        results = data.get("chart", {}).get("result", [])
        if not results:
            return None
        closes = results[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
        closes = [c for c in closes if c is not None]
        return closes[-1] if closes else None
    except Exception as e:
        logger.warning(f"feargreed yahoo fetch error ({ticker}): {e}")
        return None


async def _fetch_yahoo_closes(ticker: str, range_: str = "5d", interval: str = "1d") -> list[float]:
    """Return list of closes for percentile/trend calculations."""
    import httpx
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?range={range_}&interval={interval}"
    )
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return []
            data = resp.json()
        results = data.get("chart", {}).get("result", [])
        if not results:
            return []
        closes = results[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
        return [c for c in closes if c is not None]
    except Exception as e:
        logger.warning(f"feargreed closes fetch error ({ticker}): {e}")
        return []


async def _score_vix() -> tuple[int, float | None, str]:
    """VIX score — 20 points max. Lower VIX = greed."""
    vix = await _fetch_yahoo_last_close("^VIX")
    if vix is None:
        return 10, None, "無法取得"
    if vix < 15:
        pts = 20
        label = f"{vix:.1f}（極低恐慌）"
    elif vix < 20:
        pts = 15
        label = f"{vix:.1f}（低恐慌）"
    elif vix < 25:
        pts = 10
        label = f"{vix:.1f}（中度恐慌）"
    elif vix < 30:
        pts = 5
        label = f"{vix:.1f}（高恐慌）"
    else:
        pts = 0
        label = f"{vix:.1f}（極度恐慌）"
    return pts, vix, label


async def _score_pcr() -> tuple[int, float | None, str]:
    """PCR score — 20 points max. High PCR = fear = low score."""
    pcr_val: float | None = None
    try:
        from backend.services.pcr_service import get_pcr_data
        pcr_data = await get_pcr_data()
        pcr_val = pcr_data.get("pcr")
    except Exception as e:
        logger.debug(f"feargreed pcr import failed: {e}")

    if pcr_val is None:
        return 10, None, "無法取得"

    if pcr_val > 1.2:
        pts = 0
        label = f"{pcr_val:.2f}（極度恐慌）"
    elif pcr_val >= 1.0:
        pts = 5
        label = f"{pcr_val:.2f}（偏恐慌）"
    elif pcr_val >= 0.8:
        pts = 12
        label = f"{pcr_val:.2f}（中性）"
    else:
        pts = 20
        label = f"{pcr_val:.2f}（偏貪婪）"
    return pts, pcr_val, label


async def _score_margin() -> tuple[int, float | None, str]:
    """融資水位 score — 20 points max. High margin = greed = high score."""
    margin_ratio: float | None = None
    try:
        from backend.services import margin_tracker_service  # type: ignore
        mdata = await margin_tracker_service.get_margin_ratio()
        margin_ratio = mdata.get("ratio") if isinstance(mdata, dict) else None
    except Exception as e:
        pass

    if margin_ratio is None:
        # proxy: use ^TWII 20-day return as rough sentiment proxy
        closes = await _fetch_yahoo_closes("^TWII", range_="1mo", interval="1d")
        if len(closes) >= 5:
            ret = (closes[-1] - closes[-5]) / closes[-5] * 100 if closes[-5] > 0 else 0
            margin_ratio = min(max(50 + ret * 5, 0), 100)  # normalize roughly
        else:
            margin_ratio = 50.0

    if margin_ratio > 80:
        pts = 20
        label = f"{margin_ratio:.0f}%（高融資）"
    elif margin_ratio > 60:
        pts = 15
        label = f"{margin_ratio:.0f}%（中高融資）"
    elif margin_ratio > 40:
        pts = 10
        label = f"{margin_ratio:.0f}%（中等融資）"
    else:
        pts = 5
        label = f"{margin_ratio:.0f}%（低融資）"
    return pts, margin_ratio, label


async def _score_twii_momentum() -> tuple[int, float | None, str]:
    """大戶比例 proxy via TWII 5-day momentum — 20 points max."""
    closes = await _fetch_yahoo_closes("^TWII", range_="10d", interval="1d")
    if len(closes) < 5:
        return 10, None, "無法取得"
    change = (closes[-1] - closes[-5]) / closes[-5] * 100 if closes[-5] > 0 else 0.0
    if change > 3:
        pts = 20
        label = f"{change:+.2f}%（強勁上漲）"
    elif change > 1:
        pts = 15
        label = f"{change:+.2f}%（溫和上漲）"
    elif change >= -1:
        pts = 10
        label = f"{change:+.2f}%（盤整）"
    elif change >= -3:
        pts = 5
        label = f"{change:+.2f}%（溫和下跌）"
    else:
        pts = 0
        label = f"{change:+.2f}%（大幅下跌）"
    return pts, round(change, 2), label


async def _score_adr() -> tuple[int, float | None, str]:
    """ADR 溢價 score — 20 points max. High ADR premium = greed."""
    premium: float | None = None
    try:
        from backend.services.adr_service import get_adr
        adr_data = await get_adr()
        items = adr_data.get("items", [])
        for item in items:
            if item.get("code") == "2330":
                premium = item.get("premium_pct")
                break
        if premium is None and items:
            prems = [i.get("premium_pct", 0) for i in items if i.get("premium_pct") is not None]
            premium = sum(prems) / len(prems) if prems else None
    except Exception as e:
        logger.debug(f"feargreed adr import failed: {e}")

    if premium is None:
        # fallback: fetch TSM vs 2330.TW directly
        tsm = await _fetch_yahoo_last_close("TSM")
        tw = await _fetch_yahoo_last_close("2330.TW")
        usd_twd = 32.0
        if tsm and tw:
            tsm_twd = tsm * usd_twd
            premium = (tsm_twd - tw) / tw * 100 if tw > 0 else 0.0
        else:
            premium = 0.0

    if premium > 2:
        pts = 20
        label = f"{premium:+.2f}%（ADR大幅溢價）"
    elif premium >= 0:
        pts = 15
        label = f"{premium:+.2f}%（ADR小幅溢價）"
    elif premium >= -2:
        pts = 10
        label = f"{premium:+.2f}%（ADR小幅折價）"
    else:
        pts = 5
        label = f"{premium:+.2f}%（ADR大幅折價）"
    return pts, round(premium, 2) if premium is not None else None, label


# ---------------------------------------------------------------------------
# Main fetch
# ---------------------------------------------------------------------------

async def _fetch_feargreed() -> dict:
    global _history

    vix_task = _score_vix()
    pcr_task = _score_pcr()
    margin_task = _score_margin()
    twii_task = _score_twii_momentum()
    adr_task = _score_adr()

    results = await asyncio.gather(
        vix_task, pcr_task, margin_task, twii_task, adr_task,
        return_exceptions=True,
    )

    def safe(r, fallback=(10, None, "錯誤")):
        return r if isinstance(r, tuple) else fallback

    vix_pts, vix_val, vix_label = safe(results[0])
    pcr_pts, pcr_val, pcr_label = safe(results[1])
    margin_pts, margin_val, margin_label = safe(results[2])
    twii_pts, twii_val, twii_label = safe(results[3])
    adr_pts, adr_val, adr_label = safe(results[4])

    total = vix_pts + pcr_pts + margin_pts + twii_pts + adr_pts
    total = max(0, min(100, total))

    # Update rolling history
    _history.append(float(total))
    if len(_history) > _MAX_HISTORY:
        _history = _history[-_MAX_HISTORY:]

    percentile = None
    if len(_history) > 1:
        pct = len([h for h in _history if h <= total]) / len(_history) * 100
        percentile = round(pct, 1)

    # Bucket label
    if total <= 20:
        bucket = "極度恐慌"
        recommendation = (
            "極度恐慌通常是買點，歷史上在此區間買入報酬率較高，建議分批布局"
        )
    elif total <= 40:
        bucket = "恐慌"
        recommendation = (
            "市場偏恐慌，考慮逢低加碼績優股，但需設定停損"
        )
    elif total <= 60:
        bucket = "中性"
        recommendation = (
            "市場情緒中性，以個股選股為主，注意量能變化"
        )
    elif total <= 80:
        bucket = "貪婪"
        recommendation = (
            "市場偏貪婪，注意風險控管，建議減少追高"
        )
    else:
        bucket = "極度貪婪"
        recommendation = (
            "極度貪婪警示！歷史上此區間後市場易回調，建議獲利了結部分持股"
        )

    return {
        "score": total,
        "bucket": bucket,
        "recommendation": recommendation,
        "percentile": percentile,
        "history_count": len(_history),
        "indicators": {
            "vix":     {"pts": vix_pts,    "value": vix_val,    "label": vix_label,    "max": 20},
            "pcr":     {"pts": pcr_pts,    "value": pcr_val,    "label": pcr_label,    "max": 20},
            "margin":  {"pts": margin_pts, "value": margin_val, "label": margin_label, "max": 20},
            "twii":    {"pts": twii_pts,   "value": twii_val,   "label": twii_label,   "max": 20},
            "adr":     {"pts": adr_pts,    "value": adr_val,    "label": adr_label,    "max": 20},
        },
        "error": None,
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _progress_bar(score: int, width: int = 20) -> str:
    """Draw a text progress bar from FEAR(0) to GREED(100)."""
    filled = round(score / 100 * width)
    filled = max(0, min(width, filled))
    bar = "█" * filled + "░" * (width - filled)
    return f"恐慌 [{bar}] 貪婪"


def _indicator_bar(pts: int, max_pts: int = 20, width: int = 10) -> str:
    filled = round(pts / max_pts * width) if max_pts > 0 else 0
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def format_feargreed_report(data: dict) -> str:
    if data.get("error"):
        return f"❌ 無法計算恐慌貪婪指數：{data['error']}"

    score = data.get("score", 0)
    bucket = data.get("bucket", "")
    recommendation = data.get("recommendation", "")
    percentile = data.get("percentile")
    indicators = data.get("indicators", {})

    score_emoji = (
        "😱" if score <= 20
        else "😰" if score <= 40
        else "😐" if score <= 60
        else "😏" if score <= 80
        else "🤑"
    )

    lines = [
        f"{score_emoji} 台股恐慌貪婪指數",
        "─" * 28,
        f"當前指數：{score}/100  【{bucket}】",
        _progress_bar(score),
    ]

    if percentile is not None:
        lines.append(f"歷史百分位：{percentile:.0f}%（近 {data.get('history_count', 0)} 次讀值）")

    lines += [
        "",
        "📊 指標明細（各20分）：",
    ]

    indicator_names = {
        "vix":    "VIX 恐慌指數",
        "pcr":    "PCR 買賣比",
        "margin": "融資水位",
        "twii":   "大盤動能",
        "adr":    "ADR 溢價",
    }

    for key, name in indicator_names.items():
        ind = indicators.get(key, {})
        pts = ind.get("pts", 0)
        label = ind.get("label", "")
        bar = _indicator_bar(pts, 20, 10)
        lines.append(f"  {name}: [{bar}] {pts}/20  {label}")

    lines += [
        "",
        f"💡 AI 建議：{recommendation}",
    ]

    text = "\n".join(lines)
    # Cap to 4500 chars
    if len(text) > 4500:
        text = text[:4497] + "..."
    return text
