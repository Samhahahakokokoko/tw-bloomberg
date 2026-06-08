#!/usr/bin/env python3
"""
auto_improve.py — 每日自動改善工作流

使用方式：
  python auto_improve.py            # 掃描 + 推送 LINE 等待確認
  python auto_improve.py --dry-run  # 只印出分析結果，不推送 LINE

需要設定的環境變數：
  ANTHROPIC_API_KEY     Claude API 金鑰
  LINE_CHANNEL_ACCESS_TOKEN  LINE Bot token
  ADMIN_LINE_UID        接收通知的 LINE 使用者 ID
  RAILWAY_TOKEN         Railway API token（選填，沒有則只用 CLI）
  RAILWAY_PROJECT_ID    Railway 專案 ID（選填）
  RAILWAY_SERVICE_ID    Railway 服務 ID（選填）
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

# 加入 backend 至 sys.path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()


async def main(dry_run: bool = False) -> None:
    from backend.services.fix_engine import (
        fetch_railway_logs,
        parse_errors,
        analyze_with_claude,
        save_plan,
        format_plan_for_line,
    )

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    admin_uid = os.getenv("ADMIN_LINE_UID", "")

    if not api_key:
        print("[ERROR] ANTHROPIC_API_KEY 未設定")
        sys.exit(1)

    print(f"[auto_improve] 開始掃描 {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # Step 1: 取得 Railway logs
    print("[1/4] 取得 Railway logs...")
    logs = await fetch_railway_logs(lines=600)
    if not logs:
        print("  ⚠️  無法取得 logs（RAILWAY_TOKEN 未設定或 CLI 不可用）")
        print("  使用本機 logs 作為替代...")
        log_file = Path("data/app.log")
        logs = log_file.read_text(encoding="utf-8", errors="ignore")[-50000:] if log_file.exists() else ""

    if not logs:
        print("  ⚠️  無可用 logs，結束")
        return

    print(f"  取得 {len(logs)} 字元 logs")

    # Step 2: 解析錯誤
    print("[2/4] 解析錯誤...")
    errors = parse_errors(logs)
    print(f"  發現 {len(errors)} 個錯誤/警告")

    if not errors:
        msg = "今日掃描未發現需要修復的問題 ✅"
        print(f"  {msg}")
        if not dry_run and admin_uid:
            await _push_line(admin_uid, msg)
        return

    for i, e in enumerate(errors[:5], 1):
        snippet = e["content"][:80].replace("\n", " ")
        print(f"  [{i}] ({e['type']}) {snippet}")

    # Step 3: Claude 分析
    print("[3/4] Claude 分析中...")
    fixes = await analyze_with_claude(errors, api_key)
    print(f"  生成 {len(fixes)} 個修復方案")

    for f in fixes:
        print(f"  [{f.get('id')}] {f.get('severity','?')} — {f.get('title','?')}")

    if dry_run:
        print("\n[dry-run] 不儲存計劃，不推送 LINE")
        for f in fixes:
            print(f"\n=== [{f['id']}] {f['title']} ===")
            print(f"檔案：{f.get('file_path','N/A')}")
            print(f"說明：{f.get('description','')}")
            print(f"修復：\n{f.get('patch','')[:300]}")
        return

    # Step 4: 儲存計劃 + 推送 LINE
    print("[4/4] 儲存計劃並推送 LINE...")
    save_plan(fixes, log_snippet=logs[-1000:])

    message = format_plan_for_line(fixes)
    print(f"\n--- LINE 訊息預覽 ---\n{message}\n---")

    if admin_uid:
        await _push_line(admin_uid, message)
        print("  LINE 推送完成，等待您回覆「執行」")
    else:
        print("  ⚠️  ADMIN_LINE_UID 未設定，無法推送 LINE")
        print(f"  計劃已儲存至 data/pending_fixes.json，共 {len(fixes)} 個修復")


async def _push_line(uid: str, text: str) -> None:
    from backend.services.line_push import push_line_messages
    await push_line_messages(uid, [{"type": "text", "text": text}], timeout=15, context="auto_improve")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-Improve 自動修復工作流")
    parser.add_argument("--dry-run", action="store_true", help="只分析，不推送 LINE 也不儲存計劃")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
