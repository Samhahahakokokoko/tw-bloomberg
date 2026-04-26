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
    """即時報價：優先用 STOCK_DAY_ALL（每日收盤後），盤中 fallback MIS"""
    # 先試 STOCK_DAY_ALL（欄位齊全、穩定）
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
        logger.error(f"Realtime quote error for {stock_code}: {e}")

    # fallback: 盤中即時
    return await _fetch_twse_mi(stock_code)


async def _fetch_twse_mi(stock_code: str) -> dict:
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_code}.tw"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            data = resp.json()
            if data.get("msgArray"):
                item = data["msgArray"][0]
                return {
                    "code": stock_code,
                    "name": item.get("n", ""),
                    "price": float(item.get("z", 0) or 0),
                    "open": float(item.get("o", 0) or 0),
                    "high": float(item.get("h", 0) or 0),
                    "low": float(item.get("l", 0) or 0),
                    "volume": int(item.get("v", 0) or 0),
                    "change": float(item.get("y", 0) or 0),
                    "change_pct": _calc_change_pct(item.get("z"), item.get("y")),
                    "timestamp": datetime.now().isoformat(),
                }
    except Exception as e:
        logger.error(f"MI fetch error: {e}")
    return {}


async def fetch_kline(stock_code: str, date: Optional[str] = None) -> list[dict]:
    """月K線資料（抓近 3 個月合併）"""
    from datetime import date as date_cls
    results: list[dict] = []
    today = datetime.now()
    # 抓近 3 個月
    months = [
        (today.year, today.month),
        (today.year if today.month > 1 else today.year - 1, today.month - 1 if today.month > 1 else 12),
        (today.year if today.month > 2 else today.year - 1, today.month - 2 if today.month > 2 else today.month + 10),
    ]
    for y, m in months:
        date_str = f"{y}{m:02d}01"
        url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={date_str}&stockNo={stock_code}"
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
            logger.error(f"K-line error {stock_code} {date_str}: {e}")
    # 依日期排序、去重
    seen = set()
    unique = []
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
    return {
        "code": item.get("Code", ""),
        "name": item.get("Name", ""),
        "price": float(item.get("ClosingPrice", 0) or 0),
        "open": float(item.get("OpeningPrice", 0) or 0),
        "high": float(item.get("HighestPrice", 0) or 0),
        "low": float(item.get("LowestPrice", 0) or 0),
        "volume": _parse_int(item.get("TradeVolume")),
        "change": float(item.get("Change", 0) or 0),
        "change_pct": _calc_pct(item.get("ClosingPrice"), item.get("Change")),
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
