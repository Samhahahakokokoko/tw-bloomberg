"""Behavior Service — AI 個人交易行為分析（從投資日記萃取模式）"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 3600


async def get_behavior(uid: str) -> dict:
    key = uid
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_behavior(uid)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_behavior(uid: str) -> dict:
    try:
        from .journal_service import get_journal
        entries = await get_journal(uid, limit=100)
    except Exception as e:
        logger.warning(f"[behavior] get_journal failed: {e}")
        entries = []

    if not entries:
        return {
            "uid":     uid,
            "total":   0,
            "has_data": False,
            "message": "投資日記尚無記錄。\n使用 /journal add 買入 2330 10張 900元 原因 開始記錄",
        }

    return _analyze_entries(uid, entries)


def _analyze_entries(uid: str, entries: list[dict]) -> dict:
    from collections import Counter

    buys  = [e for e in entries if e.get("action") == "buy"]
    sells = [e for e in entries if e.get("action") == "sell"]
    total = len(entries)

    # 最常交易的股票
    all_codes = [e.get("code", "") for e in entries if e.get("code")]
    top_stocks = Counter(all_codes).most_common(5)

    # 買入原因分析
    buy_reasons  = [e.get("reason", "") for e in buys if e.get("reason")]
    sell_reasons = [e.get("reason", "") for e in sells if e.get("reason")]

    buy_keywords  = _extract_keywords(buy_reasons)
    sell_keywords = _extract_keywords(sell_reasons)

    # 損益分析（如果有記錄的話）
    completed_trades = _match_trades(buys, sells)
    wins  = [t for t in completed_trades if t.get("pnl", 0) > 0]
    losses = [t for t in completed_trades if t.get("pnl", 0) < 0]
    win_rate = len(wins) / len(completed_trades) * 100 if completed_trades else 0.0
    avg_win  = sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0

    # 行為模式識別
    patterns  = _identify_patterns(buys, sells, buy_keywords, sell_keywords, completed_trades)
    strengths = _identify_strengths(patterns, wins, losses, completed_trades)
    weaknesses = _identify_weaknesses(patterns, wins, losses, completed_trades)
    advice    = _gen_advice(weaknesses, patterns)

    return {
        "uid":          uid,
        "has_data":     True,
        "total":        total,
        "buy_count":    len(buys),
        "sell_count":   len(sells),
        "top_stocks":   top_stocks[:3],
        "buy_keywords": buy_keywords[:5],
        "sell_keywords": sell_keywords[:5],
        "win_rate":     round(win_rate, 1),
        "avg_win":      round(avg_win, 1),
        "avg_loss":     round(avg_loss, 1),
        "patterns":     patterns,
        "strengths":    strengths,
        "weaknesses":   weaknesses,
        "advice":       advice,
        "completed":    len(completed_trades),
    }


def _extract_keywords(reasons: list[str]) -> list[str]:
    from collections import Counter
    keywords = [
        "技術面", "均線", "突破", "籌碼", "外資", "投信", "法人",
        "基本面", "獲利", "EPS", "股息", "除息", "題材", "AI",
        "消息面", "新聞", "跌深", "反彈", "停損", "目標價", "超跌",
        "量縮", "量增", "轉強", "動能", "族群", "輪動",
    ]
    counts = Counter()
    for r in reasons:
        for kw in keywords:
            if kw in r:
                counts[kw] += 1
    return [kw for kw, _ in counts.most_common(5)]


def _match_trades(buys: list[dict], sells: list[dict]) -> list[dict]:
    matched = []
    for b in buys:
        code = b.get("code", "")
        matching_sells = [s for s in sells if s.get("code") == code]
        if matching_sells:
            s = matching_sells[0]
            b_price = b.get("price", 0)
            s_price = s.get("price", 0)
            if b_price > 0 and s_price > 0:
                pnl = (s_price - b_price) / b_price * 100
                matched.append({"code": code, "pnl": round(pnl, 2),
                                 "buy_price": b_price, "sell_price": s_price})
    return matched


def _identify_patterns(buys, sells, buy_kw, sell_kw, completed) -> list[str]:
    patterns = []
    if "技術面" in buy_kw or "均線" in buy_kw or "突破" in buy_kw:
        patterns.append("技術派操作者：主要依賴均線和突破訊號買賣")
    if "籌碼" in buy_kw or "外資" in buy_kw or "法人" in buy_kw:
        patterns.append("籌碼流派：重視法人動向和籌碼集中度")
    if "題材" in buy_kw or "AI" in buy_kw or "消息面" in buy_kw:
        patterns.append("題材投資者：偏好追逐熱門題材")
    if "基本面" in buy_kw or "EPS" in buy_kw or "獲利" in buy_kw:
        patterns.append("基本面導向：重視企業獲利和估值")
    if len(buys) > len(sells) * 2:
        patterns.append("買多賣少型：傾向長期持有，少做賣出決策")
    if "停損" in sell_kw:
        patterns.append("有紀律停損習慣：賣出原因包含主動停損")
    if not patterns:
        patterns.append("操作風格尚未明確，需更多交易記錄")
    return patterns


def _identify_strengths(patterns, wins, losses, completed) -> list[str]:
    strengths = []
    if "有紀律停損習慣" in " ".join(patterns):
        strengths.append("具備紀律性停損，避免大幅虧損")
    if wins and losses and len(wins) > len(losses):
        strengths.append(f"交易勝率偏高（{len(wins)}/{len(completed)} = {len(wins)/len(completed)*100:.0f}%）")
    if wins:
        max_win = max(t["pnl"] for t in wins)
        if max_win > 15:
            strengths.append(f"能讓獲利奔跑（最大單筆獲利 +{max_win:.1f}%）")
    if "籌碼流派" in " ".join(patterns) or "技術派" in " ".join(patterns):
        strengths.append("有明確操作系統，非隨機買賣")
    if not strengths:
        strengths.append("記錄交易行為本身即是優點，持續追蹤有助進步")
    return strengths[:3]


def _identify_weaknesses(patterns, wins, losses, completed) -> list[str]:
    weaknesses = []
    if losses and wins:
        avg_w = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_l = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        if abs(avg_l) > avg_w:
            weaknesses.append(f"虧損平均大於獲利（賺小賠大：+{avg_w:.1f}% vs {avg_l:.1f}%）")
    if "題材投資者" in " ".join(patterns):
        weaknesses.append("追題材風險：題材退潮時易套牢，需設定停利點")
    if "買多賣少型" in " ".join(patterns):
        weaknesses.append("缺乏明確賣出紀律：建議設定目標價和停損位再進場")
    if len(completed) < 3 and len(patterns) > 0:
        weaknesses.append("交易記錄數量較少，分析準確度有限")
    if not weaknesses:
        weaknesses.append("目前無明顯系統性弱點，維持現有操作風格")
    return weaknesses[:3]


def _gen_advice(weaknesses: list[str], patterns: list[str]) -> list[str]:
    advice = []
    if "賺小賠大" in " ".join(weaknesses):
        advice.append("建議：每筆交易進場前設定「目標價=停損幅度×2」的最低標準")
    if "缺乏明確賣出紀律" in " ".join(weaknesses):
        advice.append("建議：建立「3 成獲利就先出一半」的紀律，留倉等更高目標")
    if "追題材" in " ".join(weaknesses):
        advice.append("建議：題材股設定嚴格停損（-8%），不因期待而無限等待")
    if "技術派" in " ".join(patterns):
        advice.append("建議：均線策略加入成交量確認，突破無量則不追高")
    if not advice:
        advice.append("持續記錄每筆交易的原因，累積足夠樣本後，系統將提供更精準分析")
    return advice[:3]


def format_behavior_report(data: dict) -> str:
    if not data.get("has_data"):
        return f"📓 個人交易行為分析\n\n{data.get('message', '尚無資料')}"

    total   = data["total"]
    buys    = data["buy_count"]
    sells   = data["sell_count"]
    wr      = data["win_rate"]
    aw      = data["avg_win"]
    al      = data["avg_loss"]
    top_s   = data["top_stocks"]
    patterns = data["patterns"]
    strengths = data["strengths"]
    weaknesses = data["weaknesses"]
    advice  = data["advice"]

    lines = [
        "📓 個人交易行為分析",
        "─" * 32, "",
        f"分析記錄：{total} 筆（買入{buys} / 賣出{sells}）",
        f"完整交易：{data['completed']} 筆  勝率：{wr:.1f}%",
        f"平均獲利：{aw:+.1f}%  平均虧損：{al:+.1f}%",
        "",
    ]

    if top_s:
        stocks_str = "  ".join(f"{c}({n}次)" for c, n in top_s)
        lines.append(f"最常交易：{stocks_str}")
        lines.append("")

    if patterns:
        lines.append("🎯 操作風格識別：")
        for p in patterns:
            lines.append(f"  • {p}")
        lines.append("")

    if strengths:
        lines.append("✅ 你的優點：")
        for s in strengths:
            lines.append(f"  • {s}")
        lines.append("")

    if weaknesses:
        lines.append("⚠️  需要改善：")
        for w in weaknesses:
            lines.append(f"  • {w}")
        lines.append("")

    if advice:
        lines.append("💡 AI 具體建議：")
        for a in advice:
            lines.append(f"  • {a}")

    lines += [
        "",
        "─" * 28,
        "輸入 /journal add 記錄交易 | /journal list 查看日記",
    ]
    return "\n".join(lines)
