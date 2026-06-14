"""Black Swan Service — 黑天鵝風險預警掃描"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict | None = None
_cache_ts: float = 0.0
_TTL = 600  # 10 min

THRESHOLDS = {
    "us_futures_drop":  -2.0,   # 美股期貨跌幅 > 2%
    "usd_surge":         1.0,   # 美元指數漲幅 > 1%
    "gold_surge":        2.0,   # 黃金漲幅 > 2%
    "twd_depreciation": -0.5,   # 台幣貶值 > 0.5%
}

SIGNALS_META = {
    "us_futures": {"name": "美股期貨",  "icon": "📉", "symbol": "ES=F"},
    "usd_index":  {"name": "美元指數",  "icon": "💵", "symbol": "DX-Y.NYB"},
    "gold":       {"name": "黃金現貨",  "icon": "🥇", "symbol": "GC=F"},
    "twd":        {"name": "台幣匯率",  "icon": "💰", "symbol": "TWDUSD=X"},
}


async def get_black_swan_risk() -> dict:
    global _cache, _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < _TTL:
        return _cache
    result = await _scan_signals()
    _cache = result
    _cache_ts = now
    return result


async def _scan_signals() -> dict:
    import asyncio
    tasks = {k: _fetch_change(v["symbol"]) for k, v in SIGNALS_META.items()}
    results = dict(zip(tasks.keys(),
                       await asyncio.gather(*tasks.values(), return_exceptions=True)))

    signals = []
    risk_score = 0

    # 美股期貨
    us_chg = results.get("us_futures", 0)
    if isinstance(us_chg, (int, float)) and us_chg <= THRESHOLDS["us_futures_drop"]:
        signals.append({"key": "us_futures", "name": "美股期貨大跌",
                         "icon": "📉", "value": us_chg, "threshold": THRESHOLDS["us_futures_drop"],
                         "severity": "HIGH"})
        risk_score += 30

    # 美元指數
    usd_chg = results.get("usd_index", 0)
    if isinstance(usd_chg, (int, float)) and usd_chg >= THRESHOLDS["usd_surge"]:
        signals.append({"key": "usd_index", "name": "美元指數急漲",
                         "icon": "💵", "value": usd_chg, "threshold": THRESHOLDS["usd_surge"],
                         "severity": "MEDIUM"})
        risk_score += 20

    # 黃金
    gold_chg = results.get("gold", 0)
    if isinstance(gold_chg, (int, float)) and gold_chg >= THRESHOLDS["gold_surge"]:
        signals.append({"key": "gold", "name": "黃金急漲（避險）",
                         "icon": "🥇", "value": gold_chg, "threshold": THRESHOLDS["gold_surge"],
                         "severity": "MEDIUM"})
        risk_score += 20

    # 台幣
    twd_chg = results.get("twd", 0)
    if isinstance(twd_chg, (int, float)):
        twd_invert = -twd_chg  # TWDUSD 漲 = 台幣升值
        if twd_invert <= THRESHOLDS["twd_depreciation"]:
            signals.append({"key": "twd", "name": "台幣急貶",
                             "icon": "💰", "value": twd_invert,
                             "threshold": THRESHOLDS["twd_depreciation"],
                             "severity": "MEDIUM"})
            risk_score += 15

    all_changes = {
        "us_futures": results.get("us_futures", 0) if isinstance(results.get("us_futures"), (int, float)) else 0,
        "usd_index":  results.get("usd_index", 0)  if isinstance(results.get("usd_index"), (int, float)) else 0,
        "gold":       results.get("gold", 0)        if isinstance(results.get("gold"), (int, float)) else 0,
        "twd":        results.get("twd", 0)         if isinstance(results.get("twd"), (int, float)) else 0,
    }

    risk_level = _risk_level(risk_score)
    outlook    = _ai_outlook(signals, risk_score, all_changes)

    return {
        "signals":    signals,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "all_changes": all_changes,
        "outlook":    outlook,
        "triggered":  len(signals) > 0,
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


async def _fetch_change(symbol: str) -> float:
    """抓取資產單日漲跌幅 (%)"""
    import httpx
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"interval": "1d", "range": "2d"}
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=8, headers=headers) as client:
            r = await client.get(url, params=params)
        data = r.json()
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c is not None]
        if len(closes) < 2:
            return 0.0
        return round((closes[-1] - closes[-2]) / closes[-2] * 100, 3)
    except Exception as e:
        logger.debug(f"[black_swan] {symbol}: {e}")
        return 0.0


def _risk_level(score: int) -> str:
    if score >= 60: return "🚨 極高風險"
    if score >= 40: return "⚠️ 高度警戒"
    if score >= 20: return "🟡 輕度警戒"
    return "🟢 正常"


def _ai_outlook(signals: list, score: int, changes: dict) -> str:
    if not signals:
        return "目前無黑天鵝訊號，市場運作正常。維持正常操作節奏，持續監控中。"

    names = [s["name"] for s in signals]
    trigger_desc = "、".join(names) + " 觸發警戒"

    if score >= 60:
        action = "建議立即降低風險敞口，減少倉位至 30% 以下，等待市場穩定"
    elif score >= 40:
        action = "建議縮減持倉，提高現金比例，設緊縮停損"
    elif score >= 20:
        action = "建議保持警覺，不追高，現有倉位確認停損位"
    else:
        action = "保持觀察，維持正常操作"

    return f"{trigger_desc}。{action}。"


def format_black_swan_report(data: dict) -> str:
    if not data:
        return "❌ 無法取得黑天鵝風險資料"

    signals   = data["signals"]
    score     = data["risk_score"]
    level     = data["risk_level"]
    changes   = data["all_changes"]
    outlook   = data["outlook"]
    ts        = data["updated_at"]

    lines = [
        "🦢 黑天鵝風險預警",
        "─" * 32,
        f"風險指數：{score}/100  {level}",
        "",
        "📡 今日訊號掃描",
        "─" * 28,
    ]

    # 全部訊號顯示
    labels = [
        ("美股期貨", changes.get("us_futures", 0), THRESHOLDS["us_futures_drop"], "📉"),
        ("美元指數", changes.get("usd_index",  0), THRESHOLDS["usd_surge"],       "💵"),
        ("黃金現貨", changes.get("gold",        0), THRESHOLDS["gold_surge"],      "🥇"),
        ("台幣匯率", -changes.get("twd",        0), THRESHOLDS["twd_depreciation"],"💰"),
    ]
    for name, val, thr, icon in labels:
        triggered = ""
        if name == "美股期貨" and val <= thr:
            triggered = " ⚡觸發"
        elif name in ("美元指數", "黃金現貨") and val >= thr:
            triggered = " ⚡觸發"
        elif name == "台幣匯率" and val <= thr:
            triggered = " ⚡觸發"
        lines.append(f"{icon} {name}：{val:>+6.2f}%  (閾值{thr:>+.1f}%){triggered}")

    if signals:
        lines += ["", "─" * 28, "🚨 觸發警報"]
        for s in signals:
            sev = {"HIGH": "高", "MEDIUM": "中", "LOW": "低"}.get(s["severity"], "")
            lines.append(f"{s['icon']} {s['name']}  實際{s['value']:+.2f}%  嚴重度：{sev}")
    else:
        lines += ["", "✅ 無警報觸發，市場正常"]

    lines += [
        "", "─" * 32,
        "🤖 AI 研判", outlook,
        "", f"更新：{ts}",
    ]
    return "\n".join(lines)


async def check_and_push_alert() -> int:
    """掃描黑天鵝並推播，回傳推播數"""
    from .line_push import push_to_admin
    try:
        data = await get_black_swan_risk()
        if not data.get("triggered"):
            return 0
        report = format_black_swan_report(data)
        await push_to_admin(f"🦢 黑天鵝警告\n\n{report[:2000]}")
        return 1
    except Exception as e:
        logger.error(f"[black_swan] push: {e}")
        return 0
