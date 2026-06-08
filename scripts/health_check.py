#!/usr/bin/env python3
"""
Comprehensive health check for tw-bloomberg backend.

Checks:
  1. /health — basic liveness
  2. /api/system/health — module status, DB, scheduler
  3. /api/quote/2330 — TWSE data pipeline
  4. /api/advice/daily — AI recommendation engine
  5. Scheduler jobs count in source
  6. LINE Bot webhook reachable (optional)

Exit codes:
  0 — all critical checks passed
  1 — one or more critical checks failed
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

BACKEND_URL = os.getenv("RAILWAY_BACKEND_URL", "").rstrip("/")

CRITICAL_ENDPOINTS = [
    ("基本健康",   "/health",            "GET"),
    ("系統儀表板", "/api/system/health", "GET"),
    ("台積電報價", "/api/quote/2330",    "GET"),
]

OPTIONAL_ENDPOINTS = [
    ("每日建議",   "/api/advice/daily",   "GET"),
    ("早報生成",   "/api/report/morning", "POST"),
]


def _probe(label: str, path: str, method: str) -> dict:
    try:
        import httpx
        url = BACKEND_URL + path
        if method == "POST":
            r = httpx.post(url, json={}, timeout=20)
        else:
            r = httpx.get(url, timeout=20)
        ok = r.status_code < 400
        body = {}
        if ok and "application/json" in r.headers.get("content-type", ""):
            try:
                body = r.json()
            except Exception as e:
                pass
        return {"label": label, "status": r.status_code, "ok": ok, "body": body,
                "detail": "" if ok else r.text[:200]}
    except Exception as exc:
        return {"label": label, "status": 0, "ok": False, "body": {},
                "detail": str(exc)[:150]}


def check_endpoints() -> tuple[list[dict], list[dict]]:
    critical, optional = [], []
    for label, path, method in CRITICAL_ENDPOINTS:
        critical.append(_probe(label, path, method))
    for label, path, method in OPTIONAL_ENDPOINTS:
        optional.append(_probe(label, path, method))
    return critical, optional


def check_system_health(body: dict) -> list[str]:
    issues = []
    modules = body.get("modules", [])
    red = [m.get("name", "?") for m in modules if m.get("status") == "red"]
    if red:
        issues.append(f"紅燈模組：{', '.join(red)}")
    api_rate = body.get("api_success_rate")
    if api_rate is not None:
        pct = api_rate * 100 if api_rate <= 1 else api_rate
        if pct < 70:
            issues.append(f"API 成功率過低：{pct:.0f}%")
    if body.get("kill_switch_active"):
        issues.append("風控熔斷開關已啟動！")
    return issues


def check_scheduler_source() -> tuple[int, bool]:
    sched = Path("backend/utils/scheduler.py")
    if not sched.exists():
        return 0, False
    text = sched.read_text(encoding="utf-8", errors="ignore")
    jobs = re.findall(r"scheduler\.add_job\(", text)
    tz_ok = "Asia/Taipei" in text
    return len(jobs), tz_ok


def check_quote_data(body: dict) -> list[str]:
    issues = []
    if not body:
        return ["報價回應為空"]
    price = body.get("price") or body.get("close") or body.get("current_price")
    if price is None:
        issues.append("報價資料缺少 price 欄位")
    return issues


def send_line_alert(text: str) -> None:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    uid   = os.getenv("ADMIN_LINE_UID", "")
    if not token or not uid:
        return
    try:
        import httpx
        httpx.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"to": uid, "messages": [{"type": "text", "text": text[:4900]}]},
            timeout=10,
        )
    except Exception as exc:
        print(f"  LINE alert error: {exc}", file=sys.stderr)


def main() -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[health_check] {now}\n")

    all_issues: list[str] = []
    critical_fail = False

    # ── Endpoint checks ───────────────────────────────────────────────
    if not BACKEND_URL:
        print("⚠️  RAILWAY_BACKEND_URL 未設定，略過端點測試")
    else:
        print("[1/3] 端點健康檢查...")
        critical, optional = check_endpoints()

        for r in critical:
            icon = "✅" if r["ok"] else "❌"
            print(f"  {icon} [{r['status']}] {r['label']}")
            if not r["ok"]:
                print(f"       {r['detail']}")
                all_issues.append(f"{r['label']} 失敗（HTTP {r['status']}）")
                critical_fail = True

            # Deep check for system health
            if r["ok"] and "系統" in r["label"] and r["body"]:
                sys_issues = check_system_health(r["body"])
                for issue in sys_issues:
                    print(f"  ⚠️  {issue}")
                    all_issues.append(issue)

            # Deep check for quote data
            if r["ok"] and "報價" in r["label"] and r["body"]:
                q_issues = check_quote_data(r["body"])
                for issue in q_issues:
                    print(f"  ⚠️  {issue}")
                    all_issues.append(issue)

        for r in optional:
            icon = "✅" if r["ok"] else "⚠️"
            print(f"  {icon} [{r['status']}] {r['label']}（選用）")

    # ── Scheduler source check ─────────────────────────────────────────
    print("\n[2/3] 排程器原始碼檢查...")
    job_count, tz_ok = check_scheduler_source()
    print(f"  排程 jobs：{job_count} 個")
    print(f"  時區設定：{'✅ Asia/Taipei' if tz_ok else '❌ 未找到 Asia/Taipei'}")
    if job_count < 5:
        all_issues.append(f"排程 jobs 數量過少（{job_count}，預期 ≥5）")
    if not tz_ok:
        all_issues.append("scheduler.py 缺少 Asia/Taipei 時區設定")

    # ── Summary ───────────────────────────────────────────────────────
    print("\n[3/3] 健康摘要")
    if all_issues:
        print(f"  發現 {len(all_issues)} 個問題：")
        for issue in all_issues:
            print(f"    • {issue}")
    else:
        print("  ✅ 全部檢查通過")

    # Save report
    report = {
        "timestamp": now,
        "critical_fail": critical_fail,
        "issues": all_issues,
        "job_count": job_count,
    }
    Path("data").mkdir(exist_ok=True)
    Path("data/health_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Alert on critical failure
    if critical_fail:
        msg = f"🚨 tw-bloomberg 健康檢查失敗\n{now}\n\n" + "\n".join(f"• {i}" for i in all_issues)
        send_line_alert(msg)

    return 1 if critical_fail else 0


if __name__ == "__main__":
    sys.exit(main())
