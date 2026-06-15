"""Bearish Scanner Service — 全市場空頭訊號偵測"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: float = 0.0
_TTL = 1800  # 30 min

# 常態掃描標的（重要個股 + 大型ETF）
_SCAN_UNIVERSE = [
    "2330", "2317", "2454", "2382", "2308",
    "2881", "2882", "2884", "2886", "2887",
    "2303", "3008", "2357", "4938", "2379",
    "6669", "3443", "2376", "2345", "2337",
    "2412", "2609", "2615", "2603", "2618",
    "0050", "0056",
]


async def get_bearish_scan() -> dict:
    global _cache, _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < _TTL:
        return _cache
    result = await _fetch_bearish_scan()
    _cache = result
    _cache_ts = now
    return result


async def _fetch_bearish_scan() -> dict:
    import asyncio
    tasks = [_check_stock(code) for code in _SCAN_UNIVERSE]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    signals: list[dict] = []
    for r in results:
        if isinstance(r, dict) and r.get("bearish_count", 0) > 0:
            signals.append(r)

    signals.sort(key=lambda x: x.get("bearish_count", 0), reverse=True)

    # 高風險個股（2+ 空頭訊號）
    high_risk  = [s for s in signals if s.get("bearish_count", 0) >= 2]
    medium_risk = [s for s in signals if s.get("bearish_count", 0) == 1]

    return {
        "high_risk":    high_risk[:8],
        "medium_risk":  medium_risk[:5],
        "total_scanned": len(_SCAN_UNIVERSE),
        "total_bearish": len(signals),
        "updated_at":   time.strftime("%Y-%m-%d %H:%M"),
    }


async def _check_stock(code: str) -> dict:
    import httpx
    signals: list[str] = []

    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url, params={"interval": "1d", "range": "3mo"},
                            headers={"User-Agent": "Mozilla/5.0"})
        data   = r.json()
        result = data["chart"]["result"][0]
        q      = result["indicators"]["quote"][0]
        closes = [x for x in q.get("close", []) if x is not None]
        vols   = [x for x in q.get("volume", []) if x is not None]

        if len(closes) < 20:
            return {"code": code, "bearish_count": 0, "signals": []}

        price   = closes[-1]
        ma20    = sum(closes[-20:]) / 20
        ma60    = sum(closes[-min(60, len(closes)):]) / min(60, len(closes))
        prev_price = closes[-2] if len(closes) >= 2 else price
        chg_pct    = (price - prev_price) / prev_price * 100 if prev_price else 0

        # 訊號 1：跌破重要均線
        if price < ma20 and price < ma60:
            signals.append("跌破MA20&MA60")
        elif price < ma20:
            signals.append("跌破MA20")

        # 訊號 2：RSI 下降跌破 50
        rsi = _calc_rsi(closes, 14)
        rsi_5 = _calc_rsi(closes[:-5], 14) if len(closes) > 20 else rsi
        if rsi < 50 and rsi < rsi_5:
            signals.append(f"RSI跌破50({rsi:.0f}↓)")

        # 訊號 3：爆量下跌（成交量 > 1.5x 均量且收黑）
        if len(vols) >= 10:
            avg_vol = sum(vols[-10:]) / 10
            last_vol = vols[-1]
            if last_vol > avg_vol * 1.5 and chg_pct < -2:
                signals.append(f"爆量殺跌({chg_pct:.1f}%)")

        # 訊號 4：連續 5 日下跌
        if len(closes) >= 6:
            recent_5 = closes[-6:]
            if all(recent_5[i] < recent_5[i - 1] for i in range(1, len(recent_5))):
                signals.append("連續5日下跌")

        # 訊號 5：融資大增但股價不漲（用成交量放大 + 價格不漲作代理）
        if len(vols) >= 10 and len(closes) >= 10:
            avg_vol_20 = sum(vols[-20:]) / 20 if len(vols) >= 20 else sum(vols) / len(vols)
            last_vol = vols[-1]
            price_change_5d = (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) >= 6 else 0
            if last_vol > avg_vol_20 * 1.3 and price_change_5d < 0:
                signals.append("量增價跌（籌碼惡化）")

    except Exception as e:
        logger.debug(f"[bearish_scan] {code}: {e}")
        return {"code": code, "bearish_count": 0, "signals": []}

    return {
        "code":          code,
        "price":         round(price, 2),
        "chg_pct":       round(chg_pct, 2),
        "ma20":          round(ma20, 2),
        "rsi":           round(rsi, 1),
        "bearish_count": len(signals),
        "signals":       signals,
    }


def _calc_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def format_bearish_report(data: dict) -> str:
    high   = data.get("high_risk", [])
    medium = data.get("medium_risk", [])
    total  = data.get("total_scanned", 0)
    bearish = data.get("total_bearish", 0)
    updated = data.get("updated_at", "")

    lines = [
        "📉 空頭訊號偵測報告",
        "─" * 32, "",
        f"掃描標的：{total} 檔  發現空頭訊號：{bearish} 檔",
        f"更新時間：{updated}",
        "",
    ]

    if high:
        lines.append("🚨 高風險（2+ 空頭訊號）：")
        for s in high:
            sig_str = " / ".join(s.get("signals", [])[:3])
            lines.append(
                f"  ⚠️  {s['code']}  ${s.get('price',0):.1f}  {s.get('chg_pct',0):+.1f}%"
                f"  RSI:{s.get('rsi',50):.0f}"
            )
            lines.append(f"     訊號：{sig_str}")
        lines.append("")

    if medium:
        lines.append("⚠️  中風險（1 個訊號）：")
        for s in medium:
            sig_str = " / ".join(s.get("signals", []))
            lines.append(f"  {s['code']}  ${s.get('price',0):.1f}  {s.get('chg_pct',0):+.1f}%  {sig_str}")
        lines.append("")

    if not high and not medium:
        lines.append("✅ 目前無明顯空頭訊號，市場整體健康")
    else:
        lines += [
            "─" * 28,
            "🤖 AI 風險提示",
            f"共 {len(high)} 檔高風險個股，建議：",
            "• 持有上述個股者：確認停損位，設定下跌 5-8% 停損",
            "• 準備進場者：等待量縮整理或反彈確認後再介入",
            "• 空頭個股通常延伸期為 1-2 週，不宜逆勢搶反彈",
        ]

    lines += ["", "輸入 /screener 查看強勢選股 | /feargreed 查恐慌指數"]
    return "\n".join(lines)
