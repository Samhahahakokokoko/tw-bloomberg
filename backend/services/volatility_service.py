"""Volatility Service — 波動率分析（歷史/相對/趨勢）"""
from __future__ import annotations

import math
import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 600


async def get_volatility_analysis(code: str) -> dict:
    now = time.time()
    if code in _cache and now - _cache_ts.get(code, 0) < _TTL:
        return _cache[code]

    result = await _calc_volatility(code)
    _cache[code] = result
    _cache_ts[code] = now
    return result


async def _calc_volatility(code: str) -> dict:
    import asyncio
    from .twse_service import fetch_kline, fetch_realtime_quote

    kline_task = fetch_kline(code)
    quote_task = fetch_realtime_quote(code)
    # 大盤 kline (0050 為替代)
    taiex_task = _safe_kline("0050")

    kline, quote, taiex = await asyncio.gather(
        kline_task, quote_task, taiex_task, return_exceptions=True
    )
    kline  = kline  if isinstance(kline,  list) else []
    taiex  = taiex  if isinstance(taiex,  list) else []
    quote  = quote  if isinstance(quote,  dict) else {}

    closes_stock = [float(k.get("close", 0) or 0) for k in kline  if k.get("close")]
    closes_mkt   = [float(k.get("close", 0) or 0) for k in taiex  if k.get("close")]

    hv20  = _hist_vol(closes_stock, 20)
    hv60  = _hist_vol(closes_stock, 60)
    mkt20 = _hist_vol(closes_mkt,   20)

    relative = _relative_vol(hv20, mkt20)
    trend    = _vol_trend(closes_stock)
    rec      = _recommend(hv20, hv60, relative, trend)

    return {
        "code":     code,
        "name":     quote.get("name", code),
        "hv20":     hv20,
        "hv60":     hv60,
        "mkt20":    mkt20,
        "relative": relative,
        "trend":    trend,
        "rec":      rec,
    }


async def _safe_kline(code: str) -> list:
    try:
        from .twse_service import fetch_kline
        return await fetch_kline(code) or []
    except Exception:
        return []


def _hist_vol(closes: list[float], window: int) -> float:
    """年化歷史波動率（%）"""
    if len(closes) < window + 1:
        return 0.0
    sub = closes[-(window + 1):]
    rets = []
    for i in range(1, len(sub)):
        if sub[i - 1] > 0:
            rets.append(math.log(sub[i] / sub[i - 1]))
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    variance = sum((r - mean) ** 2 for r in rets) / len(rets)
    daily_std = math.sqrt(variance)
    annual    = daily_std * math.sqrt(252) * 100
    return round(annual, 2)


def _relative_vol(stock_hv: float, mkt_hv: float) -> float:
    """相對波動率 = 個股 / 大盤"""
    if mkt_hv <= 0:
        return 1.0
    return round(stock_hv / mkt_hv, 2)


def _vol_trend(closes: list[float]) -> str:
    """波動率趨勢：擴張 / 收縮 / 穩定"""
    hv_recent = _hist_vol(closes, 10)
    hv_old    = _hist_vol(closes[:-10] if len(closes) > 20 else closes, 20)
    if hv_recent == 0 or hv_old == 0:
        return "穩定"
    ratio = hv_recent / hv_old
    if ratio > 1.2:
        return "擴張 📈"
    if ratio < 0.8:
        return "收縮 📉"
    return "穩定 ➡️"


def _recommend(hv20: float, hv60: float, relative: float, trend: str) -> str:
    lines = []

    if hv20 < 20:
        lines.append("✅ 波動率低（<20%），適合長線佈局")
    elif hv20 < 35:
        lines.append("⚖️ 波動率中等（20-35%），短中線均可")
    else:
        lines.append("⚠️ 波動率高（>35%），適合短線操作，需嚴格停損")

    if relative > 1.5:
        lines.append(f"🔴 個股波動是大盤 {relative}x，風險明顯偏高")
    elif relative > 1.2:
        lines.append(f"🟡 個股波動略高於大盤（{relative}x）")
    else:
        lines.append(f"🟢 個股波動與大盤相當（{relative}x）")

    if "擴張" in trend:
        lines.append("📊 波動率上升中 — 近期行情可能加劇")
    elif "收縮" in trend:
        lines.append("📊 波動率下降中 — 行情趨於平靜")

    return "\n".join(lines)


def format_volatility_report(data: dict) -> str:
    lines = [
        f"📉 {data['code']} {data['name']} 波動率分析",
        "─" * 28,
        "",
        f"歷史波動率 (20日)：{data['hv20']:.1f}%",
        f"歷史波動率 (60日)：{data['hv60']:.1f}%",
        f"大盤波動率 (20日)：{data['mkt20']:.1f}%",
        "",
        f"相對波動率：{data['relative']:.2f}x（個股/大盤）",
        f"波動率趨勢：{data['trend']}",
        "",
        "─" * 28,
        "📋 操作建議",
        data["rec"],
    ]
    return "\n".join(lines)
