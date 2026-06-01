#!/usr/bin/env python3
"""
Rule-based daily enhancement — called by daily_enhance.yml.

Applies safe, deterministic fixes without requiring Claude API.

Exit codes:
  0 — clean or no fixable issues
  1 — script error
  2 — patches applied (git diff will show changes)
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO_ROOT = Path(__file__).parent.parent
PY_DIRS   = ["backend", "quant", "line_webhook", "scripts"]


# ── Rule 1: Missing standard-library imports ──────────────────────────────────

_IMPORT_RULES: list[tuple[str, str]] = [
    # (usage_pattern, import_line)
    (r"\basyncio\.",   "import asyncio"),
    (r"\bre\.",        "import re"),
    (r"\bjson\.",      "import json"),
    (r"\bmath\.",      "import math"),
]


def _has_import(text: str, module: str) -> bool:
    return bool(re.search(rf"^(?:import {module}|from {module} )", text, re.MULTILINE))


def fix_missing_imports(path: Path) -> bool:
    """Add missing stdlib imports detected by usage patterns."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    additions: list[str] = []
    for usage_pat, import_line in _IMPORT_RULES:
        module = import_line.split()[-1]
        if re.search(usage_pat, text) and not _has_import(text, module):
            additions.append(import_line)

    if not additions:
        return False

    lines = text.splitlines(keepends=True)
    # Insert after the last existing import block
    insert_at = 0
    for i, line in enumerate(lines):
        if re.match(r"^(import |from )\w", line):
            insert_at = i + 1

    if insert_at == 0:
        # No existing imports — add at top (after shebang/encoding comments)
        for i, line in enumerate(lines[:5]):
            if not re.match(r"^(#|$)", line):
                insert_at = i
                break

    for imp in reversed(additions):
        lines.insert(insert_at, imp + "\n")

    path.write_text("".join(lines), encoding="utf-8")
    rel = path.relative_to(REPO_ROOT)
    for imp in additions:
        print(f"  [FIX] {rel}: added '{imp}'")
    return True


# ── Rule 2: bare `except Exception:` → `except Exception as e:` ──────────────

_BARE_EXCEPT = re.compile(r"^(\s*)except Exception:\s*$", re.MULTILINE)


def fix_bare_except(path: Path) -> bool:
    """Add 'as e' to bare except-Exception clauses."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    new_text, n = _BARE_EXCEPT.subn(r"\1except Exception as e:", text)
    if n == 0:
        return False

    path.write_text(new_text, encoding="utf-8")
    print(f"  [FIX] {path.relative_to(REPO_ROOT)}: {n} bare except(s) fixed")
    return True


# ── Rule 3: warn about anthropic calls missing credit check (no auto-fix) ─────

def audit_anthropic_calls(path: Path) -> list[str]:
    """Return warning strings for anthropic calls without credit balance guards."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    if "anthropic" not in text or "credit balance is too low" in text:
        return []

    calls = re.findall(r"client\.messages\.create", text)
    if calls:
        return [f"  [WARN] {path.relative_to(REPO_ROOT)}: {len(calls)} anthropic call(s) without credit guard"]
    return []


# ── Log analysis ──────────────────────────────────────────────────────────────

async def fetch_logs() -> str:
    try:
        from backend.services.fix_engine import fetch_railway_logs
        return await fetch_railway_logs(lines=300) or ""
    except Exception as e:
        print(f"  Log fetch skipped: {e}", file=sys.stderr)
        return ""


def summarise_log_errors(logs: str) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for line in logs.splitlines():
        if any(tok in line for tok in ("ERROR", "Traceback", "Exception:", "Error:")):
            key = line.strip()[:80]
            if key not in seen:
                seen.add(key)
                results.append(key)
    return results[:20]


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
    print(f"[ci_daily_enhance] {now}\n")

    # 1. Fetch Railway logs
    print("[1/4] Fetching Railway logs...")
    logs      = await fetch_logs()
    log_errs  = summarise_log_errors(logs) if logs else []
    print(f"  {len(log_errs)} unique error lines found")

    # 2. Rule-based fixes
    print("\n[2/4] Applying rule-based fixes...")
    fixed_files: list[str] = []
    warnings:    list[str] = []

    for dir_name in PY_DIRS:
        dir_path = REPO_ROOT / dir_name
        if not dir_path.exists():
            continue
        for py_file in sorted(dir_path.rglob("*.py")):
            changed = False
            try:
                changed |= fix_missing_imports(py_file)
                changed |= fix_bare_except(py_file)
                warnings += audit_anthropic_calls(py_file)
            except Exception as e:
                print(f"  [ERR] {py_file.name}: {e}", file=sys.stderr)
            if changed:
                fixed_files.append(str(py_file.relative_to(REPO_ROOT)))

    for w in warnings:
        print(w)
    print(f"\n  Fixed {len(fixed_files)} file(s), {len(warnings)} warning(s)")

    # 3. Build LINE report
    print("\n[3/4] Building report...")
    icon = "✅" if not log_errs and not warnings else ("⚠️" if log_errs else "🔔")
    lines = [
        f"{icon} 每日增強維護",
        f"📅 {now}",
        "─" * 22,
    ]

    if fixed_files:
        lines.append(f"🔧 自動修復 {len(fixed_files)} 個問題：")
        for f in fixed_files[:5]:
            lines.append(f"  • {f}")
        if len(fixed_files) > 5:
            lines.append(f"  ...共 {len(fixed_files)} 個")
    else:
        lines.append("✅ 程式碼規則：全部符合")

    if warnings:
        lines.append(f"\n🔔 待人工確認（{len(warnings)} 項）：")
        for w in warnings[:3]:
            lines.append(f"  {w.strip()}")

    if log_errs:
        lines.append(f"\n⚠️ 日誌錯誤（{len(log_errs)} 條）：")
        for e in log_errs[:4]:
            lines.append(f"  {e[:75]}")
    else:
        lines.append("\n✅ Railway 日誌：無新錯誤")

    report = "\n".join(lines)
    print(report)

    # Save
    Path("data").mkdir(exist_ok=True)
    Path("data/daily_enhance_report.txt").write_text(report, encoding="utf-8")

    # 4. Send LINE
    print("\n[4/4] Sending LINE report...")
    ok = send_line_message(report)
    print(f"  {'✅ 推送成功' if ok else '略過（token 未設定）'}")

    return 2 if fixed_files else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
