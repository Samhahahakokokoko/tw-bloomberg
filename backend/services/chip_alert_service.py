"""籌碼異動警示服務

針對自選股清單，每日掃描三大法人籌碼異動與融資變化，
偵測到異常時回傳文字警示列表。

支援三種警示：
  A. 投信連續買超後轉賣（反轉訊號）
  B. 外資爆量買超（前5日均值 × 3 以上）
  C. 融資單日暴增超過 15%

使用方式：
    from backend.services.chip_alert_service import check_chip_alerts_async
    alerts = await check_chip_alerts_async(["2330", "2454", "6669"])
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import requests
from loguru import logger

FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"


# ── 工具函式 ──────────────────────────────────────────────────────────────────

def _get_finmind_token() -> str:
    """讀取 FinMind token（若有設定）"""
    try:
        from ..models.database import settings
        return getattr(settings, "finmind_token", "") or ""
    except Exception:
        return ""


def _finmind_get(dataset: str, stock_id: str, start_date: str) -> list[dict]:
    """同步呼叫 FinMind API，回傳 data 列表；失敗時回傳空列表。"""
    params: dict[str, Any] = {
        "dataset":    dataset,
        "data_id":    stock_id,
        "start_date": start_date,
    }
    token = _get_finmind_token()
    if token:
        params["token"] = token

    try:
        resp = requests.get(FINMIND_BASE, params=params, timeout=20)
        if resp.status_code == 200:
            payload = resp.json()
            if payload.get("status") == 200:
                return payload.get("data", [])
            logger.warning(f"[chip_alert] FinMind {dataset}/{stock_id}: {payload.get('msg')}")
    except Exception as e:
        logger.warning(f"[chip_alert] FinMind request error {dataset}/{stock_id}: {type(e).__name__}: {e}")
    return []


def get_daily_net(rows: list[dict], name_set: set[str]) -> list[int]:
    """
    將 FinMind TaiwanStockInstitutionalInvestorsBuySell 的明細列表
    依日期分組、過濾指定 name，計算每日淨買超（張）。

    name_set: 要加總的機構名稱集合，例如 {"Investment_Trust"}
              或 {"Foreign_Investor", "Foreign_Dealer_Self"}

    回傳：依日期升序排列的每日淨買超（張）列表
    """
    daily: dict[str, int] = defaultdict(int)
    for row in rows:
        if row.get("name", "") not in name_set:
            continue
        d   = row.get("date", "")
        buy  = int(float(row.get("buy",  0) or 0))
        sell = int(float(row.get("sell", 0) or 0))
        daily[d] += (buy - sell) // 1000  # 股 → 張

    return [daily[d] for d in sorted(daily.keys())]


# ── 個股警示判斷 ──────────────────────────────────────────────────────────────

def _check_one_stock(code: str) -> list[str]:
    """
    對單一股票執行三項警示檢查。
    回傳該股票觸發的警示文字列表（可能為空）。
    """
    alerts: list[str] = []

    # ── 三大法人資料（近 15 天）─────────────────────────────────────────────
    inst_start = (date.today() - timedelta(days=15)).strftime("%Y-%m-%d")
    inst_rows  = _finmind_get("TaiwanStockInstitutionalInvestorsBuySell", code, inst_start)

    if inst_rows:
        # ── A. 投信連續買超後轉賣 ─────────────────────────────────────────
        trust_series = get_daily_net(inst_rows, {"Investment_Trust"})
        if len(trust_series) >= 6:
            prev5  = trust_series[-6:-1]   # 前5日
            latest = trust_series[-1]       # 今日
            if all(v > 0 for v in prev5) and latest < 0:
                avg = sum(prev5) / len(prev5)
                alerts.append(
                    f"⚠️ {code} 投信轉向！\n"
                    f"前5日均買超 {avg:.0f} 張 → 今日賣超 {abs(latest)} 張\n"
                    f"🔴 反轉訊號，留意下行風險！"
                )

        # ── B. 外資爆量買超（前5日均值 × 3）─────────────────────────────
        foreign_series = get_daily_net(inst_rows, {"Foreign_Investor", "Foreign_Dealer_Self"})
        if len(foreign_series) >= 6:
            prev5_f  = foreign_series[-6:-1]
            today_f  = foreign_series[-1]
            avg_f    = sum(prev5_f) / len(prev5_f) if prev5_f else 0
            if today_f > 0 and avg_f > 0 and today_f > avg_f * 3:
                ratio = today_f / avg_f
                alerts.append(
                    f"🚀 {code} 外資爆量買超！\n"
                    f"今日 {today_f:+,} 張（前5日均 {avg_f:.0f} 張的 {ratio:.1f} 倍）\n"
                    f"💚 強力機構買盤湧入！"
                )

    # ── 融資資料（近 10 天）──────────────────────────────────────────────────
    margin_start = (date.today() - timedelta(days=10)).strftime("%Y-%m-%d")
    margin_rows  = _finmind_get("TaiwanStockMarginPurchaseShortSale", code, margin_start)

    if margin_rows:
        # ── C. 融資單日大增超過 15% ──────────────────────────────────────
        # 依日期升序排列，取 MarginPurchaseTodayBalance 欄位
        daily_margin: dict[str, float] = {}
        for row in margin_rows:
            d   = row.get("date", "")
            bal = float(row.get("MarginPurchaseTodayBalance", 0) or 0)
            if d:
                daily_margin[d] = bal

        sorted_dates = sorted(daily_margin.keys())
        if len(sorted_dates) >= 2:
            prev_date = sorted_dates[-2]
            curr_date = sorted_dates[-1]
            prev_bal  = daily_margin[prev_date]
            curr_bal  = daily_margin[curr_date]

            if prev_bal > 0 and curr_bal > prev_bal * 1.15:
                chg_pct = (curr_bal / prev_bal - 1) * 100
                alerts.append(
                    f"⚠️ {code} 融資暴增！\n"
                    f"單日增加 {chg_pct:.1f}%（{prev_bal:.0f}→{curr_bal:.0f} 張）\n"
                    f"🟡 散戶追高警告，注意過熱風險！"
                )

    return alerts


# ── 公開 async API ────────────────────────────────────────────────────────────

async def check_chip_alerts_async(watch_list: list[str]) -> list[str]:
    """
    非同步掃描自選股清單的籌碼異動。

    內部使用同步 requests 呼叫 FinMind API，
    透過 run_in_executor 逐檔平行執行，避免阻塞 event loop。

    回傳觸發的警示文字列表；若無異動回傳空列表。
    """
    if not watch_list:
        return []

    loop = asyncio.get_running_loop()

    # 在 executor 中並行掃描每檔股票
    tasks = [
        loop.run_in_executor(None, _check_one_stock, code)
        for code in watch_list
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_alerts: list[str] = []
    for code, result in zip(watch_list, results):
        if isinstance(result, Exception):
            logger.error(f"[chip_alert] {code} 掃描失敗: {result}")
            continue
        all_alerts.extend(result)

    return all_alerts
