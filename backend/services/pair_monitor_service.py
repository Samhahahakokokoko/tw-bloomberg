"""Pair Monitor Service — 配對交易持續監控 / 價差偏離推播"""
from __future__ import annotations

import time
import math
from loguru import logger

# 持久化監控列表（按 uid 存儲）
_monitors: dict[str, list[dict]] = {}  # uid -> [{code1, code2, z_threshold, added_at}]
_cache: dict = {}       # "code1:code2" -> analysis result
_cache_ts: dict = {}
_TTL = 1800  # 30 分鐘


def add_pair_monitor(uid: str, code1: str, code2: str, z_threshold: float = 2.0):
    """新增配對監控"""
    key = f"{code1}:{code2}"
    if uid not in _monitors:
        _monitors[uid] = []
    existing = [m for m in _monitors[uid] if f"{m['code1']}:{m['code2']}" == key]
    if existing:
        return False  # 已存在
    _monitors[uid].append({
        "code1": code1, "code2": code2,
        "z_threshold": z_threshold,
        "added_at": time.strftime("%Y-%m-%d %H:%M"),
    })
    return True


def remove_pair_monitor(uid: str, code1: str, code2: str):
    if uid not in _monitors:
        return False
    before = len(_monitors[uid])
    _monitors[uid] = [m for m in _monitors[uid]
                      if not (m["code1"] == code1 and m["code2"] == code2)]
    return len(_monitors[uid]) < before


def list_pair_monitors(uid: str) -> list:
    return _monitors.get(uid, [])


async def get_pair_monitor(code1: str, code2: str) -> dict:
    key = f"{code1}:{code2}"
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_pair_analysis(code1, code2)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_pair_analysis(code1: str, code2: str) -> dict:
    import httpx, asyncio

    async def fetch_prices(code: str):
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(url, params={"interval": "1d", "range": "6mo"},
                                headers={"User-Agent": "Mozilla/5.0"})
            q = r.json()["chart"]["result"][0]
            closes = q["indicators"]["quote"][0].get("close", [])
            closes = [x for x in closes if x is not None]
            timestamps = q.get("timestamp", [])
            return closes, timestamps
        except Exception as e:
            logger.debug(f"[pair_monitor] fetch {code}: {e}")
            return [], []

    (closes1, ts1), (closes2, ts2) = await asyncio.gather(
        fetch_prices(code1), fetch_prices(code2)
    )

    if not closes1 or not closes2:
        return {"error": f"無法取得 {code1} 或 {code2} 的歷史資料"}

    # 對齊長度
    min_len = min(len(closes1), len(closes2))
    if min_len < 20:
        return {"error": f"{code1}/{code2} 共同交易日不足（{min_len}天）"}

    c1 = closes1[-min_len:]
    c2 = closes2[-min_len:]

    # 計算對數價差序列
    spread = [math.log(c1[i]) - math.log(c2[i]) for i in range(min_len)]

    # 過去60天的均值和標準差（用於Z值）
    window = min(60, min_len)
    spread_w = spread[-window:]
    mu  = sum(spread_w) / len(spread_w)
    std = math.sqrt(sum((x - mu) ** 2 for x in spread_w) / len(spread_w))

    current_spread = spread[-1]
    z_score = (current_spread - mu) / std if std > 0 else 0.0

    # 相關係數（過去60天）
    ret1 = [(c1[i] - c1[i-1]) / c1[i-1] for i in range(max(1, min_len-window), min_len)]
    ret2 = [(c2[i] - c2[i-1]) / c2[i-1] for i in range(max(1, min_len-window), min_len)]
    n_r  = min(len(ret1), len(ret2))
    if n_r >= 5:
        m1 = sum(ret1[:n_r]) / n_r
        m2 = sum(ret2[:n_r]) / n_r
        cov = sum((ret1[i] - m1) * (ret2[i] - m2) for i in range(n_r)) / n_r
        s1  = math.sqrt(sum((x - m1) ** 2 for x in ret1[:n_r]) / n_r)
        s2  = math.sqrt(sum((x - m2) ** 2 for x in ret2[:n_r]) / n_r)
        corr = cov / (s1 * s2) if s1 * s2 > 0 else 0
    else:
        corr = 0

    # 過去5天Z值走勢（看偏離是在擴大還是收斂）
    z_recent = [(spread[-5 + i] - mu) / std for i in range(min(5, len(spread)))] if std > 0 else [0]*5
    z_trend  = "擴大" if len(z_recent) >= 2 and abs(z_recent[-1]) > abs(z_recent[-2]) else "收斂"

    # 交易信號
    if z_score > 2.5:
        signal     = "🔴 強烈賣出配對（做空差距）"
        suggestion = f"價差偏高（Z={z_score:.2f}），賣 {code1} 買 {code2}，等待回歸均值"
        action     = "sell_code1_buy_code2"
    elif z_score > 1.5:
        signal     = "🟡 輕度賣出配對"
        suggestion = f"價差偏大（Z={z_score:.2f}），可小量布局做空差距，設停損 Z>3"
        action     = "mild_sell"
    elif z_score < -2.5:
        signal     = "🟢 強烈買入配對（做多差距）"
        suggestion = f"價差偏低（Z={z_score:.2f}），買 {code1} 賣 {code2}，等待回歸均值"
        action     = "buy_code1_sell_code2"
    elif z_score < -1.5:
        signal     = "🟢 輕度買入配對"
        suggestion = f"價差偏小（Z={z_score:.2f}），可小量布局做多差距"
        action     = "mild_buy"
    else:
        signal     = "⬜ 中性（持有觀察）"
        suggestion = f"價差在正常範圍（Z={z_score:.2f}），無明顯套利機會"
        action     = "neutral"

    # 歷史績效估算（過去60天，以Z>2觸發、Z=0平倉為準）
    trades = []
    i = 0
    while i < len(spread_w) - 1:
        z = (spread_w[i] - mu) / std if std > 0 else 0
        if z > 2.0:
            entry_z = z
            for j in range(i+1, min(i+20, len(spread_w))):
                if spread_w[j] < mu:
                    pnl = entry_z - (spread_w[j] - mu) / std if std > 0 else 0
                    trades.append(round(pnl, 3))
                    i = j
                    break
            else:
                i += 1
        elif z < -2.0:
            entry_z = z
            for j in range(i+1, min(i+20, len(spread_w))):
                if spread_w[j] > mu:
                    pnl = abs(entry_z) - abs((spread_w[j] - mu) / std) if std > 0 else 0
                    trades.append(round(pnl, 3))
                    i = j
                    break
            else:
                i += 1
        else:
            i += 1

    win_rate = sum(1 for t in trades if t > 0) / len(trades) * 100 if trades else 0
    avg_pnl  = sum(trades) / len(trades) if trades else 0

    return {
        "code1":        code1,
        "code2":        code2,
        "price1":       round(c1[-1], 1),
        "price2":       round(c2[-1], 1),
        "z_score":      round(z_score, 2),
        "z_trend":      z_trend,
        "corr":         round(corr, 3),
        "spread_mu":    round(mu, 4),
        "spread_std":   round(std, 4),
        "signal":       signal,
        "suggestion":   suggestion,
        "action":       action,
        "hist_trades":  len(trades),
        "win_rate":     round(win_rate, 1),
        "avg_pnl_z":    round(avg_pnl, 3),
        "updated_at":   time.strftime("%Y-%m-%d %H:%M"),
    }


def format_pair_monitor_report(data: dict, code1: str, code2: str) -> str:
    if data.get("error"):
        return f"❌ {data['error']}"

    z       = data.get("z_score", 0)
    z_trend = data.get("z_trend", "")
    corr    = data.get("corr", 0)
    p1      = data.get("price1", 0)
    p2      = data.get("price2", 0)
    sig     = data.get("signal", "")
    sugg    = data.get("suggestion", "")
    trades  = data.get("hist_trades", 0)
    wr      = data.get("win_rate", 0)
    updated = data.get("updated_at", "")

    abs_z = abs(z)
    z_bar = "█" * min(int(abs_z * 2), 10) + "░" * max(0, 10 - int(abs_z * 2))

    lines = [
        f"🔗 配對交易監控  {code1} vs {code2}",
        "─" * 32, "",
        f"現價：{code1}={p1:.1f}  {code2}={p2:.1f}",
        f"相關係數：{corr:.3f}  更新：{updated}",
        "",
        f"── Z 值偏離 ──",
        f"  Z = {z:+.2f}  [{z_bar}]  趨勢：{z_trend}",
        f"  （正值=配對1相對貴；負值=配對1相對便宜）",
        "",
        f"交易信號：{sig}",
        f"操作建議：{sugg}",
        "",
    ]

    if trades >= 3:
        lines += [
            "── 歷史回測（過去60天）──",
            f"  觸發次數：{trades}  勝率：{wr:.0f}%",
            f"  相關係數：{corr:.3f}（≥0.7 表示適合配對）",
            "",
        ]

    if corr < 0.5:
        lines.append("⚠️ 相關係數較低，配對交易風險較高，謹慎使用")
    elif corr >= 0.8:
        lines.append("✅ 相關係數高，配對關係穩固，信號可信度較高")

    lines += [
        "",
        "─" * 28,
        "⚠️ 配對交易說明：",
        "  Z > +2：價差偏大，賣高買低，等待均值回歸",
        "  Z < -2：價差偏小，買低賣高，等待均值回歸",
        "  停損建議：Z > 3 或持有超過 20 個交易日",
        "",
        f"輸入 /pairmonitor {code1} {code2} 手動更新 | /pair {code1} {code2} 深度配對分析",
    ]
    return "\n".join(lines)


async def check_pair_alerts(uid: str) -> list[dict]:
    """檢查該用戶所有配對，偏離超過閾值則回傳警報"""
    alerts = []
    monitors = _monitors.get(uid, [])
    for m in monitors:
        try:
            data = await get_pair_monitor(m["code1"], m["code2"])
            z    = data.get("z_score", 0)
            thr  = m.get("z_threshold", 2.0)
            if abs(z) >= thr:
                alerts.append({
                    "code1": m["code1"], "code2": m["code2"],
                    "z": z, "signal": data.get("signal", ""),
                    "suggestion": data.get("suggestion", ""),
                })
        except Exception as e:
            logger.debug(f"[pair_monitor] alert check {m}: {e}")
    return alerts
