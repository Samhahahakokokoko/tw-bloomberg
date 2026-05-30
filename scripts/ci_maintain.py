#!/usr/bin/env python3
"""
CI-mode maintenance script (called by auto_maintain.yml).

Exit codes:
  0 — no errors found, or fixes are manual-only (no code changes)
  1 — script error (bad credentials, API failure, etc.)
  2 — code patches applied; git diff will show changes
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()


async def main() -> int:
    from backend.services.fix_engine import (
        analyze_with_claude,
        apply_patch,
        fetch_railway_logs,
        format_plan_for_line,
        parse_errors,
        save_plan,
    )

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[ERROR] ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    # ── 1. Fetch logs ────────────────────────────────────────────────
    print("[1/4] Fetching Railway logs...")
    logs = await fetch_railway_logs(lines=600)
    if not logs:
        log_file = Path("data/app.log")
        if log_file.exists():
            logs = log_file.read_text(encoding="utf-8", errors="ignore")[-50_000:]
    if not logs:
        print("  No logs available — nothing to analyse")
        return 0
    print(f"  Got {len(logs):,} chars")

    # ── 2. Parse errors ──────────────────────────────────────────────
    print("[2/4] Parsing errors...")
    errors = parse_errors(logs)
    print(f"  Found {len(errors)} error(s)")
    if not errors:
        return 0

    # ── 3. Claude analysis ───────────────────────────────────────────
    print("[3/4] Analysing with Claude...")
    try:
        fixes = await analyze_with_claude(errors, api_key)
    except Exception as exc:
        print(f"  Claude API error: {exc}", file=sys.stderr)
        return 1
    print(f"  Generated {len(fixes)} fix proposal(s)")
    if not fixes:
        return 0

    save_plan(fixes, log_snippet=logs[-1_000:])

    # Write plain-text summary for the PR body
    summary = format_plan_for_line(fixes)
    Path("data").mkdir(exist_ok=True)
    Path("data/ci_fix_summary.txt").write_text(summary, encoding="utf-8")

    # ── 4. Apply patches ─────────────────────────────────────────────
    print("[4/4] Applying patches...")
    applied: list[int] = []
    results: list[dict] = []
    for fix in fixes:
        ok, msg = apply_patch(fix.get("patch", ""), fix.get("file_path", ""))
        results.append({"id": fix["id"], "title": fix["title"], "ok": ok, "message": msg})
        tag = "OK  " if ok else "SKIP"
        print(f"  [{tag}] [{fix['id']}] {fix['title']}: {msg[:80]}")
        if ok:
            applied.append(fix["id"])

    Path("data/ci_fix_results.json").write_text(
        json.dumps(
            {"fixes": fixes, "results": results, "applied_ids": applied},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if applied:
        print(f"\nApplied {len(applied)} patch(es) — git diff will show changes")
        return 2
    print("\nAll proposals are manual steps — no code changes")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
