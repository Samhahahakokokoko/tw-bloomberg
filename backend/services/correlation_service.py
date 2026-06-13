"""個股相關性分析服務 — Pearson 相關係數"""
import asyncio
import math
from loguru import logger


def _pearson(a: list[float], b: list[float]) -> float | None:
    """Calculate Pearson correlation between two equal-length series."""
    n = min(len(a), len(b))
    if n < 5:
        return None
    a, b = a[-n:], b[-n:]
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    num = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    den_a = math.sqrt(sum((x - mean_a) ** 2 for x in a))
    den_b = math.sqrt(sum((x - mean_b) ** 2 for x in b))
    if den_a == 0 or den_b == 0:
        return None
    return num / (den_a * den_b)


def _returns(closes: list[float]) -> list[float]:
    """Convert price series to daily return series."""
    if len(closes) < 2:
        return []
    return [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]


async def _fetch_closes(code: str, days: int = 60) -> list[float]:
    try:
        from backend.services.twse_service import fetch_kline
        kline = await fetch_kline(code)
        closes = [float(k["close"]) for k in kline if k.get("close") and float(k["close"]) > 0]
        return closes[-days:]
    except Exception as e:
        logger.debug(f"[corr] {code} fetch error: {e}")
        return []


async def calc_correlation(code: str, n_stocks: int = 20) -> dict:
    """
    Compute correlation between `code` and top-volume stocks from _rt_cache.
    Returns dict with positive/negative correlation lists.
    """
    # 1. Fetch target kline
    target_closes = await _fetch_closes(code, 60)
    if len(target_closes) < 10:
        return {"error": f"無法取得 {code} 的歷史資料"}

    target_name = code
    target_returns = _returns(target_closes)

    # 2. Get top-volume stocks from rt_cache
    compare_codes: list[tuple[str, str]] = []
    try:
        from backend.services.report_screener import _rt_cache
        prices = _rt_cache.get("prices", {})
        if prices:
            # Sort by volume descending, exclude target code
            sorted_stocks = sorted(
                [(c, v) for c, v in prices.items() if c != code],
                key=lambda x: float(x[1].get("volume", 0) or 0),
                reverse=True,
            )
            compare_codes = [(c, str(v.get("name", c))) for c, v in sorted_stocks[:n_stocks]]
            # Also get target name
            if code in prices:
                target_name = prices[code].get("name", code) or code
    except Exception as e:
        logger.debug(f"[corr] rt_cache error: {e}")

    if not compare_codes:
        # Fallback to a small set of liquid stocks
        compare_codes = [
            ("2454", "聯發科"), ("2317", "鴻海"), ("2308", "台達電"),
            ("2382", "廣達"), ("3034", "聯詠"), ("2303", "聯電"),
            ("2882", "國泰金"), ("2609", "陽明"), ("2603", "長榮"),
            ("6505", "台塑化"),
        ]
        compare_codes = [(c, n) for c, n in compare_codes if c != code]

    # 3. Parallel fetch klines
    fetch_tasks = [_fetch_closes(c, 60) for c, _ in compare_codes]
    all_closes = await asyncio.gather(*fetch_tasks, return_exceptions=True)

    # 4. Calculate Pearson correlations
    correlations: list[dict] = []
    for (c, name), closes in zip(compare_codes, all_closes):
        if isinstance(closes, Exception) or not closes:
            continue
        ret = _returns(closes)
        n = min(len(target_returns), len(ret))
        if n < 5:
            continue
        corr = _pearson(target_returns[-n:], ret[-n:])
        if corr is not None:
            correlations.append({"code": c, "name": name, "corr": round(corr, 3)})

    if not correlations:
        return {"error": "計算相關性失敗，資料不足"}

    correlations.sort(key=lambda x: x["corr"], reverse=True)
    positive = [x for x in correlations if x["corr"] > 0][:3]
    negative = sorted([x for x in correlations if x["corr"] < 0], key=lambda x: x["corr"])[:3]

    return {
        "code": code,
        "name": target_name,
        "positive": positive,
        "negative": negative,
        "total": len(correlations),
    }


def format_correlation(data: dict) -> str:
    if "error" in data:
        return f"❌ 相關性分析失敗：{data['error']}"

    lines = [
        f"📊 {data['code']} {data['name']} 相關性分析",
        "─" * 22,
        "",
    ]

    pos = data.get("positive", [])
    if pos:
        lines.append("🔗 高度正相關（同漲同跌）：")
        for i, item in enumerate(pos, 1):
            lines.append(f"  #{i} {item['code']} {item['name']}   +{item['corr']:.2f}")
    else:
        lines.append("🔗 無明顯正相關標的")

    lines.append("")

    neg = data.get("negative", [])
    if neg:
        lines.append("🔀 負相關（對沖標的）：")
        for i, item in enumerate(neg, 1):
            lines.append(f"  #{i} {item['code']} {item['name']}   {item['corr']:.2f}")
    else:
        lines.append("🔀 無明顯負相關標的")

    lines += [
        "",
        f"📌 分析基於近60日日報酬率（{data.get('total', 0)}支對比）",
        "分散建議：高度正相關標的不宜同時持有",
    ]

    return "\n".join(lines)
