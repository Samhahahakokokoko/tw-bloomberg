"""股票健診 — 技術面 + 籌碼面 + 估值面 綜合評分"""
import httpx
from loguru import logger
from .twse_service import fetch_kline, fetch_institutional


async def check_stock_health(stock_code: str) -> dict:
    """
    健診評分 (0-100)：
    - 技術面 40%: 均線趨勢、RSI
    - 籌碼面 30%: 三大法人淨買賣
    - 估值面 30%: PE、PB、殖利率
    """
    scores = {}
    details = {}

    # 技術面
    try:
        tech_score, tech_detail = await _tech_score(stock_code)
        scores["technical"] = tech_score
        details["technical"] = tech_detail
    except Exception as e:
        logger.error(f"Health tech score error {stock_code}: {e}")
        scores["technical"] = 50
        details["technical"] = {}

    # 籌碼面
    try:
        chip_score, chip_detail = await _chip_score(stock_code)
        scores["chip"] = chip_score
        details["chip"] = chip_detail
    except Exception as e:
        scores["chip"] = 50
        details["chip"] = {}

    # 估值面
    try:
        val_score, val_detail = await _valuation_score(stock_code)
        scores["valuation"] = val_score
        details["valuation"] = val_detail
    except Exception as e:
        scores["valuation"] = 50
        details["valuation"] = {}

    overall = scores["technical"] * 0.4 + scores["chip"] * 0.3 + scores["valuation"] * 0.3

    if overall >= 80:
        grade, grade_label = "A", "強勢"
    elif overall >= 65:
        grade, grade_label = "B", "偏多"
    elif overall >= 50:
        grade, grade_label = "C", "中性"
    elif overall >= 35:
        grade, grade_label = "D", "偏空"
    else:
        grade, grade_label = "F", "弱勢"

    # 操作建議
    suggestions = _build_suggestions(scores, details, overall)

    return {
        "stock_code": stock_code,
        "overall_score": round(overall, 1),
        "grade": grade,
        "grade_label": grade_label,
        "scores": {k: round(v, 1) for k, v in scores.items()},
        "details": details,
        "suggestions": suggestions,
    }


async def _tech_score(stock_code: str) -> tuple[float, dict]:
    kline = await fetch_kline(stock_code)
    closes = [float(k["close"]) for k in kline if k.get("close") and float(k["close"]) > 0]
    if len(closes) < 20:
        return 50.0, {"error": "K線資料不足"}

    current = closes[-1]
    ma5  = sum(closes[-5:])  / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20

    rsi = _calc_rsi(closes, 14)

    trend_score = 0
    if current > ma5:  trend_score += 20
    if current > ma10: trend_score += 20
    if current > ma20: trend_score += 20
    if ma5 > ma10:     trend_score += 20
    if ma10 > ma20:    trend_score += 20

    if 40 <= rsi <= 60:
        rsi_score = 80
    elif 30 <= rsi < 40 or 60 < rsi <= 70:
        rsi_score = 60
    elif 20 <= rsi < 30 or 70 < rsi <= 80:
        rsi_score = 40
    else:
        rsi_score = 20

    score = trend_score * 0.6 + rsi_score * 0.4

    # 計算 5 日、20 日漲跌幅
    chg5 = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else 0
    chg20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 else 0

    return score, {
        "ma5": round(ma5, 2), "ma10": round(ma10, 2), "ma20": round(ma20, 2),
        "rsi": round(rsi, 1), "current": round(current, 2),
        "above_ma5": current > ma5,
        "above_ma10": current > ma10,
        "above_ma20": current > ma20,
        "chg5d": round(chg5, 2),
        "chg20d": round(chg20, 2),
    }


async def _chip_score(stock_code: str) -> tuple[float, dict]:
    inst = await fetch_institutional(stock_code)
    if not inst:
        return 50.0, {}

    total_net    = inst.get("total_net", 0)
    foreign_net  = inst.get("foreign_net", 0)
    trust_net    = inst.get("investment_trust_net", 0)
    dealer_net   = inst.get("dealer_net", 0)

    score = 50.0
    if total_net > 1000:   score += 30
    elif total_net > 0:    score += 15
    elif total_net < -1000: score -= 30
    elif total_net < 0:    score -= 15

    if foreign_net > 0:  score += 15
    elif foreign_net < 0: score -= 15

    if trust_net > 0:  score += 5
    elif trust_net < 0: score -= 5

    score = max(0, min(100, score))
    return score, {
        "foreign_net": foreign_net,
        "trust_net": trust_net,
        "dealer_net": dealer_net,
        "total_net": total_net,
        "date": inst.get("date", ""),
    }


async def _valuation_score(stock_code: str) -> tuple[float, dict]:
    url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_d"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            data = resp.json()
            item = next((x for x in data if x.get("Code") == stock_code), None)
            if not item:
                return 50.0, {}

            pe = float(item.get("PEratio", 0) or 0)
            pb = float(item.get("PBratio", 0) or 0)
            dy = float(item.get("DividendYield", 0) or 0)

            score = 50.0
            if 0 < pe <= 15:   score += 20
            elif 15 < pe <= 25: score += 10
            elif pe > 40:       score -= 15

            if 0 < pb <= 1.5:  score += 15
            elif 1.5 < pb <= 2.5: score += 5
            elif pb > 5:        score -= 10

            if dy >= 5:   score += 15
            elif dy >= 3: score += 10
            elif dy >= 1: score += 5

            score = max(0, min(100, score))
            return score, {"pe_ratio": pe, "pb_ratio": pb, "dividend_yield": dy}
    except Exception as e:
        logger.error(f"Valuation score error {stock_code}: {e}")
    return 50.0, {}


def _build_suggestions(scores: dict, details: dict, overall: float) -> list[str]:
    tips = []
    tech = details.get("technical", {})
    chip = details.get("chip", {})
    val  = details.get("valuation", {})

    if overall >= 70:
        tips.append("整體評分偏強，可考慮持有或小幅加碼。")
    elif overall < 40:
        tips.append("整體評分偏弱，建議觀望或減碼。")

    if tech.get("rsi", 50) > 75:
        tips.append("RSI 過熱（超買），注意短線拉回風險。")
    elif tech.get("rsi", 50) < 25:
        tips.append("RSI 超賣，可能存在反彈機會。")

    if chip.get("total_net", 0) > 0 and chip.get("foreign_net", 0) > 0:
        tips.append("外資持續買超，籌碼面偏多。")
    elif chip.get("total_net", 0) < 0:
        tips.append("法人持續賣超，注意籌碼賣壓。")

    if val.get("dividend_yield", 0) >= 5:
        tips.append(f"殖利率 {val['dividend_yield']}% 具吸引力，適合存股族。")

    return tips


def _calc_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    recent = deltas[-period:]
    gains  = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))
