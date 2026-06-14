"""Psychology Service — 市場交易心理分析"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict | None = None
_cache_ts: float = 0.0
_TTL = 900  # 15 min


async def get_market_psychology() -> dict:
    global _cache, _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < _TTL:
        return _cache
    result = await _analyze_psychology()
    _cache = result
    _cache_ts = now
    return result


async def _analyze_psychology() -> dict:
    import asyncio
    vix_task      = _safe_vix()
    overview_task = _safe_overview()
    sentiment_task = _safe_sentiment()

    vix, overview, sentiment = await asyncio.gather(
        vix_task, overview_task, sentiment_task, return_exceptions=True
    )
    vix       = vix       if isinstance(vix, dict)       else {}
    overview  = overview  if isinstance(overview, dict)  else {}
    sentiment = sentiment if isinstance(sentiment, dict) else {}

    fear_greed  = _calc_fear_greed(vix, overview, sentiment)
    chase_ratio = _calc_chase_ratio(overview)
    retail_sent = _calc_retail_sentiment(overview, sentiment)
    advice      = _build_advice(fear_greed, chase_ratio, retail_sent)

    return {
        "fear_greed":   fear_greed,
        "chase_ratio":  chase_ratio,
        "retail_sent":  retail_sent,
        "advice":       advice,
        "updated_at":   time.strftime("%Y-%m-%d %H:%M"),
    }


def _calc_fear_greed(vix: dict, overview: dict, sentiment: dict) -> dict:
    score = 50.0
    vix_val = float(vix.get("us_vix", {}).get("value", 0) if "us_vix" in vix else vix.get("value", 18))
    if vix_val > 30:   score -= 20
    elif vix_val > 20: score -= 10
    elif vix_val < 15: score += 10
    elif vix_val < 12: score += 20

    adv_dec = float(overview.get("advance_decline", 1.0))
    if adv_dec > 2:   score += 15
    elif adv_dec > 1: score += 7
    elif adv_dec < 0.5: score -= 15
    elif adv_dec < 1:   score -= 7

    sent_score = float(sentiment.get("score", 50))
    score += (sent_score - 50) * 0.3

    score = min(100, max(0, score))
    label = _fear_greed_label(score)
    return {"score": round(score, 1), "label": label}


def _fear_greed_label(score: float) -> str:
    if score >= 80: return "極度貪婪"
    if score >= 65: return "貪婪"
    if score >= 50: return "中性偏多"
    if score >= 35: return "恐懼"
    return "極度恐懼"


def _calc_chase_ratio(overview: dict) -> dict:
    turnover = float(overview.get("turnover_rate", 1.0) or 1.0)
    limit_up_cnt = int(overview.get("limit_up_count", 0) or 0)
    total_stocks = int(overview.get("total_stocks", 1700) or 1700)
    ratio = round(limit_up_cnt / total_stocks * 100, 2)
    if ratio > 5:      level = "追高氛圍濃厚（漲停板多）"
    elif ratio > 2:    level = "略有追高"
    elif turnover > 2: level = "換手率高但漲停不多（普漲）"
    else:              level = "追高比例低，市場謹慎"
    return {"ratio": ratio, "limit_up": limit_up_cnt, "level": level}


def _calc_retail_sentiment(overview: dict, sentiment: dict) -> dict:
    margin = float(overview.get("margin_balance", 0) or 0)
    short_cover = float(overview.get("short_cover_ratio", 0) or 0)
    news_pos = float(sentiment.get("positive_ratio", 0.5) or 0.5)

    score = 50.0
    if margin > 5000:  score += 10
    elif margin < -2000: score -= 10
    if news_pos > 0.6: score += 10
    elif news_pos < 0.4: score -= 10
    score = min(100, max(0, score))
    label = "散戶偏多" if score >= 60 else ("散戶偏空" if score <= 40 else "散戶中性")
    return {"score": round(score, 1), "label": label, "news_positive": round(news_pos * 100, 1)}


def _build_advice(fear_greed: dict, chase: dict, retail: dict) -> str:
    fg_score = fear_greed.get("score", 50)
    fg_label = fear_greed.get("label", "中性")
    cr_level = chase.get("level", "")
    rt_label = retail.get("label", "")

    if fg_score >= 80:
        action = "市場極度貪婪，追高風險大，建議保守持倉、分批減碼"
    elif fg_score >= 65:
        action = "貪婪情緒上升，維持既有倉位，不宜大幅加碼追高"
    elif fg_score >= 45:
        action = "市場情緒中性，可正常操作，遵守既定策略"
    elif fg_score >= 30:
        action = "恐懼情緒偏高，優質股逢低可分批建倉"
    else:
        action = "市場極度恐懼，歷史顯示為中長期買點，可逢低積極布局"

    return f"貪婪恐懼指數 {fg_score:.0f}（{fg_label}），{cr_level}，{rt_label}。{action}。"


async def _safe_vix() -> dict:
    try:
        from .vix_service import get_vix_data
        return await get_vix_data()
    except Exception:
        return {}

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


def format_psychology_report(data: dict) -> str:
    if not data:
        return "❌ 無法取得市場心理資料"
    fg   = data["fear_greed"]; chase = data["chase_ratio"]
    ret  = data["retail_sent"]; advice = data["advice"]; ts = data["updated_at"]
    fg_s = fg.get("score", 50)

    def _gauge(s: float) -> str:
        n = int(s / 10)
        return "█" * n + "░" * (10 - n) + f" {s:.0f}"

    lines = [
        "🧠 市場交易心理分析",
        "─" * 32, "",
        f"貪婪/恐懼指數：{_gauge(fg_s)}",
        f"情緒狀態：  【{fg.get('label','')}】",
        "",
        f"追高比例：   {chase.get('ratio', 0):.2f}%（漲停 {chase.get('limit_up', 0)} 支）",
        f"追高氛圍：  {chase.get('level','')}",
        "",
        f"散戶情緒：  {ret.get('label','')}（{ret.get('score',50):.0f}/100）",
        f"新聞正面比：{ret.get('news_positive', 50):.0f}%",
        "",
        "─" * 32,
        "📊 指數說明",
        "0-20: 極度恐懼（潛在買點）",
        "20-40: 恐懼",
        "40-60: 中性",
        "60-80: 貪婪",
        "80-100: 極度貪婪（注意風險）",
        "",
        "─" * 32,
        "🤖 AI 操作建議",
        advice,
        "", f"更新：{ts}",
    ]
    return "\n".join(lines)
