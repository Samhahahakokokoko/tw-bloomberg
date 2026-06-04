"""ETF 分析服務 — 基本查詢、比較、定期定額試算"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from loguru import logger

# ── ETF 靜態資料庫 ─────────────────────────────────────────────────────────────

ETF_META: dict[str, dict] = {
    "0050":  {"name": "元大台灣50",       "fee": 0.32, "category": "大盤型"},
    "0056":  {"name": "元大高股息",        "fee": 0.66, "category": "高股息型"},
    "00878": {"name": "國泰永續高股息",    "fee": 0.65, "category": "高股息型"},
    "00881": {"name": "國泰台灣5G+",      "fee": 0.75, "category": "主題型"},
    "00900": {"name": "富邦特選高股息30",  "fee": 0.74, "category": "高股息型"},
    "00919": {"name": "群益台灣精選高息",  "fee": 0.82, "category": "高股息型"},
    "00929": {"name": "復華台灣科技優息",  "fee": 0.82, "category": "科技高息型"},
}

# 前5大持股（定期人工更新，以官方月報為準）
_ETF_HOLDINGS: dict[str, list[tuple[str, float]]] = {
    "0050":  [("台積電", 32.5), ("鴻海", 5.2), ("聯發科", 4.8), ("廣達", 3.2), ("中信金", 2.1)],
    "0056":  [("統一超", 4.1), ("聯詠", 3.9), ("英業達", 3.5), ("緯創", 3.2), ("光寶科", 3.0)],
    "00878": [("聯詠", 4.2), ("廣達", 3.8), ("英業達", 3.5), ("緯創", 3.2), ("台達電", 3.0)],
    "00881": [("台積電", 28.5), ("聯發科", 8.2), ("鴻海", 6.5), ("台達電", 5.8), ("日月光投控", 4.2)],
    "00900": [("廣達", 4.5), ("英業達", 4.2), ("緯創", 4.0), ("聯詠", 3.8), ("光寶科", 3.5)],
    "00919": [("台積電", 10.2), ("聯發科", 8.5), ("鴻海", 7.2), ("廣達", 6.8), ("台達電", 5.5)],
    "00929": [("台積電", 25.5), ("聯發科", 12.2), ("鴻海", 8.5), ("廣達", 6.8), ("日月光投控", 5.2)],
}

# 近期年化殖利率估計（以近4季配息÷現價計算，為參考值）
_ETF_YIELD: dict[str, float] = {
    "0050":  3.1,
    "0056":  6.8,
    "00878": 6.5,
    "00881": 4.2,
    "00900": 7.5,
    "00919": 7.8,
    "00929": 8.2,
}

SUPPORTED_ETFS = set(ETF_META.keys())


# ── 資料抓取 ───────────────────────────────────────────────────────────────────

async def _fetch_history_prices(code: str) -> list[dict]:
    """取得近13個月日K（yfinance 優先，TWSE 備援）"""
    try:
        from .yfinance_service import fetch_kline_yf
        records = await fetch_kline_yf(code, months=13)
        if records and len(records) >= 20:
            return records
    except Exception as e:
        logger.warning("[etf] yfinance failed for %s: %s", code, e)

    # TWSE 月別備援：抓近 13 個月
    import httpx
    from datetime import datetime
    results: list[dict] = []
    today = datetime.now()
    for offset in range(13):
        m = today.month - offset
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        date_str = f"{y}{m:02d}01"
        url = (
            f"https://www.twse.com.tw/exchangeReport/STOCK_DAY"
            f"?response=json&date={date_str}&stockNo={code}"
        )
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                resp = await client.get(url)
                data = resp.json()
            if data.get("stat") != "OK":
                continue
            for row in data.get("data", []):
                try:
                    # TWSE row: [民國日期, 成交股數, 成交金額, 開盤, 最高, 最低, 收盤, 漲跌, 成交筆數]
                    date_parts = row[0].split("/")
                    iso = f"{int(date_parts[0])+1911}-{date_parts[1]}-{date_parts[2]}"
                    close = float(row[6].replace(",", ""))
                    results.append({"date": iso, "close": close})
                except Exception:
                    continue
        except Exception as e:
            logger.warning("[etf] TWSE kline %s/%s error: %s", y, m, e)
    results.sort(key=lambda x: x["date"])
    return results


def _pct_change(records: list[dict], days_back: int) -> float | None:
    """從日K list 計算 N 天前至今的報酬率"""
    if not records:
        return None
    today_price = records[-1].get("close") or records[-1].get("Close")
    if not today_price:
        return None
    target_date = (date.today() - timedelta(days=days_back)).isoformat()
    # 找最接近目標日期的資料點
    past = None
    for r in records:
        d = r.get("date") or r.get("Date") or ""
        if str(d) <= target_date:
            past = r
        else:
            break
    if not past:
        past = records[0]
    past_price = past.get("close") or past.get("Close")
    if not past_price or past_price == 0:
        return None
    return round((today_price - past_price) / past_price * 100, 2)


# ── 核心分析 ───────────────────────────────────────────────────────────────────

async def get_etf_analysis(code: str) -> dict:
    """取得 ETF 完整分析資料"""
    from .twse_service import fetch_realtime_quote

    code = code.upper()
    meta = ETF_META.get(code)
    if not meta:
        raise ValueError(f"不支援的 ETF：{code}。支援清單：{', '.join(sorted(SUPPORTED_ETFS))}")

    quote_task = asyncio.create_task(fetch_realtime_quote(code))
    hist_task  = asyncio.create_task(_fetch_history_prices(code))

    quote   = await quote_task
    records = await hist_task

    price = quote.get("price", 0)
    ret_1m  = _pct_change(records, 30)
    ret_1y  = _pct_change(records, 365)

    return {
        "code":     code,
        "name":     meta["name"],
        "category": meta["category"],
        "fee":      meta["fee"],
        "price":    price,
        "change":   quote.get("change", 0),
        "change_pct": quote.get("change_pct", 0),
        "ret_1m":   ret_1m,
        "ret_1y":   ret_1y,
        "yield_est": _ETF_YIELD.get(code),
        "holdings": _ETF_HOLDINGS.get(code, []),
    }


async def compare_etfs(code1: str, code2: str) -> tuple[dict, dict]:
    """並行取得兩檔 ETF 分析"""
    a, b = await asyncio.gather(
        get_etf_analysis(code1),
        get_etf_analysis(code2),
        return_exceptions=True,
    )
    if isinstance(a, Exception):
        raise a
    if isinstance(b, Exception):
        raise b
    return a, b  # type: ignore[return-value]


def calculate_dca(price: float, monthly_amount: int) -> dict:
    """定期定額試算（複利模型）"""
    if price <= 0 or monthly_amount <= 0:
        return {}

    shares_per_month = monthly_amount / price
    shares_per_year  = shares_per_month * 12

    def future_value(rate_annual: float, years: int) -> int:
        """每月定額，年化報酬率複利終值"""
        r = rate_annual / 12  # 月利率
        n = years * 12        # 期數
        if r == 0:
            fv = monthly_amount * n
        else:
            fv = monthly_amount * ((1 + r) ** n - 1) / r
        return int(fv)

    return {
        "price":            price,
        "monthly_amount":   monthly_amount,
        "shares_per_month": round(shares_per_month, 1),
        "shares_per_year":  round(shares_per_year, 0),
        "fv_10y_10pct":     future_value(0.10, 10),
        "fv_10y_7pct":      future_value(0.07, 10),
        "fv_5y_10pct":      future_value(0.10, 5),
        "fv_1y_10pct":      future_value(0.10, 1),
        "fv_3y_10pct":      future_value(0.10, 3),
    }


# ── 格式化 ─────────────────────────────────────────────────────────────────────

def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}%"


def format_etf_analysis(d: dict, dca_amount: int = 3000) -> str:
    """格式化單檔 ETF 分析訊息"""
    price    = d["price"]
    chg      = d["change_pct"]
    sign     = "+" if chg >= 0 else ""
    holdings = d["holdings"][:5]
    hold_str = " ／ ".join(f"{n} {w:.0f}%" for n, w in holdings)

    dca = calculate_dca(price, dca_amount)
    dca_lines = ""
    if dca:
        dca_lines = (
            f"\n定期定額建議：\n"
            f"每月投入 ${dca_amount:,} → 約{dca['shares_per_month']:.0f}股\n"
            f"每月投入 ${dca_amount*2:,} → 約{calculate_dca(price, dca_amount*2)['shares_per_month']:.0f}股"
        )

    yield_str = f"{d['yield_est']:.1f}%" if d.get("yield_est") else "N/A"

    return (
        f"📊 {d['code']} {d['name']}\n"
        f"{d['category']} ｜ 費用率 {d['fee']:.2f}%\n"
        f"{'─'*18}\n"
        f"現價：{price:.1f}元 漲跌：{sign}{chg:.1f}%\n"
        f"近1月：{_fmt_pct(d['ret_1m'])}　近1年：{_fmt_pct(d['ret_1y'])}\n"
        f"殖利率：{yield_str}（估）\n"
        f"\n前5大持股：\n{hold_str}"
        f"{dca_lines}"
    )


def format_etf_compare(a: dict, b: dict) -> str:
    """格式化兩檔 ETF 並排比較"""
    def col(v, width=8):
        return str(v).rjust(width)

    header = f"{'':14}{col(a['code'])}  {col(b['code'])}"
    rows = [
        ("現價(元)",  f"{a['price']:.1f}",       f"{b['price']:.1f}"),
        ("近1月",     _fmt_pct(a["ret_1m"]),       _fmt_pct(b["ret_1m"])),
        ("近1年",     _fmt_pct(a["ret_1y"]),       _fmt_pct(b["ret_1y"])),
        ("殖利率",    f"{a['yield_est']:.1f}%" if a.get('yield_est') else "N/A",
                      f"{b['yield_est']:.1f}%" if b.get('yield_est') else "N/A"),
        ("費用率",    f"{a['fee']:.2f}%",          f"{b['fee']:.2f}%"),
        ("類型",      a["category"],               b["category"]),
    ]
    body = "\n".join(f"{label:<10}{col(va)}  {col(vb)}" for label, va, vb in rows)
    return (
        f"📊 ETF 比較：{a['code']} vs {b['code']}\n"
        f"{'─'*18}\n"
        f"{header}\n"
        f"{body}\n"
        f"{'─'*18}\n"
        f"{a['code']} {a['name']}\n"
        f"{b['code']} {b['name']}"
    )


def format_dca(code: str, name: str, d: dict) -> str:
    """格式化定期定額試算訊息"""
    price  = d["price"]
    amt    = d["monthly_amount"]
    return (
        f"💰 定期定額試算\n"
        f"{code} {name}\n"
        f"每月投入：${amt:,}\n"
        f"{'─'*18}\n"
        f"以現價 {price:.1f} 元計算：\n"
        f"每月約可買：{d['shares_per_month']:.1f}股\n"
        f"每年累積：{d['shares_per_year']:.0f}股\n"
        f"\n假設年化報酬 10%（歷史參考）：\n"
        f"1年後估值：${d['fv_1y_10pct']:,}\n"
        f"3年後估值：${d['fv_3y_10pct']:,}\n"
        f"5年後估值：${d['fv_5y_10pct']:,}\n"
        f"10年後估值：${d['fv_10y_10pct']:,}\n"
        f"\n保守估算年化 7%：\n"
        f"10年後估值：${d['fv_10y_7pct']:,}"
    )
