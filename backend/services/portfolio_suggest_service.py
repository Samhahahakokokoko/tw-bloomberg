"""Portfolio Suggest Service — AI 自動投資組合建議"""
from __future__ import annotations

import time
from loguru import logger
import asyncio

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 1800  # 30 min


async def get_portfolio_suggestion(uid: str) -> dict:
    now = time.time()
    if uid in _cache and now - _cache_ts.get(uid, 0) < _TTL:
        return _cache[uid]
    result = await _build_suggestion(uid)
    _cache[uid] = result
    _cache_ts[uid] = now
    return result


async def _build_suggestion(uid: str) -> dict:
    import asyncio
    cycle_task   = _safe_cycle()
    psych_task   = _safe_psychology()
    sector_task  = _safe_sector()
    watch_task   = _safe_watchlist(uid)

    cycle, psych, sectors, watchlist = await asyncio.gather(
        cycle_task, psych_task, sector_task, watch_task, return_exceptions=True
    )
    cycle     = cycle     if isinstance(cycle, dict)     else {}
    psych     = psych     if isinstance(psych, dict)     else {}
    sectors   = sectors   if isinstance(sectors, list)   else []
    watchlist = watchlist if isinstance(watchlist, list) else []

    cash_ratio  = _calc_cash_ratio(cycle, psych)
    allocation  = _build_allocation(cycle, sectors, watchlist, cash_ratio)
    strategy    = _select_strategy(cycle, psych)
    advice      = _build_advice_text(cash_ratio, allocation, strategy, cycle, psych)

    return {
        "uid":         uid,
        "cash_ratio":  cash_ratio,
        "allocation":  allocation,
        "strategy":    strategy,
        "advice":      advice,
        "cycle_phase": cycle.get("phase", "未知"),
        "fg_score":    psych.get("fear_greed", {}).get("score", 50),
        "updated_at":  time.strftime("%Y-%m-%d %H:%M"),
    }


def _calc_cash_ratio(cycle: dict, psych: dict) -> float:
    """根據市場週期和情緒計算建議現金比例 (0-1)"""
    base = 0.2  # 基礎 20% 現金
    phase = cycle.get("phase", "")
    fg    = psych.get("fear_greed", {}).get("score", 50)

    if "末期" in phase or "空頭" in phase:
        base += 0.3
    elif "初期" in phase and "多頭" in phase:
        base -= 0.1
    elif "中期" in phase and "多頭" in phase:
        base += 0.05

    if fg >= 80:   base += 0.2
    elif fg >= 65: base += 0.1
    elif fg <= 20: base -= 0.15
    elif fg <= 35: base -= 0.05

    return round(min(0.8, max(0.05, base)), 2)


def _build_allocation(cycle: dict, sectors: list[dict], watchlist: list, cash_ratio: float) -> list[dict]:
    """建立資產配置建議"""
    equity_ratio = 1 - cash_ratio
    alloc = []

    # 強勢產業分配
    top_sectors = sorted(sectors, key=lambda x: x.get("score", 0), reverse=True)[:3]
    if top_sectors:
        per_sector = equity_ratio * 0.6 / len(top_sectors)
        for s in top_sectors:
            alloc.append({
                "type":    "sector",
                "name":    s.get("sector", ""),
                "ratio":   round(per_sector, 2),
                "note":    f"強勢產業，均線多排 avg_chg={s.get('avg_chg',0):+.1f}%",
            })

    # 自選股健康優先
    healthy = sorted(watchlist, key=lambda x: x.get("score", 0), reverse=True)[:5]
    if healthy:
        per_stock = equity_ratio * 0.35 / len(healthy)
        for s in healthy:
            alloc.append({
                "type":  "stock",
                "name":  f"{s.get('code','')} {s.get('name','')}",
                "ratio": round(per_stock, 2),
                "note":  f"健康分數 {s.get('score',0):.0f}",
            })

    # 現金
    alloc.append({"type": "cash", "name": "現金/貨幣基金", "ratio": cash_ratio, "note": "防禦緩衝"})
    return alloc


def _select_strategy(cycle: dict, psych: dict) -> str:
    phase = cycle.get("phase", "")
    fg    = psych.get("fear_greed", {}).get("score", 50)
    if "多頭初期" in phase:    return "積極成長（趨勢起漲）"
    if "多頭中期" in phase:    return "動能追蹤（強者恆強）"
    if "多頭末期" in phase:    return "防禦輪動（逐步減碼）"
    if "空頭初期" in phase:    return "保守防禦（縮短倉位）"
    if "空頭中期" in phase:    return "低波避險（現金為王）"
    if "空頭末期" in phase:    return "逢低布局（分批建倉）"
    if fg >= 70:               return "謹慎持守（情緒過熱）"
    if fg <= 30:               return "分批買入（情緒過冷）"
    return "均衡配置（等待方向）"


def _build_advice_text(cash: float, alloc: list, strategy: str, cycle: dict, psych: dict) -> str:
    phase  = cycle.get("phase", "未知")
    fg     = psych.get("fear_greed", {}).get("score", 50)
    stocks = [a for a in alloc if a["type"] == "stock"]
    sectors = [a for a in alloc if a["type"] == "sector"]

    stock_names = "、".join(a["name"].split()[0] for a in stocks[:3]) if stocks else "無自選股"
    sector_names = "、".join(a["name"] for a in sectors[:2]) if sectors else "無強勢產業"

    return (
        f"目前市場週期：{phase}，貪婪恐懼 {fg:.0f}/100。\n"
        f"建議策略：{strategy}。\n"
        f"現金比例 {cash*100:.0f}%，股票部位 {(1-cash)*100:.0f}%。\n"
        f"優先配置：{stock_names}（自選股健康優先）。\n"
        f"強勢產業：{sector_names}。\n"
        f"操作原則：嚴守停損，順勢而為，避免情緒性決策。"
    )


async def _safe_cycle() -> dict:
    try:
        from .market_cycle_service import get_market_cycle
        return await get_market_cycle()
    except Exception as e:
        return {}

async def _safe_psychology() -> dict:
    try:
        from .psychology_service import get_market_psychology
        return await get_market_psychology()
    except Exception as e:
        return {}

async def _safe_sector() -> list:
    try:
        from .sector_flow_service import get_sector_flow
        d = await get_sector_flow()
        return d.get("sectors", [])
    except Exception as e:
        return []

async def _safe_watchlist(uid: str) -> list:
    try:
        from .watchlist_monitor import scan_user_watchlist
        results = await scan_user_watchlist(uid)
        return results if isinstance(results, list) else []
    except Exception as e:
        return []


def format_portfolio_suggest(data: dict) -> str:
    if not data:
        return "❌ 無法產生投資組合建議"
    cash   = data["cash_ratio"]; alloc = data["allocation"]
    strat  = data["strategy"]; advice = data["advice"]; ts = data["updated_at"]
    phase  = data["cycle_phase"]; fg = data["fg_score"]

    lines = [
        "💼 AI 投資組合建議",
        "─" * 32, "",
        f"市場週期：{phase}",
        f"貪婪恐懼：{fg:.0f}/100",
        f"建議策略：{strat}",
        "",
        "─" * 32,
        "📊 建議配置",
        f"{'資產':<16} {'比例':>6}  說明",
        "─" * 32,
    ]
    for a in alloc:
        bar = "█" * int(a["ratio"] * 20) + "░" * (20 - int(a["ratio"] * 20))
        type_icon = {"stock": "📈", "sector": "🏭", "cash": "💵"}.get(a["type"], "")
        lines.append(f"{type_icon}{a['name']:<14} {a['ratio']*100:>5.1f}%  {a.get('note','')[:20]}")

    lines += [
        "",
        f"現金：{cash*100:.0f}%  |  股票：{(1-cash)*100:.0f}%",
        "",
        "─" * 32,
        "🤖 AI 操作建議",
        advice,
        "",
        f"更新：{ts}",
        "⚠️ 本建議僅供參考，投資請自行判斷",
    ]
    return "\n".join(lines)
