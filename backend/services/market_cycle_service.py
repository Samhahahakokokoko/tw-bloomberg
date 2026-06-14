"""Market Cycle Service — 判斷市場週期（多頭/空頭 初/中/末期）"""
from __future__ import annotations

import math
import time
from loguru import logger

_cache: dict | None = None
_cache_ts: float = 0.0
_TTL = 1800  # 30 min


async def get_market_cycle() -> dict:
    global _cache, _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < _TTL:
        return _cache

    result = await _analyze_cycle()
    _cache = result
    _cache_ts = now
    return result


async def _analyze_cycle() -> dict:
    import asyncio
    from .twse_service import fetch_kline, fetch_market_overview
    from .market_sentiment import get_sentiment_score

    kline_task     = _safe_kline("^TWII")  # 大盤 K 線（fallback to 0050）
    overview_task  = _safe_overview()
    sentiment_task = _safe_sentiment()

    kline, overview, sentiment = await asyncio.gather(
        kline_task, overview_task, sentiment_task, return_exceptions=True
    )
    kline     = kline     if isinstance(kline, list)    else []
    overview  = overview  if isinstance(overview, dict) else {}
    sentiment = sentiment if isinstance(sentiment, dict) else {}

    closes  = [float(k.get("close", 0) or 0) for k in kline if k.get("close")]
    volumes = [float(k.get("volume", 0) or 0) for k in kline if k.get("volume")]

    tech  = _technical_score(closes, volumes)
    money = _money_flow_score(overview)
    sent  = _sentiment_score(sentiment)

    # 加權綜合 → 週期判斷
    composite = tech["score"] * 0.40 + money["score"] * 0.35 + sent["score"] * 0.25
    phase     = _determine_phase(composite, tech, money, sent)
    strategy  = _suggest_strategy(phase)

    return {
        "phase":      phase,
        "composite":  round(composite, 1),
        "technical":  tech,
        "money":      money,
        "sentiment":  sent,
        "strategy":   strategy,
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


async def _safe_kline(code: str) -> list:
    try:
        from .twse_service import fetch_kline
        kl = await fetch_kline(code)
        if kl:
            return kl
        # fallback
        return await fetch_kline("0050") or []
    except Exception:
        try:
            from .twse_service import fetch_kline
            return await fetch_kline("0050") or []
        except Exception:
            return []


async def _safe_overview() -> dict:
    try:
        from .twse_service import fetch_market_overview
        return await fetch_market_overview() or {}
    except Exception:
        return {}


async def _safe_sentiment() -> dict:
    try:
        from .market_sentiment import get_sentiment_score
        return await get_sentiment_score() or {}
    except Exception:
        return {}


def _technical_score(closes: list[float], volumes: list[float]) -> dict:
    """技術面評分 0-100（均線排列 + 趨勢強度）"""
    if len(closes) < 60:
        return {"score": 50.0, "desc": "技術面：資料不足", "signals": []}

    def ma(n):
        return sum(closes[-n:]) / n if len(closes) >= n else closes[-1]

    ma5, ma20, ma60 = ma(5), ma(20), ma(60)
    price = closes[-1]

    score = 50.0
    signals = []

    # 均線多頭排列
    if price > ma5 > ma20 > ma60:
        score += 30
        signals.append("均線完美多頭排列 ✅")
    elif price > ma20 and ma20 > ma60:
        score += 15
        signals.append("中長期多頭排列 ✅")
    elif price < ma5 < ma20 < ma60:
        score -= 30
        signals.append("均線完美空頭排列 ❌")
    elif price < ma20 and ma20 < ma60:
        score -= 15
        signals.append("中長期空頭排列 ❌")

    # 3個月動能
    lb = min(60, len(closes) - 1)
    ret3m = (closes[-1] - closes[-lb]) / closes[-lb] * 100 if closes[-lb] > 0 else 0
    if ret3m > 15:
        score += 10
        signals.append(f"3個月漲幅 {ret3m:.1f}% 強勢 📈")
    elif ret3m > 5:
        score += 5
        signals.append(f"3個月漲幅 {ret3m:.1f}% 溫和")
    elif ret3m < -15:
        score -= 10
        signals.append(f"3個月跌幅 {abs(ret3m):.1f}% 弱勢 📉")
    elif ret3m < -5:
        score -= 5

    # 量能趨勢
    if len(volumes) >= 20:
        vol_recent = sum(volumes[-5:]) / 5
        vol_avg    = sum(volumes[-20:]) / 20
        if vol_recent > vol_avg * 1.3:
            score += 5
            signals.append("近期量能放大 ✅")
        elif vol_recent < vol_avg * 0.7:
            score -= 5
            signals.append("近期量能萎縮")

    score = max(0, min(100, score))
    desc = f"技術面評分：{score:.0f}/100（{ma5:.0f}/{ma20:.0f}/{ma60:.0f}）"
    return {"score": score, "desc": desc, "signals": signals, "ma5": ma5, "ma20": ma20, "ma60": ma60, "ret3m": ret3m}


def _money_flow_score(overview: dict) -> dict:
    """資金面評分（法人淨買超）"""
    def sf(v):
        try: return float(str(v).replace(",", ""))
        except: return 0.0

    score = 50.0
    signals = []

    foreign_net = sf(overview.get("foreign_net") or 0)
    trust_net   = sf(overview.get("trust_net")   or 0)
    total_value = sf(overview.get("total_value") or overview.get("volume") or 0)

    if foreign_net > 20_000_000_000:
        score += 25
        signals.append(f"外資大買超 +{foreign_net/1e8:.0f}億 ✅")
    elif foreign_net > 5_000_000_000:
        score += 15
        signals.append(f"外資買超 +{foreign_net/1e8:.0f}億")
    elif foreign_net > 0:
        score += 5
    elif foreign_net < -20_000_000_000:
        score -= 25
        signals.append(f"外資大賣超 {foreign_net/1e8:.0f}億 ❌")
    elif foreign_net < -5_000_000_000:
        score -= 15
        signals.append(f"外資賣超 {foreign_net/1e8:.0f}億")

    if trust_net > 0:
        score += 10
        signals.append("投信買超 ✅")
    elif trust_net < 0:
        score -= 5

    if total_value > 3_000_000_000_000:
        score += 10
        signals.append(f"大盤成交量大 {total_value/1e12:.1f}兆")

    score = max(0, min(100, score))
    return {"score": score, "signals": signals, "foreign_net": foreign_net, "trust_net": trust_net}


def _sentiment_score(sentiment: dict) -> dict:
    """情緒面評分"""
    raw = float(sentiment.get("score") or 50)
    signals = []

    if raw >= 75:
        signals.append(f"市場貪婪指數高 ({raw:.0f}) — 可能過熱")
    elif raw >= 60:
        signals.append(f"市場情緒偏樂觀 ({raw:.0f})")
    elif raw <= 25:
        signals.append(f"市場極度恐慌 ({raw:.0f}) — 反向機會？")
    elif raw <= 40:
        signals.append(f"市場情緒悲觀 ({raw:.0f})")
    else:
        signals.append(f"市場情緒中性 ({raw:.0f})")

    return {"score": raw, "signals": signals, "raw": raw}


def _determine_phase(composite: float, tech: dict, money: dict, sent: dict) -> str:
    """依綜合評分決定市場週期"""
    if composite >= 75:
        # 高分：多頭末期（過熱）或 多頭中期（強勢）
        if sent["score"] >= 70:
            return "多頭末期"
        return "多頭中期"
    elif composite >= 60:
        return "多頭初期"
    elif composite >= 45:
        # 中性：震盪整理
        if tech["score"] >= 55:
            return "多頭初期"
        return "空頭初期"
    elif composite >= 30:
        return "空頭初期"
    elif composite >= 15:
        # 低分：空頭中期 or 末期
        if sent["score"] <= 25:
            return "空頭末期"
        return "空頭中期"
    else:
        return "空頭末期"


_PHASE_DESC = {
    "多頭初期": "📈 多頭初期 — 趨勢剛剛翻多，資金開始流入",
    "多頭中期": "🚀 多頭中期 — 趨勢強勁，多方力道充足",
    "多頭末期": "⚠️ 多頭末期 — 市場過熱，需留意反轉風險",
    "空頭初期": "📉 空頭初期 — 趨勢剛剛翻空，謹慎觀望",
    "空頭中期": "🔴 空頭中期 — 空方主導，避免逆勢操作",
    "空頭末期": "💎 空頭末期 — 市場極度悲觀，可能接近底部",
}

_STRATEGY = {
    "多頭初期": [
        "✅ 積極佈局績優成長股",
        "✅ 提高持倉比例至 70-80%",
        "✅ 以動能選股為主",
        "🎯 聚焦：半導體、AI、電子成長股",
    ],
    "多頭中期": [
        "✅ 持倉不動，讓獲利奔跑",
        "✅ 設定移動停利，保護既有獲利",
        "⚖️ 分批獲利了結高漲幅股",
        "🎯 輪動至落後補漲族群",
    ],
    "多頭末期": [
        "⚠️ 降低持倉至 40-60%",
        "⚠️ 嚴格執行停利，不追高",
        "⚠️ 轉向防禦性股票（金融、民生必需）",
        "🔔 密切追蹤外資動向",
    ],
    "空頭初期": [
        "📉 降低持倉至 20-40%",
        "📉 停損弱勢股，現金為王",
        "⚖️ 觀望為主，等待確認訊號",
        "🔔 避免接刀，等 MA 翻多再進場",
    ],
    "空頭中期": [
        "🔴 現金或債券佔比 > 60%",
        "🔴 只留最強核心持股",
        "🔴 可考慮反向 ETF 對沖",
        "⏳ 等待恐慌性殺盤、量縮底部",
    ],
    "空頭末期": [
        "💎 分批低接優質股（5-10% 子彈）",
        "💎 追蹤基本面好但超跌的績優股",
        "⚖️ 保持 60% 現金，機動應對",
        "🚀 等 MA5 > MA20 確認翻多再加碼",
    ],
}


def _suggest_strategy(phase: str) -> list[str]:
    return _STRATEGY.get(phase, ["觀望，等待更明確訊號"])


def format_cycle_report(data: dict) -> str:
    phase = data["phase"]
    desc  = _PHASE_DESC.get(phase, phase)

    def bar(v: float, w: int = 10) -> str:
        filled = round(max(0, min(100, v)) / 100 * w)
        return "█" * filled + "░" * (w - filled)

    lines = [
        f"🌊 市場週期判斷",
        "─" * 30,
        "",
        f"當前週期：{desc}",
        f"綜合評分：{bar(data['composite'])} {data['composite']:.0f}/100",
        "",
        "【技術面】",
        f"  評分：{data['technical']['score']:.0f}/100",
    ]
    for s in data["technical"]["signals"][:3]:
        lines.append(f"  • {s}")

    lines += [
        "",
        "【資金面】",
        f"  評分：{data['money']['score']:.0f}/100",
    ]
    for s in data["money"]["signals"][:3]:
        lines.append(f"  • {s}")

    lines += [
        "",
        "【情緒面】",
        f"  評分：{data['sentiment']['score']:.0f}/100",
    ]
    for s in data["sentiment"]["signals"][:2]:
        lines.append(f"  • {s}")

    lines += [
        "",
        "─" * 30,
        f"📋 對應操作策略（{phase}）",
    ]
    for s in data["strategy"]:
        lines.append(f"  {s}")

    lines.append(f"\n更新時間：{data.get('updated_at', '')}")
    return "\n".join(lines)
