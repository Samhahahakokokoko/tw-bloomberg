"""FinMind API 整合服務

免費版限制（無 token）：30 req/min，建議 2s 間隔
有 token 限制更高（每月方案，免費 100 req/day）

設定：在 .env 加入 FINMIND_TOKEN=xxx  （留空則匿名）
"""
import asyncio
import httpx
from datetime import datetime, date, timedelta
from loguru import logger
from typing import Any

FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"
_SEMAPHORE = asyncio.Semaphore(3)   # 同時最多 3 個並發請求
_REQUEST_DELAY = 2.2                # 每次請求後的冷卻時間（秒）


async def _get(dataset: str, stock_id: str, start_date: str, end_date: str = "") -> list[dict]:
    """通用 FinMind GET，帶 rate-limit 和 retry"""
    from ..models.database import settings
    params: dict[str, Any] = {
        "dataset":    dataset,
        "data_id":    stock_id,
        "start_date": start_date,
    }
    if end_date:
        params["end_date"] = end_date
    if settings.finmind_token:
        params["token"] = settings.finmind_token

    async with _SEMAPHORE:
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    resp = await client.get(FINMIND_BASE, params=params)
                    resp.raise_for_status()
                    payload = resp.json()
                    if payload.get("status") != 200:
                        msg = payload.get("msg", "unknown error")
                        logger.warning(f"FinMind {dataset}/{stock_id}: {msg}")
                        return []
                    await asyncio.sleep(_REQUEST_DELAY)
                    return payload.get("data", [])
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    wait = 30 * (attempt + 1)
                    logger.warning(f"FinMind rate limit, waiting {wait}s")
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"FinMind HTTP error {dataset}/{stock_id}: {e}")
                    return []
            except Exception as e:
                logger.error(f"FinMind error {dataset}/{stock_id}: {e}")
                if attempt < 2:
                    await asyncio.sleep(5)
    return []


# ── 調整後股價（還原除權息）────────────────────────────────────────────────────

async def fetch_adj_price(stock_code: str, start_date: str = "", days: int = 120) -> list[dict]:
    """
    TaiwanStockPriceAdj — 還原除權息後的歷史股價
    回傳欄位：date, open, max(high), min(low), close, Trading_Volume
    """
    if not start_date:
        start_date = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    raw = await _get("TaiwanStockPriceAdj", stock_code, start_date)
    out = []
    for r in raw:
        try:
            out.append({
                "date":   r.get("date", ""),
                "open":   float(r.get("open", 0) or 0),
                "high":   float(r.get("max", 0) or 0),
                "low":    float(r.get("min", 0) or 0),
                "close":  float(r.get("close", 0) or 0),
                "volume": int(float(r.get("Trading_Volume", 0) or 0)),
            })
        except (ValueError, TypeError):
            continue
    return sorted(out, key=lambda x: x["date"])


# ── 月營收 ────────────────────────────────────────────────────────────────────

async def fetch_monthly_revenue(stock_code: str, start_date: str = "") -> list[dict]:
    """
    TaiwanStockMonthRevenue
    回傳欄位：date, revenue, revenue_month, revenue_year,
              MonthlyRevenueMoM, MonthlyRevenueYoY, CumulativeRevenue, CumulativeRevenueYoY
    """
    if not start_date:
        start_date = (date.today() - timedelta(days=400)).strftime("%Y-%m-%d")
    raw = await _get("TaiwanStockMonthRevenue", stock_code, start_date)
    out = []
    for r in raw:
        try:
            out.append({
                "date":        r.get("date", ""),
                "year":        int(r.get("revenue_year", 0) or 0),
                "month":       int(r.get("revenue_month", 0) or 0),
                "revenue":     float(r.get("revenue", 0) or 0),
                "mom":         float(r.get("MonthlyRevenueMoM", 0) or 0),
                "yoy":         float(r.get("MonthlyRevenueYoY", 0) or 0),
                "cum_revenue": float(r.get("CumulativeRevenue", 0) or 0),
                "cum_yoy":     float(r.get("CumulativeRevenueYoY", 0) or 0),
            })
        except (ValueError, TypeError):
            continue
    return sorted(out, key=lambda x: x["date"])


# ── 財務報表 ──────────────────────────────────────────────────────────────────

async def fetch_financials(stock_code: str, start_date: str = "") -> list[dict]:
    """
    TaiwanFinancialStatements — 季度損益表
    關鍵 type 值：Revenue、GrossProfit、OperatingIncome、NetIncome、EPS
    回傳整理後的季度結構
    """
    if not start_date:
        start_date = (date.today() - timedelta(days=730)).strftime("%Y-%m-%d")
    raw = await _get("TaiwanFinancialStatements", stock_code, start_date)

    # 依日期分組
    by_date: dict[str, dict] = {}
    for r in raw:
        d    = r.get("date", "")
        typ  = r.get("type", "")
        val  = float(r.get("value", 0) or 0)
        if d not in by_date:
            by_date[d] = {"date": d}
        by_date[d][typ] = val

    out = []
    for d, row in sorted(by_date.items()):
        revenue  = row.get("Revenue", 0)
        gross    = row.get("GrossProfit", 0)
        op_inc   = row.get("OperatingIncome", 0)
        net_inc  = row.get("NetIncome", 0)
        eps      = row.get("EPS", None)
        try:
            # 解析年/季
            dt = datetime.strptime(d, "%Y-%m-%d")
            year    = dt.year
            quarter = (dt.month - 1) // 3 + 1
        except ValueError:
            continue
        out.append({
            "date":             d,
            "year":             year,
            "quarter":          quarter,
            "revenue":          revenue,
            "gross_profit":     gross,
            "operating_income": op_inc,
            "net_income":       net_inc,
            "eps":              eps,
            "gross_margin":     round(gross / revenue * 100, 2) if revenue else 0,
            "operating_margin": round(op_inc / revenue * 100, 2) if revenue else 0,
            "net_margin":       round(net_inc / revenue * 100, 2) if revenue else 0,
        })
    return out


# ── 三大法人（詳細）──────────────────────────────────────────────────────────

async def fetch_institutional_detail(stock_code: str, start_date: str = "") -> list[dict]:
    """
    TaiwanStockInstitutionalInvestors
    name: 外陸資、外陸資(不含外資自營商)、投信、自營商
    回傳每日外資/投信/自營商的 buy/sell/diff（張）
    """
    if not start_date:
        start_date = (date.today() - timedelta(days=60)).strftime("%Y-%m-%d")
    raw = await _get("TaiwanStockInstitutionalInvestors", stock_code, start_date)

    by_date: dict[str, dict] = {}
    for r in raw:
        d    = r.get("date", "")
        name = r.get("name", "")
        buy  = int(float(r.get("buy", 0) or 0))
        sell = int(float(r.get("sell", 0) or 0))
        diff = int(float(r.get("diff", 0) or 0))
        if d not in by_date:
            by_date[d] = {"date": d, "foreign_buy": 0, "foreign_sell": 0, "foreign_net": 0,
                          "trust_buy": 0, "trust_sell": 0, "trust_net": 0,
                          "dealer_buy": 0, "dealer_sell": 0, "dealer_net": 0}
        if "外陸資" in name and "自營" not in name:
            by_date[d]["foreign_buy"]  += buy
            by_date[d]["foreign_sell"] += sell
            by_date[d]["foreign_net"]  += diff
        elif name == "投信":
            by_date[d]["trust_buy"]  = buy
            by_date[d]["trust_sell"] = sell
            by_date[d]["trust_net"]  = diff
        elif "自營商" in name:
            by_date[d]["dealer_buy"]  += buy
            by_date[d]["dealer_sell"] += sell
            by_date[d]["dealer_net"]  += diff

    for row in by_date.values():
        row["total_net"] = row["foreign_net"] + row["trust_net"] + row["dealer_net"]

    return sorted(by_date.values(), key=lambda x: x["date"])


# ── 大戶持股比例 ───────────────────────────────────────────────────────────────

async def fetch_shareholding(stock_code: str, start_date: str = "") -> list[dict]:
    """
    TaiwanStockShareholding — 股權分散表
    回傳 1000張以上大戶持股比例
    """
    if not start_date:
        start_date = (date.today() - timedelta(days=120)).strftime("%Y-%m-%d")
    raw = await _get("TaiwanStockShareholding", stock_code, start_date)

    by_date: dict[str, float] = {}
    for r in raw:
        d     = r.get("date", "")
        level = r.get("HoldingSharesLevel", "")
        ratio = float(r.get("HoldingSharesRatio", 0) or 0)
        # 累加 1000張以上的持股比例
        if level in ("1,000-", "2,000-", "5,000-", "10,000-", "15,000-", "20,000-",
                     "30,000-", "40,000-", "50,000-", "100,000-", "200,000-",
                     "400,000-", "600,000-", "800,000-", "1,000,000 shares and over"):
            by_date[d] = by_date.get(d, 0) + ratio

    return [{"date": d, "large_holder_ratio": round(r, 2)}
            for d, r in sorted(by_date.items())]


# ── 市場股票清單 ───────────────────────────────────────────────────────────────

async def fetch_tw_stock_info() -> list[dict]:
    """TaiwanStockInfo — 取得所有上市上櫃股票基本資訊"""
    try:
        from ..models.database import settings
        params: dict[str, Any] = {"dataset": "TaiwanStockInfo"}
        if settings.finmind_token:
            params["token"] = settings.finmind_token
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(FINMIND_BASE, params=params)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            return [
                {
                    "code":     r.get("stock_id", ""),
                    "name":     r.get("stock_name", ""),
                    "industry": r.get("industry_category", ""),
                    "market":   r.get("type", ""),
                }
                for r in data
                if r.get("stock_id", "").isdigit() and len(r.get("stock_id", "")) == 4
            ]
    except Exception as e:
        logger.error(f"FinMind stock info error: {e}")
        return []
