#!/usr/bin/env bash
# PostToolUse asyncRewake hook: runs after git push (filtered by settings.json `if` rule)
# 1. Waits for Railway deployment
# 2. Tests /health endpoint
# 3. Exits 2 on failure → wakes Claude to auto-fix and re-push

# Wait for Railway deployment (~3 min); set SKIP_SLEEP=1 to bypass in tests
if [ "${SKIP_SLEEP:-0}" != "1" ]; then
  sleep 180
fi

URL="${RAILWAY_BACKEND_URL:-}"
if [ -z "$URL" ]; then
  echo '{"systemMessage": "⚠️ RAILWAY_BACKEND_URL 未設定，跳過健康檢查。請在環境變數加入 RAILWAY_BACKEND_URL=https://your-app.railway.app"}'
  exit 0
fi

# Test /health endpoint
if resp=$(curl -sf --max-time 30 "${URL}/health" 2>&1); then
  echo '{"systemMessage": "Railway 部署完成 ✅ /health 端點正常"}'
  exit 0
else
  # Exit 2 → asyncRewake wakes Claude to investigate and auto-fix
  short=$(printf '%s' "$resp" | tr '\n' ' ' | cut -c1-300)
  printf '{"systemMessage": "Railway /health 失敗 ❌\n錯誤：%s\n\n建議：執行 python auto_improve.py 分析 Railway logs，修復後重新 git push"}\n' "$short"
  exit 2
fi
