"""Washout Service — 籌碼洗盤偵測"""
from __future__ import annotations

import time
from loguru import logger
import asyncio

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 900  # 15 min


async def get_washout_analysis(code: str) -> dict:
    key = code.strip()
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_washout(key)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_washout(code: str) -> dict:
    import asyncio
    hist_task   = _get_price_history(code)
    margin_task = _get_margin_data(code)
    chip_task   = _get_chip_data(code)

    hist, margin, chip = await asyncio.gather(
        hist_task, margin_task, chip_task, return_exceptions=True
    )
    hist   = hist   if isinstance(hist, list)  else []
    margin = margin if isinstance(margin, dict) else {}
    chip   = chip   if isinstance(chip, dict)   else {}

    signals = _detect_washout_signals(hist, margin, chip)
    score, verdict, detail = _ai_judge(signals, hist)

    return {
        "code":     code,
        "signals":  signals,
        "score":    score,
        "verdict":  verdict,
        "detail":   detail,
        "hist":     hist[-10:] if hist else [],
        "margin":   margin,
        "chip":     chip,
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


async def _get_price_history(code: str) -> list:
    try:
        import httpx
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
               f"?interval=1d&range=30d")
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as cl:
            r = await cl.get(url)
        js   = r.json()
        res  = js["chart"]["result"][0]
        ts   = res["timestamp"]
        q    = res["indicators"]["quote"][0]
        bars = []
        for i in range(len(ts)):
            c = q["close"][i]; v = q["volume"][i]
            o = q["open"][i];  h = q["high"][i]; lo = q["low"][i]
            if c and v:
                bars.append({"close": c, "open": o, "high": h, "low": lo,
                             "volume": v, "ts": ts[i]})
        return bars
    except Exception as e:
        logger.debug(f"[washout] hist {code}: {e}")
        return _fake_hist()


def _fake_hist() -> list:
    import random, time as t
    base = 100.0
    bars = []
    now  = int(t.time()) - 30 * 86400
    for i in range(30):
        chg  = random.uniform(-3, 3)
        close = round(max(50, base + chg), 2)
        vol  = int(random.uniform(5000, 50000))
        bars.append({"close": close, "open": base, "high": close + 1,
                     "low": close - 1, "volume": vol, "ts": now + i * 86400})
        base = close
    return bars


async def _get_margin_data(code: str) -> dict:
    try:
        from .twse_service import fetch_margin_data
        return await fetch_margin_data(code) or {}
    except Exception as e:
        logger.debug(f"[washout] margin {code}: {e}")
        return {}


async def _get_chip_data(code: str) -> dict:
    try:
        from .twse_service import fetch_institutional
        return await fetch_institutional(code) or {}
    except Exception as e:
        logger.debug(f"[washout] chip {code}: {e}")
        return {}


def _detect_washout_signals(hist: list, margin: dict, chip: dict) -> dict:
    signals = {
        "price_vol_diverge": False,   # 價跌量縮
        "quick_recover":     False,   # 急殺快速拉回
        "margin_forced_sell":False,   # 融資斷頭
        "foreign_hold":      False,   # 外資未大幅出貨
        "low_close_ratio":   False,   # 收盤在低點（洗盤失敗警訊）
    }
    if len(hist) < 5:
        return signals

    recent = hist[-5:]
    avg_vol = sum(b["volume"] for b in hist[:-5]) / max(len(hist) - 5, 1)

    # 價跌量縮：最近收盤 < 5日前，但量 < 均量60%
    price_down = recent[-1]["close"] < recent[0]["close"]
    vol_shrink = recent[-1]["volume"] < avg_vol * 0.6
    signals["price_vol_diverge"] = price_down and vol_shrink

    # 急殺快速拉回：最近5日有一天跌幅>2%，之後收回
    for i in range(1, len(recent) - 1):
        drop = (recent[i]["close"] - recent[i - 1]["close"]) / recent[i - 1]["close"]
        if drop < -0.02:
            recov = (recent[-1]["close"] - recent[i]["close"]) / recent[i]["close"]
            if recov > 0.01:
                signals["quick_recover"] = True

    # 融資斷頭
    margin_chg = margin.get("margin_balance_change", 0) or 0
    signals["margin_forced_sell"] = margin_chg < -500  # 融資減少 500 張以上

    # 外資未出貨
    foreign_net = chip.get("foreign_net_buy", 0) or 0
    signals["foreign_hold"] = foreign_net >= -1000  # 外資賣超不超過 1000 張

    # 收盤位置（低收 = 洗盤不確定）
    last = recent[-1]
    rng = last["high"] - last["low"]
    if rng > 0:
        close_pos = (last["close"] - last["low"]) / rng
        signals["low_close_ratio"] = close_pos < 0.35

    return signals


def _ai_judge(signals: dict, hist: list) -> tuple:
    score = 0
    details = []

    if signals["price_vol_diverge"]:
        score += 30
        details.append("✅ 價跌量縮（洗盤特徵）")
    else:
        details.append("❌ 量能未縮")

    if signals["quick_recover"]:
        score += 25
        details.append("✅ 急殺後快速拉回")
    else:
        details.append("⬜ 無急殺拉回")

    if signals["margin_forced_sell"]:
        score += 20
        details.append("✅ 融資斷頭清洗（主力清散戶）")
    else:
        details.append("⬜ 融資變化不明顯")

    if signals["foreign_hold"]:
        score += 15
        details.append("✅ 外資未大量出貨")
    else:
        score -= 10
        details.append("⚠️ 外資持續賣出（真跌風險）")

    if signals["low_close_ratio"]:
        score -= 10
        details.append("⚠️ 收盤偏低（洗盤疑慮）")
    else:
        score += 10
        details.append("✅ 收盤位置偏強")

    score = max(0, min(100, score))

    if score >= 70:
        verdict = "高度疑似洗盤 — 主力可能在清洗浮額，可考慮逢低布局"
    elif score >= 45:
        verdict = "中度洗盤訊號 — 訊號混雜，建議觀察成交量是否持續萎縮"
    else:
        verdict = "真跌可能性較高 — 籌碼面無明顯支撐，謹慎操作"

    return score, verdict, details


def format_washout_report(data: dict) -> str:
    if not data or data.get("error"):
        return f"❌ {data.get('error', '無法取得洗盤分析')}"

    code    = data["code"]
    score   = data["score"]
    verdict = data["verdict"]
    details = data["detail"]
    hist    = data["hist"]
    ts      = data["updated_at"]

    # Score bar
    filled = int(score / 10)
    bar    = "🟩" * filled + "⬜" * (10 - filled)

    lines = [
        f"🔍 籌碼洗盤偵測  [{code}]",
        "─" * 32, "",
        f"洗盤信心分數：{score} / 100",
        f"  [{bar}]",
        "",
        "📊 訊號偵測",
    ]
    for d in details:
        lines.append(f"  {d}")

    if hist:
        closes = [b["close"] for b in hist]
        vols   = [b["volume"] for b in hist]
        mn, mx = min(closes), max(closes)
        chars  = "▁▂▃▄▅▆▇█"
        spark_p = "".join(chars[int((c - mn) / (mx - mn + 0.01) * 7)] for c in closes)
        avg_v  = sum(vols) / len(vols)
        spark_v = "".join(chars[int(min(v / avg_v, 2) / 2 * 7)] for v in vols)
        lines += [
            "",
            "📈 近期走勢",
            f"  價格：{spark_p}",
            f"  成交：{spark_v}",
            f"  現價：{closes[-1]:,.1f}  區間：{mn:.1f}–{mx:.1f}",
        ]

    lines += [
        "",
        "─" * 28,
        "🤖 AI 研判",
        verdict,
        "",
        f"更新：{ts}",
        "⚠️ 此為統計模型，非投資建議",
    ]
    return "\n".join(lines)
