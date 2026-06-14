"""Smart Money Stock Service — 個股聰明錢動向追蹤"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 600  # 10 min


async def get_smart_money(code: str) -> dict:
    now = time.time()
    if code in _cache and now - _cache_ts.get(code, 0) < _TTL:
        return _cache[code]

    result = await _analyze_smart_money(code)
    _cache[code] = result
    _cache_ts[code] = now
    return result


async def _analyze_smart_money(code: str) -> dict:
    import asyncio
    from .twse_service import fetch_realtime_quote, fetch_kline

    quote_task = _safe_quote(code)
    kline_task = _safe_kline(code)
    chip_task  = _safe_chip(code)

    quote, kline, chip = await asyncio.gather(
        quote_task, kline_task, chip_task, return_exceptions=True
    )
    quote = quote if isinstance(quote, dict) else {}
    kline = kline if isinstance(kline, list) else []
    chip  = chip  if isinstance(chip, dict)  else {}

    closes  = [float(k.get("close", 0) or 0) for k in kline if k.get("close")]
    volumes = [float(k.get("volume", 0) or 0) for k in kline if k.get("volume")]

    big_order   = _estimate_big_order_ratio(volumes, kline[-5:] if len(kline) >= 5 else kline)
    branch_sig  = _branch_signal(chip, quote)
    insider_sig = _insider_signal(quote, chip)
    verdict     = _ai_verdict(code, big_order, branch_sig, insider_sig, closes, quote)

    return {
        "code":         code,
        "name":         quote.get("name", code),
        "price":        float(quote.get("close") or quote.get("price") or 0),
        "big_order":    big_order,
        "branch":       branch_sig,
        "insider":      insider_sig,
        "verdict":      verdict,
        "updated_at":   time.strftime("%Y-%m-%d %H:%M"),
    }


def _estimate_big_order_ratio(volumes: list[float], recent_klines: list[dict]) -> dict:
    """估算大單比例（近 5 日）"""
    if not volumes or not recent_klines:
        return {"buy_ratio": 50.0, "sell_ratio": 50.0, "trend": "無資料"}

    # 用量能與漲跌推估大單方向
    buy_vol  = sum(float(k.get("volume", 0) or 0) for k in recent_klines
                   if float(k.get("close", 0) or 0) > float(k.get("open", 0) or 0))
    sell_vol = sum(float(k.get("volume", 0) or 0) for k in recent_klines
                   if float(k.get("close", 0) or 0) <= float(k.get("open", 0) or 0))
    total = buy_vol + sell_vol or 1
    buy_pct  = round(buy_vol / total * 100, 1)
    sell_pct = round(sell_vol / total * 100, 1)

    avg_vol_5 = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else 0
    avg_vol_20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else avg_vol_5
    vol_ratio = avg_vol_5 / avg_vol_20 if avg_vol_20 > 0 else 1.0

    if buy_pct >= 60 and vol_ratio > 1.2:
        trend = "大單積極買進"
    elif buy_pct >= 60:
        trend = "買單略多"
    elif sell_pct >= 60 and vol_ratio > 1.2:
        trend = "大單積極出貨"
    elif sell_pct >= 60:
        trend = "賣單略多"
    else:
        trend = "多空均衡"

    return {
        "buy_ratio":  buy_pct,
        "sell_ratio": sell_pct,
        "vol_ratio":  round(vol_ratio, 2),
        "trend":      trend,
    }


def _branch_signal(chip: dict, quote: dict) -> dict:
    """主力分點連續進出訊號"""
    foreign_net = float(chip.get("foreign_net") or quote.get("foreign_buy") or 0)
    invest_net  = float(chip.get("invest_net") or 0)
    dealer_net  = float(chip.get("dealer_net") or 0)
    total_inst  = foreign_net + invest_net + dealer_net

    if total_inst > 5000:
        label = "法人連續買進"
        icon  = "🟢"
    elif total_inst > 1000:
        label = "法人小幅買進"
        icon  = "🔵"
    elif total_inst < -5000:
        label = "法人連續賣出"
        icon  = "🔴"
    elif total_inst < -1000:
        label = "法人小幅賣出"
        icon  = "🟡"
    else:
        label = "法人中立"
        icon  = "⬜"

    return {
        "foreign":  round(foreign_net),
        "invest":   round(invest_net),
        "dealer":   round(dealer_net),
        "total":    round(total_inst),
        "label":    label,
        "icon":     icon,
    }


def _insider_signal(quote: dict, chip: dict) -> dict:
    """估算內部人增持跡象"""
    chg_pct = float(quote.get("change_pct") or 0)
    volume  = float(quote.get("volume") or quote.get("trade_volume") or 0)
    pe      = float(quote.get("pe_ratio") or quote.get("pe") or 0)
    pbr     = float(quote.get("pb_ratio") or quote.get("pbr") or 0)

    signals = []
    score   = 0

    # 低本益比 + 上漲 = 可能有信心增持
    if 0 < pe < 12 and chg_pct > 0:
        signals.append("低PE上漲（估值吸引買盤）")
        score += 2
    if 0 < pbr < 1.2 and chg_pct > 0:
        signals.append("低PBR（淨值支撐）")
        score += 1
    if chg_pct > 1.5 and volume > 5000:
        signals.append("放量上漲（資金積極介入）")
        score += 2
    if not signals:
        signals.append("無明顯內部人訊號")

    if score >= 4:
        label = "有強烈增持跡象"
    elif score >= 2:
        label = "有輕微增持跡象"
    else:
        label = "無明顯增持跡象"

    return {"label": label, "signals": signals, "score": score}


def _ai_verdict(code: str, big_order: dict, branch: dict,
                insider: dict, closes: list[float], quote: dict) -> str:
    buy_ratio = big_order.get("buy_ratio", 50)
    branch_label = branch.get("label", "")
    insider_score = insider.get("score", 0)
    trend = big_order.get("trend", "")

    # 布局 vs 出清判斷
    bullish = 0
    bearish = 0

    if buy_ratio >= 60:        bullish += 2
    elif buy_ratio <= 40:      bearish += 2
    if "買進" in branch_label: bullish += 2
    if "賣出" in branch_label: bearish += 2
    if insider_score >= 3:     bullish += 1

    if bullish > bearish + 1:
        action = "聰明錢正在積極布局，短期可能迎來拉升行情"
    elif bearish > bullish + 1:
        action = "聰明錢出現出清跡象，注意下行風險"
    elif bullish == bearish:
        action = "聰明錢方向分歧，建議觀望等待確認"
    else:
        action = "聰明錢略偏多，但未形成明確趨勢"

    price = float(quote.get("close") or 0)
    chg   = float(quote.get("change_pct") or 0)
    return (
        f"現價 {price:,.1f}（{chg:+.1f}%）。"
        f"大單趨勢「{trend}」，{branch_label}。"
        f"{action}。"
    )


def format_smart_money_report(data: dict) -> str:
    if not data:
        return "❌ 無法取得聰明錢資料"

    code    = data["code"]
    name    = data["name"]
    price   = data["price"]
    bo      = data["big_order"]
    branch  = data["branch"]
    insider = data["insider"]
    verdict = data["verdict"]
    ts      = data["updated_at"]

    def _ratio_bar(pct: float) -> str:
        n = int(pct / 10)
        return "█" * n + "░" * (10 - n)

    lines = [
        f"🕵️ 聰明錢追蹤  {code} {name}",
        "─" * 32,
        "",
        f"現價：{price:,.1f}",
        "",
        "📦 近 5 日大單分析",
        f"買單比例：{_ratio_bar(bo['buy_ratio'])} {bo['buy_ratio']:.1f}%",
        f"賣單比例：{_ratio_bar(bo['sell_ratio'])} {bo['sell_ratio']:.1f}%",
        f"成交量比：{bo['vol_ratio']:.2f}x （相對 20 日均量）",
        f"趨勢判斷：{bo['trend']}",
        "",
        "🏦 主力分點動向",
        f"{branch['icon']} {branch['label']}",
        f"  外資：{branch['foreign']:>+8,} 張",
        f"  投信：{branch['invest']:>+8,} 張",
        f"  自營：{branch['dealer']:>+8,} 張",
        "",
        "👁️ 內部人跡象",
        f"研判：{insider['label']}",
    ]
    for sig in insider["signals"]:
        lines.append(f"  • {sig}")

    lines += [
        "",
        "─" * 32,
        "🤖 AI 研判",
        verdict,
        "",
        f"更新：{ts}",
    ]
    return "\n".join(lines)


async def _safe_quote(code: str) -> dict:
    try:
        from .twse_service import fetch_realtime_quote
        return await fetch_realtime_quote(code) or {}
    except Exception:
        return {}


async def _safe_kline(code: str) -> list:
    try:
        from .twse_service import fetch_kline
        return await fetch_kline(code) or []
    except Exception:
        return []


async def _safe_chip(code: str) -> dict:
    try:
        from .chip_service import get_chip_data
        return await get_chip_data(code) or {}
    except Exception:
        return {}
