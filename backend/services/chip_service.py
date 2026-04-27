"""籌碼追蹤服務 — 多日三大法人 + 主力成本估算

TWSE T86 端點說明：
  URL: https://www.twse.com.tw/fund/T86?response=json&date=YYYYMMDD&stockNo=XXXX&selectType=ALLBUT0999
  - date 為空或省略：僅回傳最新一日
  - date=YYYYMMDD 指定月份第一天：回傳該月全部資料

策略：抓當月 + 前兩個月，合併去重，取最近 days 筆。
"""
import httpx
from datetime import datetime, timedelta
from loguru import logger
from .twse_service import fetch_kline


def _parse_int(v) -> int:
    try:
        return int(str(v).replace(",", ""))
    except Exception:
        return 0


def _tw_to_iso(raw_date: str) -> str:
    """民國年 115/04/01 → 2026-04-01"""
    try:
        parts = raw_date.split("/")
        if len(parts) == 3:
            return f"{int(parts[0]) + 1911}-{parts[1]}-{parts[2]}"
    except Exception:
        pass
    return raw_date


async def _fetch_t86_month(stock_code: str, yyyymmdd: str) -> list[dict]:
    """抓 TWSE T86 指定月份資料"""
    url = (
        "https://www.twse.com.tw/fund/T86"
        f"?response=json&date={yyyymmdd}&stockNo={stock_code}&selectType=ALLBUT0999"
    )
    rows_out = []
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            for row in data.get("data", []):
                if len(row) < 13:
                    continue
                raw_date = _tw_to_iso(str(row[0]))
                total_net = (
                    _parse_int(row[18]) if len(row) > 18
                    else _parse_int(row[3]) + _parse_int(row[6]) + _parse_int(row[12])
                )
                rows_out.append({
                    "date":         raw_date,
                    "foreign_buy":  _parse_int(row[1]),
                    "foreign_sell": _parse_int(row[2]),
                    "foreign_net":  _parse_int(row[3]),
                    "trust_buy":    _parse_int(row[4]),
                    "trust_sell":   _parse_int(row[5]),
                    "trust_net":    _parse_int(row[6]),
                    "dealer_buy":   _parse_int(row[10]),
                    "dealer_sell":  _parse_int(row[11]),
                    "dealer_net":   _parse_int(row[12]),
                    "total_net":    total_net,
                })
    except Exception as e:
        logger.error(f"T86 fetch error {stock_code} {yyyymmdd}: {e}")
    return rows_out


async def fetch_chip_history(stock_code: str, days: int = 20) -> list[dict]:
    """
    抓近 days 日三大法人歷史。
    從當月往前最多取 3 個月，合併後取最近 days 筆。
    """
    now = datetime.now()
    months_to_fetch = []
    for i in range(3):
        # 往前 i 個月的第一天
        first_day = (now.replace(day=1) - timedelta(days=i * 28)).replace(day=1)
        months_to_fetch.append(first_day.strftime("%Y%m01"))

    all_rows: dict[str, dict] = {}
    for yyyymmdd in months_to_fetch:
        rows = await _fetch_t86_month(stock_code, yyyymmdd)
        for r in rows:
            all_rows[r["date"]] = r   # 以日期去重，較新的覆蓋

    # 按日期升序排列，取最近 days 筆
    sorted_rows = sorted(all_rows.values(), key=lambda x: x["date"])
    return sorted_rows[-days:] if len(sorted_rows) > days else sorted_rows


async def estimate_main_force_cost(stock_code: str) -> dict:
    """
    主力成本估算：
    找三大法人連續淨買超的日期，計算買超期間的成交量加權均價（VWAP）
    作為主力持倉估算成本，並計算目前的浮盈/浮虧。
    """
    try:
        chip_data = await fetch_chip_history(stock_code, 60)
        kline     = await fetch_kline(stock_code)

        if not chip_data or not kline:
            return {"stock_code": stock_code, "estimated_cost": None, "message": "資料不足"}

        kline_map = {k["date"]: k for k in kline}

        # 找總計淨買超的日期
        buy_days = [c["date"] for c in chip_data if c["total_net"] > 0]

        if not buy_days:
            return {
                "stock_code":      stock_code,
                "estimated_cost":  None,
                "message":         "近期無法人淨買超",
                "buy_days_count":  0,
                "analysis_days":   len(chip_data),
            }

        # 計算買超期間 VWAP（最近 20 個買超日）
        total_value = total_vol = 0.0
        for d in buy_days[-20:]:
            k = kline_map.get(d)
            if k and k.get("volume") and k.get("close"):
                close = float(k["close"])
                vol   = int(k["volume"])
                total_value += close * vol
                total_vol   += vol

        vwap = total_value / total_vol if total_vol else 0.0

        current_kline = kline[-1] if kline else {}
        current_price = float(current_kline.get("close", 0))

        profit_loss_est = (current_price - vwap) / vwap * 100 if vwap else 0.0

        # 連續買超天數（從最新往回數）
        consec = 0
        for c in reversed(chip_data):
            if c["total_net"] > 0:
                consec += 1
            else:
                break

        # 外資累積買超量（最近 60 日）
        total_foreign = sum(c["foreign_net"] for c in chip_data)
        total_trust   = sum(c["trust_net"]   for c in chip_data)

        return {
            "stock_code":            stock_code,
            "estimated_cost":        round(vwap, 2),
            "cost_range_low":        round(vwap * 0.97, 2),
            "cost_range_high":       round(vwap * 1.03, 2),
            "current_price":         round(current_price, 2),
            "profit_loss_pct":       round(profit_loss_est, 2),
            "buy_days_count":        len(buy_days),
            "consecutive_buy_days":  consec,
            "analysis_days":         len(chip_data),
            "total_foreign_60d":     total_foreign,
            "total_trust_60d":       total_trust,
            "status":                "獲利" if profit_loss_est > 0 else "套牢",
        }
    except Exception as e:
        logger.error(f"Main force cost error {stock_code}: {e}")
        return {"stock_code": stock_code, "estimated_cost": None, "message": str(e)}
