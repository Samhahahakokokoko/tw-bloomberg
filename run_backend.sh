#!/usr/bin/env bash
# 啟動 FastAPI 後端
cd "$(dirname "$0")"
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
