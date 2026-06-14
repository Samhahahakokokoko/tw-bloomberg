"""Momentum Ranking Service — 全市場個股動能排行"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict | None = None
_cache_ts: float = 0.0
_TTL = 1800  # 30 min

UNIVERSE = [
    "2330","2454","2317","2412","2882","2303","2308","2002","2603","2886",
    "2891","2881","3711","4938","2382","3034","2379","2395","3443","6669",
    "2345","6770","3231","2337","2408","2357","3045","2409","4966","2207",
]


async def get_momentum_ranking() -> dict:
    global _cache, _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < _TTL:
        return _cache
    result = await _calc_momentum()
    _cache = result
    _cache_ts = now
    return result


async def _calc_momentum() -> dict:
    import asyncio
    tasks = [_score_stock(code) for code in UNIVERSE]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    stocks = [r for r in results if isinstance(r, dict)]

    top_1m = sorted(stocks, key=lambda x: x.get("ret_1m", 0), reverse=True)[:20]
    top_3m = sorted(stocks, key=lambda x: x.get("ret_3m", 0), reverse=True)[:20]

    # 動能+籌碼篩選
    quality = [s for s in stocks
               if s.get("momentum_score", 0) >= 60 and s.get("chip_ok", False)]
    quality = sorted(quality, key=lambda x: x.get("momentum_score", 0), reverse=True)[:10]

    outlook = _ai_summary(top_1m[:5], quality[:3])
    return {
        "top_1m":    top_1m,
        "top_3m":    top_3m,
        "quality":   quality,
        "outlook":   outlook,
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


async def _score_stock(code: str) -> dict:
    try:
        from .twse_service import fetch_realtime_quote, fetch_kline
        q  = await fetch_realtime_quote(code)
        kl = await fetch_kline(code)
        q  = q or {}
        closes  = [float(k.get("close", 0) or 0) for k in (kl or []) if k.get("close")]
        volumes = [float(k.get("volume", 0) or 0) for k in (kl or []) if k.get("volume")]
        if not closes:
            return {}
        cur = closes[-1]
        r1m = _ret(closes, 20); r3m = _ret(closes, 60)
        accel = r1m - _ret(closes, 40)  # 近1月 vs 前1月（加速度）
        rsi   = _rsi(closes)
        # 動能評分
        score = 50.0
        if r1m > 5:  score += 15
        elif r1m > 2: score += 8
        elif r1m < -5: score -= 15
        if accel > 2:  score += 10
        if 40 < rsi < 70: score += 5
        elif rsi > 80:   score -= 10
        # 籌碼：外資
        foreign = float(q.get("foreign_buy") or 0)
        chip_ok = foreign >= 0
        return {
            "code": code, "name": q.get("name", code), "price": cur,
            "ret_1m": round(r1m, 2), "ret_3m": round(r3m, 2),
            "accel": round(accel, 2), "rsi": round(rsi, 1),
            "momentum_score": round(min(100, max(0, score)), 1),
            "chip_ok": chip_ok,
            "foreign": round(foreign),
        }
    except Exception:
        return {}


def _ret(closes: list[float], days: int) -> float:
    if len(closes) < days + 1:
        return 0.0
    base = closes[-days - 1]
    return (closes[-1] - base) / base * 100 if base > 0 else 0.0


def _rsi(closes: list[float], p: int = 14) -> float:
    if len(closes) <= p: return 50.0
    g = l = 0.0
    for i in range(-p, 0):
        d = closes[i] - closes[i-1]
        if d > 0: g += d
        else:     l -= d
    if l == 0: return 100.0
    return 100 - 100 / (1 + g / l)


def _ai_summary(top5: list[dict], quality: list[dict]) -> str:
    if not top5:
        return "資料不足"
    names = ", ".join(f"{s['code']}({s['ret_1m']:+.1f}%)" for s in top5[:3])
    q_names = ", ".join(s['code'] for s in quality[:3])
    return (
        f"近1月動能最強：{names}。"
        f"動能強且籌碼健康標的：{q_names or '待確認'}。"
        f"建議以動能強、RSI未超買、外資買超者為優先追蹤對象。"
    )


def format_momentum_report(data: dict) -> str:
    if not data:
        return "❌ 無法取得動能排行資料"
    top1m = data["top_1m"][:10]; top3m = data["top_3m"][:10]
    quality = data["quality"][:5]; outlook = data["outlook"]; ts = data["updated_at"]

    lines = ["⚡ 全市場動能排行", "─" * 36, "", "🏆 近 1 個月報酬前 10 名", "─" * 32]
    for i, s in enumerate(top1m, 1):
        lines.append(f"{i:>2}. [{s['code']}] {s['name']:<6} {s['ret_1m']:>+6.1f}%  RSI{s['rsi']:>4.0f}")

    lines += ["", "📅 近 3 個月報酬前 10 名", "─" * 32]
    for i, s in enumerate(top3m, 1):
        lines.append(f"{i:>2}. [{s['code']}] {s['name']:<6} {s['ret_3m']:>+6.1f}%  加速{s['accel']:>+4.1f}%")

    if quality:
        lines += ["", "✨ 動能強+籌碼健康精選", "─" * 32]
        for s in quality:
            chip = "✅外資" if s.get("foreign", 0) > 0 else "⬜"
            lines.append(f"  [{s['code']}] {s['name']:<6} 1月{s['ret_1m']:>+5.1f}%  評分{s['momentum_score']:.0f}  {chip}")

    lines += ["", "─" * 32, "🤖 AI 研判", outlook, "", f"更新：{ts}"]
    return "\n".join(lines)
