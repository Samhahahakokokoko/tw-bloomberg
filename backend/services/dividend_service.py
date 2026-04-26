"""除權息服務 — TWSE 除權息資料"""
import httpx
from datetime import datetime
from loguru import logger


async def fetch_upcoming_dividends(days_ahead: int = 30) -> list[dict]:
    """抓近期除權息日期（TWSE OpenAPI）"""
    url = "https://openapi.twse.com.tw/v1/exchangeReport/TWT49U"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            results = []
            for item in data:
                results.append({
                    "stock_code": item.get("Code", ""),
                    "stock_name": item.get("Name", ""),
                    "ex_dividend_date": _tw_date(item.get("ExRightDate", "")),
                    "ex_dividend_ref_price": _f(item.get("ExRightReferencePrice")),
                    "cash_dividend": _f(item.get("CashDividend")),
                    "stock_dividend": _f(item.get("StockDividend")),
                    "total_dividend": _f(item.get("TotalDividend")),
                })
            return results
    except Exception as e:
        logger.error(f"Dividend fetch error: {e}")
    return []


async def fetch_dividend_by_code(stock_code: str) -> list[dict]:
    all_divs = await fetch_upcoming_dividends()
    return [d for d in all_divs if d["stock_code"] == stock_code]


async def fetch_historical_dividends(stock_code: str) -> list[dict]:
    """近年配息紀錄（使用 TWSE 年度除權息資料）"""
    year = datetime.now().year
    results = []
    for y in [year, year - 1]:
        url = f"https://www.twse.com.tw/exchangeReport/TWT49U?response=json&strDate={y}0101&endDate={y}1231"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url)
                data = resp.json()
                for row in data.get("data", []):
                    if len(row) > 1 and row[0] == stock_code:
                        results.append({
                            "stock_code": stock_code,
                            "date": row[1] if len(row) > 1 else "",
                            "cash": _f(row[5]) if len(row) > 5 else 0,
                            "stock": _f(row[6]) if len(row) > 6 else 0,
                        })
        except Exception as e:
            logger.error(f"Historical dividend error: {e}")
    return results


def _tw_date(s: str) -> str:
    """民國年 1140301 → 2025-03-01"""
    try:
        s = str(s).strip()
        if len(s) == 7:
            y = int(s[:3]) + 1911
            return f"{y}-{s[3:5]}-{s[5:7]}"
    except Exception:
        pass
    return s


def _f(v) -> float:
    try:
        return float(str(v).replace(",", "") or 0)
    except Exception:
        return 0.0
