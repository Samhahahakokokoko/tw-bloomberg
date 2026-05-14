"""TWSE OpenAPI 串接服務 — 即時報價、K線、三大法人"""
import httpx
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger


TWSE_BASE = "https://openapi.twse.com.tw/v1"
TPEX_BASE = "https://www.tpex.org.tw/openapi/v1"


async def fetch_twse_quote(stock_code: str) -> dict:
    """抓即時報價（TWSE 上市）"""
    url = f"{TWSE_BASE}/exchangeReport/STOCK_DAY_ALL"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            for item in data:
                if item.get("Code") == stock_code:
                    return _normalize_twse_quote(item)
    except Exception as e:
        logger.error(f"TWSE quote error for {stock_code}: {e}")
    return {}


async def fetch_realtime_quote(stock_code: str) -> dict:
    """
    即時報價查詢順序：
    1. TWSE STOCK_DAY_ALL（上市收盤資料）
    2. MIS tse_ 端點（上市盤中）← 在 TPEX 之前，避免同代號跨市場污染
    3. TPEX mainboard（上櫃收盤）
    4. MIS otc_ 端點（上櫃盤中）

    背景：TWSE/TPEX 可能同時存在相同代號但不同公司（例如 1815 在 TWSE 是富喬，
    在 TPEX 是另一家）。必須以上市交易所為優先，不能讓 TPEX fallback 覆蓋掉
    找不到當日成交的上市股。
    """
    # 1. TWSE 上市收盤資料
    url = f"{TWSE_BASE}/exchangeReport/STOCK_DAY_ALL"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            for item in resp.json():
                if item.get("Code") == stock_code:
                    return _normalize_twse_quote(item)
    except Exception as e:
        logger.error(f"TWSE quote error for {stock_code}: {e}")

    # 2. MIS 上市盤中（tse 端點）— 先試上市，才能判斷此股是否為 TWSE 股票
    #    即使當天停牌（price=0），只要有 msgArray 就說明它是上市股，直接 return，
    #    不再往下查 TPEX 以免拿到同代號的上櫃公司名稱。
    tse_result = await _fetch_twse_mi_single(stock_code, "tse")
    if tse_result:
        return tse_result

    # 3. TPEX 上櫃收盤資料（只有在確定不是上市股時才查）
    url = f"{TPEX_BASE}/tpex_mainboard_daily_close_quotes"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            for item in resp.json():
                if item.get("SecuritiesCompanyCode") == stock_code:
                    return _normalize_tpex_quote(item)
    except Exception as e:
        logger.error(f"TPEX quote error for {stock_code}: {e}")

    # 4. MIS 上櫃盤中（otc 端點）
    otc_result = await _fetch_twse_mi_single(stock_code, "otc")
    if otc_result:
        return otc_result

    return {}


async def _fetch_twse_mi_single(stock_code: str, market: str) -> dict:
    """MIS 盤中即時 — 指定單一市場 (tse 或 otc)"""
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={market}_{stock_code}.tw"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            data = resp.json()
            msgs = data.get("msgArray", [])
            if msgs:
                item = msgs[0]
                name = item.get("n", "")
                # 確保 name 有值才算有效（避免空殼回應）
                if not name:
                    return {}
                def _safe_float(v, default=0.0):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return default
                return {
                    "code":       stock_code,
                    "name":       name,
                    "price":      _safe_float(item.get("z")),
                    "open":       _safe_float(item.get("o")),
                    "high":       _safe_float(item.get("h")),
                    "low":        _safe_float(item.get("l")),
                    "volume":     int(item.get("v", 0) or 0),
                    "change":     _safe_float(item.get("y")),
                    "change_pct": _calc_change_pct(item.get("z"), item.get("y")),
                    "timestamp":  datetime.now().isoformat(),
                }
    except Exception as e:
        logger.error(f"MI fetch error ({market}_{stock_code}): {e}")
    return {}


async def _fetch_twse_mi(stock_code: str) -> dict:
    """MIS 盤中即時：先試上市(tse)，再試上櫃(otc)（保留給外部舊呼叫用）"""
    for market in ("tse", "otc"):
        result = await _fetch_twse_mi_single(stock_code, market)
        if result:
            return result
    return {}


async def fetch_kline(stock_code: str, date: Optional[str] = None) -> list[dict]:
    """
    歷史 K 線資料。

    資料來源優先順序：
      1. TradingView (tvDatafeed) — 最多 5000 根、日/週/月線、含技術面
      2. Yahoo Finance (yfinance)  — 6 個月、已還原除權息
      3. TWSE STOCK_DAY API (備援) — 3 個月、逐月請求
    """
    # ── 1. TradingView (tvDatafeed) ──────────────────────────────────────────
    try:
        from .tvdatafeed_service import fetch_kline_tv
        records = await fetch_kline_tv(stock_code, interval="daily", n_bars=180)
        if records:
            return records
        logger.warning(f"[kline] tvdatafeed 無資料 ({stock_code})，嘗試 yfinance")
    except Exception as e:
        logger.warning(f"[kline] tvdatafeed 失敗 ({stock_code}): {e}，嘗試 yfinance")

    # ── 2. Yahoo Finance (yfinance) ───────────────────────────────────────────
    try:
        from .yfinance_service import fetch_kline_yf
        records = await fetch_kline_yf(stock_code, months=6)
        if records:
            return records
        logger.warning(f"[kline] yfinance 無資料 ({stock_code})，改用 TWSE API")
    except Exception as e:
        logger.warning(f"[kline] yfinance 失敗 ({stock_code}): {e}，改用 TWSE API")

    # ── 3. TWSE STOCK_DAY 月別 API ────────────────────────────────────────────
    return await _fetch_kline_twse(stock_code)


async def _fetch_kline_twse(stock_code: str) -> list[dict]:
    """TWSE STOCK_DAY 月別 API — fetch_kline 的備援來源，抓近 3 個月"""
    results: list[dict] = []
    today = datetime.now()
    months = [
        (today.year, today.month),
        (today.year if today.month > 1 else today.year - 1,
         today.month - 1 if today.month > 1 else 12),
        (today.year if today.month > 2 else today.year - 1,
         today.month - 2 if today.month > 2 else today.month + 10),
    ]
    for y, m in months:
        date_str = f"{y}{m:02d}01"
        url = (f"https://www.twse.com.tw/exchangeReport/STOCK_DAY"
               f"?response=json&date={date_str}&stockNo={stock_code}")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                payload = resp.json()
                for row in payload.get("data", []):
                    parsed = _normalize_kline(row)
                    if parsed["date"]:
                        results.append(parsed)
        except Exception as e:
            logger.error(f"[kline] TWSE error {stock_code} {date_str}: {e}")

    seen: set = set()
    unique: list[dict] = []
    for r in sorted(results, key=lambda x: x["date"]):
        if r["date"] not in seen:
            seen.add(r["date"])
            unique.append(r)
    return unique


async def fetch_institutional(stock_code: str) -> dict:
    """三大法人買賣超"""
    url = f"{TWSE_BASE}/fund/TWT38U"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            for item in data:
                if item.get("Code") == stock_code:
                    return {
                        "code": stock_code,
                        "foreign_net": _parse_int(item.get("Foreign_Investor_Diff")),
                        "investment_trust_net": _parse_int(item.get("Investment_Trust_Diff")),
                        "dealer_net": _parse_int(item.get("Dealer_Diff")),
                        "total_net": _parse_int(item.get("Total_Diff")),
                        "date": item.get("Date", ""),
                    }
    except Exception as e:
        logger.error(f"Institutional error for {stock_code}: {e}")
    return {}


async def fetch_market_overview() -> dict:
    """大盤指數概況"""
    url = f"{TWSE_BASE}/exchangeReport/MI_INDEX"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            taiex = next((x for x in data if x.get("指數") == "發行量加權股價指數"), None)
            if taiex:
                sign = 1 if taiex.get("漲跌", "+") == "+" else -1
                change = sign * float(str(taiex.get("漲跌點數", "0")).replace(",", ""))
                pct = sign * float(str(taiex.get("漲跌百分比", "0")).replace(",", ""))
                return {
                    "index": "TAIEX",
                    "value": float(str(taiex.get("收盤指數", "0")).replace(",", "")),
                    "change": change,
                    "change_pct": pct,
                    "date": taiex.get("日期", ""),
                }
    except Exception as e:
        logger.error(f"Market overview error: {e}")
    return {}


async def fetch_stock_list() -> list[dict]:
    """全部上市股票清單"""
    url = f"{TWSE_BASE}/exchangeReport/STOCK_DAY_ALL"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return [
                {"code": x["Code"], "name": x["Name"], "market": "TWSE"}
                for x in resp.json()
                if x.get("Code", "").isdigit()
            ]
    except Exception as e:
        logger.error(f"Stock list error: {e}")
    return []


def _normalize_twse_quote(item: dict) -> dict:
    def _sf(v, default=0.0):
        """安全解析，處理 '--'、'X...' 等非數值格式"""
        try:
            return float(str(v or "").replace(",", ""))
        except (ValueError, TypeError):
            return default

    return {
        "code": item.get("Code", ""),
        "name": item.get("Name", ""),
        "price": _sf(item.get("ClosingPrice")),
        "open": _sf(item.get("OpeningPrice")),
        "high": _sf(item.get("HighestPrice")),
        "low": _sf(item.get("LowestPrice")),
        "volume": _parse_int(item.get("TradeVolume")) // 1000,  # 股 → 張
        "change": _sf(item.get("Change")),
        "change_pct": _calc_pct(item.get("ClosingPrice"), item.get("Change")),
        "timestamp": datetime.now().isoformat(),
    }


def _normalize_tpex_quote(item: dict) -> dict:
    def _f(v):
        try:
            return float(str(v).replace(",", "") or 0)
        except Exception:
            return 0.0
    return {
        "code": item.get("SecuritiesCompanyCode", ""),
        "name": item.get("CompanyName", ""),
        "price": _f(item.get("Close")),
        "open": _f(item.get("Open")),
        "high": _f(item.get("High")),
        "low": _f(item.get("Low")),
        "volume": _parse_int(item.get("TradingShares")) // 1000,  # 股 → 張
        "change": _f(item.get("Change")),
        "change_pct": _calc_pct(item.get("Close"), item.get("Change")),
        "timestamp": datetime.now().isoformat(),
    }


def _normalize_kline(row: list) -> dict:
    def _to_float(val):
        try:
            return float(str(val).replace(",", ""))
        except Exception:
            return 0.0

    raw_date = row[0] if len(row) > 0 else ""
    # 民國年 "115/04/01" → "2026-04-01"
    try:
        parts = raw_date.split("/")
        if len(parts) == 3:
            western_year = int(parts[0]) + 1911
            raw_date = f"{western_year}-{parts[1]}-{parts[2]}"
    except Exception:
        pass

    return {
        "date": raw_date,
        "volume": _parse_int(str(row[1]).replace(",", "")) if len(row) > 1 else 0,
        "open": _to_float(row[3]) if len(row) > 3 else 0,
        "high": _to_float(row[4]) if len(row) > 4 else 0,
        "low": _to_float(row[5]) if len(row) > 5 else 0,
        "close": _to_float(row[6]) if len(row) > 6 else 0,
    }


def _parse_int(val) -> int:
    try:
        return int(str(val).replace(",", ""))
    except Exception:
        return 0


def _calc_change_pct(price, yesterday) -> float:
    try:
        p, y = float(price), float(yesterday)
        return round((p - y) / y * 100, 2) if y else 0.0
    except Exception:
        return 0.0


def _calc_pct(closing, change) -> float:
    try:
        c, ch = float(str(closing).replace(",", "")), float(str(change).replace(",", ""))
        prev = c - ch
        return round(ch / prev * 100, 2) if prev else 0.0
    except Exception:
        return 0.0
