#!/usr/bin/env python3
"""
auto_maintain.py — 規則式自動維護腳本 v3（無 Claude API）

功能：
  1. 抓取 Railway logs，逐條比對規則表
  2. 每條規則有對應的自動修復函式
  3. 每週日 03:00 執行完整 LINE 指令測試
  4. 有修復就 exit 2（CI 據此 commit）；無修復 exit 0
  5. 結果推播到管理員 LINE

使用方式：
  python auto_maintain.py              # 完整維護流程
  python auto_maintain.py --dry-run    # 只偵測，不修改檔案、不推播
  python auto_maintain.py --test-only  # 只執行健康 + LINE 指令測試
  python auto_maintain.py --weekly     # 強制執行週報（不論今天星期幾）

需要的環境變數：
  LINE_CHANNEL_ACCESS_TOKEN
  ADMIN_LINE_UID
  RAILWAY_BACKEND_URL
  RAILWAY_TOKEN        (選填，抓 logs 用)
  RAILWAY_PROJECT_ID   (選填)
  RAILWAY_SERVICE_ID   (選填)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))

# Windows cp950 終端機：強制 UTF-8 避免 emoji 爆炸
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure") and _s.encoding.lower() not in ("utf-8", "utf8"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

_ROOT = Path(__file__).parent


# ══════════════════════════════════════════════════════════════════════════════
# 共用型別 & 工具
# ══════════════════════════════════════════════════════════════════════════════

def _r(title: str, severity: str, applied: bool, detail: str = "") -> dict:
    return {"title": title, "severity": severity, "applied": applied, "detail": detail}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _write(path: Path, text: str, dry_run: bool) -> bool:
    if dry_run:
        return False
    path.write_text(text, encoding="utf-8")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# 修復函式
# ══════════════════════════════════════════════════════════════════════════════

# ── 1. LINE 400 / Flex 錯誤 ───────────────────────────────────────────────────

_HANDLER = _ROOT / "line_webhook" / "handler.py"

def fix_line_400(logs: str, dry_run: bool) -> list[dict]:
    results: list[dict] = []
    src = _read(_HANDLER)
    if not src:
        return [_r("LINE 400 修復", "critical", False, "handler.py 不存在")]

    changed = False

    # 修復 1：TextMessage 截斷到 4900 字元
    if "TextMessage(text=text)" in src:
        src = src.replace(
            "TextMessage(text=text)",
            "TextMessage(text=(text or '')[:4900])",
        )
        changed = True
        results.append(_r("LINE text 截斷至 4900", "critical", not dry_run))

    # 修復 2：alt_text 截斷（LINE 限制 400 字元）
    if "alt_text=alt_text," in src and "alt_text=alt_text[:400]" not in src:
        src = src.replace(
            "alt_text=alt_text,",
            "alt_text=(alt_text or '')[:400],",
        )
        changed = True
        results.append(_r("LINE alt_text 截斷至 400", "critical", not dry_run))

    # 修復 3：carousel contents 不得為空（"At least one block" 錯誤來源）
    old_carousel = 'if container.get("type") == "carousel":'
    new_carousel = (
        'if container.get("type") == "carousel":\n'
        '        # 過濾空 bubble，避免 "At least one block" 400 錯誤\n'
        '        container["contents"] = [c for c in container.get("contents", []) if c]\n'
        '        if not container["contents"]:\n'
        '            return False'
    )
    if old_carousel in src and '"At least one block"' not in src:
        src = src.replace(old_carousel, new_carousel, 1)
        changed = True
        results.append(_r("Flex carousel 空 contents 過濾", "critical", not dry_run))

    # 修復 4：reply token 過期時靜默略過，不記錄 error
    old_err = 'logger.error(f"Reply error: {resp.status_code} {resp.text}")'
    new_err = (
        'if resp.status_code == 400 and "Invalid reply token" in (resp.text or ""):\n'
        '            logger.warning("Reply token expired — reply skipped")\n'
        '        else:\n'
        '            logger.error(f"Reply error: {resp.status_code} {resp.text}")'
    )
    if old_err in src and "Reply token expired" not in src:
        src = src.replace(old_err, new_err, 1)
        changed = True
        results.append(_r("Reply token 過期靜默略過", "warning", not dry_run))

    if changed:
        _write(_HANDLER, src, dry_run)

    if not results:
        results.append(_r("LINE 400 偵測到但程式碼已是最新", "warning", True))

    return results


# ── 2. 字型缺失 ───────────────────────────────────────────────────────────────

_DOCKERFILE = _ROOT / "Dockerfile"
_FONT_PKG   = "fonts-noto-cjk"

def fix_font_missing(logs: str, dry_run: bool) -> list[dict]:
    src = _read(_DOCKERFILE)
    if not src:
        return [_r("字型修復", "warning", False, "Dockerfile 不存在")]

    if _FONT_PKG in src:
        return [_r("字型已安裝，無需修復", "minor", True, f"{_FONT_PKG} 已在 Dockerfile")]

    # 在 apt-get install 行末加上 fonts-noto-cjk
    old = re.search(r"(apt-get install -y[^\n]+)", src)
    if old:
        new_line = old.group(1).rstrip() + f" \\\n    {_FONT_PKG}"
        new_src = src.replace(old.group(1), new_line, 1)
        _write(_DOCKERFILE, new_src, dry_run)
        return [_r(f"Dockerfile 加入 {_FONT_PKG}", "warning", not dry_run)]

    return [_r("字型修復：找不到 apt-get install 行", "warning", False)]


# ── 3. API 302 端點失效 ───────────────────────────────────────────────────────

_TWSE_SVC = _ROOT / "backend" / "services" / "twse_service.py"

_ENDPOINT_PROBES = [
    ("TWSE_BASE", [
        "https://openapi.twse.com.tw/v1",
        "https://openapi.twse.com.tw/v2",
    ], "/exchangeReport/STOCK_DAY_ALL"),
    ("TPEX_BASE", [
        "https://www.tpex.org.tw/openapi/v1",
        "https://www.tpex.org.tw/openapi/v2",
    ], "/tpex_mainboard_daily_close_quotes"),
]

async def fix_api_302(logs: str, dry_run: bool) -> list[dict]:
    import httpx

    src = _read(_TWSE_SVC)
    if not src:
        return [_r("API 302 修復", "critical", False, "twse_service.py 不存在")]

    results: list[dict] = []
    changed = False

    async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
        for var, candidates, probe_path in _ENDPOINT_PROBES:
            cur_match = re.search(rf'{var}\s*=\s*"([^"]+)"', src)
            cur_url = cur_match.group(1) if cur_match else ""

            # 先確認現有端點是否正常
            if cur_url:
                try:
                    r = await client.get(cur_url + probe_path)
                    if r.status_code == 200:
                        print(f"    {var} 現有端點正常 ({cur_url})")
                        continue
                    print(f"    {var} 現有端點回應 {r.status_code}")
                except Exception as e:
                    print(f"    {var} 現有端點連線失敗：{e}")

            # 探測替代端點
            for url in candidates:
                if url == cur_url:
                    continue
                try:
                    r = await client.get(url + probe_path)
                    if r.status_code == 200:
                        if cur_url:
                            src = src.replace(f'{var} = "{cur_url}"', f'{var} = "{url}"')
                        changed = True
                        results.append(_r(
                            f"API 端點更新：{var}",
                            "critical",
                            not dry_run,
                            f"{cur_url} → {url}",
                        ))
                        break
                    elif r.status_code in (301, 302, 308):
                        loc = r.headers.get("location", "")
                        results.append(_r(
                            f"API 302 追蹤：{var}",
                            "warning",
                            False,
                            f"重定向至 {loc}，需人工確認",
                        ))
                except Exception:
                    pass

    if changed:
        _write(_TWSE_SVC, src, dry_run)

    if not results:
        results.append(_r("API 端點全數正常", "minor", True))

    return results


# ── 4. Import 錯誤 ────────────────────────────────────────────────────────────

_IMPORT_PAT = re.compile(
    r"ModuleNotFoundError: No module named '([^']+)'|"
    r"ImportError: cannot import name '([^']+)' from '([^']+)'"
)

def fix_import_error(logs: str, dry_run: bool) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()

    for m in _IMPORT_PAT.finditer(logs):
        module = (m.group(1) or m.group(2) or "").strip()
        if not module or module in seen:
            continue
        seen.add(module)

        parts = module.split(".")
        candidates = list(_ROOT.rglob(f"{parts[-1]}.py"))
        candidates = [c for c in candidates if "__pycache__" not in str(c)]

        if not candidates:
            results.append(_r(
                f"缺少套件：{module}",
                "critical",
                False,
                f"建議：pip install {parts[0]}",
            ))
            continue

        found = candidates[0]
        rel = found.parent.relative_to(_ROOT)
        insert = f'sys.path.insert(0, str(Path(__file__).parent / "{rel}"))\n'

        # 嘗試在 backend/main.py 加入 sys.path
        entry = _ROOT / "backend" / "main.py"
        if entry.exists():
            esrc = _read(entry)
            if insert not in esrc and "import sys" in esrc:
                new_esrc = esrc.replace(
                    "import sys\n",
                    f"import sys\nfrom pathlib import Path\n{insert}",
                    1,
                )
                _write(entry, new_esrc, dry_run)
                results.append(_r(
                    f"Import 路徑修復：{module}",
                    "critical",
                    not dry_run,
                    f"在 backend/main.py 加入 sys.path → {rel}",
                ))
                continue

        results.append(_r(
            f"Import 找到但需手動確認：{module}",
            "warning",
            False,
            f"找到 {found.relative_to(_ROOT)}",
        ))

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 規則表
# ══════════════════════════════════════════════════════════════════════════════

# pattern → (name, severity, sync_fix | async_fix)
# fix 函式簽名：(logs: str, dry_run: bool) -> list[dict]
# async fix 會被偵測並 await

_RULES: list[dict] = [
    {
        "id": "LINE_400",
        "pattern": re.compile(
            r"Reply error: 400|400.*?line.*?api|At least one block|"
            r"Invalid reply token|message quota exceeded",
            re.IGNORECASE,
        ),
        "name": "LINE 400 / Flex 錯誤",
        "severity": "critical",
        "fix": fix_line_400,
    },
    {
        "id": "FONT_MISSING",
        "pattern": re.compile(
            r"missing from font|cannot.*?open.*?font|font.*?not found|"
            r"Glyph.*?missing|FreeType.*?error",
            re.IGNORECASE,
        ),
        "name": "字型缺失",
        "severity": "warning",
        "fix": fix_font_missing,
    },
    {
        "id": "API_302",
        "pattern": re.compile(
            r"302 Moved|302 Found|Moved Permanently|"
            r"openapi\.twse.*?30[12]|tpex.*?30[12]",
            re.IGNORECASE,
        ),
        "name": "API 302 端點失效",
        "severity": "critical",
        "fix": fix_api_302,          # async
    },
    {
        "id": "IMPORT_ERROR",
        "pattern": re.compile(
            r"ModuleNotFoundError: No module named|"
            r"ImportError: cannot import name|"
            r"ImportError: No module",
            re.IGNORECASE,
        ),
        "name": "Import 錯誤",
        "severity": "critical",
        "fix": fix_import_error,
    },
    {
        "id": "SERVER_ERROR",
        "pattern": re.compile(
            r"\b(ERROR|CRITICAL)\b.*?(Exception|Error:)|"
            r"HTTP/[0-9.]+ 50[0-9]|status[_\s]?code.*?50[0-9]",
            re.IGNORECASE,
        ),
        "name": "伺服器 5xx 錯誤",
        "severity": "warning",
        "fix": None,                 # 只記錄，不修
    },
]


async def run_rules(logs: str, dry_run: bool) -> tuple[list[dict], list[str]]:
    """
    逐條比對規則，回傳：
      fixes    — 所有修復結果
      detected — 偵測到的規則 id 清單
    """
    fixes: list[dict] = []
    detected: list[str] = []

    if not logs:
        print("  (無 logs，規則引擎跳過)")
        return fixes, detected

    for rule in _RULES:
        hits = rule["pattern"].findall(logs)
        if not hits:
            continue

        rule_id: str = rule["id"]
        detected.append(rule_id)
        sample = next(
            (l.strip()[:100] for l in logs.splitlines() if rule["pattern"].search(l)),
            ""
        )
        print(f"  [{rule_id}] 偵測到 {len(hits)} 筆  → {sample}")

        fn: Callable | None = rule.get("fix")
        if fn is None:
            fixes.append(_r(f"偵測到：{rule['name']}", rule["severity"], False,
                            f"需人工確認（{len(hits)} 筆）"))
            continue

        # 判斷是否為 async 函式
        import inspect
        if inspect.iscoroutinefunction(fn):
            result = await fn(logs, dry_run)
        else:
            result = fn(logs, dry_run)

        fixes.extend(result)

    return fixes, detected


# ══════════════════════════════════════════════════════════════════════════════
# 健康測試
# ══════════════════════════════════════════════════════════════════════════════

_HEALTH_TESTS = [
    ("基本健康",  "GET",  "/health",             None),
    ("台積電報價", "GET",  "/api/quote/2330",      None),
    ("ETF 報價",  "GET",  "/api/quote/0050",       None),
    ("每日建議",  "GET",  "/api/advice/daily",     None),
    ("早報生成",  "POST", "/api/report/morning",   None),
]

# 週報額外測試
_WEEKLY_EXTRA_TESTS = [
    ("系統健康詳情", "GET",  "/health/detail",         None),
    ("週報生成",    "POST", "/api/report/weekly",      None),
]

async def run_health_tests(base_url: str, extra: bool = False) -> list[dict]:
    import httpx

    tests = _HEALTH_TESTS + (_WEEKLY_EXTRA_TESTS if extra else [])
    results: list[dict] = []

    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        for name, method, path, body in tests:
            url = base_url.rstrip("/") + path
            try:
                resp = await (client.get(url) if method == "GET"
                              else client.post(url, json=body))
                ok = resp.status_code < 400
                results.append({
                    "name": name,
                    "ok": ok,
                    "status": resp.status_code,
                    "detail": "" if ok else resp.text[:150],
                })
            except Exception as e:
                results.append({
                    "name": name,
                    "ok": False,
                    "status": 0,
                    "detail": str(e)[:150],
                })
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Railway logs
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_logs(lines: int = 800) -> str:
    token = os.getenv("RAILWAY_TOKEN", "")

    # 1. Railway CLI
    try:
        r = subprocess.run(
            ["railway", "logs", "--tail", str(lines)],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "RAILWAY_TOKEN": token},
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 2. Railway GraphQL API（需要 RAILWAY_TOKEN + PROJECT_ID + SERVICE_ID）
    project_id = os.getenv("RAILWAY_PROJECT_ID", "")
    service_id  = os.getenv("RAILWAY_SERVICE_ID", "")
    if token and project_id and service_id:
        try:
            import httpx
            query = """
            query($sid:String!,$limit:Int!){
              service(id:$sid){
                deployments(first:1){
                  edges{ node{ logs(limit:$limit){ edges{ node{ message timestamp }}}}}
                }
              }
            }
            """
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    "https://backboard.railway.app/graphql/v2",
                    json={"query": query, "variables": {"sid": service_id, "limit": lines}},
                    headers={"Authorization": f"Bearer {token}"},
                )
                data = resp.json()
                nodes = (
                    data.get("data", {})
                        .get("service", {})
                        .get("deployments", {})
                        .get("edges", [{}])[0]
                        .get("node", {})
                        .get("logs", {})
                        .get("edges", [])
                )
                return "\n".join(n["node"]["message"] for n in nodes if n.get("node"))
        except Exception as e:
            print(f"  [logs] GraphQL API 失敗：{e}")

    # 3. 本機 data/app.log
    log_file = _ROOT / "data" / "app.log"
    if log_file.exists():
        return log_file.read_text(encoding="utf-8", errors="ignore")[-60_000:]

    return ""


# ══════════════════════════════════════════════════════════════════════════════
# LINE 推播
# ══════════════════════════════════════════════════════════════════════════════

async def push_line(token: str, uid: str, text: str, dry_run: bool = False) -> None:
    if dry_run:
        print("[LINE] dry-run，跳過推播")
        return
    if not token or not uid:
        print("[LINE] 缺少 LINE_CHANNEL_ACCESS_TOKEN / ADMIN_LINE_UID")
        return

    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.line.me/v2/bot/message/push",
                json={"to": uid, "messages": [{"type": "text", "text": text[:4900]}]},
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
            )
        status = "成功" if resp.status_code == 200 else f"失敗 {resp.status_code}"
        print(f"[LINE] 推播{status}")
    except Exception as e:
        print(f"[LINE] 推播異常：{e}")


def _build_daily_report(
    fixes: list[dict],
    test_results: list[dict],
    detected: list[str],
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    all_ok = all(r["ok"] for r in test_results) if test_results else True
    any_unfixed = any(
        f.get("severity") == "critical" and not f.get("applied") for f in fixes
    )
    overall = "✅ 系統正常" if all_ok and not any_unfixed else "⚠️ 需要確認"

    lines = [f"🤖 自動維護 {now}", "─" * 16, f"【狀態】{overall}"]

    if fixes:
        lines += ["", "【修復項目】"]
        for f in fixes:
            icon = "✅" if f.get("applied") else ("⚠️" if f.get("severity") != "critical" else "❌")
            lines.append(f"  {icon} {f['title']}")
            if f.get("detail"):
                lines.append(f"     {f['detail'][:60]}")
    else:
        lines += ["", "【修復項目】", "  ✅ 無需修復"]

    if test_results:
        lines += ["", "【健康測試】"]
        for r in test_results:
            icon = "✅" if r["ok"] else "❌"
            suffix = f"（{r['status']}）" if not r["ok"] else ""
            lines.append(f"  {icon} {r['name']}{suffix}")

    return "\n".join(lines)


def _build_weekly_report(
    fixes: list[dict],
    test_results: list[dict],
) -> str:
    now = datetime.now().strftime("%Y-%m-%d")
    passed = sum(1 for r in test_results if r["ok"])
    total  = len(test_results)

    lines = [
        f"📊 每週完整測試 {now}",
        "─" * 16,
        f"測試通過：{passed}/{total}",
        "",
        "【端點測試】",
    ]
    for r in test_results:
        icon = "✅" if r["ok"] else "❌"
        detail = f" → {r['detail'][:60]}" if not r["ok"] else ""
        lines.append(f"  {icon} {r['name']} ({r['status']}){detail}")

    if fixes:
        lines += ["", "【本週修復】"]
        for f in fixes:
            icon = "✅" if f.get("applied") else "⚠️"
            lines.append(f"  {icon} {f['title']}")

    lines += ["", "─" * 16, "下週日 03:00 再見 🤖"]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════════

async def main(
    dry_run: bool = False,
    test_only: bool = False,
    weekly: bool = False,
) -> int:
    """
    exit 0 = 無程式碼變更
    exit 1 = 腳本執行錯誤
    exit 2 = 已套用修復（CI 據此 commit）
    """
    line_token  = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    admin_uid   = os.getenv("ADMIN_LINE_UID", "")
    backend_url = os.getenv("RAILWAY_BACKEND_URL", "").rstrip("/")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    flags = " ".join(f for f, v in [("dry-run", dry_run), ("test-only", test_only),
                                     ("weekly", weekly)] if v)
    print(f"[auto_maintain v3] {now_str} {flags}".rstrip())

    fixes:    list[dict] = []
    detected: list[str]  = []
    applied   = 0

    # ── 步驟 1：抓 logs ───────────────────────────────────────────────
    if not test_only:
        print("\n[1/4] 取得 Railway logs...")
        logs = await fetch_logs()
        print(f"  {'取得' if logs else '無可用'} logs"
              + (f"（{len(logs):,} 字元）" if logs else ""))

        # ── 步驟 2：規則引擎 ─────────────────────────────────────────
        print("\n[2/4] 規則引擎掃描...")
        fixes, detected = await run_rules(logs, dry_run)

        n_applied = sum(1 for f in fixes if f.get("applied") and
                        f.get("severity") in ("critical", "warning"))
        applied = n_applied
        print(f"  偵測規則：{len(detected)} 條｜修復：{n_applied} 項")

    # ── 步驟 3：健康 / 週報測試 ──────────────────────────────────────
    test_results: list[dict] = []
    if backend_url:
        step = "[3/4]" if not test_only else "[1/2]"
        extra = weekly or test_only
        label = "週報完整測試" if extra else "健康測試"
        print(f"\n{step} {label}...")
        test_results = await run_health_tests(backend_url, extra=extra)
        for r in test_results:
            print(f"  {'✅' if r['ok'] else '❌'} {r['name']} ({r['status']})"
                  + (f" — {r['detail'][:80]}" if not r["ok"] else ""))
    else:
        print("\n[3/4] RAILWAY_BACKEND_URL 未設定，跳過測試")

    # 寫 CI summary
    summary_lines: list[str] = []
    if fixes:
        summary_lines += ["=== 規則修復 ==="] + [
            f"{'✅' if f.get('applied') else '⚠️'} [{f['severity']}] {f['title']}"
            for f in fixes
        ]
    if test_results:
        summary_lines += ["\n=== 健康測試 ==="] + [
            f"{'✅' if r['ok'] else '❌'} {r['name']} ({r['status']})"
            for r in test_results
        ]
    if summary_lines:
        sp = _ROOT / "data" / "ci_fix_summary.txt"
        sp.parent.mkdir(exist_ok=True)
        sp.write_text("\n".join(summary_lines), encoding="utf-8")

    # ── 步驟 4：推播 LINE ────────────────────────────────────────────
    print("\n[4/4] 推播 LINE 報告...")
    if weekly or test_only:
        msg = _build_weekly_report(fixes, test_results)
    else:
        msg = _build_daily_report(fixes, test_results, detected)

    await push_line(line_token, admin_uid, msg, dry_run)

    # ── 結束 ─────────────────────────────────────────────────────────
    if applied > 0:
        print(f"\n[完成] 套用了 {applied} 個修復（exit 2）")
        return 2

    all_ok = all(r["ok"] for r in test_results) if test_results else True
    print(f"\n[完成] {'系統正常' if all_ok else '有測試失敗，請確認'}（exit 0）")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="規則式自動維護 v3（無 Claude API）")
    ap.add_argument("--dry-run",   action="store_true")
    ap.add_argument("--test-only", action="store_true")
    ap.add_argument("--weekly",    action="store_true")
    args = ap.parse_args()

    sys.exit(asyncio.run(main(
        dry_run=args.dry_run,
        test_only=args.test_only,
        weekly=args.weekly,
    )))
