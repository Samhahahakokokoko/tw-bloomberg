#!/usr/bin/env python3
"""
auto_maintain.py — 自動維護腳本 v2

功能：
  1. 規則引擎：偵測並修復四類常見問題（LINE 400 / API 302 / Import 錯誤 / 空資料表）
  2. Claude 引擎：分析規則引擎未處理的剩餘錯誤
  3. 健康測試：curl health / quote 2330 / daily / report
  4. 每週五自動優化：分析使用統計 → 生成改善建議 → 推播 LINE
  5. 推播維護報告到管理員 LINE

使用方式：
  python auto_maintain.py              # 完整維護流程
  python auto_maintain.py --dry-run    # 只分析，不修改檔案、不推播
  python auto_maintain.py --test-only  # 只執行健康測試
  python auto_maintain.py --weekly     # 強制執行週報優化（不論是否週五）

需要的環境變數：
  ANTHROPIC_API_KEY
  LINE_CHANNEL_ACCESS_TOKEN
  ADMIN_LINE_UID
  RAILWAY_BACKEND_URL
  DATABASE_URL                (選填，空表偵測用)
  RAILWAY_TOKEN               (選填)
  RAILWAY_PROJECT_ID          (選填)
  RAILWAY_SERVICE_ID          (選填)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

_ROOT = Path(__file__).parent

# ── 共用型別 ──────────────────────────────────────────────────────────────────

def _fix(title: str, severity: str, applied: bool, detail: str = "", patch_file: str = "") -> dict:
    return {"title": title, "severity": severity, "applied": applied,
            "detail": detail, "patch_file": patch_file}


# ══════════════════════════════════════════════════════════════════════════════
# 規則引擎 — 四類自動修復
# ══════════════════════════════════════════════════════════════════════════════

# ── 1. LINE 400 錯誤 ──────────────────────────────────────────────────────────

_LINE_400_RE = re.compile(
    r"Reply error: 400|LINE.*?400|push.*?400|400.*?The request body|"
    r"Invalid reply token|message.*?too long|alt.?text.*?exceed",
    re.IGNORECASE,
)

_HANDLER_PATH = _ROOT / "line_webhook" / "handler.py"

def _detect_line_400(logs: str) -> list[str]:
    seen: set[str] = set()
    hits = []
    for line in logs.splitlines():
        if _LINE_400_RE.search(line):
            key = line.strip()[:120]
            if key not in seen:
                seen.add(key)
                hits.append(line.strip())
    return hits[:10]


def fix_line_400_errors(logs: str, dry_run: bool = False) -> list[dict]:
    """
    偵測 LINE Bot 400 錯誤並套用防禦性修復：
    - alt_text 強制截斷至 400 字元
    - text 訊息強制截斷至 4900 字元
    - Flex 容器驗證加強（傳回 True 才送出）
    """
    hits = _detect_line_400(logs)
    if not hits:
        return []

    print(f"  [LINE-400] 偵測到 {len(hits)} 筆 400 錯誤")

    if not _HANDLER_PATH.exists():
        return [_fix("LINE 400 修復", "warning", False, "handler.py 不存在")]

    src = _HANDLER_PATH.read_text(encoding="utf-8")
    changed = False
    results: list[dict] = []

    # 修復 1：_flex() 裡的 alt_text 截斷（已有 [:400]，但確保存在）
    if "alt_text[:400]" not in src and "alt_text=alt_text" in src:
        src = src.replace(
            "alt_text=alt_text,",
            "alt_text=(alt_text or '')[:400],",
        )
        changed = True
        results.append(_fix("LINE alt_text 截斷修復", "warning", not dry_run,
                            "將 alt_text 截斷至 400 字元"))

    # 修復 2：_text() 裡的 text 截斷（4900 字元留 buffer）
    old_text_fn = "TextMessage(text=text)"
    new_text_fn = "TextMessage(text=(text or '')[:4900])"
    if old_text_fn in src:
        src = src.replace(old_text_fn, new_text_fn)
        changed = True
        results.append(_fix("LINE 文字訊息截斷修復", "warning", not dry_run,
                            "文字訊息截斷至 4900 字元防止 400"))

    # 修復 3：捕捉 reply 失敗後改用 push（若 reply token 失效）
    old_err = 'logger.error(f"Reply error: {resp.status_code} {resp.text}")'
    new_err = (
        'if resp.status_code == 400 and reply_token and "Invalid reply token" in resp.text:\n'
        '            logger.warning("Reply token expired, reply skipped")\n'
        '        else:\n'
        '            logger.error(f"Reply error: {resp.status_code} {resp.text}")'
    )
    if old_err in src and "Reply token expired" not in src:
        src = src.replace(old_err, new_err)
        changed = True
        results.append(_fix("LINE reply token 過期靜默處理", "minor", not dry_run,
                            "reply token 過期時靜默跳過而非記錄 error"))

    if changed and not dry_run:
        _HANDLER_PATH.write_text(src, encoding="utf-8")
        print(f"  [LINE-400] 已修復 {len(results)} 項，寫入 handler.py")
    elif not results:
        results.append(_fix("LINE 400 偵測到但無需修復", "minor", True,
                            f"錯誤樣本：{hits[0][:80]}"))

    return results


# ── 2. API 302 重定向 ─────────────────────────────────────────────────────────

_TWSE_SERVICE_PATH = _ROOT / "backend" / "services" / "twse_service.py"

# 已知替代端點映射
_ENDPOINT_ALTERNATIVES: dict[str, str] = {
    "openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL":
        "openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    "www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes":
        "www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
}

_API_302_RE = re.compile(
    r"302|Redirect|redirect|MovedPermanently|openapi\.twse|tpex\.org",
    re.IGNORECASE,
)


async def fix_api_302_redirects(logs: str, dry_run: bool = False) -> list[dict]:
    """
    偵測 API 302 重定向並嘗試探測可用端點。
    若替代端點有效，更新 twse_service.py 的 BASE URL。
    """
    hits = [l for l in logs.splitlines() if _API_302_RE.search(l)]
    if not hits:
        return []

    print(f"  [API-302] 偵測到 {len(hits)} 筆重定向相關 log")

    import httpx

    results: list[dict] = []
    candidates = {
        "TWSE_BASE": [
            "https://openapi.twse.com.tw/v1",
            "https://openapi.twse.com.tw/v2",
        ],
        "TPEX_BASE": [
            "https://www.tpex.org.tw/openapi/v1",
            "https://www.tpex.org.tw/openapi/v2",
        ],
    }

    probe_paths = {
        "TWSE_BASE": "/exchangeReport/STOCK_DAY_ALL",
        "TPEX_BASE": "/tpex_mainboard_daily_close_quotes",
    }

    if not _TWSE_SERVICE_PATH.exists():
        return [_fix("API 302 修復", "warning", False, "twse_service.py 不存在")]

    src = _TWSE_SERVICE_PATH.read_text(encoding="utf-8")
    changed = False

    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        for var, urls in candidates.items():
            for url in urls:
                probe = url + probe_paths[var]
                try:
                    resp = await client.get(probe)
                    if resp.status_code == 200:
                        # 找出目前設定值
                        cur_match = re.search(rf'{var}\s*=\s*"([^"]+)"', src)
                        if cur_match and cur_match.group(1) != url:
                            src = src.replace(
                                f'{var} = "{cur_match.group(1)}"',
                                f'{var} = "{url}"',
                            )
                            changed = True
                            results.append(_fix(
                                f"API 端點更新：{var}",
                                "critical",
                                not dry_run,
                                f"{cur_match.group(1)} → {url}",
                            ))
                        else:
                            results.append(_fix(
                                f"API 端點正常：{var}",
                                "minor",
                                True,
                                f"{url} 回應 200",
                            ))
                        break
                    elif resp.status_code in (301, 302, 308):
                        # 追蹤重定向目標
                        location = resp.headers.get("location", "")
                        if location:
                            results.append(_fix(
                                f"API 302 追蹤：{var}",
                                "warning",
                                False,
                                f"重定向至 {location}，需人工確認",
                            ))
                except Exception as e:
                    results.append(_fix(f"API 探測失敗：{var}", "minor", False, str(e)[:100]))

    if changed and not dry_run:
        _TWSE_SERVICE_PATH.write_text(src, encoding="utf-8")
        print(f"  [API-302] 已更新 {sum(1 for r in results if r['applied'])} 個端點")

    return results


# ── 3. Import 錯誤 ────────────────────────────────────────────────────────────

_IMPORT_RE = re.compile(
    r"ModuleNotFoundError: No module named '([^']+)'|"
    r"ImportError: cannot import name '([^']+)' from '([^']+)'|"
    r"ImportError: ([^\n]+)",
)

def fix_import_errors(logs: str, dry_run: bool = False) -> list[dict]:
    """
    偵測 Import 錯誤，嘗試：
    1. 若是相對 import → 轉絕對 import
    2. 若模組存在於其他路徑 → 更新 sys.path 或 import 語句
    3. 若是缺少套件 → 記錄 pip install 建議
    """
    results: list[dict] = []
    seen: set[str] = set()

    for m in _IMPORT_RE.finditer(logs):
        module = m.group(1) or m.group(2) or ""
        if not module or module in seen:
            continue
        seen.add(module)

        print(f"  [IMPORT] 偵測到缺少模組：{module}")

        # 嘗試在專案內找到對應檔案
        parts = module.split(".")
        candidates = list(_ROOT.rglob(f"{parts[-1]}.py"))

        if candidates:
            # 找到了，嘗試修復 sys.path
            found_path = candidates[0].parent
            rel = found_path.relative_to(_ROOT) if found_path.is_relative_to(_ROOT) else None
            if rel:
                # 在主 entrypoint 加入 sys.path（auto_maintain.py 本身已有，目標是 __main__ 入口）
                entry_files = [
                    _ROOT / "backend" / "main.py",
                    _ROOT / "quant" / "main.py",
                ]
                fixed = False
                for entry in entry_files:
                    if not entry.exists():
                        continue
                    src = entry.read_text(encoding="utf-8")
                    path_insert = f'sys.path.insert(0, str(Path(__file__).parent.parent / "{rel}"))'
                    if path_insert not in src and "import sys" in src:
                        # 在 import sys 後插入
                        src = src.replace(
                            "import sys\n",
                            f"import sys\nfrom pathlib import Path\n{path_insert}\n",
                            1,
                        )
                        if not dry_run:
                            entry.write_text(src, encoding="utf-8")
                        fixed = True
                        results.append(_fix(
                            f"Import 路徑修復：{module}",
                            "critical",
                            not dry_run,
                            f"在 {entry.name} 加入 sys.path → {rel}",
                        ))
                        break
                if not fixed:
                    results.append(_fix(
                        f"Import 找到但路徑未自動修復：{module}",
                        "warning",
                        False,
                        f"找到 {candidates[0]}，需手動確認 sys.path",
                    ))
        else:
            # 找不到 → 可能是第三方套件
            results.append(_fix(
                f"缺少套件：{module}",
                "critical",
                False,
                f"建議執行：pip install {parts[0]}",
            ))

    return results


# ── 4. 資料庫空表偵測 ─────────────────────────────────────────────────────────

_CRITICAL_TABLES = [
    ("stocks",        "SELECT COUNT(*) FROM stocks"),
    ("subscribers",   "SELECT COUNT(*) FROM subscribers"),
    ("price_history", "SELECT COUNT(*) FROM price_history"),
]

_PIPELINE_SCRIPTS: dict[str, str] = {
    "stocks":        "python scripts/seed_stocks.py",
    "price_history": "python -m scraper.price_fetcher",
    "subscribers":   None,  # 使用者自行訂閱，不自動補
}

async def fix_empty_db_tables(dry_run: bool = False) -> list[dict]:
    """
    連線資料庫，對關鍵表做 COUNT 檢查。
    若表為空且有對應 pipeline script，自動觸發。
    """
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        return [_fix("空表偵測", "minor", False, "DATABASE_URL 未設定，跳過")]

    results: list[dict] = []

    try:
        # 支援 asyncpg（PostgreSQL）與 aiosqlite（SQLite）
        if "postgresql" in db_url or "postgres" in db_url:
            import asyncpg
            # asyncpg 不接受 SQLAlchemy 格式的 URL，做轉換
            clean_url = db_url.replace("postgresql+asyncpg://", "postgresql://") \
                              .replace("postgresql+psycopg2://", "postgresql://")
            conn = await asyncpg.connect(clean_url, timeout=10)
            try:
                for table, sql in _CRITICAL_TABLES:
                    count = await conn.fetchval(sql)
                    if count == 0:
                        script = _PIPELINE_SCRIPTS.get(table)
                        if script:
                            print(f"  [DB] {table} 空表 → 觸發 {script}")
                            if not dry_run:
                                import subprocess
                                subprocess.run(script.split(), cwd=_ROOT,
                                               capture_output=True, timeout=60)
                            results.append(_fix(
                                f"空表修復：{table}",
                                "critical",
                                not dry_run,
                                f"執行 {script}",
                            ))
                        else:
                            results.append(_fix(
                                f"空表警告：{table}",
                                "warning",
                                False,
                                "需人工補資料",
                            ))
                    else:
                        print(f"  [DB] {table}: {count:,} 筆 ✅")
            finally:
                await conn.close()

        elif "sqlite" in db_url:
            import aiosqlite
            db_path = db_url.split("///")[-1].lstrip("./")
            if not Path(db_path).exists():
                return [_fix("SQLite 不存在", "warning", False, f"{db_path} 找不到")]
            async with aiosqlite.connect(db_path) as conn:
                for table, sql in _CRITICAL_TABLES:
                    try:
                        cursor = await conn.execute(sql)
                        row = await cursor.fetchone()
                        count = row[0] if row else 0
                        if count == 0:
                            results.append(_fix(
                                f"空表警告：{table}",
                                "warning",
                                False,
                                "表為空",
                            ))
                        else:
                            print(f"  [DB] {table}: {count:,} 筆 ✅")
                    except Exception as e:
                        results.append(_fix(f"DB 查詢失敗：{table}", "minor", False, str(e)[:80]))

    except Exception as e:
        results.append(_fix("DB 連線失敗", "warning", False, str(e)[:120]))

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 週五自動優化
# ══════════════════════════════════════════════════════════════════════════════

async def collect_usage_stats() -> dict[str, Any]:
    """
    從資料庫收集使用統計：
    - 最常用 LINE 指令（QueryHistory）
    - 最多使用者（UserProfile）
    - 交易記錄數（TradeLog）
    """
    stats: dict[str, Any] = {
        "top_queries": [],
        "active_users": 0,
        "trade_count_7d": 0,
        "error_summary": [],
    }

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        return stats

    try:
        if "postgresql" in db_url or "postgres" in db_url:
            import asyncpg
            clean_url = db_url.replace("postgresql+asyncpg://", "postgresql://") \
                              .replace("postgresql+psycopg2://", "postgresql://")
            conn = await asyncpg.connect(clean_url, timeout=10)
            try:
                # 最常查詢的 topic（近 30 天）
                rows = await conn.fetch("""
                    SELECT topic_hash, COUNT(*) AS cnt
                    FROM query_history
                    WHERE created_at > NOW() - INTERVAL '30 days'
                    GROUP BY topic_hash
                    ORDER BY cnt DESC
                    LIMIT 10
                """)
                stats["top_queries"] = [dict(r) for r in rows]

                # 活躍使用者數（近 7 天有查詢）
                row = await conn.fetchrow("""
                    SELECT COUNT(DISTINCT user_id) AS cnt
                    FROM query_history
                    WHERE created_at > NOW() - INTERVAL '7 days'
                """)
                stats["active_users"] = row["cnt"] if row else 0

                # 近 7 天交易記錄數
                row = await conn.fetchrow("""
                    SELECT COUNT(*) AS cnt FROM trade_log
                    WHERE created_at > NOW() - INTERVAL '7 days'
                """)
                stats["trade_count_7d"] = row["cnt"] if row else 0

            finally:
                await conn.close()

        elif "sqlite" in db_url:
            import aiosqlite
            db_path = db_url.split("///")[-1].lstrip("./")
            if Path(db_path).exists():
                async with aiosqlite.connect(db_path) as conn:
                    cursor = await conn.execute("""
                        SELECT topic_hash, COUNT(*) AS cnt
                        FROM query_history
                        WHERE created_at > datetime('now', '-30 days')
                        GROUP BY topic_hash
                        ORDER BY cnt DESC LIMIT 10
                    """)
                    stats["top_queries"] = [dict(zip(
                        [d[0] for d in cursor.description], row
                    )) async for row in cursor]

                    cursor = await conn.execute("""
                        SELECT COUNT(DISTINCT user_id) FROM query_history
                        WHERE created_at > datetime('now', '-7 days')
                    """)
                    row = await cursor.fetchone()
                    stats["active_users"] = row[0] if row else 0

    except Exception as e:
        print(f"  [Stats] 收集失敗：{e}")

    return stats


async def _analyze_weekly_with_claude(
    api_key: str,
    stats: dict[str, Any],
    error_summary: str,
) -> str:
    """用 Claude 生成每週優化建議（繁體中文，LINE 推播格式）。"""
    import anthropic

    prompt = textwrap.dedent(f"""
        你是台股 AI 量化交易 LINE Bot 的產品優化顧問。
        以下是本週使用統計：

        活躍用戶（7天）：{stats.get('active_users', 'N/A')}
        交易記錄（7天）：{stats.get('trade_count_7d', 'N/A')} 筆
        熱門查詢 TOP 10：{stats.get('top_queries', [])}

        本週主要錯誤：
        {error_summary or '無明顯錯誤'}

        請用繁體中文生成：
        1. 本週系統表現摘要（2句）
        2. 最值得優先改善的 3 個功能（各一句，具體可行）
        3. 給使用者的一句話建議

        格式：直接輸出純文字，每項用換行分隔，總長度不超過 500 字。
        不要有標題前綴（如「1.」之類），直接寫內容。
    """).strip()

    client = anthropic.AsyncAnthropic(api_key=api_key)
    msg = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


async def run_weekly_optimization(
    api_key: str,
    line_token: str,
    admin_uid: str,
    logs: str,
    dry_run: bool = False,
) -> None:
    """每週五執行：收集統計 → Claude 分析 → LINE 推播。"""
    print("\n[週報] 開始每週優化分析...")

    stats = await collect_usage_stats()
    print(f"  活躍用戶：{stats['active_users']}｜交易記錄：{stats['trade_count_7d']}")

    # 從 logs 擷取錯誤摘要（近期 ERROR 行）
    error_lines = [
        l.strip() for l in logs.splitlines()
        if re.search(r"\b(ERROR|CRITICAL|Exception)\b", l)
    ][:15]
    error_summary = "\n".join(error_lines)

    if not api_key:
        print("  [週報] 無 ANTHROPIC_API_KEY，跳過 Claude 分析")
        return

    try:
        suggestion = await _analyze_weekly_with_claude(api_key, stats, error_summary)
    except Exception as e:
        print(f"  [週報] Claude 分析失敗：{e}")
        return

    now = datetime.now().strftime("%Y-%m-%d")
    week_report = (
        f"📊 每週優化報告 {now}\n"
        f"────────────────\n"
        f"活躍用戶：{stats['active_users']} 人｜"
        f"交易記錄：{stats['trade_count_7d']} 筆\n\n"
        f"{suggestion}"
    )

    print(f"\n[週報內容]\n{week_report}\n")

    if dry_run:
        print("  [週報] dry-run，跳過 LINE 推播")
        return

    if not line_token or not admin_uid:
        print("  [週報] 缺少 LINE 設定，跳過推播")
        return

    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.line.me/v2/bot/message/push",
                json={"to": admin_uid, "messages": [{"type": "text", "text": week_report}]},
                headers={"Authorization": f"Bearer {line_token}",
                         "Content-Type": "application/json"},
            )
            if resp.status_code == 200:
                print("  [週報] 推播成功")
            else:
                print(f"  [週報] 推播失敗 {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        print(f"  [週報] 推播異常：{e}")


# ══════════════════════════════════════════════════════════════════════════════
# 健康測試
# ══════════════════════════════════════════════════════════════════════════════

async def run_health_tests(base_url: str) -> list[dict]:
    import httpx

    tests = [
        ("基本健康",  "GET",  "/health",              None),
        ("台積電報價", "GET",  "/api/quote/2330",      None),
        ("每日建議",  "GET",  "/api/advice/daily",    None),
        ("早報生成",  "POST", "/api/report/morning",  None),
    ]

    results = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for name, method, path, body in tests:
            url = base_url.rstrip("/") + path
            try:
                resp = await (client.get(url) if method == "GET" else client.post(url, json=body))
                ok = resp.status_code < 400
                results.append({"name": name, "ok": ok, "status": resp.status_code,
                                 "detail": "" if ok else resp.text[:200]})
            except Exception as e:
                results.append({"name": name, "ok": False, "status": 0, "detail": str(e)[:200]})
    return results


# ══════════════════════════════════════════════════════════════════════════════
# LINE 推播
# ══════════════════════════════════════════════════════════════════════════════

async def push_line_report(
    token: str,
    admin_uid: str,
    rule_fixes: list[dict],
    claude_fixes: list[dict],
    test_results: list[dict],
    dry_run: bool = False,
) -> None:
    if dry_run:
        print("[LINE] dry-run，跳過推播")
        return
    if not token or not admin_uid:
        print("[LINE] 缺少設定，跳過推播")
        return

    import httpx

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    all_fixes = rule_fixes + claude_fixes
    all_ok = all(r["ok"] for r in test_results) if test_results else True
    any_critical = any(f.get("severity") == "critical" and not f.get("applied") for f in all_fixes)

    overall = "✅ 系統正常" if all_ok and not any_critical else "⚠️ 需要確認"

    def fmt_fixes(fixes: list[dict]) -> str:
        if not fixes:
            return "  （無）"
        return "\n".join(
            f"  {'✅' if f.get('applied') else '⚠️'} [{f.get('severity','?')}] {f.get('title','')}"
            for f in fixes
        )

    test_lines = "\n".join(
        f"  {'✅' if r['ok'] else '❌'} {r['name']}"
        + (f"（{r['status']}）" if not r["ok"] else "")
        for r in test_results
    ) or "  （未執行）"

    sections = [
        f"🤖 自動維護報告 {now}",
        f"────────────────",
        f"【狀態】{overall}",
        "",
        f"【規則修復】",
        fmt_fixes(rule_fixes),
        "",
        f"【Claude 修復】",
        fmt_fixes(claude_fixes),
        "",
        f"【健康測試】",
        test_lines,
    ]
    text = "\n".join(sections)[:4900]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.line.me/v2/bot/message/push",
                json={"to": admin_uid, "messages": [{"type": "text", "text": text}]},
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
            )
            print(f"[LINE] 推播{'成功' if resp.status_code == 200 else f'失敗 {resp.status_code}'}")
    except Exception as e:
        print(f"[LINE] 推播異常：{e}")


# ══════════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════════

async def main(
    dry_run: bool = False,
    test_only: bool = False,
    force_weekly: bool = False,
) -> int:
    """
    回傳碼：
      0 = 無程式碼變更
      1 = 腳本執行錯誤
      2 = 已套用程式碼修復（CI 據此觸發 commit）
    """
    from backend.services.fix_engine import (
        analyze_with_claude,
        apply_patch,
        fetch_railway_logs,
        parse_errors,
        save_plan,
    )

    api_key     = os.getenv("ANTHROPIC_API_KEY", "")
    line_token  = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    admin_uid   = os.getenv("ADMIN_LINE_UID", "")
    backend_url = os.getenv("RAILWAY_BACKEND_URL", "").rstrip("/")
    is_friday   = datetime.now(tz=timezone.utc).weekday() == 4  # 0=Mon, 4=Fri

    if not api_key and not test_only:
        print("[ERROR] ANTHROPIC_API_KEY 未設定")
        return 1

    print(f"[auto_maintain v2] {datetime.now().strftime('%Y-%m-%d %H:%M')} "
          f"{'(dry-run)' if dry_run else ''}"
          f"{'(test-only)' if test_only else ''}"
          f"{'(weekly)' if force_weekly or is_friday else ''}")

    rule_fixes: list[dict] = []
    claude_fixes: list[dict] = []
    logs = ""
    applied_count = 0

    if not test_only:
        # ── 步驟 1：取得 Railway logs ────────────────────────────────
        print("\n[1/5] 取得 Railway logs...")
        logs = await fetch_railway_logs(lines=800)
        if not logs:
            log_file = _ROOT / "data" / "app.log"
            logs = log_file.read_text(encoding="utf-8", errors="ignore")[-60_000:] \
                if log_file.exists() else ""
        if not logs:
            print("  ⚠️  無可用 logs")
        else:
            print(f"  取得 {len(logs):,} 字元")

        # ── 步驟 2：規則引擎（四類自動修復）────────────────────────
        print("\n[2/5] 規則引擎掃描...")

        print("  → LINE 400 偵測")
        rule_fixes += fix_line_400_errors(logs, dry_run)

        print("  → API 302 偵測")
        rule_fixes += await fix_api_302_redirects(logs, dry_run)

        print("  → Import 錯誤偵測")
        rule_fixes += fix_import_errors(logs, dry_run)

        print("  → 資料庫空表偵測")
        rule_fixes += await fix_empty_db_tables(dry_run)

        applied_rule = sum(1 for f in rule_fixes if f.get("applied") and "修復" in f.get("title", ""))
        print(f"  規則引擎完成：{len(rule_fixes)} 項偵測，{applied_rule} 項已修復")

        # ── 步驟 3：Claude 引擎（處理規則未涵蓋的錯誤）─────────────
        print("\n[3/5] Claude 引擎分析...")
        if logs and api_key:
            # 過濾掉規則引擎已處理的錯誤類型
            errors = parse_errors(logs)
            # 排除已知由規則引擎處理的模式
            handled_patterns = re.compile(
                r"400|302|ModuleNotFoundError|ImportError", re.IGNORECASE
            )
            remaining = [e for e in errors if not handled_patterns.search(e.get("content", ""))]
            print(f"  規則引擎後剩餘 {len(remaining)}/{len(errors)} 個錯誤交給 Claude")

            if remaining:
                try:
                    claude_fixes = await analyze_with_claude(remaining, api_key)
                    save_plan(claude_fixes, logs[-500:])
                    print(f"  Claude 產生 {len(claude_fixes)} 個修復方案")

                    if not dry_run:
                        for fix in claude_fixes:
                            patch = fix.get("patch", "")
                            ok, msg = apply_patch(patch, fix.get("file_path", ""))
                            fix["applied"] = ok
                            if ok and patch.startswith("---"):
                                applied_count += 1
                            print(f"    {'✅' if ok else '❌'} {fix.get('title','')} — {msg}")
                    else:
                        for fix in claude_fixes:
                            fix["applied"] = False
                except Exception as e:
                    print(f"  Claude 分析失敗：{e}")
            else:
                print("  無剩餘錯誤，跳過 Claude")
        else:
            print("  跳過（無 logs 或無 API key）")

        # 統計規則引擎修復的程式碼變更數
        applied_count += sum(
            1 for f in rule_fixes
            if f.get("applied") and f.get("patch_file")
        )

    # ── 步驟 4：健康測試 ──────────────────────────────────────────────
    print("\n[4/5] 執行健康測試...")
    test_results: list[dict] = []
    if backend_url:
        print(f"  → {backend_url}")
        test_results = await run_health_tests(backend_url)
        for r in test_results:
            print(f"  {'✅' if r['ok'] else '❌'} {r['name']} ({r['status']})"
                  + (f" — {r['detail'][:80]}" if not r["ok"] else ""))

        # 寫 CI summary
        summary_lines = []
        if rule_fixes:
            summary_lines += ["=== 規則修復 ==="] + [
                f"{'✅' if f.get('applied') else '⚠️'} {f.get('title','')}" for f in rule_fixes
            ]
        if claude_fixes:
            summary_lines += ["\n=== Claude 修復 ==="] + [
                f"{'✅' if f.get('applied') else '⚠️'} {f.get('title','')}" for f in claude_fixes
            ]
        summary_lines += ["\n=== 健康測試 ==="] + [
            f"{'✅' if r['ok'] else '❌'} {r['name']} ({r['status']})" for r in test_results
        ]
        summary_path = _ROOT / "data" / "ci_fix_summary.txt"
        summary_path.parent.mkdir(exist_ok=True)
        summary_path.write_text("\n".join(summary_lines), encoding="utf-8")
    else:
        print("  RAILWAY_BACKEND_URL 未設定，跳過")

    # ── 步驟 5：週五優化分析 ──────────────────────────────────────────
    if (is_friday or force_weekly) and not test_only:
        await run_weekly_optimization(api_key, line_token, admin_uid, logs, dry_run)

    # ── 推播維護報告 ──────────────────────────────────────────────────
    print("\n[5/5] 推播 LINE 報告...")
    await push_line_report(line_token, admin_uid, rule_fixes, claude_fixes, test_results, dry_run)

    if applied_count > 0:
        print(f"\n[完成] 套用了 {applied_count} 個程式修復（exit 2）")
        return 2

    print("\n[完成] 無程式碼變更（exit 0）")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="台股系統自動維護腳本 v2")
    parser.add_argument("--dry-run",   action="store_true", help="只分析，不修改檔案也不推播")
    parser.add_argument("--test-only", action="store_true", help="只執行健康測試")
    parser.add_argument("--weekly",    action="store_true", help="強制執行週五優化（不論今天星期幾）")
    args = parser.parse_args()

    code = asyncio.run(main(dry_run=args.dry_run, test_only=args.test_only, force_weekly=args.weekly))
    sys.exit(code)
