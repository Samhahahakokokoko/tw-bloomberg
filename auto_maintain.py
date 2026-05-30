#!/usr/bin/env python3
"""
auto_maintain.py — 自動維護腳本

功能：
  1. 抓取 Railway logs 分析錯誤
  2. 用 Claude 產生修復方案並自動套用
  3. 執行健康測試（health / quote / daily / report）
  4. 推送維護報告到管理員 LINE

使用方式：
  python auto_maintain.py            # 完整維護流程
  python auto_maintain.py --dry-run  # 只分析，不套用修復、不推播 LINE
  python auto_maintain.py --test-only  # 只執行測試

需要的環境變數：
  ANTHROPIC_API_KEY
  LINE_CHANNEL_ACCESS_TOKEN
  ADMIN_LINE_UID
  RAILWAY_BACKEND_URL         (測試端點用)
  RAILWAY_TOKEN               (選填)
  RAILWAY_PROJECT_ID          (選填)
  RAILWAY_SERVICE_ID          (選填)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()


# ── 健康測試 ──────────────────────────────────────────────────────────────────

async def run_health_tests(base_url: str) -> list[dict]:
    """
    對部署中的後端執行四項測試。
    回傳 [{"name": ..., "ok": bool, "status": int, "detail": str}]
    """
    import httpx

    tests = [
        ("系統健康", "GET",  "/api/system/health",  None),
        ("台積電報價", "GET",  "/api/quote/2330",      None),
        ("每日建議",  "GET",  "/api/advice/daily",    None),
        ("早報生成",  "POST", "/api/report/morning",  None),
    ]

    results = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for name, method, path, body in tests:
            url = base_url.rstrip("/") + path
            try:
                if method == "GET":
                    resp = await client.get(url)
                else:
                    resp = await client.post(url, json=body)
                ok = resp.status_code < 400
                results.append({
                    "name": name,
                    "ok": ok,
                    "status": resp.status_code,
                    "detail": "" if ok else resp.text[:200],
                })
            except Exception as e:
                results.append({
                    "name": name,
                    "ok": False,
                    "status": 0,
                    "detail": str(e)[:200],
                })
    return results


# ── LINE 推播 ─────────────────────────────────────────────────────────────────

async def push_line_report(
    token: str,
    admin_uid: str,
    fixes: list[dict],
    test_results: list[dict],
    dry_run: bool = False,
) -> None:
    """組合維護報告並推播至管理員 LINE。"""
    if dry_run:
        print("[LINE] dry-run 模式，跳過推播")
        return
    if not token or not admin_uid:
        print("[LINE] 缺少 LINE_CHANNEL_ACCESS_TOKEN 或 ADMIN_LINE_UID，跳過推播")
        return

    import httpx

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    all_tests_ok = all(r["ok"] for r in test_results)

    # 修復摘要
    if fixes:
        fix_lines = "\n".join(
            f"{'✅' if f.get('applied') else '⚠️'} [{f.get('severity','?')}] {f.get('title','')}"
            for f in fixes
        )
    else:
        fix_lines = "✅ 無需修復"

    # 測試摘要
    test_lines = "\n".join(
        f"{'✅' if r['ok'] else '❌'} {r['name']}"
        + (f"（{r['status']}）" if not r["ok"] else "")
        for r in test_results
    )

    overall = "✅ 系統正常" if (not fixes or all(f.get("applied") for f in fixes)) and all_tests_ok else "⚠️ 需要人工確認"

    text = (
        f"🤖 自動維護報告 {now}\n"
        f"────────────────\n"
        f"【狀態】{overall}\n\n"
        f"【修復項目】\n{fix_lines}\n\n"
        f"【健康測試】\n{test_lines}"
    )

    payload = {
        "to": admin_uid,
        "messages": [{"type": "text", "text": text}],
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.line.me/v2/bot/message/push",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 200:
                print("[LINE] 報告已推播成功")
            else:
                print(f"[LINE] 推播失敗 HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[LINE] 推播異常：{e}")


# ── 主流程 ────────────────────────────────────────────────────────────────────

async def main(dry_run: bool = False, test_only: bool = False) -> int:
    """
    回傳碼：
      0 = 無變更 / 全程正常
      1 = 腳本執行錯誤
      2 = 已套用修復（CI 可據此判斷是否需要 commit）
    """
    from backend.services.fix_engine import (
        analyze_with_claude,
        apply_patch,
        fetch_railway_logs,
        parse_errors,
        save_plan,
    )

    api_key   = os.getenv("ANTHROPIC_API_KEY", "")
    line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    admin_uid  = os.getenv("ADMIN_LINE_UID", "")
    backend_url = os.getenv("RAILWAY_BACKEND_URL", "").rstrip("/")

    if not api_key and not test_only:
        print("[ERROR] ANTHROPIC_API_KEY 未設定")
        return 1

    print(f"[auto_maintain] 開始 {datetime.now().strftime('%Y-%m-%d %H:%M')} "
          f"{'(dry-run)' if dry_run else ''}"
          f"{'(test-only)' if test_only else ''}")

    fixes: list[dict] = []
    applied_count = 0

    if not test_only:
        # ── 步驟 1：取得 Railway logs ────────────────────────────────
        print("[1/4] 取得 Railway logs...")
        logs = await fetch_railway_logs(lines=600)
        if not logs:
            log_file = Path("data/app.log")
            logs = log_file.read_text(encoding="utf-8", errors="ignore")[-50_000:] if log_file.exists() else ""
        if not logs:
            print("  ⚠️  無可用 logs，跳過分析")
        else:
            print(f"  取得 {len(logs):,} 字元")

            # ── 步驟 2：解析錯誤 ─────────────────────────────────────
            print("[2/4] 解析錯誤...")
            errors = parse_errors(logs)
            print(f"  發現 {len(errors)} 個錯誤")

            if errors:
                # ── 步驟 3：Claude 分析 ──────────────────────────────
                print("[3/4] Claude 分析中...")
                try:
                    fixes = await analyze_with_claude(errors, api_key)
                    print(f"  產生 {len(fixes)} 個修復方案")
                    save_plan(fixes, logs[-500:])
                except Exception as e:
                    print(f"  Claude 分析失敗：{e}")
                    return 1

                # ── 步驟 4：套用修復 ──────────────────────────────────
                if not dry_run:
                    print("[4/4] 套用修復...")
                    for fix in fixes:
                        patch = fix.get("patch", "")
                        fpath = fix.get("file_path", "")
                        if not patch:
                            fix["applied"] = False
                            continue
                        ok, msg = apply_patch(patch, fpath)
                        fix["applied"] = ok
                        status = "✅" if ok else "❌"
                        print(f"  {status} [{fix.get('severity','?')}] {fix.get('title','')} — {msg}")
                        if ok and patch.startswith("---"):
                            applied_count += 1
                else:
                    print("[4/4] dry-run，跳過套用")
                    for fix in fixes:
                        fix["applied"] = False
            else:
                print("[3/4] 無錯誤，跳過 Claude 分析")
                print("[4/4] 無需修復")

    # ── 步驟 5：健康測試 ──────────────────────────────────────────────
    test_results: list[dict] = []
    if backend_url:
        print(f"[5/5] 執行健康測試 → {backend_url}")
        test_results = await run_health_tests(backend_url)
        for r in test_results:
            icon = "✅" if r["ok"] else "❌"
            print(f"  {icon} {r['name']} ({r['status']})"
                  + (f" — {r['detail']}" if not r["ok"] else ""))

        # 將測試結果寫入 CI summary 檔案
        summary_path = Path("data/ci_fix_summary.txt")
        summary_path.parent.mkdir(exist_ok=True)
        lines = []
        if fixes:
            lines.append("=== 修復項目 ===")
            for f in fixes:
                status = "✅" if f.get("applied") else "⚠️"
                lines.append(f"{status} [{f.get('severity','?')}] {f.get('title','')}")
        lines.append("\n=== 健康測試 ===")
        for r in test_results:
            lines.append(f"{'✅' if r['ok'] else '❌'} {r['name']} ({r['status']})")
        summary_path.write_text("\n".join(lines), encoding="utf-8")
    else:
        print("[5/5] RAILWAY_BACKEND_URL 未設定，跳過健康測試")

    # ── 步驟 6：推播 LINE 報告 ────────────────────────────────────────
    await push_line_report(line_token, admin_uid, fixes, test_results, dry_run)

    if applied_count > 0:
        print(f"\n[完成] 套用了 {applied_count} 個程式修復，請執行 git diff 確認")
        return 2

    print("\n[完成] 無程式碼變更")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="台股系統自動維護腳本")
    parser.add_argument("--dry-run",   action="store_true", help="只分析，不套用修復也不推播 LINE")
    parser.add_argument("--test-only", action="store_true", help="只執行健康測試")
    args = parser.parse_args()

    exit_code = asyncio.run(main(dry_run=args.dry_run, test_only=args.test_only))
    sys.exit(exit_code)
