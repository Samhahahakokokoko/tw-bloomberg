#!/usr/bin/env bash
# 啟動 LINE Bot (另開 port 8001)
cd "$(dirname "$0")"
python -m uvicorn line_webhook.handler:app --host 0.0.0.0 --port 8001 --reload
