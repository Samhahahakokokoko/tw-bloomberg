"""Factor Model Service — 多因子評分（動能/價值/品質/籌碼）"""
from __future__ import annotations

import math
import time
from loguru import logger
import asyncio

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 600  # 10 min


async def get_factor_score(code: str) -> dict:
    """計算個股多因子評分，回傳各因子分數與綜合排名"""
    now = time.time()
    if code in _cache and now - _cache_ts.get(code, 0) < _TTL:
        return _cache[code]

    result = await _calc_factors(code)
    _cache[code] = result
    _cache_ts[code] = now
    return result


async def _calc_factors(code: str) -> dict:
    import asyncio
    from .twse_service import fetch_realtime_quote, fetch_kline
    from .chip_service import get_chip_data

    quote_task = fetch_realtime_quote(code)
    kline_task = fetch_kline(code)
    chip_task  = _safe_chip(code)

    quote, kline, chip = await asyncio.gather(
        quote_task, kline_task, chip_task, return_exceptions=True
    )
    quote = quote if isinstance(quote, dict) else {}
    kline = kline if isinstance(kline, list) else []
    chip  = chip  if isinstance(chip, dict)  else {}

    closes  = [float(k.get("close", 0) or 0) for k in kline if k.get("close")]
    volumes = [float(k.get("volume", 0) or 0) for k in kline if k.get("volume")]

    momentum = _score_momentum(closes)
    value    = _score_value(quote)
    quality  = _score_quality(quote, closes)
    chip_sc  = _score_chip(chip, quote)

    composite = round(momentum * 0.25 + value * 0.25 + quality * 0.25 + chip_sc * 0.25, 1)

    peer_comparison = _peer_compare(code, quote)

    return {
        "code":       code,
        "name":       quote.get("name", code),
        "momentum":   momentum,
        "value":      value,
        "quality":    quality,
        "chip":       chip_sc,
        "composite":  composite,
        "peer":       peer_comparison,
        "details":    _build_details(code, quote, kline, chip, closes, momentum, value, quality, chip_sc),
    }


async def _safe_chip(code: str) -> dict:
    try:
        from .chip_service import get_chip_data
        return await get_chip_data(code) or {}
    except Exception as e:
        return {}


def _score_momentum(closes: list[float]) -> float:
    """動能因子：過去 3 個月（約 60 日）報酬率"""
    if len(closes) < 20:
        return 50.0
    lookback = min(60, len(closes) - 1)
    ret = (closes[-1] - closes[-lookback]) / closes[-lookback] * 100 if closes[-lookback] > 0 else 0
    # 線性映射：-15% → 0, 0% → 50, +15% → 100
    score = 50 + ret * (50 / 15)
    return round(max(0, min(100, score)), 1)


def _score_value(quote: dict) -> float:
    """價值因子：PE、PB 相對 baseline"""
    def sf(v):
        try: return float(str(v).replace(",", ""))
        except (ValueError, TypeError): return 0.0

    pe = sf(quote.get("pe_ratio") or quote.get("pe") or 0)
    pb = sf(quote.get("pb_ratio") or quote.get("pb") or 0)

    score = 50.0
    # PE 評分：PE 在 10-20 為理想，< 10 加分，> 30 扣分
    if 0 < pe <= 10:
        score += 25
    elif 10 < pe <= 15:
        score += 20
    elif 15 < pe <= 20:
        score += 10
    elif 20 < pe <= 25:
        score += 0
    elif 25 < pe <= 30:
        score -= 10
    elif pe > 30:
        score -= 20

    # PB 評分：PB < 1.5 加分，> 4 扣分
    if 0 < pb <= 1.0:
        score += 15
    elif 1.0 < pb <= 1.5:
        score += 10
    elif 1.5 < pb <= 2.5:
        score += 5
    elif 2.5 < pb <= 4.0:
        score += 0
    elif pb > 4.0:
        score -= 10

    return round(max(0, min(100, score)), 1)


def _score_quality(quote: dict, closes: list[float]) -> float:
    """品質因子：ROE、殖利率、價格穩定性"""
    def sf(v):
        try: return float(str(v).replace(",", ""))
        except (ValueError, TypeError): return 0.0

    score = 50.0
    roe = sf(quote.get("roe") or 0)
    dy  = sf(quote.get("dividend_yield") or quote.get("yield") or 0)

    if roe >= 20: score += 20
    elif roe >= 15: score += 15
    elif roe >= 10: score += 10
    elif roe >= 5: score += 5
    elif roe < 0: score -= 15

    if dy >= 4: score += 15
    elif dy >= 2: score += 8
    elif dy >= 1: score += 3

    # 波動性懲罰
    if len(closes) >= 20:
        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, min(21, len(closes)))]
        std = math.sqrt(sum(r**2 for r in returns) / len(returns)) * 100
        if std > 3: score -= 10
        elif std > 2: score -= 5

    return round(max(0, min(100, score)), 1)


def _score_chip(chip: dict, quote: dict) -> float:
    """籌碼因子：法人持股比例、近期買超"""
    def si(v):
        try: return int(str(v).replace(",", ""))
        except (ValueError, TypeError): return 0

    score = 50.0
    foreign_net = si(chip.get("foreign_net") or chip.get("foreignNet") or 0)
    trust_net   = si(chip.get("trust_net")   or chip.get("trustNet")   or 0)
    dealer_net  = si(chip.get("dealer_net")  or chip.get("dealerNet")  or 0)

    total_inst = foreign_net + trust_net + dealer_net
    # 每千張淨買超 → 調整分數
    adj = total_inst / 1000
    score += max(-30, min(30, adj * 2))

    # 外資連買訊號
    if foreign_net > 5000: score += 15
    elif foreign_net > 1000: score += 8
    elif foreign_net > 0: score += 3
    elif foreign_net < -5000: score -= 15
    elif foreign_net < 0: score -= 5

    return round(max(0, min(100, score)), 1)


def _peer_compare(code: str, quote: dict) -> str:
    """與同業比較（基於產業基準）"""
    sector = quote.get("industry") or quote.get("sector") or ""

    # 半導體類股 benchmark
    benchmarks = {
        "半導體": {"pe": 20, "pb": 3.0, "roe": 15},
        "IC設計": {"pe": 25, "pb": 4.0, "roe": 12},
        "金融":   {"pe": 12, "pb": 1.2, "roe": 10},
        "傳產":   {"pe": 10, "pb": 1.0, "roe": 8},
    }

    def sf(v):
        try: return float(str(v).replace(",", ""))
        except (ValueError, TypeError): return 0.0

    pe = sf(quote.get("pe_ratio") or quote.get("pe") or 0)
    pb = sf(quote.get("pb_ratio") or quote.get("pb") or 0)

    bench = None
    for k, v in benchmarks.items():
        if k in sector:
            bench = v
            break

    if not bench or pe <= 0:
        return "同業比較：資料不足"

    pe_rel = "低估" if pe < bench["pe"] * 0.8 else ("高估" if pe > bench["pe"] * 1.2 else "合理")
    pb_rel = "低估" if pb < bench["pb"] * 0.8 else ("高估" if pb > bench["pb"] * 1.2 else "合理")
    return f"相對{sector}同業：PE {pe_rel}、PB {pb_rel}"


def _build_details(code, quote, kline, chip, closes, momentum, value, quality, chip_sc):
    def sf(v):
        try: return float(str(v).replace(",", ""))
        except (ValueError, TypeError): return 0.0
    def si(v):
        try: return int(str(v).replace(",", ""))
        except (ValueError, TypeError): return 0

    pe  = sf(quote.get("pe_ratio") or quote.get("pe") or 0)
    pb  = sf(quote.get("pb_ratio") or quote.get("pb") or 0)
    roe = sf(quote.get("roe") or 0)
    dy  = sf(quote.get("dividend_yield") or quote.get("yield") or 0)

    ret_3m = 0.0
    if len(closes) >= 2:
        lb = min(60, len(closes) - 1)
        ret_3m = (closes[-1] - closes[-lb]) / closes[-lb] * 100 if closes[-lb] > 0 else 0

    foreign_net = si(chip.get("foreign_net") or chip.get("foreignNet") or 0)
    trust_net   = si(chip.get("trust_net")   or chip.get("trustNet")   or 0)

    return {
        "pe":         pe,
        "pb":         pb,
        "roe":        roe,
        "dy":         dy,
        "ret_3m":     round(ret_3m, 2),
        "foreign_net":foreign_net,
        "trust_net":  trust_net,
    }


def format_factor_report(data: dict) -> str:
    d = data.get("details", {})

    def bar(v: float, w: int = 10) -> str:
        filled = round(max(0, min(100, v)) / 100 * w)
        return "█" * filled + "░" * (w - filled)

    def grade(v: float) -> str:
        if v >= 80: return "A+"
        if v >= 70: return "A"
        if v >= 60: return "B+"
        if v >= 50: return "B"
        if v >= 40: return "C+"
        return "C"

    comp = data.get("composite", 50)
    lines = [
        f"📊 {data['code']} {data['name']} 多因子評分",
        "─" * 28,
        "",
        f"動能因子  {bar(data['momentum'])}  {data['momentum']:.0f}/100 ({grade(data['momentum'])})",
        f"  └ 3個月報酬：{d.get('ret_3m', 0):+.1f}%",
        f"",
        f"價值因子  {bar(data['value'])}  {data['value']:.0f}/100 ({grade(data['value'])})",
        f"  └ PE:{d.get('pe', 0):.1f}  PB:{d.get('pb', 0):.1f}",
        f"",
        f"品質因子  {bar(data['quality'])}  {data['quality']:.0f}/100 ({grade(data['quality'])})",
        f"  └ ROE:{d.get('roe', 0):.1f}%  殖利率:{d.get('dy', 0):.1f}%",
        f"",
        f"籌碼因子  {bar(data['chip'])}  {data['chip']:.0f}/100 ({grade(data['chip'])})",
        f"  └ 外資:{d.get('foreign_net', 0):+,}張  投信:{d.get('trust_net', 0):+,}張",
        f"",
        "─" * 28,
        f"綜合評分  {bar(comp, 14)}  {comp:.0f}/100",
        f"等級：{grade(comp)}  {'🔥推薦' if comp >= 70 else ('⚠️中性' if comp >= 50 else '🔻觀望')}",
        "",
        data.get("peer", ""),
        "",
        f"提示：各因子權重各25%",
    ]
    return "\n".join(l for l in lines if l is not None)
