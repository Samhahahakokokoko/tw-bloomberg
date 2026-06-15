"""Global Market Service — 跨市場相關性與台股開盤預測"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 900  # 15 min

GLOBAL_SYMBOLS = {
    "S&P500":  "^GSPC",
    "那斯達克": "^IXIC",
    "費城半導": "^SOX",
    "道瓊":    "^DJI",
    "日經225": "^N225",
    "韓國綜合": "^KS11",
    "恆生":    "^HSI",
    "VIX":     "^VIX",
    "美元指數": "DX-Y.NYB",
    "台灣50":  "0050.TW",
}


async def get_global_market() -> dict:
    key = "global"
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_global()
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_global() -> dict:
    import asyncio
    tasks  = {name: _get_quote(sym) for name, sym in GLOBAL_SYMBOLS.items()}
    names  = list(tasks.keys())
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    markets = {}
    for name, res in zip(names, results):
        if isinstance(res, dict) and res:
            markets[name] = res

    twii_task  = _get_quote("^TWII")
    futures_task = _get_tw_futures()
    twii, futures = await asyncio.gather(twii_task, futures_task, return_exceptions=True)
    twii    = twii    if isinstance(twii, dict)    else {}
    futures = futures if isinstance(futures, dict) else {}

    signal, pred_pct, rationale = _predict_open(markets, futures)

    return {
        "markets":    markets,
        "twii":       twii,
        "futures":    futures,
        "signal":     signal,
        "pred_pct":   pred_pct,
        "rationale":  rationale,
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


async def _get_quote(symbol: str) -> dict:
    try:
        import httpx
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?interval=1d&range=5d")
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as cl:
            r = await cl.get(url)
        js  = r.json()
        res = js["chart"]["result"][0]
        q   = res["indicators"]["quote"][0]
        closes = [c for c in q.get("close", []) if c]
        if len(closes) < 2:
            return {}
        chg = (closes[-1] / closes[-2] - 1) * 100
        return {
            "close": round(closes[-1], 2),
            "prev":  round(closes[-2], 2),
            "chg":   round(chg, 2),
        }
    except Exception as e:
        logger.debug(f"[global] quote {symbol}: {e}")
        return {}


async def _get_tw_futures() -> dict:
    try:
        import httpx, re
        url = "https://mis.taifex.com.tw/futures/api/getQuoteList"
        params = {"MarketType": "0", "CommodityID": "TX"}
        async with httpx.AsyncClient(timeout=10) as cl:
            r = await cl.get(url, params=params)
        js   = r.json()
        data = js.get("RtnData", {}).get("QuoteList", [{}])[0]
        return {
            "close":   float(data.get("CLastPrice", 0) or 0),
            "chg":     float(data.get("CChange", 0) or 0),
            "chg_pct": float(data.get("CChangeRate", 0) or 0),
        }
    except Exception as e:
        logger.debug(f"[global] tw_futures: {e}")
        return {}


def _predict_open(markets: dict, futures: dict) -> tuple:
    score = 0.0
    reasons = []

    sp = markets.get("S&P500", {}).get("chg", 0)
    nq = markets.get("那斯達克", {}).get("chg", 0)
    sox= markets.get("費城半導", {}).get("chg", 0)
    nk = markets.get("日經225", {}).get("chg", 0)
    vix= markets.get("VIX",     {}).get("chg", 0)
    usd= markets.get("美元指數", {}).get("chg", 0)

    # US market weight = 0.45
    us_score = sp * 0.25 + nq * 0.20
    score += us_score
    if us_score > 0.5:
        reasons.append(f"美股強勢 (S&P {sp:+.1f}%、那指 {nq:+.1f}%)")
    elif us_score < -0.5:
        reasons.append(f"美股走弱 (S&P {sp:+.1f}%、那指 {nq:+.1f}%)")

    # SOX weight = 0.25 (台股科技連動)
    score += sox * 0.25
    if abs(sox) > 1:
        reasons.append(f"費城半導 {sox:+.1f}%（台股半導體連動）")

    # Japan weight = 0.10
    score += nk * 0.10
    if abs(nk) > 1:
        reasons.append(f"日股 {nk:+.1f}%")

    # VIX: negative weight
    if vix > 10:
        score -= 0.5
        reasons.append(f"VIX 大漲 {vix:+.1f}%（恐慌升溫）")
    elif vix < -10:
        score += 0.3
        reasons.append(f"VIX 大跌（恐慌消退）")

    # USD: negative for TW
    if usd > 0.5:
        score -= 0.3
        reasons.append("美元走強（外資傾向匯出）")

    # Futures adjustment
    fut_chg = futures.get("chg_pct", 0)
    if abs(fut_chg) > 0.3:
        score += fut_chg * 0.3
        reasons.append(f"台指期夜盤 {fut_chg:+.1f}%")

    pred_pct = round(max(-3.5, min(3.5, score)), 2)

    if pred_pct >= 1.0:
        signal = "📈 預測開盤偏強"
    elif pred_pct >= 0.3:
        signal = "📈 預測小幅開高"
    elif pred_pct >= -0.3:
        signal = "⬜ 預測平盤震盪"
    elif pred_pct >= -1.0:
        signal = "📉 預測小幅開低"
    else:
        signal = "📉 預測開盤偏弱"

    rationale = "；".join(reasons) if reasons else "市場無明顯方向性訊號"
    return signal, pred_pct, rationale


def format_global_report(data: dict) -> str:
    if not data or data.get("error"):
        return f"❌ {data.get('error', '無法取得全球市場資料')}"

    markets  = data["markets"]
    futures  = data["futures"]
    signal   = data["signal"]
    pred_pct = data["pred_pct"]
    rationale= data["rationale"]
    ts       = data["updated_at"]

    def _row(name, info):
        if not info:
            return f"  {name:<8}：─"
        chg  = info.get("chg", 0)
        icon = "▲" if chg > 0 else ("▼" if chg < 0 else "─")
        return f"  {name:<8}：{info['close']:>10,.2f}  {icon}{abs(chg):.2f}%"

    lines = ["🌍 跨市場相關性", "─" * 32, ""]

    # US markets
    lines.append("🇺🇸 美股（昨夜收盤）")
    for name in ["S&P500", "那斯達克", "費城半導", "道瓊"]:
        lines.append(_row(name, markets.get(name)))
    lines.append("")

    # Asia
    lines.append("🌏 亞股（今日開盤）")
    for name in ["日經225", "韓國綜合", "恆生"]:
        lines.append(_row(name, markets.get(name)))
    lines.append("")

    # Risk indicators
    lines.append("⚡ 風險指標")
    for name in ["VIX", "美元指數"]:
        lines.append(_row(name, markets.get(name)))
    lines.append("")

    # Taiwan futures
    if futures:
        fchg = futures.get("chg_pct", 0)
        icon = "▲" if fchg > 0 else "▼"
        lines += [
            "🇹🇼 台指期（夜盤）",
            f"  收盤：{futures.get('close',0):>10,.0f}  {icon}{abs(fchg):.2f}%",
            "",
        ]

    # Prediction
    icon_p = "▲" if pred_pct > 0 else "▼"
    lines += [
        "─" * 28,
        f"🎯 今日台股開盤預測",
        f"  {signal}",
        f"  預估漲跌：{icon_p}{abs(pred_pct):.2f}%",
        "",
        "🤖 AI 研判",
        rationale,
        "",
        f"更新：{ts}",
        "⚠️ 預測基於統計模型，市場隨時變化",
    ]
    return "\n".join(lines)
