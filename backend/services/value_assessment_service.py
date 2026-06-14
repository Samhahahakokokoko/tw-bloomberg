"""Value Assessment Service — 個股多維度價值評估"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 1800  # 30 min

# Sector PE benchmarks (approximate Taiwan market)
SECTOR_PE: dict[str, float] = {
    "半導體":   22.0, "電子":    18.0, "金融":    12.0, "傳產":    14.0,
    "航運":     10.0, "生技":    35.0, "電動車":  28.0, "通訊":    20.0,
    "default":  18.0,
}


async def get_value_assessment(code: str) -> dict:
    key = code.strip()
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_value(key)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_value(code: str) -> dict:
    import asyncio
    quote_task  = _get_quote(code)
    hist_task   = _get_hist_pe(code)
    finance_task = _get_financials(code)

    quote, hist_pe, financials = await asyncio.gather(
        quote_task, hist_task, finance_task, return_exceptions=True
    )
    quote      = quote      if isinstance(quote, dict)      else {}
    hist_pe    = hist_pe    if isinstance(hist_pe, dict)    else {}
    financials = financials if isinstance(financials, dict) else {}

    current_price = float(quote.get("close") or quote.get("price") or 0)
    eps     = financials.get("eps", 0)
    bps     = financials.get("bps", 0)
    dps     = financials.get("dps", 0)
    revenue = financials.get("revenue", 0)

    pe  = round(current_price / eps,     2) if eps  > 0 else None
    pb  = round(current_price / bps,     2) if bps  > 0 else None
    div = round(dps / current_price * 100, 2) if current_price > 0 and dps > 0 else None
    ps  = round(current_price / (revenue / 1e8), 2) if revenue > 0 else None  # price/sales per share

    pe_pct  = _pe_percentile(pe,  hist_pe.get("pe_hist",  []))
    pb_pct  = _pb_percentile(pb,  hist_pe.get("pb_hist",  []))
    dcf_val = _dcf_estimate(eps, financials.get("growth_rate", 0.05))
    dcf_upside = round((dcf_val / current_price - 1) * 100, 1) if current_price > 0 and dcf_val > 0 else None

    score, grade, verdict = _composite_grade(pe, pe_pct, pb, pb_pct, div, dcf_upside)

    return {
        "code":          code,
        "name":          quote.get("name", code),
        "price":         current_price,
        "eps":           eps,
        "bps":           bps,
        "dps":           dps,
        "pe":            pe,
        "pb":            pb,
        "div_yield":     div,
        "ps":            ps,
        "pe_pct":        pe_pct,
        "pb_pct":        pb_pct,
        "dcf_value":     dcf_val,
        "dcf_upside":    dcf_upside,
        "score":         score,
        "grade":         grade,
        "verdict":       verdict,
        "updated_at":    time.strftime("%Y-%m-%d %H:%M"),
    }


async def _get_quote(code: str) -> dict:
    try:
        from .twse_service import fetch_realtime_quote
        q = await fetch_realtime_quote(code)
        return q or {}
    except Exception as e:
        logger.debug(f"[value] quote {code}: {e}")
        return {}


async def _get_hist_pe(code: str) -> dict:
    try:
        import httpx
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW?interval=1mo&range=2y"
        async with httpx.AsyncClient(timeout=10) as cl:
            r = await cl.get(url, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        closes = data["chart"]["result"][0]["indicators"]["quote"][0].get("close", [])
        closes = [c for c in closes if c]
        return {"pe_hist": closes, "pb_hist": closes}
    except Exception as e:
        logger.debug(f"[value] hist_pe {code}: {e}")
        return {}


async def _get_financials(code: str) -> dict:
    try:
        import httpx
        url = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{code}.TW"
               f"?modules=defaultKeyStatistics,financialData,summaryDetail")
        async with httpx.AsyncClient(timeout=12) as cl:
            r = await cl.get(url, headers={"User-Agent": "Mozilla/5.0"})
        js = r.json()
        res = js.get("quoteSummary", {}).get("result", [{}])[0]
        ks  = res.get("defaultKeyStatistics", {})
        fd  = res.get("financialData", {})
        sd  = res.get("summaryDetail", {})

        def _v(d, k):
            val = d.get(k, {})
            return val.get("raw", 0) if isinstance(val, dict) else val or 0

        eps     = _v(ks, "trailingEps")
        bps     = _v(ks, "bookValue")
        dps     = _v(sd, "dividendRate")
        revenue = _v(fd, "totalRevenue")
        growth  = _v(fd, "revenueGrowth") or 0.05

        return {"eps": eps, "bps": bps, "dps": dps, "revenue": revenue, "growth_rate": growth}
    except Exception as e:
        logger.debug(f"[value] financials {code}: {e}")
        return {}


def _pe_percentile(pe: float | None, hist: list) -> float | None:
    if pe is None or not hist:
        return None
    below = sum(1 for p in hist if p < pe)
    return round(below / len(hist) * 100, 0)


def _pb_percentile(pb: float | None, hist: list) -> float | None:
    return _pe_percentile(pb, hist)


def _dcf_estimate(eps: float, growth: float, years: int = 5, discount: float = 0.10) -> float:
    if eps <= 0:
        return 0
    g = min(growth, 0.30)
    pv = 0.0
    for y in range(1, years + 1):
        pv += eps * (1 + g) ** y / (1 + discount) ** y
    terminal = eps * (1 + g) ** years * (1 + 0.03) / (discount - 0.03)
    pv += terminal / (1 + discount) ** years
    return round(pv, 1)


def _composite_grade(pe, pe_pct, pb, pb_pct, div_yield, dcf_upside):
    score = 0
    reasons = []

    if pe_pct is not None:
        if pe_pct < 25:   score += 30; reasons.append(f"PE分位低({pe_pct:.0f}%)")
        elif pe_pct < 50: score += 15; reasons.append(f"PE中性({pe_pct:.0f}%)")
        else:             reasons.append(f"PE偏高({pe_pct:.0f}%)")

    if pb_pct is not None:
        if pb_pct < 25:   score += 25; reasons.append("PB低估")
        elif pb_pct < 50: score += 10

    if div_yield is not None:
        if div_yield >= 5:   score += 25; reasons.append(f"高殖利率{div_yield:.1f}%")
        elif div_yield >= 3: score += 15; reasons.append(f"殖利率{div_yield:.1f}%")

    if dcf_upside is not None:
        if dcf_upside >= 20:   score += 20; reasons.append(f"DCF低估{dcf_upside:.0f}%")
        elif dcf_upside >= 0:  score += 10
        else:                  reasons.append(f"DCF高估{abs(dcf_upside):.0f}%")

    if score >= 70:   grade = "A — 深度低估"
    elif score >= 50: grade = "B — 合理偏低"
    elif score >= 30: grade = "C — 合理"
    elif score >= 15: grade = "D — 偏貴"
    else:             grade = "F — 高估"

    verdict = "；".join(reasons) if reasons else "資料不足，無法完整評估"
    return score, grade, verdict


def format_value_report(data: dict) -> str:
    if not data or data.get("error"):
        return f"❌ {data.get('error', '無法取得估值資料')}"

    def _fmt(v, unit="", na="N/A"):
        return f"{v:.2f}{unit}" if v else na

    def _pct_bar(pct, w=10):
        if pct is None:
            return "─" * w
        filled = int(pct / 100 * w)
        return "█" * filled + "░" * (w - filled)

    code  = data["code"]; name  = data["name"];  price = data["price"]
    pe    = data["pe"];   pb    = data["pb"];     div   = data["div_yield"]
    ps    = data["ps"];   dcf   = data["dcf_value"]; ups = data["dcf_upside"]
    ppct  = data["pe_pct"]; bpct = data["pb_pct"]
    score = data["score"]; grade = data["grade"]; verdict = data["verdict"]

    lines = [
        f"💎 個股估值  [{code}] {name}",
        "─" * 32, "",
        f"現價：{price:,.1f} 元",
        "",
        "📊 估值指標",
        f"  PE：{_fmt(pe,'x')}  {'[' + _pct_bar(ppct) + f' {ppct:.0f}%]' if ppct else ''}",
        f"  PB：{_fmt(pb,'x')}  {'[' + _pct_bar(bpct) + f' {bpct:.0f}%]' if bpct else ''}",
        f"  殖利率：{_fmt(div,'%')}",
        f"  PS：{_fmt(ps,'x')}",
        "",
        "💡 DCF 估算",
        f"  合理價值：{dcf:,.1f} 元" if dcf else "  DCF：資料不足",
    ]
    if ups is not None:
        updown = "低估" if ups > 0 else "高估"
        lines.append(f"  空間：{updown} {abs(ups):.1f}%")

    lines += [
        "",
        "─" * 28,
        f"綜合評分：{score} / 100",
        f"投資等級：{grade}",
        "",
        "🤖 AI 研判",
        verdict,
        "",
        f"更新：{data['updated_at']}",
        "⚠️ 估值為歷史資料推估，非投資建議",
    ]
    return "\n".join(lines)
