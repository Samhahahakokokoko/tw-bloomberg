"""Journal Service — 投資日記 (SQLite/PostgreSQL backed)"""
from __future__ import annotations

import time
import json
import datetime
from loguru import logger

# In-memory cache per uid for quick reads
_cache: dict = {}


async def add_journal_entry(uid: str, raw_text: str) -> dict:
    """Parse and save a journal entry from free-form text."""
    entry = _parse_entry(raw_text)
    if not entry:
        return {"ok": False, "error": "無法解析日記格式，請使用：買入/賣出 股票代號 張數 價格 原因"}
    entry["uid"]       = uid
    entry["id"]        = int(time.time() * 1000)
    entry["created_at"]= datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    await _save_entry(uid, entry)
    _cache.pop(uid, None)  # invalidate cache
    return {"ok": True, "entry": entry}


async def get_journal(uid: str, limit: int = 20) -> list:
    if uid in _cache:
        return _cache[uid][:limit]
    entries = await _load_entries(uid)
    _cache[uid] = entries
    return entries[:limit]


async def delete_journal_entry(uid: str, entry_id: int) -> bool:
    entries = await _load_entries(uid)
    before  = len(entries)
    entries = [e for e in entries if e.get("id") != entry_id]
    if len(entries) == before:
        return False
    await _save_all(uid, entries)
    _cache.pop(uid, None)
    return True


async def analyze_journal(uid: str) -> dict:
    entries = await get_journal(uid, limit=100)
    if not entries:
        return {"verdict": "尚無投資日記記錄，請先新增交易記錄。", "stats": {}}
    return _analyze_entries(entries)


# ── Parsing ───────────────────────────────────────────────────────────────────

def _parse_entry(text: str) -> dict | None:
    import re
    text = text.strip()

    # Pattern: 買入/賣出 CODE SHARES張/股 PRICE元 REASON
    pattern = r"(買入|賣出|買進|賣出|停利|停損|買|賣)\s+([A-Za-z0-9]{4,6})\s+(\d+(?:\.\d+)?)[張股]?\s+(\d+(?:\.\d+)?)元?\s*(.*)"
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        # Try looser pattern: CODE SHARES PRICE REASON
        pattern2 = r"([買賣買進停利停損])\s*(\d{4,6})\s+(\d+)\s+(\d+(?:\.\d+)?)\s*(.*)"
        m = re.search(pattern2, text)
        if not m:
            return None

    action = m.group(1)
    code   = m.group(2).upper()
    shares = float(m.group(3))
    price  = float(m.group(4))
    reason = m.group(5).strip() if m.group(5) else ""

    action_norm = "買入" if any(x in action for x in ["買", "進", "多"]) else "賣出"

    return {
        "action": action_norm,
        "code":   code,
        "shares": shares,
        "price":  price,
        "amount": round(shares * price * 1000, 0),  # 1張=1000股
        "reason": reason,
        "date":   datetime.date.today().isoformat(),
    }


# ── Storage (DB → fallback JSON file) ────────────────────────────────────────

async def _save_entry(uid: str, entry: dict) -> None:
    await _file_append(uid, entry)


async def _load_entries(uid: str) -> list:
    return await _file_load(uid)


async def _save_all(uid: str, entries: list) -> None:
    await _file_save_all(uid, entries)


# ── JSON file fallback ────────────────────────────────────────────────────────

import os
import re

_JOURNAL_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "journals")


def _journal_path(uid: str) -> str:
    safe_uid = uid.replace("/", "_").replace("\\", "_")
    os.makedirs(_JOURNAL_DIR, exist_ok=True)
    return os.path.join(_JOURNAL_DIR, f"{safe_uid}.json")


async def _file_append(uid: str, entry: dict) -> None:
    entries = await _file_load(uid)
    entries.insert(0, entry)
    await _file_save_all(uid, entries[:200])


async def _file_load(uid: str) -> list:
    try:
        with open(_journal_path(uid), encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return []


async def _file_save_all(uid: str, entries: list) -> None:
    with open(_journal_path(uid), "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


# ── Analysis ──────────────────────────────────────────────────────────────────

def get_monthly_stats(entries: list) -> dict:
    """計算本月交易次數統計"""
    import datetime as _dt
    today      = _dt.date.today()
    this_month = f"{today.year}-{today.month:02d}"
    monthly    = [e for e in entries if (e.get("date") or "").startswith(this_month)]
    buys       = [e for e in monthly if e.get("action") == "買入"]
    sells      = [e for e in monthly if e.get("action") == "賣出"]
    codes      = list({e["code"] for e in monthly if e.get("code")})
    total_amt  = sum(e.get("amount", 0) or 0 for e in monthly)
    return {
        "month":       this_month,
        "total":       len(monthly),
        "buy_count":   len(buys),
        "sell_count":  len(sells),
        "codes":       codes,
        "total_amount": total_amt,
    }


def _analyze_entries(entries: list) -> dict:
    buys  = [e for e in entries if e.get("action") == "買入"]
    sells = [e for e in entries if e.get("action") == "賣出"]

    total   = len(entries)
    buy_cnt = len(buys)
    sel_cnt = len(sells)

    # Reason analysis
    reasons = [e.get("reason", "") for e in entries if e.get("reason")]
    reason_kw_good = ["RSI", "超賣", "支撐", "突破", "均線", "法人", "低接"]
    reason_kw_bad  = ["感覺", "聽說", "朋友", "消息", "直覺", "Line群"]
    good_reason = sum(1 for r in reasons if any(k in r for k in reason_kw_good))
    bad_reason  = sum(1 for r in reasons if any(k in r for k in reason_kw_bad))

    # Most traded
    from collections import Counter
    codes    = [e.get("code", "") for e in entries]
    top_code = Counter(codes).most_common(1)[0] if codes else ("─", 0)

    # Common mistakes
    mistakes = []
    if bad_reason > 0:
        mistakes.append(f"根據{bad_reason}次非理性因素（如感覺/聽說）交易，建議改善決策依據")
    if buy_cnt > sel_cnt * 2:
        mistakes.append("買多賣少，可能存在持倉過重的問題")

    verdict = (f"分析 {total} 筆交易記錄：買入 {buy_cnt} 次，賣出 {sel_cnt} 次。"
               f"最常交易標的：{top_code[0]}（{top_code[1]}次）。")
    if good_reason > 0:
        verdict += f" {good_reason}次使用技術/籌碼依據，交易品質良好。"
    if mistakes:
        verdict += " 常見問題：" + "；".join(mistakes) + "。"
    else:
        verdict += " 整體交易紀律良好，繼續保持！"

    return {
        "total":       total,
        "buy_count":   buy_cnt,
        "sell_count":  sel_cnt,
        "top_code":    top_code[0],
        "good_reason": good_reason,
        "bad_reason":  bad_reason,
        "mistakes":    mistakes,
        "verdict":     verdict,
    }


# ── Formatting ────────────────────────────────────────────────────────────────

def format_journal_list(entries: list, analysis: dict | None = None) -> str:
    if not entries:
        return ("📔 投資日記\n\n尚無記錄\n"
                "新增方式：/journal add 買入 2330 10張 900元 RSI超賣+支撐確立")

    ACTION_ICON = {"買入": "🟢", "賣出": "🔴"}
    m = get_monthly_stats(entries)
    month_line = ""
    if m["total"] > 0:
        codes_str = " ".join(m["codes"][:4]) + ("…" if len(m["codes"]) > 4 else "")
        month_line = f"本月({m['month']})：{m['total']}筆 買{m['buy_count']}賣{m['sell_count']}  {codes_str}"

    lines = [
        "📔 投資日記",
        "─" * 32, "",
        f"共 {len(entries)} 筆記錄",
    ]
    if month_line:
        lines += [month_line, ""]
    else:
        lines.append("")

    for e in entries[:10]:
        icon   = ACTION_ICON.get(e.get("action", "買入"), "📝")
        shares = e.get("shares", 0)
        price  = e.get("price", 0)
        reason = e.get("reason", "─")[:30]
        lines.append(
            f"  {icon} {e.get('date', '─')}  {e.get('action', '─')}"
            f"  {e.get('code', '─')}  {shares:.0f}張@{price:.0f}  ID:{e.get('id','─')}"
        )
        if reason:
            lines.append(f"     原因：{reason}")

    if analysis:
        lines += [
            "",
            "─" * 28,
            "🤖 AI 交易品質分析",
            analysis.get("verdict", ""),
        ]
        mistakes = analysis.get("mistakes", [])
        if mistakes:
            lines.append("⚠️ 改善建議：")
            for m in mistakes:
                lines.append(f"  • {m}")

    lines += [
        "",
        "指令：/journal add 買入 2330 10張 900元 原因",
        "      /journal analysis — AI月度分析",
    ]
    return "\n".join(lines)


def format_journal_add_confirm(entry: dict) -> str:
    icon = "🟢" if entry.get("action") == "買入" else "🔴"
    return (
        f"📔 投資日記已記錄\n\n"
        f"{icon} {entry.get('action')}  {entry.get('code')}\n"
        f"張數：{entry.get('shares'):.0f} 張\n"
        f"價格：{entry.get('price'):.0f} 元\n"
        f"金額：{entry.get('amount'):,.0f} 元\n"
        f"原因：{entry.get('reason') or '─'}\n"
        f"時間：{entry.get('created_at')}\n"
        f"ID：{entry.get('id')}\n\n"
        f"查看：/journal\n刪除：/journal del {entry.get('id')}"
    )
