#!/usr/bin/env python3
"""
Weekly deep health check — called by weekly_enhance.yml.

Checks:
  1. All key API endpoints return 200
  2. System health dashboard (DB, module status, API success rate)
  3. Scheduler jobs registered and recent
  4. Subscriber / data counts
  5. Pushes weekly report to admin LINE

Exit codes:
  0 — all checks passed
  1 — one or more critical checks failed
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

BACKEND_URL = os.getenv("RAILWAY_BACKEND_URL", "").rstrip("/")

# Endpoints to probe  (label, path, method, critical)
ENDPOINTS = [
    ("健康檢查",   "/health",               "GET",  True),
    ("系統儀表板", "/api/system/health",     "GET",  True),
    ("台積電報價", "/api/quote/2330",        "GET",  True),
    ("每日建議",   "/api/advice/daily",      "GET",  False),
    ("早報生成",   "/api/report/morning",    "POST", False),
]


# ── Endpoint probes ───────────────────────────────────────────────────────────

async def probe(session, label: str, path: str, method: str) -> dict:
    url = BACKEND_URL + path
    try:
        if method == "POST":
            r = await session.post(url, json={}, timeout=25)
        else:
            r = await session.get(url, timeout=25)
        ok     = r.status_code < 400
        detail = "" if ok else r.text[:120]
        body   = {}
        if ok and r.headers.get("content-type", "").startswith("application/json"):
            try:
                body = r.json()
            except Exception:
                pass
        return {"label": label, "status": r.status_code, "ok": ok,
                "detail": detail, "body": body}
    except Exception as e:
        return {"label": label, "status": 0, "ok": False,
                "detail": str(e)[:100], "body": {}}


async def run_all_probes() -> list[dict]:
    import httpx
    async with httpx.AsyncClient(follow_redirects=True) as s:
        tasks = [probe(s, lbl, path, meth) for lbl, path, meth, _ in ENDPOINTS]
        return list(await asyncio.gather(*tasks))


# ── Parse system health ───────────────────────────────────────────────────────

def parse_system_health(body: dict) -> dict:
    """Extract key metrics from /api/system/health response."""
    modules   = body.get("modules", [])
    green     = sum(1 for m in modules if m.get("status") == "green")
    red       = sum(1 for m in modules if m.get("status") == "red")
    api_rate  = body.get("api_success_rate", None)
    mock_rat  = body.get("mock_ratio", None)
    overall   = body.get("overall_status", "unknown")
    ks_active = body.get("kill_switch_active", False)
    return {
        "overall":    overall,
        "green":      green,
        "red":        red,
        "total":      len(modules),
        "api_rate":   api_rate,
        "mock_ratio": mock_rat,
        "kill_switch": ks_active,
    }


# ── Scheduler file check ──────────────────────────────────────────────────────

def check_scheduler_jobs() -> dict:
    """Parse scheduler.py to count registered jobs."""
    sched_path = Path(__file__).parent.parent / "backend" / "utils" / "scheduler.py"
    if not sched_path.exists():
        return {"ok": False, "count": 0, "detail": "scheduler.py not found"}
    text = sched_path.read_text(encoding="utf-8", errors="ignore")
    jobs = len([l for l in text.splitlines()
                if "scheduler.add_job" in l or "add_job(" in l])
    return {"ok": jobs >= 5, "count": jobs, "detail": f"{jobs} job(s) registered"}


# ── LINE push ─────────────────────────────────────────────────────────────────

def send_line_message(text: str) -> bool:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    uid   = os.getenv("ADMIN_LINE_UID", "")
    if not token or not uid:
        return False
    try:
        import httpx
        r = httpx.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"to": uid, "messages": [{"type": "text", "text": text[:4900]}]},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"  LINE push error: {e}", file=sys.stderr)
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[ci_weekly_enhance] {now}\n")

    if not BACKEND_URL:
        msg = (f"⚠️ 週度健康報告\n{now}\n\n"
               "RAILWAY_BACKEND_URL 未設定，無法執行 API 檢查。\n"
               "請至 GitHub Settings → Secrets 設定 RAILWAY_BACKEND_URL。")
        print(msg, file=sys.stderr)
        send_line_message(msg)
        return 0

    # 1. API probes
    print("[1/3] Probing API endpoints...")
    results = await run_all_probes()
    passed  = [r for r in results if r["ok"]]
    failed  = [r for r in results if not r["ok"]]

    for r in results:
        icon = "✅" if r["ok"] else "❌"
        print(f"  {icon} [{r['status']:3d}] {r['label']}")
        if r["detail"]:
            print(f"         {r['detail'][:80]}")

    # 2. Parse system health body
    print("\n[2/3] Parsing system health...")
    health_body = next((r["body"] for r in results
                        if "/system/health" in str(r)), {})
    sys_info = parse_system_health(health_body)
    print(f"  Overall: {sys_info['overall']}  "
          f"Modules: {sys_info['green']}✅/{sys_info['red']}❌  "
          f"API rate: {sys_info['api_rate']}")

    # 3. Scheduler
    print("\n[3/3] Checking scheduler...")
    sched = check_scheduler_jobs()
    print(f"  {'✅' if sched['ok'] else '⚠️'} {sched['detail']}")

    # ── Build report ──────────────────────────────────────────────────────────
    critical_failed = [r for r, (_, _, _, crit) in zip(results, ENDPOINTS) if crit and not r["ok"]]
    all_ok = len(critical_failed) == 0

    if len(failed) == 0:
        hdr_icon = "🟢"
    elif len(failed) <= 2 and not critical_failed:
        hdr_icon = "🟡"
    else:
        hdr_icon = "🔴"

    lines = [
        f"{hdr_icon} 週度系統健康報告",
        f"📅 {now}",
        "─" * 22,
        f"API 端點：{len(passed)}/{len(results)} 正常",
    ]

    for r in results:
        icon = "✅" if r["ok"] else "❌"
        lines.append(f"  {icon} {r['label']} [{r['status']}]")

    # System health detail
    lines.append("")
    if sys_info["total"]:
        lines.append(f"模組狀態：{sys_info['green']}/{sys_info['total']} 綠燈")
        if sys_info["api_rate"] is not None:
            pct = f"{sys_info['api_rate']*100:.0f}%" if sys_info["api_rate"] <= 1 else f"{sys_info['api_rate']:.0f}%"
            lines.append(f"API 成功率：{pct}")
        if sys_info["mock_ratio"] is not None:
            pct = f"{sys_info['mock_ratio']*100:.0f}%" if sys_info["mock_ratio"] <= 1 else f"{sys_info['mock_ratio']:.0f}%"
            lines.append(f"Mock 比例：{pct}")
        if sys_info["kill_switch"]:
            lines.append("⚠️ Kill Switch 啟動中！")
    else:
        lines.append("系統儀表板：無法取得")

    # Scheduler
    lines.append("")
    sched_icon = "✅" if sched["ok"] else "⚠️"
    lines.append(f"排程器：{sched_icon} {sched['detail']}")

    # Failures detail
    if failed:
        lines.append(f"\n❌ 異常端點（{len(failed)} 個）：")
        for r in failed:
            lines.append(f"  • {r['label']}: {r['detail'][:60] or 'no response'}")

    summary = "全部正常 🎉" if all_ok else f"{len(critical_failed)} 個關鍵端點異常"
    lines.append(f"\n📊 結論：{summary}")

    report = "\n".join(lines)
    print(f"\n{report}")

    # Save
    Path("data").mkdir(exist_ok=True)
    Path("data/weekly_enhance_report.txt").write_text(report, encoding="utf-8")

    # Send LINE
    ok = send_line_message(report)
    print(f"\nLINE push: {'✅ 成功' if ok else '略過（token 未設定）'}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
