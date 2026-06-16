"""dynstop_service.py — 動態停損計算（ATR / 支撐位 / 均線成本 三法）"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 1800  # 30 min


# ── 快取包裝 ──────────────────────────────────────────────────────────────────

async def get_dynstop(code: str) -> dict:
    """取得動態停損建議，TTL=30 分鐘快取。"""
    now = time.time()
    if code in _cache and now - _cache_ts.get(code, 0) < _TTL:
        return _cache[code]
    result = await _fetch_dynstop(code)
    _cache[code] = result
    _cache_ts[code] = now
    return result


# ── 核心抓取 ──────────────────────────────────────────────────────────────────

async def _fetch_dynstop(code: str) -> dict:
    """
    從 Yahoo Finance 抓取 30 日 OHLCV，計算三種動態停損：
      1. ATR-based stop       (14 日 ATR × 2.5)
      2. Support-based stop   (近 10 日低點 - 1%)
      3. Cost-based stop      (20 日 MA - 3%)
    """
    import asyncio
    import httpx

    ticker = f"{code}.TW"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?range=1mo&interval=1d"
    )
    headers = {"User-Agent": "Mozilla/5.0"}

    ohlcv: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as cl:
            resp = await cl.get(url)
            resp.raise_for_status()
            js = resp.json()
            result0 = js["chart"]["result"][0]
            quotes  = result0["indicators"]["quote"][0]
            times   = result0["timestamp"]

            opens   = quotes.get("open",   [])
            highs   = quotes.get("high",   [])
            lows    = quotes.get("low",    [])
            closes  = quotes.get("close",  [])
            volumes = quotes.get("volume", [])

            for i, ts in enumerate(times):
                o = opens[i]   if i < len(opens)   else None
                h = highs[i]   if i < len(highs)   else None
                l = lows[i]    if i < len(lows)    else None
                c = closes[i]  if i < len(closes)  else None
                v = volumes[i] if i < len(volumes) else 0
                if None in (o, h, l, c) or c is None or c <= 0:
                    continue
                ohlcv.append({
                    "open": float(o), "high": float(h),
                    "low":  float(l), "close": float(c),
                    "volume": int(v or 0),
                })
    except Exception as e:
        logger.warning("[dynstop] YF fetch failed for {}: {}", code, e)
        # fallback: try .TWO
        try:
            ticker2 = f"{code}.TWO"
            url2 = (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker2}"
                f"?range=1mo&interval=1d"
            )
            async with httpx.AsyncClient(timeout=15, headers=headers) as cl:
                resp2 = await cl.get(url2)
                resp2.raise_for_status()
                js2 = resp2.json()
                result1 = js2["chart"]["result"][0]
                q2 = result1["indicators"]["quote"][0]
                t2 = result1["timestamp"]
                o2, h2 = q2.get("open", []), q2.get("high", [])
                l2, c2 = q2.get("low",  []), q2.get("close",[])
                v2     = q2.get("volume", [])
                for i, ts in enumerate(t2):
                    o = o2[i] if i < len(o2) else None
                    h = h2[i] if i < len(h2) else None
                    l = l2[i] if i < len(l2) else None
                    c = c2[i] if i < len(c2) else None
                    vv = v2[i] if i < len(v2) else 0
                    if None in (o, h, l, c) or c <= 0:
                        continue
                    ohlcv.append({
                        "open": float(o), "high": float(h),
                        "low":  float(l), "close": float(c),
                        "volume": int(vv or 0),
                    })
        except Exception as e2:
            logger.error("[dynstop] .TWO fallback also failed for {}: {}", code, e2)

    if not ohlcv:
        return _empty_result(code)

    closes_list = [r["close"] for r in ohlcv]
    highs_list  = [r["high"]  for r in ohlcv]
    lows_list   = [r["low"]   for r in ohlcv]

    current_price = closes_list[-1]

    # ── Method 1: ATR-based stop ──────────────────────────────────────────────
    atr = _calc_atr(highs_list, lows_list, closes_list, period=14)
    atr_stop = current_price - 2.5 * atr
    atr_pct  = (current_price - atr_stop) / current_price * 100

    # ── Method 2: Support-based stop ─────────────────────────────────────────
    recent_lows = lows_list[-10:] if len(lows_list) >= 10 else lows_list
    support     = min(recent_lows)
    sup_stop    = support * 0.99   # 1% below support
    sup_pct     = (current_price - sup_stop) / current_price * 100

    # ── Method 3: Cost-based stop (MA20) ─────────────────────────────────────
    ma20_window = closes_list[-20:] if len(closes_list) >= 20 else closes_list
    ma20        = sum(ma20_window) / len(ma20_window)
    ma20_stop   = ma20 * 0.97      # 3% below MA20
    ma20_pct    = (current_price - ma20_stop) / current_price * 100

    # ── Conservative: most restrictive (highest stop price) ──────────────────
    conservative_stop = max(atr_stop, sup_stop, ma20_stop)
    conservative_pct  = (current_price - conservative_stop) / current_price * 100

    methods = [
        {
            "name":          "ATR動態停損（2.5倍ATR）",
            "stop":          round(atr_stop, 2),
            "pct_from_price": round(atr_pct, 2),
        },
        {
            "name":          "支撐位停損（近10日低點-1%）",
            "stop":          round(sup_stop, 2),
            "pct_from_price": round(sup_pct, 2),
        },
        {
            "name":          "均線成本停損（20日均線-3%）",
            "stop":          round(ma20_stop, 2),
            "pct_from_price": round(ma20_pct, 2),
        },
    ]

    data = {
        "code":              code,
        "price":             round(current_price, 2),
        "atr":               round(atr, 2),
        "ma20":              round(ma20, 2),
        "methods":           methods,
        "conservative_stop": round(conservative_stop, 2),
        "conservative_pct":  round(conservative_pct, 2),
        "updated_at":        time.strftime("%Y-%m-%d %H:%M"),
    }
    data["verdict"] = _gen_dynstop_verdict(data)
    return data


# ── ATR 計算 ──────────────────────────────────────────────────────────────────

def _calc_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """Simple ATR over `period` days."""
    if len(highs) < 2:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(highs)):
        hl  = highs[i] - lows[i]
        hcp = abs(highs[i] - closes[i - 1])
        lcp = abs(lows[i]  - closes[i - 1])
        trs.append(max(hl, hcp, lcp))
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window) if window else 0.0


# ── 空結果 ────────────────────────────────────────────────────────────────────

def _empty_result(code: str) -> dict:
    return {
        "code":              code,
        "price":             0.0,
        "atr":               0.0,
        "ma20":              0.0,
        "methods":           [],
        "conservative_stop": 0.0,
        "conservative_pct":  0.0,
        "updated_at":        time.strftime("%Y-%m-%d %H:%M"),
        "verdict":           "無法取得資料，請確認股票代碼。",
    }


# ── 結論生成 ──────────────────────────────────────────────────────────────────

def _gen_dynstop_verdict(data: dict) -> str:
    pct = data.get("conservative_pct", 0)
    if pct > 10:
        return "停損空間過大，建議分批進場降低成本"
    if pct < 5:
        return "停損位緊湊，適合追漲型操作"
    return f"停損空間合理（距現價 {pct:.1f}%），可依風險承受度選擇適合方法"


# ── 報告格式化 ────────────────────────────────────────────────────────────────

def format_dynstop_report(data: dict, code: str) -> str:
    if not data.get("methods"):
        return f"❌ {code} 動態停損：無法取得資料"

    price     = data["price"]
    cons_stop = data["conservative_stop"]
    cons_pct  = data["conservative_pct"]
    lines = [
        f"🛡️ {code} 動態停損分析",
        "─" * 28,
        f"現價：{price:,.2f}",
        f"ATR(14)：{data['atr']:,.2f}　MA20：{data['ma20']:,.2f}",
        "",
        "📐 三種停損方法",
    ]
    for m in data["methods"]:
        dist = m["pct_from_price"]
        lines.append(
            f"• {m['name']}\n"
            f"  停損價：{m['stop']:,.2f}（距現價 -{dist:.1f}%）"
        )
    lines += [
        "",
        "─" * 28,
        f"⚠️ 保守停損（最高停損價）",
        f"  建議停損：{cons_stop:,.2f}（距現價 -{cons_pct:.1f}%）",
        "",
        f"📋 建議：{data['verdict']}",
        "",
        f"更新：{data['updated_at']}",
    ]
    return "\n".join(lines)
