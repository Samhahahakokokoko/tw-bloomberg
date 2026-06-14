"""Monthly Report Service — 每月績效月報生成與推播"""
from __future__ import annotations

import time
from loguru import logger
import asyncio
import re

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 3600  # 1hr


async def get_monthly_report() -> dict:
    key = "monthly"
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _build_monthly_report()
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _build_monthly_report() -> dict:
    import asyncio, datetime
    today = datetime.date.today()
    month_label = today.strftime("%Y年%m月")

    market_task    = _get_market_summary()
    portfolio_task = _get_portfolio_perf()
    sector_task    = _get_sector_summary()
    news_task      = _get_monthly_news()

    market, portfolio, sectors, news = await asyncio.gather(
        market_task, portfolio_task, sector_task, news_task, return_exceptions=True
    )
    market    = market    if isinstance(market, dict)    else {}
    portfolio = portfolio if isinstance(portfolio, dict) else {}
    sectors   = sectors   if isinstance(sectors, list)   else []
    news      = news      if isinstance(news, list)       else []

    highlights, risks = _generate_insights(market, portfolio, sectors)

    return {
        "month_label": month_label,
        "market":      market,
        "portfolio":   portfolio,
        "sectors":     sectors,
        "news":        news[:5],
        "highlights":  highlights,
        "risks":       risks,
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


async def _get_market_summary() -> dict:
    try:
        import httpx
        symbols = {"台股": "^TWII", "那指": "^IXIC", "道指": "^DJI", "VIX": "^VIX"}
        results = {}
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as cl:
            for name, sym in symbols.items():
                try:
                    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1mo&range=2mo"
                    r = await cl.get(url)
                    js = r.json()
                    closes = js["chart"]["result"][0]["indicators"]["quote"][0].get("close", [])
                    closes = [c for c in closes if c]
                    if len(closes) >= 2:
                        chg = (closes[-1] / closes[-2] - 1) * 100
                        results[name] = {"close": round(closes[-1], 2), "chg": round(chg, 2)}
                except Exception as e:
                    continue
        return results
    except Exception as e:
        logger.debug(f"[monthly] market: {e}")
        return {}


async def _get_portfolio_perf() -> dict:
    try:
        from .performance_service import get_performance_summary
        perf = await get_performance_summary()
        return perf or {}
    except Exception as e:
        pass
    try:
        from .portfolio_manager import get_portfolio_summary
        return await get_portfolio_summary() or {}
    except Exception as e:
        logger.debug(f"[monthly] portfolio: {e}")
        return {}


async def _get_sector_summary() -> list:
    try:
        from .sector_flow_service import get_sector_flow
        data = await get_sector_flow()
        sectors = data.get("sectors", [])
        return sorted(sectors, key=lambda x: x.get("chg", 0), reverse=True)[:5]
    except Exception as e:
        logger.debug(f"[monthly] sector: {e}")
        return []


async def _get_monthly_news() -> list:
    try:
        import httpx, re
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as cl:
            r = await cl.get("https://tw.stock.yahoo.com/news/")
        titles = re.findall(r'<h3[^>]*>([^<]{10,60})</h3>', r.text)
        return [{"title": t.strip()} for t in titles[:8]]
    except Exception as e:
        logger.debug(f"[monthly] news: {e}")
        return []


def _generate_insights(market: dict, portfolio: dict, sectors: list):
    highlights = []
    risks = []

    twii = market.get("台股", {})
    if twii.get("chg", 0) > 3:
        highlights.append(f"台股本月上漲 {twii['chg']:.1f}%，市場動能強勁")
    elif twii.get("chg", 0) < -3:
        risks.append(f"台股本月下跌 {abs(twii['chg']):.1f}%，留意系統性風險")

    nas = market.get("那指", {})
    if nas.get("chg", 0) > 3:
        highlights.append("那指強勢，科技股外資持續流入")
    elif nas.get("chg", 0) < -5:
        risks.append("那指重挫，台灣科技股連動風險高")

    vix = market.get("VIX", {})
    if vix.get("close", 0) > 25:
        risks.append(f"VIX {vix.get('close',0):.1f}，市場恐慌情緒升溫")

    ret = portfolio.get("total_return_pct") or portfolio.get("return_pct", 0)
    if ret > 5:
        highlights.append(f"投資組合月報酬 +{ret:.1f}%，優於大盤")
    elif ret < -5:
        risks.append(f"投資組合月報酬 {ret:.1f}%，需檢視持股")

    if sectors:
        best = sectors[0]
        highlights.append(f"{best.get('name','?')} 為本月最強族群")

    if not highlights:
        highlights.append("市場整體平穩，無重大異常事件")
    if not risks:
        risks.append("目前無特別重大風險，持續觀察即可")

    return highlights, risks


def format_monthly_report(data: dict) -> str:
    if not data:
        return "❌ 無法生成月報"

    label  = data["month_label"]; market = data["market"]
    pf     = data["portfolio"];   sectors = data["sectors"]
    news   = data["news"];        hi = data["highlights"]; ri = data["risks"]
    ts     = data["generated_at"]

    lines = [f"📋 {label} 月度績效報告", "═" * 32, ""]

    # Market overview
    if market:
        lines.append("🌍 全球市場")
        for name, info in market.items():
            chg  = info.get("chg", 0)
            icon = "▲" if chg > 0 else ("▼" if chg < 0 else "─")
            lines.append(f"  {name:<4}：{info.get('close',0):>10,.2f}  {icon}{abs(chg):.1f}%")
        lines.append("")

    # Portfolio
    if pf:
        lines.append("💼 投資組合")
        ret  = pf.get("total_return_pct") or pf.get("return_pct", None)
        val  = pf.get("total_value") or pf.get("portfolio_value", None)
        cash = pf.get("cash", None)
        if ret  is not None: lines.append(f"  月報酬：{ret:+.2f}%")
        if val  is not None: lines.append(f"  總市值：{val:,.0f} 元")
        if cash is not None: lines.append(f"  現金部位：{cash:,.0f} 元")
        lines.append("")

    # Sector ranking
    if sectors:
        lines.append("🏆 本月強勢族群 TOP 5")
        for i, s in enumerate(sectors[:5], 1):
            chg  = s.get("chg", 0)
            icon = "▲" if chg > 0 else "▼"
            lines.append(f"  {i}. {s.get('name','?'):<8} {icon}{abs(chg):.1f}%")
        lines.append("")

    # Highlights
    lines.append("✅ 本月亮點")
    for h in hi:
        lines.append(f"  • {h}")
    lines.append("")

    # Risks
    lines.append("⚠️ 留意事項")
    for r in ri:
        lines.append(f"  • {r}")
    lines.append("")

    # News
    if news:
        lines.append("📰 重點新聞")
        for n in news[:4]:
            lines.append(f"  • {n.get('title','')[:35]}")
        lines.append("")

    lines += ["─" * 32, f"生成時間：{ts}", "📊 by tw-bloomberg AI 系統"]
    return "\n".join(lines)


async def push_monthly_report() -> bool:
    """月底最後一個交易日推播月報"""
    try:
        from .line_push import push_to_admin
        data   = await get_monthly_report()
        report = format_monthly_report(data)
        await push_to_admin(report[:4000])
        logger.info("[monthly] pushed monthly report")
        return True
    except Exception as e:
        logger.error(f"[monthly] push error: {e}")
        return False


def is_last_trading_day_of_month() -> bool:
    """Check if today is approximately the last trading day of the month."""
    import datetime
    today  = datetime.date.today()
    if today.weekday() >= 5:
        return False
    next_bd = today + datetime.timedelta(days=1)
    while next_bd.weekday() >= 5:
        next_bd += datetime.timedelta(days=1)
    return next_bd.month != today.month
