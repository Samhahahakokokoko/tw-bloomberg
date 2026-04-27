"""籌碼追蹤服務 — 多日三大法人 + 主力成本估算"""
import httpx
from loguru import logger
from .twse_service import fetch_kline


async def fetch_chip_history(stock_code: str, days: int = 20) -> list[dict]:
    """抓近 days 日三大法人歷史 (TWSE T86)"""
    url = (
        f"https://www.twse.com.tw/fund/T86"
        f"?response=json&date=&stockNo={stock_code}&selectType=ALLBUT0999"
    )
    results = []
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("data", [])
            for row in rows:
                if len(row) < 13:
                    continue
                raw_date = str(row[0])
                try:
                    parts = raw_date.split("/")
                    if len(parts) == 3:
                        raw_date = f"{int(parts[0])+1911}-{parts[1]}-{parts[2]}"
                except Exception:
                    pass

                def _n(v):
                    try:
                        return int(str(v).replace(",", ""))
                    except Exception:
                        return 0

                total_net = _n(row[18]) if len(row) > 18 else _n(row[3]) + _n(row[6]) + _n(row[12])
                results.append({
                    "date": raw_date,
                    "foreign_buy": _n(row[1]),
                    "foreign_sell": _n(row[2]),
                    "foreign_net": _n(row[3]),
                    "trust_buy": _n(row[4]),
                    "trust_sell": _n(row[5]),
                    "trust_net": _n(row[6]),
                    "dealer_buy": _n(row[10]),
                    "dealer_sell": _n(row[11]),
                    "dealer_net": _n(row[12]),
                    "total_net": total_net,
                })
    except Exception as e:
        logger.error(f"Chip history error {stock_code}: {e}")

    return results[-days:] if len(results) > days else results


async def estimate_main_force_cost(stock_code: str) -> dict:
    """
    主力成本估算：
    找外資/投信連續淨買超的區間，計算該區間 VWAP 作為估算成本。
    """
    try:
        chip_data = await fetch_chip_history(stock_code, 60)
        kline = await fetch_kline(stock_code)

        if not chip_data or not kline:
            return {"stock_code": stock_code, "estimated_cost": None, "message": "資料不足"}

        kline_map = {k["date"]: k for k in kline}

        # 找總計淨買超的日期
        buy_days = [c["date"] for c in chip_data if c["total_net"] > 0]

        if not buy_days:
            return {
                "stock_code": stock_code,
                "estimated_cost": None,
                "message": "近期無法人淨買超",
                "buy_days_count": 0,
            }

        # 計算買超期間的 VWAP（取最近 20 個買超日）
        total_value = total_vol = 0.0
        for d in buy_days[-20:]:
            k = kline_map.get(d)
            if k and k.get("volume") and k.get("close"):
                close = float(k["close"])
                vol = int(k["volume"])
                total_value += close * vol
                total_vol += vol

        vwap = total_value / total_vol if total_vol else 0.0

        current_kline = kline[-1] if kline else {}
        current_price = float(current_kline.get("close", 0))

        profit_loss_est = (current_price - vwap) / vwap * 100 if vwap else 0.0

        # 估算成本區間 ± 3%
        cost_low = round(vwap * 0.97, 2)
        cost_high = round(vwap * 1.03, 2)

        # 連續買超天數（最近一段）
        consec = 0
        for c in reversed(chip_data):
            if c["total_net"] > 0:
                consec += 1
            else:
                break

        return {
            "stock_code": stock_code,
            "estimated_cost": round(vwap, 2),
            "cost_range_low": cost_low,
            "cost_range_high": cost_high,
            "current_price": round(current_price, 2),
            "profit_loss_pct": round(profit_loss_est, 2),
            "buy_days_count": len(buy_days),
            "consecutive_buy_days": consec,
            "analysis_days": len(chip_data),
            "status": "獲利" if profit_loss_est > 0 else "套牢",
        }
    except Exception as e:
        logger.error(f"Main force cost error {stock_code}: {e}")
        return {"stock_code": stock_code, "estimated_cost": None, "message": str(e)}
