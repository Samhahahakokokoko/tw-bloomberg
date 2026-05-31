"""TWSE OpenAPI 串接服務 — 即時報價、K線、三大法人"""
import httpx
from datetime import datetime, timedelta
import time
from typing import Optional
from loguru import logger


TWSE_BASE = "https://openapi.twse.com.tw/v1"
TPEX_BASE = "https://www.tpex.org.tw/openapi/v1"


def _safe_float(v, default=0.0):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _first_level(raw: str) -> float:
    if not raw:
        return 0.0
    return _safe_float(str(raw).split("_")[0])


def _twse_date_to_iso(raw: str) -> str:
    value = str(raw or "").strip()
    if len(value) == 8 and value.isdigit() and value.startswith("20"):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    if len(value) == 7 and value.isdigit():
        year = int(value[:3]) + 1911
        return f"{year:04d}-{value[3:5]}-{value[5:]}"
    return value


def _is_today_yyyymmdd(raw: str) -> bool:
    value = str(raw or "").strip()
    today = datetime.now()
    if value == today.strftime("%Y%m%d"):
        return True
    # ROC year format (YYYMMDD, 7 digits): e.g. 1140528
    roc_today = f"{today.year - 1911:03d}{today.strftime('%m%d')}"
    return value == roc_today


async def fetch_twse_quote(stock_code: str) -> dict:
    """Fetch latest TWSE daily close quote."""
    return await _fetch_twse_daily_quote(stock_code)


async def fetch_realtime_quote(stock_code: str) -> dict:
    """Fetch quote with live MIS first; daily close is fallback only."""
    for market in ("tse", "otc"):
        result = await _fetch_twse_mi_single(stock_code, market)
        if result:
            return result

    result = await _fetch_twse_daily_quote(stock_code)
    if result:
        return result

    return await _fetch_tpex_daily_quote(stock_code)


async def _fetch_twse_daily_quote(stock_code: str) -> dict:
    url = f"{TWSE_BASE}/exchangeReport/STOCK_DAY_ALL"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                url,
                params={"_": str(int(time.time() * 1000))},
                headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
            )
            resp.raise_for_status()
            for item in resp.json():
                if item.get("Code") == stock_code:
                    return _normalize_twse_quote(item)
    except Exception as e:
        logger.error(f"TWSE quote error for {stock_code}: {e}")
    return {}


async def _fetch_tpex_daily_quote(stock_code: str) -> dict:
    url = f"{TPEX_BASE}/tpex_mainboard_daily_close_quotes"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                url,
                params={"_": str(int(time.time() * 1000))},
                headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
            )
            resp.raise_for_status()
            for item in resp.json():
                if item.get("SecuritiesCompanyCode") == stock_code:
                    return _normalize_tpex_quote(item)
    except Exception as e:
        logger.error(f"TPEX quote error for {stock_code}: {e}")
    return {}


async def _fetch_twse_mi_single(stock_code: str, market: str) -> dict:
    """Fetch live quote from TWSE MIS for one market: tse or otc."""
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={market}_{stock_code}.tw"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                params={"json": "1", "delay": "0", "_": str(int(time.time() * 1000))},
                headers={
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "Referer": f"https://mis.twse.com.tw/stock/fibest.jsp?stock={stock_code}",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            msgs = data.get("msgArray", [])
            if not msgs:
                return {}

            item = msgs[0]
            name = item.get("n", "")
            if not name:
                return {}

            price = _safe_float(item.get("z")) or _safe_float(item.get("pz"))
            if price <= 0:
                best_bid = _first_level(item.get("b", ""))
                best_ask = _first_level(item.get("a", ""))
                if best_bid and best_ask:
                    price = round((best_bid + best_ask) / 2, 2)
                else:
                    price = best_bid or best_ask
            prev_close = _safe_float(item.get("y"))
            if price <= 0:
                return {}

            change = price - prev_close if prev_close else 0.0
            trade_date = item.get("d") or data.get("queryTime", {}).get("sysDate", "")
            trade_time = item.get("t") or item.get("%") or data.get("queryTime", {}).get("sysTime", "")
            return {
                "code": stock_code,
                "name": name,
                "price": price,
                "open": _safe_float(item.get("o")),
                "high": _safe_float(item.get("h")),
                "low": _safe_float(item.get("l")),
                "volume": int(item.get("v", 0) or 0),
                "change": round(change, 4),
                "change_pct": round(change / prev_close * 100, 2) if prev_close else 0.0,
                "date": trade_date,
                "time": trade_time,
                "source": f"twse_mis_{market}",
                "source_label": "TWSE MIS 即時",
                "is_realtime": True,
                "is_stale": not _is_today_yyyymmdd(trade_date),
                "data_status": "realtime" if _is_today_yyyymmdd(trade_date) else "stale_realtime",
                "as_of": f"{_twse_date_to_iso(trade_date)} {trade_time}".strip(),
                "timestamp": f"{trade_date} {trade_time}".strip(),
            }
    except Exception as e:
        logger.error(f"MI fetch error ({market}_{stock_code}): {e}")
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
    """三大法人買賣超 — classic TWSE T86 端點（OpenAPI 已 302 失效）"""
    for delta in range(4):
        date_str = (datetime.now() - timedelta(days=delta)).strftime("%Y%m%d")
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                resp = await client.get(
                    "https://www.twse.com.tw/fund/T86",
                    params={"response": "json", "date": date_str},
                )
            if resp.status_code != 200:
                continue
            if "json" not in resp.headers.get("content-type", ""):
                continue
            d = resp.json()
            if d.get("stat") != "OK" or not d.get("data"):
                continue

            fields = d.get("fields", [])

            def _fi(keyword: str) -> Optional[int]:
                for i, f in enumerate(fields):
                    if keyword in f:
                        return i
                return None

            foreign_i = _fi("外陸資淨") or _fi("外資淨") or 4
            trust_i   = _fi("投信淨") or 7
            dealer_candidates = [i for i, f in enumerate(fields) if "自營商淨" in f]
            dealer_i  = dealer_candidates[-1] if dealer_candidates else 16
            total_i   = _fi("三大法人淨") or 19

            for row in d["data"]:
                if row[0].strip() != stock_code:
                    continue
                return {
                    "code": stock_code,
                    "foreign_net": _parse_int(row[foreign_i]) if foreign_i < len(row) else 0,
                    "investment_trust_net": _parse_int(row[trust_i]) if trust_i < len(row) else 0,
                    "dealer_net": _parse_int(row[dealer_i]) if dealer_i < len(row) else 0,
                    "total_net": _parse_int(row[total_i]) if total_i < len(row) else 0,
                    "date": d.get("date", ""),
                }
        except Exception as e:
            logger.error(f"Institutional T86 error {stock_code} {date_str}: {e}")
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
    date = item.get("Date", "")
    iso_date = _twse_date_to_iso(date)
    return {
        "code": item.get("Code", ""),
        "name": item.get("Name", ""),
        "price": _safe_float(item.get("ClosingPrice")),
        "open": _safe_float(item.get("OpeningPrice")),
        "high": _safe_float(item.get("HighestPrice")),
        "low": _safe_float(item.get("LowestPrice")),
        "volume": _parse_int(item.get("TradeVolume")) // 1000,
        "change": _safe_float(item.get("Change")),
        "change_pct": _calc_pct(item.get("ClosingPrice"), item.get("Change")),
        "date": date,
        "source": "twse_daily_close",
        "source_label": "TWSE 收盤",
        "is_realtime": False,
        "is_stale": iso_date != datetime.now().strftime("%Y-%m-%d"),
        "data_status": "daily_close",
        "as_of": iso_date,
        "timestamp": date,
    }


def _normalize_tpex_quote(item: dict) -> dict:
    date = item.get("Date", "")
    iso_date = _twse_date_to_iso(date)
    return {
        "code": item.get("SecuritiesCompanyCode", ""),
        "name": item.get("CompanyName", ""),
        "price": _safe_float(item.get("Close")),
        "open": _safe_float(item.get("Open")),
        "high": _safe_float(item.get("High")),
        "low": _safe_float(item.get("Low")),
        "volume": _parse_int(item.get("TradingShares")) // 1000,
        "change": _safe_float(item.get("Change")),
        "change_pct": _calc_pct(item.get("Close"), item.get("Change")),
        "date": date,
        "source": "tpex_daily_close",
        "source_label": "TPEx 收盤",
        "is_realtime": False,
        "is_stale": iso_date != datetime.now().strftime("%Y-%m-%d"),
        "data_status": "daily_close",
        "as_of": iso_date,
        "timestamp": date,
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
