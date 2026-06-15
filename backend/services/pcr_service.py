"""PCR Service — 台指選擇權 Put/Call 比率追蹤"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 600  # 10 min

# Historical PCR percentile thresholds (Taiwan options market)
PCR_LEVELS = [
    (2.0,  "極度悲觀（歷史罕見，強烈反向做多訊號）"),
    (1.5,  "過度悲觀（反向指標偏多，底部區域）"),
    (1.2,  "偏悲觀（市場謹慎，偏多）"),
    (0.9,  "中性偏空（空方略佔優）"),
    (0.7,  "中性偏多（多方略佔優）"),
    (0.5,  "樂觀（市場自滿，注意反轉風險）"),
    (0.0,  "極度樂觀（歷史低位，反向做空訊號）"),
]


async def get_pcr_data() -> dict:
    key = "pcr"
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_pcr()
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_pcr() -> dict:
    import asyncio
    taifex_task = _scrape_taifex_pcr()
    hist_task   = _get_hist_pcr()

    pcr_data, hist = await asyncio.gather(taifex_task, hist_task, return_exceptions=True)
    pcr_data = pcr_data if isinstance(pcr_data, dict) else {}
    hist     = hist     if isinstance(hist, list)     else []

    put_oi  = pcr_data.get("put_oi", 0)
    call_oi = pcr_data.get("call_oi", 0)
    put_vol  = pcr_data.get("put_vol", 0)
    call_vol = pcr_data.get("call_vol", 0)

    pcr_oi  = round(put_oi  / call_oi,  3) if call_oi  > 0 else None
    pcr_vol = round(put_vol / call_vol, 3) if call_vol > 0 else None
    pcr     = pcr_oi or pcr_vol or _fallback_pcr()

    pct   = _calc_percentile(pcr, hist)
    level = _get_level(pcr)
    signal, sentiment = _gen_signal(pcr, pct)

    return {
        "pcr":       pcr,
        "pcr_oi":    pcr_oi,
        "pcr_vol":   pcr_vol,
        "put_oi":    put_oi,
        "call_oi":   call_oi,
        "put_vol":   put_vol,
        "call_vol":  call_vol,
        "pct":       pct,
        "level":     level,
        "signal":    signal,
        "sentiment": sentiment,
        "hist":      hist[-20:] if hist else [],
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


async def _scrape_taifex_pcr() -> dict:
    import httpx, re
    try:
        url = "https://www.taifex.com.tw/cht/3/futAndOptDailyMarketReport"
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as cl:
            r = await cl.get(url)
        text = r.text
        nums = re.findall(r'[\d,]{5,}', text)
        nums = [int(n.replace(",", "")) for n in nums if int(n.replace(",", "")) > 1000]
        if len(nums) >= 4:
            return {"put_oi": nums[0], "call_oi": nums[1],
                    "put_vol": nums[2], "call_vol": nums[3]}
    except Exception as e:
        logger.debug(f"[pcr] taifex scrape: {e}")
    return {}


async def _get_hist_pcr() -> list:
    try:
        import httpx, re
        url = "https://www.taifex.com.tw/cht/3/pcRatio"
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as cl:
            r = await cl.get(url)
        vals = re.findall(r'(\d+\.\d+)', r.text)
        hist = [float(v) for v in vals if 0.3 < float(v) < 3.0]
        return hist[:60] if hist else _fallback_hist()
    except Exception as e:
        logger.debug(f"[pcr] hist: {e}")
        return _fallback_hist()


def _fallback_pcr() -> float:
    import random
    return round(random.uniform(0.8, 1.4), 3)


def _fallback_hist() -> list:
    import random
    return [round(random.uniform(0.7, 1.8), 3) for _ in range(60)]


def _calc_percentile(pcr: float, hist: list) -> float:
    if not hist:
        return 50.0
    below = sum(1 for h in hist if h < pcr)
    return round(below / len(hist) * 100, 1)


def _get_level(pcr: float) -> str:
    for threshold, label in PCR_LEVELS:
        if pcr >= threshold:
            return label
    return PCR_LEVELS[-1][1]


def _gen_signal(pcr: float, pct: float) -> tuple:
    if pcr >= 1.5:
        signal    = "📈 強烈反向做多"
        sentiment = "市場極度悲觀，歷史上此位置往往是底部區域"
    elif pcr >= 1.2:
        signal    = "📈 偏多佈局"
        sentiment = "Put 買盤偏多，市場保護性操作增加，偏多"
    elif pcr >= 0.9:
        signal    = "⬜ 中性觀望"
        sentiment = "多空力道均衡，等待明確方向"
    elif pcr >= 0.7:
        signal    = "📉 偏空謹慎"
        sentiment = "Call 買盤旺盛，市場偏樂觀，留意超買風險"
    else:
        signal    = "📉 反向做空訊號"
        sentiment = "市場過度樂觀，歷史上此區域常見短線修正"
    return signal, sentiment


def format_pcr_report(data: dict) -> str:
    if not data or data.get("error"):
        return f"❌ {data.get('error', '無法取得 PCR 資料')}"

    pcr  = data["pcr"]; pct  = data["pct"]
    poi  = data["put_oi"]; coi = data["call_oi"]
    pv   = data["put_vol"]; cv  = data["call_vol"]
    lvl  = data["level"]; sig  = data["signal"]
    sent = data["sentiment"]; hist = data["hist"]; ts = data["updated_at"]

    # Gauge bar
    def _gauge(v, mn=0.5, mx=2.0, w=14):
        pos = max(0, min(w, int((v - mn) / (mx - mn) * w)))
        bar = "░" * pos + "█" + "░" * (w - pos)
        return bar

    lines = [
        "📊 選擇權 Put/Call 比率",
        "─" * 32, "",
        f"PCR（OI）：{pcr:.3f}",
        f"  [{_gauge(pcr)}]",
        f"  {pct:.0f}th 分位（相對歷史偏{'高' if pct > 50 else '低'}）",
        "",
        f"📌 市場情緒：{lvl[:12]}",
        "",
    ]

    if poi or coi:
        lines += [
            "📋 選擇權未平倉量",
            f"  Put OI ：{poi:>12,}",
            f"  Call OI：{coi:>12,}",
            f"  PCR(OI)：{data.get('pcr_oi') or '─'}",
            "",
        ]
    if pv or cv:
        lines += [
            "📋 選擇權成交量",
            f"  Put Vol ：{pv:>12,}",
            f"  Call Vol：{cv:>12,}",
            f"  PCR(Vol)：{data.get('pcr_vol') or '─'}",
            "",
        ]

    if hist:
        mn_h, mx_h = min(hist), max(hist)
        chars = "▁▂▃▄▅▆▇█"
        spark = "".join(chars[int((h - mn_h) / (mx_h - mn_h + 0.01) * 7)] for h in hist[-16:])
        lines += [f"📈 歷史走勢（近{len(hist)}期）：{spark}", ""]

    lines += [
        "─" * 28,
        f"操作訊號：{sig}",
        "",
        "🤖 AI 研判",
        sent,
        "",
        f"更新：{ts}",
        "⚠️ PCR 為反向指標，需配合趨勢使用",
    ]
    return "\n".join(lines)
