"""
line_agent.py — triggered by repository_dispatch (line-agent event) from LINE Bot /agent command.

Usage: python scripts/line_agent.py "<task>" "<user_id>"
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime

import httpx

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
LINE_TOKEN         = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
RAILWAY_TOKEN      = os.environ.get("RAILWAY_TOKEN", "")
RAILWAY_PROJECT_ID = os.environ.get("RAILWAY_PROJECT_ID", "")
RAILWAY_SERVICE_ID = os.environ.get("RAILWAY_SERVICE_ID", "")
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO        = os.environ.get("GITHUB_REPOSITORY", "Samhahahakokokoko/tw-bloomberg")


async def fetch_railway_logs(lines: int = 150) -> str:
    """Fetch recent Railway deployment logs."""
    # Try Railway CLI first
    try:
        env = {**os.environ, "RAILWAY_TOKEN": RAILWAY_TOKEN}
        result = subprocess.run(
            ["railway", "logs", "--limit", str(lines)],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout[-8000:]
    except Exception:
        pass

    # Fallback: Railway GraphQL API
    if not RAILWAY_TOKEN or not RAILWAY_PROJECT_ID:
        return "（未設定 RAILWAY_TOKEN/RAILWAY_PROJECT_ID，略過日誌抓取）"
    try:
        gql = """
        query($projectId: String!, $limit: Int!) {
          project(id: $projectId) {
            deployments(first: 1) {
              edges { node { logs(limit: $limit) { message timestamp } } }
            }
          }
        }"""
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://backboard.railway.app/graphql/v2",
                headers={
                    "Authorization": f"Bearer {RAILWAY_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={"query": gql, "variables": {"projectId": RAILWAY_PROJECT_ID, "limit": lines}},
            )
            data = resp.json()
            logs = (
                data.get("data", {})
                    .get("project", {})
                    .get("deployments", {})
                    .get("edges", [{}])[0]
                    .get("node", {})
                    .get("logs", [])
            )
            return "\n".join(f"[{l.get('timestamp','')}] {l.get('message','')}" for l in logs)[-8000:]
    except Exception as e:
        return f"（Railway API 取得失敗：{e}）"


async def ask_claude(task: str, logs: str) -> str:
    """Ask Claude to analyze the task + logs and return a LINE-friendly response."""
    if not ANTHROPIC_API_KEY:
        return "（未設定 ANTHROPIC_API_KEY，無法執行 AI 分析）"

    prompt = f"""你是 tw-bloomberg 台股 AI 交易機器人的維護工程師。

用戶任務：{task}

最近 Railway 部署日誌（最後 8000 字）：
{logs[:5000]}

請：
1. 分析日誌中是否有錯誤或警告
2. 針對用戶任務給出具體建議或修復方案
3. 如果確定需要修改程式碼，用以下格式輸出（否則省略）：

<fix file="相對路徑">
修改後的完整函式或程式碼片段
</fix>

回覆用繁體中文，長度控制在 400 字以內，格式適合 LINE 訊息顯示。"""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-opus-4-7",
                    "max_tokens": 2048,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            data = resp.json()
            return data["content"][0]["text"]
    except Exception as e:
        return f"（Claude API 呼叫失敗：{e}）"


def apply_fixes(ai_response: str) -> list[str]:
    """Parse <fix file="...">...</fix> blocks and apply them. Returns list of changed files."""
    import re
    changed = []
    pattern = re.compile(r'<fix file="([^"]+)">(.*?)</fix>', re.DOTALL)
    for match in pattern.finditer(ai_response):
        path, code = match.group(1).strip(), match.group(2).strip()
        if not path or ".." in path:
            continue
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(code)
            changed.append(path)
            print(f"[line_agent] applied fix → {path}")
        except Exception as e:
            print(f"[line_agent] failed to write {path}: {e}")
    return changed


async def push_line(user_id: str, text: str) -> None:
    if not LINE_TOKEN or not user_id:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                "https://api.line.me/v2/bot/message/push",
                headers={
                    "Authorization": f"Bearer {LINE_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "to": user_id,
                    "messages": [{"type": "text", "text": text[:4900]}],
                },
            )
    except Exception as e:
        print(f"[line_agent] LINE push failed: {e}")


async def main() -> None:
    task    = sys.argv[1] if len(sys.argv) > 1 else "分析系統狀態與近期錯誤"
    user_id = sys.argv[2] if len(sys.argv) > 2 else ""

    ts = datetime.now().strftime("%H:%M")
    print(f"[line_agent] {ts} 任務：{task}  回報：{user_id or '(無)'}")

    logs    = await fetch_railway_logs()
    result  = await ask_claude(task, logs)
    changed = apply_fixes(result)

    # Build reply
    status_line = ""
    if changed:
        status_line = f"\n\n✅ 已自動修復 {len(changed)} 個檔案：\n" + "\n".join(f"  • {f}" for f in changed)
    else:
        status_line = "\n\n（無需修改程式碼）"

    msg = (
        f"🤖 LINE Agent 完成\n"
        f"任務：{task}\n"
        f"時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"{'─'*20}\n"
        f"{result[:3500]}"
        f"{status_line}"
    )

    print(f"\n{'='*40}\n{msg}\n{'='*40}")
    if user_id:
        await push_line(user_id, msg)
        print(f"[line_agent] 已推送結果給 {user_id}")


if __name__ == "__main__":
    asyncio.run(main())
