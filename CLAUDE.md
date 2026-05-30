# tw-bloomberg — 台股 AI 量化交易系統

## 專案概覽

這是一套整合台灣股市資料、量化分析引擎與 LINE Bot 推播的自動交易輔助系統，部署於 Railway 雲端平台。

## 技術架構

| 層級 | 技術 | 位置 |
|------|------|------|
| API 後端 | FastAPI + Uvicorn | `backend/` |
| 資料庫 | PostgreSQL（Railway）/ SQLite（本機） | `backend/models/` |
| LINE Bot | LINE Messaging API v3 | `line_webhook/handler.py` |
| 量化引擎 | 自研多因子模型 | `quant/` |
| 排程 | APScheduler | `backend/utils/scheduler.py` |
| 部署 | Railway（Docker） | `railway.toml`, `Dockerfile` |

## 目錄結構

```
tw-bloomberg/
├── backend/
│   ├── main.py                  # FastAPI 應用程式入口
│   ├── api/routes.py            # 所有 REST 端點
│   ├── models/database.py       # DB 連線與初始化
│   ├── services/                # 業務邏輯（73 個服務）
│   │   ├── twse_service.py      # 台股報價抓取（TWSE/TPEX API）
│   │   ├── line_push.py         # LINE 推播服務
│   │   ├── fix_engine.py        # 自動修復引擎（Railway logs + Claude）
│   │   └── morning_report.py    # 早報生成
│   └── utils/scheduler.py       # 背景排程（APScheduler）
├── line_webhook/
│   ├── handler.py               # 主 Webhook 處理器（訊息路由）
│   ├── callback_router.py       # Postback 回調路由
│   └── flex_messages.py         # Flex 訊息模板
├── quant/                       # 53 個量化分析引擎
│   ├── decision_engine.py       # 交易決策
│   ├── risk_kill_switch.py      # 風控熔斷
│   └── system_health_dashboard.py  # 系統健康度監控
├── scripts/
│   └── ci_maintain.py           # CI 維護腳本（被 auto_maintain.yml 呼叫）
├── auto_maintain.py             # 本機互動式自動維護
├── auto_improve.py              # 本機互動式自動改善（需人工確認）
└── .github/workflows/
    ├── auto_maintain.yml        # 每日 03:00 自動維護 + LINE 報告
    └── auto_test.yml            # 每次 push 自動測試
```

## 關鍵端點

| 用途 | 方法 | 路徑 |
|------|------|------|
| 系統健康檢查 | GET | `/api/system/health` |
| 股票報價 | GET | `/api/quote/{stock_code}` |
| 每日建議 | GET | `/api/advice/daily` |
| 早報生成 | POST | `/api/report/morning` |
| LINE Webhook | POST | `/webhook` |

## 自動維護優先順序

遇到問題時，請依下列順序優先修復：

1. **LINE Bot 回應錯誤** — handler.py 異常、postback 失敗、推播逾時
2. **API 失效** — TWSE/TPEX 報價 API 格式變更、httpx 逾時
3. **資料庫問題** — SQLAlchemy 連線失敗、migration 錯誤、async session 洩漏
4. **排程失效** — APScheduler job 未觸發、scheduler 重啟失敗
5. **量化引擎錯誤** — quant/ 模組 import 失敗、計算結果 NaN

## 測試清單

在做任何變更後，必須依序確認以下測試通過：

```bash
# 1. 系統健康
curl -s $RAILWAY_BACKEND_URL/api/system/health | python -m json.tool

# 2. 台積電報價
curl -s $RAILWAY_BACKEND_URL/api/quote/2330

# 3. 每日建議
curl -s $RAILWAY_BACKEND_URL/api/advice/daily

# 4. 早報生成
curl -s -X POST $RAILWAY_BACKEND_URL/api/report/morning
```

所有測試均需回傳 HTTP 200，不得有 500 錯誤或空回應。

## 禁止修改的檔案

以下檔案絕對不能動：

- `.env` — 正式環境密鑰，僅能透過 Railway 環境變數管理
- `data/bloomberg.db` — 本機 SQLite 資料，誤改會遺失歷史資料

## 環境變數

| 變數名稱 | 用途 |
|----------|------|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Bot 推播 token |
| `LINE_CHANNEL_SECRET` | LINE Webhook 驗簽密鑰 |
| `ANTHROPIC_API_KEY` | Claude AI API |
| `ADMIN_LINE_UID` | 接收維護報告的 LINE 使用者 ID |
| `RAILWAY_TOKEN` | Railway CLI/API 認證 |
| `RAILWAY_PROJECT_ID` | Railway 專案 ID |
| `RAILWAY_SERVICE_ID` | Railway 服務 ID |
| `RAILWAY_BACKEND_URL` | 部署後的 HTTPS 位址 |
| `DATABASE_URL` | PostgreSQL 連線字串（Railway 自動注入） |
| `ADMIN_API_TOKEN` | 後端管理 API 認證 |

## 自動維護系統

### auto_maintain.yml（每日 03:00 台灣時間）
1. 抓取 Railway 部署日誌
2. 用 Claude 分析錯誤並產生修復 patch
3. 執行語法測試
4. 有修復時建立 PR → 測試通過自動 merge
5. 推送維護報告到管理員 LINE

### auto_test.yml（每次 push）
1. 等待 Railway 部署完成
2. 測試 `/api/system/health` 端點
3. 測試 `/api/quote/2330` 股票報價
4. 結果回報至 GitHub Actions summary

## 常見問題

**Q: TWSE API 504/timeout**
A: `twse_service.py` 有多層 fallback（TWSE → TPEX → cache），先確認 `_BASE_TWSE` 域名可連線。

**Q: LINE push 失敗（401）**
A: `LINE_CHANNEL_ACCESS_TOKEN` 過期，需在 LINE Developers Console 重新生成。

**Q: Railway 部署後 DB migration 失敗**
A: 確認 `DATABASE_URL` 指向正確的 PostgreSQL，並手動執行 `alembic upgrade head`。

**Q: APScheduler job 沒有觸發**
A: 檢查 `backend/utils/scheduler.py` 中 timezone 設定是否為 `Asia/Taipei`。
