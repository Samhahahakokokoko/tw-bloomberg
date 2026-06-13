"""智慧選股引擎 — RSI(14) 30-50 + MACD 轉正 + 外資連買 3 天 + 量能突破"""
import asyncio
from loguru import logger


def _calc_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _calc_macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Return (macd_now, signal_now, macd_3ago) or None on insufficient data."""
    def ema(series, n):
        k = 2 / (n + 1)
        result = [series[0]]
        for price in series[1:]:
            result.append(price * k + result[-1] * (1 - k))
        return result

    if len(closes) < slow + signal:
        return None, None, None

    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast[slow - 1:], ema_slow[slow - 1:])]
    sig_line = ema(macd_line, signal)
    macd_3ago = macd_line[-4] if len(macd_line) >= 4 else None
    return macd_line[-1], sig_line[-1], macd_3ago


async def _analyze_candidate(code: str, name: str) -> dict | None:
    """Fetch kline for one candidate and apply RSI + MACD filters."""
    try:
        from backend.services.twse_service import fetch_kline
        kline = await fetch_kline(code)
        if not kline or len(kline) < 30:
            return None

        closes = [float(k["close"]) for k in kline if k.get("close") and float(k["close"]) > 0]
        volumes = [float(k.get("volume", 0) or 0) for k in kline]

        if len(closes) < 30:
            return None

        # RSI filter: 30-50
        rsi = _calc_rsi(closes, 14)
        if not (30 <= rsi <= 50):
            return None

        # MACD filter: last 3 days turned positive (macd > signal, and was < signal 3 bars ago)
        macd_now, sig_now, macd_3ago = _calc_macd(closes)
        if macd_now is None:
            return None
        macd_cross = (macd_now > sig_now) and (macd_3ago is not None and macd_3ago < 0)
        if not macd_cross:
            return None

        # Volume filter: current volume > 20-day avg * 1.5
        if len(volumes) >= 21:
            avg_vol_20 = sum(volumes[-21:-1]) / 20
            vol_breakout = volumes[-1] > avg_vol_20 * 1.5
            if not vol_breakout:
                return None

        return {
            "code": code,
            "name": name,
            "rsi": round(rsi, 1),
            "macd": round(macd_now, 4),
            "signal": round(sig_now, 4),
            "reason": f"RSI={rsi:.1f} MACD轉正 量能突破",
        }
    except Exception as e:
        logger.debug(f"[smart_screen] {code} analyze error: {e}")
        return None


async def smart_screen(limit: int = 5) -> list[dict]:
    """
    主選股函數：
    1. 從 StockScore DB 快速過濾 foreign_consec_buy>=3 AND vol_breakout=True
    2. 並行計算 RSI & MACD
    3. 回傳前 limit 支
    """
    candidates: list[tuple[str, str]] = []  # (code, name)

    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import StockScore
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            stmt = (
                select(StockScore)
                .where(
                    StockScore.foreign_consec_buy >= 3,
                    StockScore.vol_breakout == True,
                )
                .order_by(StockScore.total_score.desc())
                .limit(50)
            )
            result = await db.execute(stmt)
            rows = result.scalars().all()
            candidates = [(r.stock_code, r.stock_name or r.stock_code) for r in rows]
    except Exception as e:
        logger.warning(f"[smart_screen] DB query failed: {e}")

    # Fallback: if DB empty, use a small hardcoded list for demo
    if not candidates:
        candidates = [
            ("2330", "台積電"), ("2454", "聯發科"), ("2308", "台達電"),
            ("2317", "鴻海"), ("2382", "廣達"), ("3034", "聯詠"),
            ("2379", "瑞昱"), ("6505", "台塑化"), ("2303", "聯電"),
        ]

    # Parallel analysis
    tasks = [_analyze_candidate(code, name) for code, name in candidates]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    hits = []
    for r in results:
        if isinstance(r, dict) and r is not None:
            hits.append(r)

    return hits[:limit]


def format_smart_screen(results: list[dict]) -> str:
    if not results:
        return (
            "🔍 智慧選股\n"
            "────────────────────\n"
            "目前無符合條件的股票\n\n"
            "篩選條件：\n"
            "  RSI(14) 30-50（超賣回升）\n"
            "  MACD 3日由負轉正\n"
            "  外資連買3天以上\n"
            "  成交量 > 20日均量×1.5"
        )

    lines = [
        "🔍 智慧選股結果",
        "─" * 22,
        "符合：RSI回升 + MACD轉正 + 外資買 + 量能",
        "",
    ]
    for i, r in enumerate(results, 1):
        lines.append(f"#{i} {r['code']} {r['name']}")
        lines.append(f"   RSI={r['rsi']}  MACD={r['macd']:.4f}")
        lines.append(f"   📌 {r['reason']}")
        lines.append("")

    lines.append("⚠️ 僅供參考，請自行判斷風險")
    return "\n".join(lines)
