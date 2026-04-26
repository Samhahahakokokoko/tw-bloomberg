"""融資融券服務 — TWSE 每日融資券餘額"""
import httpx
from loguru import logger


async def fetch_margin_today(stock_code: str) -> dict:
    """抓個股今日融資券餘額（T86）"""
    url = "https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&type=ALL"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            data = resp.json()
            for row in data.get("data", []):
                if len(row) > 0 and row[0] == stock_code:
                    return _normalize_margin(stock_code, row, data.get("date", ""))
    except Exception as e:
        logger.error(f"Margin fetch error for {stock_code}: {e}")
    return {}


async def fetch_margin_list() -> list[dict]:
    """全市場融資券彙總（用於篩選高融資使用率個股）"""
    url = "https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&type=ALL"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            data = resp.json()
            date = data.get("date", "")
            return [_normalize_margin(r[0], r, date) for r in data.get("data", []) if len(r) > 10]
    except Exception as e:
        logger.error(f"Margin list error: {e}")
    return []


async def fetch_margin_history(stock_code: str, date: str = None) -> list[dict]:
    """個股近期融資券歷史"""
    from datetime import datetime
    if not date:
        date = datetime.now().strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY_AVG?response=json&date={date}&stockNo={stock_code}"
    # 使用個股融資券個別查詢
    url2 = f"https://www.twse.com.tw/exchangeReport/BWIBBU_d?response=json&date={date}&stockNo={stock_code}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://www.twse.com.tw/exchangeReport/MARGIN_BUY_SELL?response=json&stockNo={stock_code}&date={date}"
            )
            data = resp.json()
            rows = []
            for r in data.get("data", []):
                if len(r) >= 6:
                    rows.append({
                        "date": _tw_date(r[0]),
                        "margin_buy": _int(r[1]),
                        "margin_sell": _int(r[2]),
                        "margin_balance": _int(r[3]),
                        "short_sell": _int(r[4]),
                        "short_buy": _int(r[5]),
                        "short_balance": _int(r[6]) if len(r) > 6 else 0,
                    })
            return rows
    except Exception as e:
        logger.error(f"Margin history error: {e}")
    return []


def _normalize_margin(code: str, row: list, date: str) -> dict:
    def _i(v): return _int(str(v).replace(",", ""))
    return {
        "stock_code": code,
        "date": date,
        "margin_buy": _i(row[2]) if len(row) > 2 else 0,
        "margin_sell": _i(row[3]) if len(row) > 3 else 0,
        "margin_balance": _i(row[4]) if len(row) > 4 else 0,
        "margin_change": _i(row[5]) if len(row) > 5 else 0,
        "short_sell": _i(row[6]) if len(row) > 6 else 0,
        "short_buy": _i(row[7]) if len(row) > 7 else 0,
        "short_balance": _i(row[8]) if len(row) > 8 else 0,
        "short_change": _i(row[9]) if len(row) > 9 else 0,
    }


def _int(v) -> int:
    try:
        return int(str(v).replace(",", "").replace("+", "") or 0)
    except Exception:
        return 0


def _tw_date(s: str) -> str:
    try:
        s = str(s).strip()
        if "/" in s:
            parts = s.split("/")
            return f"{int(parts[0])+1911}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
    except Exception:
        pass
    return s
