"""Stress Test Service — 個股極端情境壓力測試"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 3600

# 各股票對美股/匯率/景氣的歷史敏感度係數（Beta-like）
_SENSITIVITY: dict[str, dict] = {
    "2330": {"us_beta": 0.85, "fx_sensitivity": 0.60, "recession_discount": 0.35},
    "2454": {"us_beta": 0.90, "fx_sensitivity": 0.55, "recession_discount": 0.40},
    "2317": {"us_beta": 0.75, "fx_sensitivity": 0.45, "recession_discount": 0.45},
    "2382": {"us_beta": 0.95, "fx_sensitivity": 0.50, "recession_discount": 0.50},
    "2308": {"us_beta": 0.70, "fx_sensitivity": 0.40, "recession_discount": 0.30},
    "2881": {"us_beta": 0.50, "fx_sensitivity": 0.80, "recession_discount": 0.25},
    "2882": {"us_beta": 0.55, "fx_sensitivity": 0.75, "recession_discount": 0.25},
    "2303": {"us_beta": 0.80, "fx_sensitivity": 0.50, "recession_discount": 0.42},
    "3008": {"us_beta": 0.65, "fx_sensitivity": 0.30, "recession_discount": 0.38},
    "2412": {"us_beta": 0.45, "fx_sensitivity": 0.20, "recession_discount": 0.20},
    "0050": {"us_beta": 0.80, "fx_sensitivity": 0.50, "recession_discount": 0.35},
}
_DEFAULT_SENS = {"us_beta": 0.80, "fx_sensitivity": 0.45, "recession_discount": 0.40}


async def get_stress_test(code: str) -> dict:
    key = code.upper()
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_stress(key)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_stress(code: str) -> dict:
    import httpx
    sens = _SENSITIVITY.get(code, _DEFAULT_SENS)

    # 抓取現價
    price = 0.0
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url, params={"interval": "1d", "range": "5d"},
                            headers={"User-Agent": "Mozilla/5.0"})
        closes = r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        closes = [x for x in closes if x is not None]
        price = closes[-1] if closes else 0.0
    except Exception as e:
        logger.debug(f"[stress] fetch price {code}: {e}")
        price = 100.0  # fallback

    # 情境 1：美股崩跌 10%
    us_drop_10 = -10.0 * sens["us_beta"]
    us_price_10 = price * (1 + us_drop_10 / 100)

    # 情境 2：美股崩跌 20%（黑天鵝）
    us_drop_20 = -20.0 * sens["us_beta"]
    us_price_20 = price * (1 + us_drop_20 / 100)

    # 情境 3：台幣貶值 5%（出口股受惠、進口成本上升）
    # 出口導向股：台幣貶值對毛利正面（約 +0.3~0.5x），扣掉原物料成本
    fx_impact = 5.0 * sens["fx_sensitivity"] * 0.6   # net positive for exporters
    fx_exporter = price * (1 + fx_impact / 100)
    fx_direction = "正面（出口競爭力提升）" if sens["fx_sensitivity"] > 0.35 else "中性（內需為主）"

    # 情境 4：景氣衰退（EPS 下修 + 本益比收縮）
    recession_drop = price * sens["recession_discount"]
    recession_price = price - recession_drop

    # 情境 5：台股大盤跌 5%（系統性風險）
    market_drop_5 = -5.0 * sens["us_beta"] * 0.9
    market_price_5 = price * (1 + market_drop_5 / 100)

    scenarios = [
        {
            "name":    "美股崩跌 10%",
            "impact":  round(us_drop_10, 1),
            "price":   round(us_price_10, 1),
            "level":   "中風險",
            "strategy": "分批承接，跌至強支撐區（20週線）可逢低布局，停損設 -15%",
        },
        {
            "name":    "美股崩跌 20%（黑天鵝）",
            "impact":  round(us_drop_20, 1),
            "price":   round(us_price_20, 1),
            "level":   "高風險",
            "strategy": "現金為王，等待恐慌指標極值後再進場，優先保留子彈",
        },
        {
            "name":    f"台幣貶值 5%",
            "impact":  round(fx_impact, 1),
            "price":   round(fx_exporter, 1),
            "level":   "低風險",
            "strategy": f"影響方向：{fx_direction}。台幣貶通常對出口導向半導體/電子股偏正面",
            "positive": True,
        },
        {
            "name":    "景氣衰退（最壞情況）",
            "impact":  round(-sens["recession_discount"] * 100, 1),
            "price":   round(recession_price, 1),
            "level":   "最壞情境",
            "strategy": f"目標價：{recession_price:.0f}。佈局時機：本益比回到歷史低點（景氣低谷的 15-20x PE）",
        },
        {
            "name":    "台股大盤跌 5%",
            "impact":  round(market_drop_5, 1),
            "price":   round(market_price_5, 1),
            "level":   "中風險",
            "strategy": "確認個股基本面未變，利用大盤回測支撐時加碼",
        },
    ]

    overall = _gen_overall_strategy(code, price, sens, scenarios)

    return {
        "code":      code,
        "price":     round(price, 2),
        "sens":      sens,
        "scenarios": scenarios,
        "overall":   overall,
    }


def _gen_overall_strategy(code: str, price: float, sens: dict, scenarios: list) -> str:
    beta = sens["us_beta"]
    if beta >= 0.85:
        profile = "高Beta科技股（與美股連動性強）"
        tip = "建議美股大跌前降低持倉至 3-5 成，等待恐慌指標見底後加碼"
    elif beta >= 0.65:
        profile = "中Beta成長股"
        tip = "可保持 5-7 成持倉，關注美股費半走勢作為領先指標"
    else:
        profile = "低Beta防禦股（受市場波動影響較小）"
        tip = "適合作為避風港，可維持較高持倉，注意匯率和利率風險"
    return f"{code} 屬於{profile}。{tip}"


def format_stress_report(data: dict, code: str) -> str:
    price     = data.get("price", 0)
    scenarios = data.get("scenarios", [])
    overall   = data.get("overall", "")

    LEVEL_ICON = {"低風險": "🟢", "中風險": "🟡", "高風險": "🔴", "最壞情境": "💀"}

    lines = [
        f"⚡ 壓力測試  {code}",
        "─" * 32, "",
        f"目前股價：{price:.2f}",
        "",
        "情境模擬：",
    ]

    for s in scenarios:
        icon    = LEVEL_ICON.get(s["level"], "⬜")
        impact  = s["impact"]
        pos     = s.get("positive", False)
        imp_str = f"+{impact:.1f}%" if pos else f"{impact:.1f}%"
        lines += [
            f"",
            f"  {icon} {s['name']}",
            f"     預計影響：{imp_str}  →  目標價 {s['price']:.1f}",
            f"     因應策略：{s['strategy']}",
        ]

    lines += [
        "",
        "─" * 28,
        "🤖 AI 整體風險評估",
        overall,
        "",
        "⚠️ 以上為統計估算，實際影響因個股基本面而異",
        "輸入 /chiphealth 查籌碼健康 | /feargreed 查恐慌指數",
    ]
    return "\n".join(lines)
