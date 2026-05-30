"""
fix_engine.py — 自動修復引擎

職責：
  - 讀取 Railway logs（CLI 或 API）
  - 呼叫 Claude 分析錯誤、產生修復計劃（JSON）
  - 儲存待確認的計劃到 data/pending_fixes.json
  - 套用已確認的修復（patch 檔案）
  - git commit + push
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

_PLAN_PATH = Path(__file__).parent.parent.parent / "data" / "pending_fixes.json"
_PROJECT_ROOT = Path(__file__).parent.parent.parent


# ── Railway logs ─────────────────────────────────────────────────────────────

async def fetch_railway_logs(lines: int = 500) -> str:
    """嘗試用 CLI 取得 Railway logs；失敗時用 API。"""
    token = os.getenv("RAILWAY_TOKEN", "")

    # 1. Railway CLI
    try:
        result = subprocess.run(
            ["railway", "logs", "--tail", str(lines)],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "RAILWAY_TOKEN": token},
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.info(f"[FixEngine] Railway CLI logs: {len(result.stdout)} chars")
            return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 2. Railway GraphQL API
    if token:
        try:
            import httpx
            project_id = os.getenv("RAILWAY_PROJECT_ID", "")
            service_id = os.getenv("RAILWAY_SERVICE_ID", "")
            if project_id and service_id:
                query = """
                query getLogs($serviceId: String!, $limit: Int!) {
                  service(id: $serviceId) {
                    logs(limit: $limit) {
                      timestamp
                      message
                      severity
                    }
                  }
                }
                """
                async with httpx.AsyncClient(timeout=20) as c:
                    resp = await c.post(
                        "https://backboard.railway.app/graphql/v2",
                        json={"query": query, "variables": {"serviceId": service_id, "limit": lines}},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    data = resp.json()
                    logs = data.get("data", {}).get("service", {}).get("logs", [])
                    text = "\n".join(f"[{l['timestamp']}] {l['message']}" for l in logs)
                    logger.info(f"[FixEngine] Railway API logs: {len(text)} chars")
                    return text
        except Exception as e:
            logger.warning(f"[FixEngine] Railway API failed: {e}")

    return ""


# ── 錯誤解析 ─────────────────────────────────────────────────────────────────

def parse_errors(logs: str) -> list[dict[str, str]]:
    """從 log 文字中提取錯誤 traceback 和 ERROR 行。"""
    errors: list[dict[str, str]] = []
    seen: set[str] = set()

    # 擷取 Python traceback 區塊
    tb_pattern = re.compile(
        r"(Traceback \(most recent call last\).*?)(?=\n\n|\nTraceback|\Z)",
        re.DOTALL,
    )
    for m in tb_pattern.finditer(logs):
        block = m.group(1).strip()
        # 取最後一行作為 key（錯誤類型行）
        key = block.splitlines()[-1][:120]
        if key not in seen:
            seen.add(key)
            errors.append({"type": "traceback", "content": block[-2000:]})

    # 擷取 ERROR / CRITICAL 行
    for line in logs.splitlines():
        if re.search(r"\b(ERROR|CRITICAL|Exception|Error:)\b", line):
            key = line.strip()[:120]
            if key not in seen:
                seen.add(key)
                errors.append({"type": "error_line", "content": line.strip()})

    return errors[:30]  # 最多 30 筆，避免 Claude prompt 過大


# ── Claude 分析 ──────────────────────────────────────────────────────────────

async def analyze_with_claude(errors: list[dict], api_key: str) -> list[dict[str, Any]]:
    """
    傳送錯誤給 Claude，取得結構化修復計劃。
    回傳格式：[{id, severity, title, file_path, description, patch}]
    """
    if not errors:
        return []

    import anthropic

    error_text = "\n\n---\n\n".join(
        f"[{i+1}] ({e['type']})\n{e['content']}" for i, e in enumerate(errors)
    )

    system = textwrap.dedent("""
        你是一個 Python/FastAPI 後端工程師，專門分析 Railway 部署日誌中的錯誤並提出修復方案。

        回應格式必須是合法 JSON 陣列，每個元素包含：
        {
          "id": 1,
          "severity": "critical|warning|minor",
          "title": "簡短錯誤標題（中文，30字以內）",
          "file_path": "相對路徑，如 backend/services/foo.py（若無法確定則留空）",
          "description": "問題說明（中文，100字以內）",
          "patch": "unified diff 格式的修復，或若只需要設定/環境變數則說明步驟（300字以內）"
        }

        規則：
        - 只列出有明確修復方案的問題
        - 若是環境設定問題（缺少 env var），patch 欄填「設定 ENV_VAR=xxx」
        - 若是版本/依賴問題，patch 欄填「pip install xxx==x.x.x」
        - 若是程式邏輯錯誤，patch 欄填標準 unified diff（--- a/xxx +++b/xxx）
        - 最多回傳 5 個最重要的問題
        - 直接回傳 JSON 陣列，不要有任何其他文字
    """).strip()

    client = anthropic.AsyncAnthropic(api_key=api_key)
    message = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": f"以下是 Railway 日誌中的錯誤：\n\n{error_text}"}],
    )

    raw = message.content[0].text.strip()
    # 移除可能的 markdown code block
    raw = re.sub(r"^```(?:json)?\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)

    fixes = json.loads(raw)
    logger.info(f"[FixEngine] Claude 產生 {len(fixes)} 個修復方案")
    return fixes


# ── 儲存 / 讀取計劃 ──────────────────────────────────────────────────────────

def save_plan(fixes: list[dict], log_snippet: str = "") -> None:
    _PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    plan = {
        "generated_at": datetime.now().isoformat(),
        "log_snippet": log_snippet[-500:],
        "fixes": fixes,
        "applied": [],
    }
    _PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"[FixEngine] 計劃已儲存至 {_PLAN_PATH}")


def load_plan() -> dict | None:
    if not _PLAN_PATH.exists():
        return None
    try:
        return json.loads(_PLAN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_plan() -> None:
    if _PLAN_PATH.exists():
        _PLAN_PATH.unlink()


# ── 套用修復 ─────────────────────────────────────────────────────────────────

def apply_patch(patch_text: str, file_path: str) -> tuple[bool, str]:
    """
    嘗試套用 unified diff patch。
    若 patch 欄是說明文字（非 diff 格式），直接回傳說明讓使用者手動處理。
    """
    if not patch_text.startswith("---"):
        # 非 diff 格式 → 說明文字（如環境變數、pip install）
        return True, f"[手動處理] {patch_text}"

    try:
        # 寫入暫存 patch 檔
        patch_file = _PROJECT_ROOT / "data" / "_tmp.patch"
        patch_file.write_text(patch_text, encoding="utf-8")

        result = subprocess.run(
            ["git", "apply", "--check", str(patch_file)],
            cwd=_PROJECT_ROOT, capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False, f"patch 驗證失敗：{result.stderr[:200]}"

        subprocess.run(
            ["git", "apply", str(patch_file)],
            cwd=_PROJECT_ROOT, capture_output=True, text=True, check=True,
        )
        patch_file.unlink(missing_ok=True)
        return True, f"已套用 {file_path}"
    except Exception as e:
        return False, f"套用失敗：{e}"


def git_commit_and_push(message: str) -> tuple[bool, str]:
    """git add -A → commit → push"""
    try:
        subprocess.run(["git", "add", "-A"], cwd=_PROJECT_ROOT, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=_PROJECT_ROOT, capture_output=True, text=True,
        )
        if result.returncode != 0:
            if "nothing to commit" in result.stdout + result.stderr:
                return True, "無變更需提交"
            return False, result.stderr[:300]

        push = subprocess.run(
            ["git", "push"],
            cwd=_PROJECT_ROOT, capture_output=True, text=True,
        )
        if push.returncode != 0:
            return False, f"push 失敗：{push.stderr[:300]}"

        return True, "git push 成功"
    except Exception as e:
        return False, str(e)


# ── 執行修復計劃 ─────────────────────────────────────────────────────────────

async def execute_fixes(fix_ids: list[int] | None = None) -> dict[str, Any]:
    """
    套用 pending_fixes.json 中的修復。
    fix_ids=None 表示全部執行；否則只執行指定 id。
    回傳結果摘要。
    """
    plan = load_plan()
    if not plan:
        return {"ok": False, "message": "沒有待確認的修復計劃，請先執行 auto_improve.py"}

    fixes = plan["fixes"]
    if fix_ids:
        fixes = [f for f in fixes if f["id"] in fix_ids]

    results = []
    applied_ids = []

    for fix in fixes:
        ok, msg = apply_patch(fix.get("patch", ""), fix.get("file_path", ""))
        results.append({
            "id": fix["id"],
            "title": fix["title"],
            "ok": ok,
            "message": msg,
        })
        if ok:
            applied_ids.append(fix["id"])

    # git commit & push
    commit_ok, commit_msg = True, "無修復套用"
    if applied_ids:
        titles = "；".join(f["title"] for f in fixes if f["id"] in applied_ids)
        commit_ok, commit_msg = git_commit_and_push(f"fix: auto-improve #{','.join(map(str, applied_ids))} {titles[:60]}")

    # 更新計劃已套用記錄
    if applied_ids:
        plan["applied"].extend(applied_ids)
        plan["applied_at"] = datetime.now().isoformat()
        _PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": commit_ok,
        "results": results,
        "commit_message": commit_msg,
        "applied_count": len(applied_ids),
        "total": len(fixes),
    }


# ── LINE 摘要格式 ─────────────────────────────────────────────────────────────

def format_plan_for_line(fixes: list[dict]) -> str:
    if not fixes:
        return "今日掃描未發現需要修復的問題 ✅"

    sev_icon = {"critical": "🔴", "warning": "🟡", "minor": "🔵"}
    lines = [
        "🤖 Auto-Improve 修復計劃",
        f"{'─' * 22}",
        f"發現 {len(fixes)} 個問題，請確認後回覆：",
        "",
        "全部執行 → 回覆「執行」",
        "部分執行 → 回覆「執行 1 3」",
        "",
    ]
    for f in fixes:
        icon = sev_icon.get(f.get("severity", "minor"), "⚪")
        lines.append(f"{icon} [{f['id']}] {f['title']}")
        lines.append(f"   {f['description'][:60]}")
        if f.get("file_path"):
            lines.append(f"   📄 {f['file_path']}")
        lines.append("")

    lines.append(f"生成時間：{datetime.now().strftime('%m/%d %H:%M')}")
    return "\n".join(lines)


def format_result_for_line(result: dict) -> str:
    lines = [
        "🔧 修復執行結果",
        f"{'─' * 22}",
        f"套用：{result['applied_count']}/{result['total']} 個",
        f"Git：{result['commit_message']}",
        "",
    ]
    for r in result.get("results", []):
        mark = "✅" if r["ok"] else "❌"
        lines.append(f"{mark} [{r['id']}] {r['title']}")
        lines.append(f"   {r['message'][:60]}")
    return "\n".join(lines)
