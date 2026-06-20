"""Divergence Service — 量價背離 & RSI 背離偵測"""
from __future__ import annotations

import time
from loguru import logger
import asyncio
import math

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 900  # 15 min


async def get_divergence(code: str) -> dict:
    key = code.strip()
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_divergence(key)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_divergence(code: str) -> dict:
    import asyncio
    hist_task  = _get_hist(code)
    quote_task = _get_quote(code)

    hist, quote = await asyncio.gather(hist_task, quote_task, return_exceptions=True)
    hist  = hist  if isinstance(hist, list)  else []
    quote = quote if isinstance(quote, dict) else {}

    price = float(quote.get("close") or (hist[-1]["close"] if hist else 0))
    signals = _detect_signals(hist)
    strength, verdict = _ai_judge(signals, hist, price)

    return {
        "code":     code,
        "name":     quote.get("name", code),
        "price":    price,
        "signals":  signals,
        "strength": strength,
        "verdict":  verdict,
        "hist":     hist[-10:] if hist else [],
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


async def _get_hist(code: str) -> list:
    try:
        import httpx
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
               f"?interval=1d&range=30d")
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as cl:
            r = await cl.get(url)
        js = r.json()
        q  = js["chart"]["result"][0]["indicators"]["quote"][0]
        bars = []
        for c, h, lo, v in zip(
            q.get("close",  []), q.get("high",   []),
            q.get("low",    []), q.get("volume", [])
        ):
            if c:
                bars.append({"close": c, "high": h or c, "low": lo or c, "volume": v or 0})
        return bars
    except Exception as e:
        logger.debug(f"[divergence] hist {code}: {e}")
        return []
    return bars


async def _get_quote(code: str) -> dict:
    try:
        from .twse_service import fetch_realtime_quote
        return await fetch_realtime_quote(code) or {}
    except Exception as e:
        return {}


def _calc_rsi(closes: list, n: int = 14) -> list:
    if len(closes) < n + 1:
        return [50.0] * len(closes)
    rsis = [50.0] * n
    gains  = [max(0, closes[i] - closes[i-1]) for i in range(1, len(closes))]
    losses = [max(0, closes[i-1] - closes[i]) for i in range(1, len(closes))]
    ag = sum(gains[:n])  / n
    al = sum(losses[:n]) / n or 0.001
    rsis.append(round(100 - 100 / (1 + ag / al), 1))
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i])  / n
        al = (al * (n - 1) + losses[i]) / n or 0.001
        rsis.append(round(100 - 100 / (1 + ag / al), 1))
    return rsis


def _detect_signals(hist: list) -> dict:
    signals = {
        "price_up_vol_down":   False,  # 價漲量縮
        "price_down_vol_down": False,  # 價跌量縮
        "price_up_vol_spike":  False,  # 價漲量爆（出貨訊號）
        "rsi_bull_diverge":    False,  # RSI 多頭背離（底部走強）
        "rsi_bear_diverge":    False,  # RSI 空頭背離（頂部走弱）
    }
    if len(hist) < 10:
        return signals

    closes = [b["close"] for b in hist]
    vols   = [b["volume"] for b in hist]
    rsis   = _calc_rsi(closes)

    avg_vol = sum(vols[:-5]) / max(len(vols[:-5]), 1)
    recent  = hist[-5:]
    r_vols  = [b["volume"] for b in recent]
    r_closes= [b["close"]  for b in recent]

    price_up   = r_closes[-1] > r_closes[0]
    price_down = r_closes[-1] < r_closes[0]
    avg_recent_vol = sum(r_vols) / len(r_vols)
    vol_shrink = avg_recent_vol < avg_vol * 0.65
    vol_spike  = r_vols[-1] > avg_vol * 2.5

    signals["price_up_vol_down"]   = price_up   and vol_shrink
    signals["price_down_vol_down"] = price_down and vol_shrink
    signals["price_up_vol_spike"]  = price_up   and vol_spike

    # RSI divergence: compare last two swing lows/highs
    if len(rsis) >= 15:
        # Bear divergence: price makes higher high, RSI makes lower high
        ph1, ph2 = max(closes[-15:-8]), max(closes[-8:])
        rh1 = max(rsis[-15:-8]); rh2 = max(rsis[-8:])
        if ph2 > ph1 * 1.01 and rh2 < rh1 - 3:
            signals["rsi_bear_diverge"] = True

        # Bull divergence: price makes lower low, RSI makes higher low
        pl1, pl2 = min(closes[-15:-8]), min(closes[-8:])
        rl1 = min(rsis[-15:-8]); rl2 = min(rsis[-8:])
        if pl2 < pl1 * 0.99 and rl2 > rl1 + 3:
            signals["rsi_bull_diverge"] = True

    return signals


def _ai_judge(signals: dict, hist: list, price: float) -> tuple:
    active = [k for k, v in signals.items() if v]
    if not active:
        return "無明顯背離", "目前無量價背離訊號，走勢與成交量同步，型態健康。"

    parts = []
    if signals["price_up_vol_spike"]:
        parts.append("價漲量爆（主力可能借漲出貨，需謹慎追高）")
        strength = "警示訊號"
    elif signals["price_up_vol_down"]:
        parts.append("價漲量縮（上漲動能不足，突破需放量確認）")
        strength = "弱多訊號"
    elif signals["price_down_vol_down"]:
        parts.append("價跌量縮（下跌動能減弱，賣壓趨緩，可能止跌）")
        strength = "止跌訊號"
    else:
        strength = "中性"

    if signals["rsi_bear_diverge"]:
        parts.append("RSI 空頭背離（價格創高但 RSI 未跟上，潛在反轉風險）")
        strength = "警示訊號"
    if signals["rsi_bull_diverge"]:
        parts.append("RSI 多頭背離（價格創低但 RSI 走強，底部反轉機會）")
        strength = "底部訊號"

    verdict = "偵測到：" + "；".join(parts) + "。"
    if "止跌" in verdict or "底部" in verdict:
        verdict += " 建議逢低觀察，等量能放大再確認進場。"
    elif "警示" in strength:
        verdict += " 建議謹慎，不追高，已持有者可考慮分批減碼。"
    return strength, verdict


def format_divergence_report(data: dict) -> str:
    if not data or data.get("error"):
        return f"❌ {data.get('error', '無法取得背離資料')}"

    code     = data["code"]; name = data["name"]; price = data["price"]
    signals  = data["signals"]; strength = data["strength"]
    verdict  = data["verdict"]; hist = data["hist"]; ts = data["updated_at"]

    SIGNAL_LABELS = {
        "price_up_vol_down":   ("⚠️", "價漲量縮", "上漲動能不足"),
        "price_down_vol_down": ("🟡", "價跌量縮", "賣壓趨緩"),
        "price_up_vol_spike":  ("🔴", "價漲量爆", "可能出貨訊號"),
        "rsi_bull_diverge":    ("🟢", "RSI多頭背離", "底部反轉機會"),
        "rsi_bear_diverge":    ("🔴", "RSI空頭背離", "頂部反轉風險"),
    }

    # Sparklines
    closes = [b["close"] for b in hist]
    vols   = [b["volume"] for b in hist]
    chars  = "▁▂▃▄▅▆▇█"
    if closes:
        mn, mx = min(closes), max(closes)
        spark_p = "".join(chars[int((c - mn) / (mx - mn + 0.01) * 7)] for c in closes)
    else:
        spark_p = "─"
    if vols:
        avg_v  = sum(vols) / len(vols)
        spark_v = "".join(chars[int(min(v / avg_v, 2) / 2 * 7)] for v in vols)
    else:
        spark_v = "─"

    lines = [
        f"🔍 量價背離偵測  [{code}] {name}",
        "─" * 32, "",
        f"現價：{price:,.1f} 元",
        "",
        f"  價格：{spark_p}",
        f"  成交：{spark_v}",
        "",
        "📊 訊號偵測",
    ]
    any_signal = False
    for key, (icon, label, desc) in SIGNAL_LABELS.items():
        if signals.get(key):
            lines.append(f"  {icon} {label}：{desc}")
            any_signal = True
    if not any_signal:
        lines.append("  ✅ 無明顯背離，量價同步健康")

    lines += [
        "",
        f"強度評估：【{strength}】",
        "",
        "─" * 28,
        "🤖 AI 研判",
        verdict,
        "",
        f"更新：{ts}",
        "⚠️ 背離訊號需結合大盤與基本面綜合判斷",
    ]
    return "\n".join(lines)
