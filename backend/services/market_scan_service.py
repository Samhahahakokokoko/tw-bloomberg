"""Market Scan Service — 全市場掃描（漲幅/爆量/外資買超前10）"""
from __future__ import annotations

import time
from loguru import logger
import asyncio

_cache: dict | None = None
_cache_ts: float = 0.0
_TTL = 300  # 5 min


async def run_market_scan() -> dict:
    """執行全市場掃描，回傳三大榜單"""
    global _cache, _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < _TTL:
        return _cache

    result = await _do_scan()
    _cache = result
    _cache_ts = now
    return result


async def _do_scan() -> dict:
    import asyncio

    try:
        from .twse_service import _raw_twse_daily_all, _raw_tpex_daily_all
        twse_raw, tpex_raw = await asyncio.gather(
            _raw_twse_daily_all(), _raw_tpex_daily_all(), return_exceptions=True
        )
        twse_list = twse_raw if isinstance(twse_raw, list) else []
        tpex_list = tpex_raw if isinstance(tpex_raw, list) else []
        all_raw = twse_list + tpex_list
    except Exception as e:
        logger.error(f"[market_scan] fetch raw all failed: {e}")
        all_raw = []

    if not all_raw:
        return {"gainers": [], "volume": [], "foreign": [], "ts": time.strftime("%H:%M")}

    def sf(v):
        try: return float(str(v).replace(",", ""))
        except (ValueError, TypeError): return 0.0
    def si(v):
        try: return int(str(v).replace(",", ""))
        except (ValueError, TypeError): return 0

    def calc_chg_pct(item):
        close  = sf(item.get("ClosingPrice") or item.get("Close") or 0)
        change = sf(item.get("Change") or item.get("變動價格") or 0)
        if close > 0 and change != 0:
            prev = close - change
            return change / prev * 100 if prev > 0 else 0.0
        return sf(item.get("change_pct") or 0)

    stocks_with_data = []
    for s in all_raw:
        code = str(s.get("Code") or s.get("SecuritiesCompanyCode") or s.get("code") or "").strip()
        name = str(s.get("Name") or s.get("CompanyName") or s.get("name") or code)
        if not code or len(code) != 4 or not code.isdigit():
            continue

        price   = sf(s.get("ClosingPrice") or s.get("Close") or s.get("close") or s.get("price") or 0)
        volume  = si(s.get("TradeVolume") or s.get("TradingShares") or s.get("volume") or 0)
        chg_pct = calc_chg_pct(s)

        if price <= 0:
            continue

        stocks_with_data.append({
            "code":     code,
            "name":     name,
            "chg_pct":  chg_pct,
            "volume":   volume // 1000 if volume > 1000 else volume,
            "price":    price,
        })

    gainers = sorted(stocks_with_data, key=lambda x: x["chg_pct"], reverse=True)[:10]
    by_vol  = sorted(stocks_with_data, key=lambda x: x["volume"], reverse=True)[:10]

    # 外資買超：從籌碼面 or 法人資料取
    foreign = await _get_foreign_top10()

    return {
        "gainers": gainers,
        "volume":  by_vol,
        "foreign": foreign,
        "ts":      time.strftime("%H:%M"),
    }


async def _get_foreign_top10() -> list[dict]:
    """取得外資買超前10名（TWSE TWT38U）"""
    try:
        import httpx
        url = "https://www.twse.com.tw/fund/TWT38U"
        params = {"response": "json", "date": "", "selectType": "ALLBUT0999"}
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as c:
            r = await c.get(url, params=params)
            js = r.json()

        data = js.get("data", [])
        results = []
        for row in data:
            if len(row) < 6:
                continue
            code = str(row[1]).strip()
            name = str(row[2]).strip()
            if not code or len(code) != 4 or not code.isdigit():
                continue
            try:
                net_shares = int(str(row[5]).replace(",", "").replace("+", ""))
                net = net_shares // 1000  # 股 → 張
                if net > 0:
                    results.append({"code": code, "name": name, "net": net})
            except Exception as e:
                continue

        results.sort(key=lambda x: x["net"], reverse=True)
        return results[:10]
    except Exception as e:
        logger.warning(f"[scan] foreign top10 fallback: {e}")
        return []


def _parse_int(v) -> int:
    try: return int(str(v).replace(",", ""))
    except (ValueError, TypeError): return 0


def format_scan_report(data: dict) -> str:
    ts = data.get("ts", "")
    lines = [f"🔍 市場掃描 {ts}", "─" * 30]

    lines += ["", "🚀 今日漲幅前10名"]
    for i, s in enumerate(data.get("gainers", []), 1):
        lines.append(f"  {i:2}. {s['code']} {s['name'][:5]:<5}  {s['chg_pct']:+.2f}%  ${s['price']:,.0f}")

    lines += ["", "💥 今日爆量前10名"]
    for i, s in enumerate(data.get("volume", []), 1):
        vol_k = s["volume"] // 1000
        lines.append(f"  {i:2}. {s['code']} {s['name'][:5]:<5}  {vol_k:,}張  {s['chg_pct']:+.1f}%")

    lines += ["", "🏦 外資買超前10名"]
    if data.get("foreign"):
        for i, s in enumerate(data.get("foreign", []), 1):
            lines.append(f"  {i:2}. {s['code']} {s['name'][:5]:<5}  +{s['net']:,}張")
    else:
        lines.append("  (今日尚無外資資料)")

    return "\n".join(lines)


async def push_scan_to_admin() -> None:
    """排程推播給管理員"""
    import os
    try:
        data = await run_market_scan()
        text = format_scan_report(data)
        admin_uid = os.getenv("ADMIN_LINE_UID", "")
        if not admin_uid:
            return
        from .line_push import push_line_messages
        await push_line_messages(admin_uid, [{"type": "text", "text": text[:4800]}])
        logger.info("[market_scan] pushed to admin")
    except Exception as e:
        logger.error(f"[market_scan] push failed: {e}")


async def push_scan_to_subscribers() -> None:
    """推播給訂閱用戶"""
    try:
        data = await run_market_scan()
        text = format_scan_report(data)

        from ..models.database import AsyncSessionLocal
        from sqlalchemy import select
        try:
            from ..models.models import Subscriber
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Subscriber))
                subs = result.scalars().all()
            uids = [s.line_user_id for s in subs]
        except Exception as e:
            uids = []

        if not uids:
            await push_scan_to_admin()
            return

        from .line_push import push_line_messages
        for uid in uids[:50]:
            try:
                await push_line_messages(uid, [{"type": "text", "text": text[:4800]}])
            except Exception as e:
                pass
        logger.info(f"[market_scan] pushed to {len(uids)} subscribers")
    except Exception as e:
        logger.error(f"[market_scan] push_subscribers failed: {e}")
