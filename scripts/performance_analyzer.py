#!/usr/bin/env python3
"""
Weekly (and daily) performance analyzer for tw-bloomberg.

Daily mode (--daily):
  - Count error patterns in logs
  - Check API response times
  - Brief status push to LINE

Weekly mode (--weekly):
  - Most-failed LINE commands
  - Least-stable API endpoints
  - Most-used features (from logs)
  - Improvement suggestions
  - Full report pushed to LINE

Exit code: always 0 (reporting tool, non-critical)
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

BACKEND_URL = os.getenv("RAILWAY_BACKEND_URL", "").rstrip("/")

# LINE commands recognised in the system
KNOWN_COMMANDS = [
    "報價", "quote", "市場", "market", "portfolio", "buy", "sell",
    "績效", "持倉", "history", "analysis", "ai分析", "pe", "估值",
    "法人", "融資", "早報", "weekly", "report", "morning",
    "自選", "watchlist", "favorites", "screener", "strategy",
    "backtest", "rs", "breadth", "movers", "theme", "analyst",
    "system_health", "agent", "risk_report", "compare", "etf", "dca",
    "dividend", "除權息", "exdiv", "chart", "rebalance", "optimize",
    "news", "sector", "flow", "insider", "earnings",
]

# API paths to spot-check for response time
PROBE_ENDPOINTS = [
    ("/health",               "GET"),
    ("/api/quote/2330",       "GET"),
    ("/api/advice/daily",     "GET"),
    ("/api/system/health",    "GET"),
]


# ── Log helpers ───────────────────────────────────────────────────────────────

async def _fetch_logs(lines: int = 800) -> str:
    try:
        from backend.services.fix_engine import fetch_railway_logs
        return await fetch_railway_logs(lines=lines) or ""
    except Exception as e:
        return ""


def _parse_command_stats(logs: str) -> Counter:
    counts: Counter = Counter()
    for line in logs.splitlines():
        if "cmd=" in line or "command=" in line or "_cmd_" in line:
            for cmd in KNOWN_COMMANDS:
                if cmd in line.lower():
                    counts[cmd] += 1
    return counts


def _parse_error_stats(logs: str) -> Counter:
    counts: Counter = Counter()
    patterns = {
        "LINE_400":      r"status[_\s]?code[=:\s]+400",
        "LINE_timeout":  r"LINE.*timeout|timeout.*LINE",
        "TWSE_timeout":  r"twse.*timeout|timeout.*twse",
        "DB_error":      r"sqlalchemy|asyncpg|psycopg.*[Ee]rror",
        "import_error":  r"ImportError|ModuleNotFoundError",
        "quant_error":   r"quant/.*[Ee]rror|Error.*quant/",
        "schedule_miss": r"job.*missed|missed.*job",
        "API_500":       r"HTTP 500|status[_\s]?code[=:\s]+500",
        "credit_low":    r"credit balance is too low",
    }
    for line in logs.splitlines():
        for name, pat in patterns.items():
            if re.search(pat, line, re.IGNORECASE):
                counts[name] += 1
    return counts


def _parse_api_usage(logs: str) -> Counter:
    counts: Counter = Counter()
    for line in logs.splitlines():
        m = re.search(r'"(?:GET|POST|PUT|DELETE) (/api/[^\s"?]+)', line)
        if m:
            counts[m.group(1)] += 1
    return counts


# ── API response time probe ───────────────────────────────────────────────────

def _probe_latencies() -> list[dict]:
    results = []
    if not BACKEND_URL:
        return results
    try:
        import httpx
        import time
        for path, method in PROBE_ENDPOINTS:
            url = BACKEND_URL + path
            t0 = time.time()
            try:
                if method == "POST":
                    r = httpx.post(url, json={}, timeout=15)
                else:
                    r = httpx.get(url, timeout=15)
                ms = int((time.time() - t0) * 1000)
                results.append({"path": path, "status": r.status_code,
                                 "ms": ms, "ok": r.status_code < 400})
            except Exception as exc:
                ms = int((time.time() - t0) * 1000)
                results.append({"path": path, "status": 0,
                                 "ms": ms, "ok": False, "error": str(exc)[:80]})
    except ImportError:
        pass
    return results


# ── Improvement suggestions ───────────────────────────────────────────────────

def _generate_suggestions(errors: Counter, cmd_stats: Counter,
                           latencies: list[dict]) -> list[str]:
    suggestions = []

    if errors.get("LINE_400", 0) > 2:
        suggestions.append("LINE 400 錯誤頻繁 → 建議加強 Flex Message 格式驗證或改用純文字")

    if errors.get("TWSE_timeout", 0) > 3:
        suggestions.append("TWSE timeout 頻繁 → 考慮縮短 timeout 設定，更積極切換到 TPEX/cache")

    if errors.get("DB_error", 0) > 2:
        suggestions.append("資料庫錯誤頻繁 → 檢查連線池設定，考慮加入 retry decorator")

    if errors.get("credit_low", 0) > 0:
        suggestions.append("Claude API 餘額不足 → 請補充 Anthropic API 信用")

    slow = [r for r in latencies if r["ok"] and r.get("ms", 0) > 3000]
    for r in slow:
        suggestions.append(f"{r['path']} 回應慢（{r['ms']}ms）→ 考慮加入快取")

    failing = [r for r in latencies if not r["ok"]]
    for r in failing:
        suggestions.append(f"{r['path']} 無法連線 → 確認 Railway 部署狀態")

    if not suggestions:
        suggestions.append("✅ 本週無明顯問題，系統運作正常")

    return suggestions


# ── LINE report ───────────────────────────────────────────────────────────────

def _send_line(text: str) -> bool:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    uid   = os.getenv("ADMIN_LINE_UID", "")
    if not token or not uid:
        print("  LINE 推播略過（未設定 token/uid）")
        return False
    try:
        import httpx
        r = httpx.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"to": uid, "messages": [{"type": "text", "text": text[:4900]}]},
            timeout=10,
        )
        ok = r.status_code == 200
        print(f"  LINE 推播：{'✅' if ok else f'❌ {r.status_code}'}")
        return ok
    except Exception as exc:
        print(f"  LINE 推播失敗：{exc}", file=sys.stderr)
        return False


# ── Modes ─────────────────────────────────────────────────────────────────────

async def run_daily() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[performance_analyzer] daily — {now}\n")

    print("[1/2] 抓取 Railway 日誌...")
    logs = await _fetch_logs(lines=400)
    errors = _parse_error_stats(logs) if logs else Counter()

    print("[2/2] API 延遲測試...")
    latencies = _probe_latencies()

    # Build short daily report
    error_total = sum(errors.values())
    lines_report = [f"📊 每日健康 {now}"]

    if latencies:
        for r in latencies:
            icon = "✅" if r["ok"] else "❌"
            ms_str = f" {r['ms']}ms" if r.get("ms") else ""
            lines_report.append(f"{icon} {r['path']}{ms_str}")

    if error_total:
        lines_report.append(f"\n⚠️ 錯誤統計（{error_total} 筆）：")
        for name, cnt in errors.most_common(5):
            lines_report.append(f"  • {name}: {cnt}")
    else:
        lines_report.append("\n✅ 日誌無異常錯誤")

    report = "\n".join(lines_report)
    print(report)
    _send_line(report)


async def run_weekly() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[performance_analyzer] weekly — {now}\n")

    print("[1/4] 抓取 Railway 日誌...")
    logs = await _fetch_logs(lines=1200)

    print("[2/4] 分析使用模式...")
    errors    = _parse_error_stats(logs) if logs else Counter()
    cmd_stats = _parse_command_stats(logs) if logs else Counter()
    api_usage = _parse_api_usage(logs) if logs else Counter()

    print("[3/4] 測試 API 延遲...")
    latencies = _probe_latencies()

    print("[4/4] 生成改善建議...")
    suggestions = _generate_suggestions(errors, cmd_stats, latencies)

    # ── Build weekly report ──────────────────────────────────────────
    lines_report = [
        f"📊 週報 — tw-bloomberg",
        f"📅 {now}",
        "",
    ]

    # API latencies
    if latencies:
        lines_report.append("🌐 API 回應時間：")
        for r in latencies:
            icon = "✅" if r["ok"] else "❌"
            ms_str = f" {r.get('ms',0)}ms" if r.get("ms") else ""
            lines_report.append(f"  {icon} {r['path']}{ms_str}")
        lines_report.append("")

    # Error summary
    if errors:
        lines_report.append(f"⚠️ 錯誤統計（前 5 項）：")
        for name, cnt in errors.most_common(5):
            lines_report.append(f"  • {name}: {cnt} 次")
        lines_report.append("")

    # Most used commands
    if cmd_stats:
        lines_report.append("💬 最常使用指令（前 5）：")
        for cmd, cnt in cmd_stats.most_common(5):
            lines_report.append(f"  • /{cmd}: {cnt} 次")
        lines_report.append("")

    # Most hit APIs
    if api_usage:
        lines_report.append("🔗 最常呼叫 API（前 5）：")
        for path, cnt in api_usage.most_common(5):
            lines_report.append(f"  • {path}: {cnt} 次")
        lines_report.append("")

    # Suggestions
    lines_report.append("💡 改善建議：")
    for s in suggestions:
        lines_report.append(f"  • {s}")

    report = "\n".join(lines_report)
    print("\n" + report)

    # Save report
    Path("data").mkdir(exist_ok=True)
    Path("data/weekly_perf_report.txt").write_text(report, encoding="utf-8")
    print("\n  報告已存至 data/weekly_perf_report.txt")

    _send_line(report)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> int:
    mode = "--daily"
    if len(sys.argv) > 1:
        mode = sys.argv[1]

    if mode == "--weekly":
        await run_weekly()
    else:
        await run_daily()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        print(f"[performance_analyzer] Error: {exc}", file=sys.stderr)
        sys.exit(0)  # non-critical, always exit 0
