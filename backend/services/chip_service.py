"""籌碼追蹤服務 — 多日三大法人 + 主力成本估算

TWSE T86 端點說明：
  URL: https://www.twse.com.tw/fund/T86?response=json&date=YYYYMMDD&stockNo=XXXX&selectType=ALLBUT0999
  - date 為空或省略：僅回傳最新一日
  - date=YYYYMMDD 指定月份第一天：回傳該月全部資料

策略：抓當月 + 前兩個月，合併去重，取最近 days 筆。
"""
import time
import httpx
from datetime import datetime, timedelta
from loguru import logger
from .twse_service import fetch_kline

# T86 月份資料 TTL 快取：key=(stock_code, yyyymmdd), value=(timestamp, rows)
_T86_CACHE: dict[tuple, tuple] = {}
_T86_TTL = 3600  # 1 hour


def _parse_int(v) -> int:
    try:
        return int(str(v).replace(",", ""))
    except Exception as e:
        return 0


def _tw_to_iso(raw_date: str) -> str:
    """民國年 115/04/01 → 2026-04-01"""
    try:
        parts = raw_date.split("/")
        if len(parts) == 3:
            return f"{int(parts[0]) + 1911}-{parts[1]}-{parts[2]}"
    except Exception as e:
        pass
    return raw_date


async def _fetch_t86_month(stock_code: str, yyyymmdd: str) -> list[dict]:
    """抓 TWSE T86 指定月份資料（帶 1h TTL 快取）"""
    cache_key = (stock_code, yyyymmdd)
    cached = _T86_CACHE.get(cache_key)
    if cached:
        ts, rows = cached
        if time.time() - ts < _T86_TTL:
            return rows

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
        _T86_CACHE[cache_key] = (time.time(), rows_out)
    except Exception as e:
        logger.warning(f"T86 fetch error {stock_code} {yyyymmdd}: {type(e).__name__}: {e}")
    return rows_out


async def _fetch_chip_from_db(stock_code: str, days: int) -> list[dict]:
    """DB price_history 籌碼快取（Agent B 每日更新，T86 失敗時 fallback）"""
    try:
        from datetime import date as _date, timedelta as _td
        from ..models.database import AsyncSessionLocal
        from ..models.models import PriceHistory
        from sqlalchemy import select as _sel

        cutoff = (_date.today() - _td(days=days * 2)).isoformat()
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                _sel(PriceHistory)
                .where(
                    PriceHistory.stock_code == stock_code,
                    PriceHistory.date >= cutoff,
                    PriceHistory.foreign_net.isnot(None),
                )
                .order_by(PriceHistory.date)
            )
            rows = r.scalars().all()

        result = []
        for row in rows:
            f = int(row.foreign_net or 0)
            t = int(row.investment_trust_net or 0)
            d = int(row.dealer_net or 0)
            result.append({
                "date":         str(row.date),
                "foreign_buy":  max(f, 0),
                "foreign_sell": max(-f, 0),
                "foreign_net":  f,
                "trust_buy":    max(t, 0),
                "trust_sell":   max(-t, 0),
                "trust_net":    t,
                "dealer_buy":   max(d, 0),
                "dealer_sell":  max(-d, 0),
                "dealer_net":   d,
                "total_net":    f + t + d,
            })
        return result[-days:] if len(result) > days else result
    except Exception as e:
        logger.warning(f"[Chip] DB fallback error {stock_code}: {e}")
        return []


async def fetch_chip_history(stock_code: str, days: int = 20) -> list[dict]:
    """
    抓近 days 日三大法人歷史。
    T86 有資料時優先使用；否則從 price_history DB 快取（Agent B 每日更新）。
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

    if not all_rows:
        logger.info(f"[Chip] T86 無資料 {stock_code}，嘗試 DB price_history 快取")
        return await _fetch_chip_from_db(stock_code, days)

    # 按日期升序排列，取最近 days 筆
    sorted_rows = sorted(all_rows.values(), key=lambda x: x["date"])
    return sorted_rows[-days:] if len(sorted_rows) > days else sorted_rows


# ══════════════════════════════════════════════════════════════════════════════
# 籌碼追蹤升級版（/chip CODE — 增強版）
# ══════════════════════════════════════════════════════════════════════════════

from dataclasses import dataclass
from typing import Optional as _Optional


@dataclass
class ChipSummary:
    stock_id: str
    stock_name: str
    # 融資融券
    margin_balance: int      # 融資餘額（張）
    margin_change: int       # 融資增減
    short_balance: int       # 融券餘額
    short_change: int        # 融券增減
    margin_ratio: float      # 融資使用率 (0-1)
    # 法人
    foreign_net: float       # 外資買超（張）
    trust_net: float         # 投信買超（張）
    dealer_net: float        # 自營商買超（張）
    foreign_consec: int      # 外資連買/賣天數（正=連買，負=連賣）
    # AI 判斷
    verdict: str             # "建倉" / "出貨" / "觀察" / "洗盤"
    verdict_reason: str
    chip_score: float        # 籌碼強度 0-100


async def get_chip_summary(code: str) -> _Optional[ChipSummary]:
    """取得籌碼摘要（增強版）"""
    try:
        from backend.services.twse_service import fetch_realtime_quote, fetch_institutional, fetch_margin
        import asyncio as _asyncio

        quote_task = fetch_realtime_quote(code)
        inst_task = fetch_institutional(code)
        margin_task = fetch_margin(code)
        chip_hist_task = fetch_chip_history(code, days=10)

        quote, inst, margin, chip_hist = await _asyncio.gather(
            quote_task, inst_task, margin_task, chip_hist_task,
            return_exceptions=True
        )

        name = code
        if isinstance(quote, dict):
            name = quote.get("name", code)

        # 融資融券
        margin_bal = margin_change_val = short_bal = short_change_val = 0
        margin_ratio = 0.0
        if isinstance(margin, dict):
            margin_bal = int(margin.get("margin_balance", 0) or 0)
            margin_change_val = int(margin.get("margin_change", 0) or 0)
            short_bal = int(margin.get("short_balance", 0) or 0)
            short_change_val = int(margin.get("short_change", 0) or 0)
            margin_limit = margin.get("margin_limit", 0) or 0
            margin_ratio = margin_bal / margin_limit if margin_limit > 0 else 0.0

        # 法人
        foreign_net = trust_net = dealer_net = 0.0
        if isinstance(inst, dict):
            foreign_net = float(inst.get("foreign_net", 0) or 0) / 1000  # 轉換為張
            trust_net = float(inst.get("trust_net", 0) or 0) / 1000
            dealer_net = float(inst.get("dealer_net", 0) or 0) / 1000

        # 計算外資連買天數
        foreign_consec = 0
        if isinstance(chip_hist, list):
            for day in chip_hist:
                fn = float(day.get("foreign_net", 0) or 0)
                if fn > 0:
                    if foreign_consec >= 0:
                        foreign_consec += 1
                    else:
                        break
                elif fn < 0:
                    if foreign_consec <= 0:
                        foreign_consec -= 1
                    else:
                        break

        # AI 判斷邏輯
        verdict, reason = _judge_chip(
            foreign_net, trust_net, margin_change_val, short_change_val,
            margin_ratio, foreign_consec
        )

        # 籌碼強度分數
        score = _calc_chip_score(foreign_net, trust_net, dealer_net, margin_change_val, short_change_val, foreign_consec)

        return ChipSummary(
            stock_id=code,
            stock_name=name,
            margin_balance=margin_bal,
            margin_change=margin_change_val,
            short_balance=short_bal,
            short_change=short_change_val,
            margin_ratio=margin_ratio,
            foreign_net=round(foreign_net, 0),
            trust_net=round(trust_net, 0),
            dealer_net=round(dealer_net, 0),
            foreign_consec=foreign_consec,
            verdict=verdict,
            verdict_reason=reason,
            chip_score=round(score, 1),
        )
    except Exception as e:
        logger.error("[chip_service.get_chip_summary] %s: %s", code, e)
        return None


def _judge_chip(foreign_net, trust_net, margin_change, short_change, margin_ratio, foreign_consec) -> tuple:
    """AI 判斷主力行為"""
    # 建倉訊號：外資買超 + 投信跟進 + 融資沒有大幅增加（主力不希望散戶跟風）
    if foreign_net > 500 and trust_net > 0 and margin_change < foreign_net * 0.3:
        return "建倉", f"外資買超{abs(foreign_net):.0f}張，投信同步進場，融資未大量跟進（主力悄悄布局）"
    # 出貨訊號：外資賣超 + 但融資大增（散戶搶進，主力出貨）
    if foreign_net < -200 and margin_change > 200:
        return "出貨", f"外資賣超{abs(foreign_net):.0f}張，但融資增加{margin_change}張（主力借散戶人氣出貨）"
    # 洗盤訊號：股價跌但融資減少 + 外資持續買
    if foreign_consec >= 3 and margin_change < -100:
        return "洗盤", f"外資連買{foreign_consec}日，融資減少{abs(margin_change)}張（可能在洗盤）"
    # 融資過高警戒
    if margin_ratio > 0.7:
        return "警戒", f"融資使用率{margin_ratio*100:.0f}%偏高，散戶擠入，注意風險"
    # 法人撤退
    if foreign_net < -1000 and trust_net < 0:
        return "出貨", f"外資+投信同步賣超，法人撤退訊號明顯"
    return "觀察", "籌碼無明顯異動，持續追蹤"


def _calc_chip_score(foreign_net, trust_net, dealer_net, margin_change, short_change, foreign_consec) -> float:
    """計算籌碼強度分數 0-100"""
    score = 50.0
    # 外資方向（最重要，±30分）
    if foreign_net > 0:
        score += min(foreign_net / 100, 30)
    else:
        score += max(foreign_net / 100, -30)
    # 投信方向（±15分）
    if trust_net > 0:
        score += min(trust_net / 50, 15)
    else:
        score += max(trust_net / 50, -15)
    # 外資連買加分（±10分）
    score += min(max(foreign_consec * 2, -10), 10)
    return round(min(max(score, 0), 100), 1)


def format_chip(chip: ChipSummary) -> str:
    """格式化籌碼報告"""
    verdict_icon = {"建倉": "🟢", "出貨": "🔴", "洗盤": "🟡", "警戒": "⚠️", "觀察": "📊"}.get(chip.verdict, "📊")
    margin_arrow = "▲" if chip.margin_change > 0 else ("▼" if chip.margin_change < 0 else "─")
    short_arrow = "▲" if chip.short_change > 0 else ("▼" if chip.short_change < 0 else "─")
    foreign_arrow = "▲" if chip.foreign_net > 0 else ("▼" if chip.foreign_net < 0 else "─")
    consec_str = (
        f"連買{chip.foreign_consec}日" if chip.foreign_consec > 0
        else (f"連賣{abs(chip.foreign_consec)}日" if chip.foreign_consec < 0 else "持平")
    )

    score_bar = "█" * int(chip.chip_score / 10)
    lines = [
        f"🔬 籌碼追蹤｜{chip.stock_name}（{chip.stock_id}）",
        "─" * 22,
        f"籌碼強度：{chip.chip_score:.0f}分  {score_bar}",
        "",
        "📊 三大法人（今日）",
        f"  外資 {foreign_arrow}{chip.foreign_net:+.0f}張  {consec_str}",
        f"  投信 {'▲' if chip.trust_net>0 else '▼'}{chip.trust_net:+.0f}張",
        f"  自營 {'▲' if chip.dealer_net>0 else '▼'}{chip.dealer_net:+.0f}張",
        "",
        "💳 融資融券",
        f"  融資餘額 {chip.margin_balance:,}張  {margin_arrow}{chip.margin_change:+}張",
        f"  融券餘額 {chip.short_balance:,}張  {short_arrow}{chip.short_change:+}張",
        f"  融資使用率 {chip.margin_ratio*100:.0f}%",
        "",
        f"{verdict_icon} AI研判：{chip.verdict}",
        f"   {chip.verdict_reason}",
    ]
    return "\n".join(lines)


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
