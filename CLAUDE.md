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
│   ├── main.py                  # FastAPI 應用程式入口（所有路由掛載點）
│   ├── api/routes.py            # 所有 REST 端點
│   ├── models/database.py       # DB 連線與初始化（SQLAlchemy async）
│   ├── services/                # 業務邏輯（77 個服務）
│   └── utils/scheduler.py       # 背景排程（APScheduler，25+ jobs）
├── line_webhook/
│   ├── handler.py               # 主 Webhook 處理器（113 個指令路由）
│   ├── callback_router.py       # Postback 回調路由
│   └── flex_messages.py         # Flex 訊息模板
├── quant/                       # 58 個量化分析引擎
├── scripts/
│   ├── ci_maintain.py           # Claude API 分析 + patch（auto_maintain.yml）
│   ├── ci_daily_enhance.py      # 規則式修復（daily_enhance.yml）
│   ├── ci_weekly_enhance.py     # 週報健康檢查（weekly_enhance.yml）
│   ├── line_agent.py            # LINE /agent 指令（line_agent.yml）
│   ├── health_check.py          # 端點 + 系統健康全檢（self_improve.yml）
│   ├── auto_fix.py              # 規則式自動修復引擎（self_improve.yml）
│   └── performance_analyzer.py  # 每週效能分析 + LINE 報告
├── auto_maintain.py             # 本機互動式自動維護
├── auto_improve.py              # 本機互動式自動改善（需人工確認）
└── .github/workflows/
    ├── self_improve.yml         # 每日 02:00 自強化 + 健康檢查
    ├── auto_maintain.yml        # 每日 03:00 Claude 分析修復
    ├── daily_enhance.yml        # 每日 04:00 規則式強化
    ├── weekly_enhance.yml       # 每週日 05:00 深度週報
    ├── auto_test.yml            # 每次 push 語法 + 端點測試
    └── line_agent.yml           # LINE /agent 觸發
```

## 關鍵 API 端點

| 用途 | 方法 | 路徑 |
|------|------|------|
| 基本健康檢查 | GET | `/health` |
| 系統健康儀表板 | GET | `/api/system/health` |
| 股票報價 | GET | `/api/quote/{stock_code}` |
| K 線資料 | GET | `/api/quote/{stock_code}/kline` |
| 法人買賣 | GET | `/api/quote/{stock_code}/institutional` |
| 估值資料 | GET | `/api/quote/{stock_code}/valuation` |
| 每日建議 | GET | `/api/advice/daily` |
| 早報生成 | POST | `/api/report/morning` |
| 投資組合 | GET/POST | `/api/portfolio` |
| 警報管理 | GET/POST | `/api/alerts` |
| 新聞 | GET | `/api/news` |
| AI 問答 | POST | `/api/ai/ask` |
| 除權息資料 | GET | `/api/dividend/{stock_code}` |
| 融資融券 | GET | `/api/margin/{stock_code}` |
| LINE Webhook | POST | `/webhook` |

## 核心服務模組（backend/services/）

| 服務 | 功能 |
|------|------|
| `twse_service.py` | 台股報價抓取（TWSE → TPEX → cache 三層 fallback） |
| `line_push.py` | LINE Bot 推播（push/reply/broadcast） |
| `fix_engine.py` | 自動修復引擎（Railway logs → Claude API → patch） |
| `morning_report.py` | 早報生成（市場概況 + AI 分析） |
| `etf_service.py` | ETF 分析、比較、定期定額試算 |
| `backup_service.py` | PostgreSQL 備份到 Google Drive |
| `system_monitor.py` | 系統健康度監控 + 警報 |
| `data_pipeline.py` | 資料抓取 pipeline（股價、財報、籌碼） |
| `ai_decision_agent.py` | AI 交易決策代理 |
| `performance_service.py` | 績效分析與追蹤 |
| `portfolio_manager.py` | 投資組合管理 |
| `chart_service.py` | 技術線圖生成 |
| `news_pipeline.py` | 新聞抓取 + 情緒分析 |
| `stock_favorites.py` | 自選股管理 |
| `analyst_*.py` (8個) | 分析師評級追蹤與共識分析 |
| `self_optimizer.py` | 系統自我優化建議引擎 |

## 量化引擎模組（quant/）

| 模組 | 功能 |
|------|------|
| `decision_engine.py` | 最終交易決策整合 |
| `risk_kill_switch.py` | 風控熔斷（市場異常時停止交易） |
| `system_health_dashboard.py` | 系統健康儀表板資料 |
| `backtest.py` / `walkforward.py` | 回測 + 滾動驗證 |
| `alpha_model.py` | 多因子 Alpha 模型 |
| `regime.py` | 市場狀態（多頭/空頭/震盪）偵測 |
| `sentiment.py` | 市場情緒分析 |
| `risk.py` | 風險計算（VaR、Beta、Sharpe） |
| `capital_flow.py` | 資金流向分析 |
| `sector_rotation.py` | 產業輪動策略 |
| `meta_alpha.py` | Meta 學習 Alpha 權重調整 |
| `prediction_market.py` | 預測市場機制 |

## LINE Bot 指令分類（handler.py）

**報價與分析**：股票代號、報價、ai分析、pe、估值、法人、融資、比較

**投資組合**：portfolio、buy、sell、stops、績效、持倉、history、rebalance、optimize、VAR、correlation

**選股與策略**：screener、strategy、backtest、custom_screen、rs、breadth、movers、theme

**早報與週報**：早報、weekly、report、morning、timeline

**自選股**：自選 add/del/list、watchlist、favorites

**分析師**：analyst today/list/ranking/add/remove、consensus、debate、predict

**風控**：risk_report、euphoria、stress、drift、footprint

**系統管理**：system_health、agent、adduser、removeuser、userlist、feedback

## 排程工作（scheduler.py，25+ jobs）

| 時間 | 工作 |
|------|------|
| 08:15 週一至五 | 財報提醒 |
| 08:30 週一至五 | 早報 + 晨間選股 |
| 08:31 週一至五 | AI Feed 更新 |
| 09:00–13:00 每 3 分鐘 | 警報檢查 |
| 09:00–13:00 每 15 分鐘 | 市場廣度更新 |
| 09:00–13:00 每 30 分鐘 | 即時新聞 + 智慧警報 |
| 08:00–18:00 每 30 分鐘 | 新聞爬取 |
| 14:00 週一至五 | 績效快照 |
| 15:00 週一至五 | 週五摘要 + 選股建議 |
| 15:30 週一至五 | 推薦回填 |
| 16:00 週一至五 | YouTube Alpha 抓取 |
| 16:30 週一至五 | 分析師警報 |
| 18:00–19:00 週一至五 | Alpha Pipeline（多步驟） |
| 19:00 週一至五 | 投資組合 Overlay + Watchlist 每日更新 |
| 19:30 週一至五 | 每日建議 + 決策 + 群組報告 |
| 20:00 週一至五 | 產業情緒更新 |
| 07:00 週一至五 | 除權息資料刷新 |
| 08:00 每週一 | 評分權重更新 |
| 22:00 每週日 | 特徵權重調整 |
| 1 日 08:00 | 月報生成 |

## 自動維護與自強化架構

### self_improve.yml（每日 02:00 台灣時間）
1. 執行 `scripts/health_check.py` — 端點 + 系統全面檢查
2. 執行 `scripts/auto_fix.py` — 規則式自動修復（不依賴 Claude API）
3. 語法測試
4. 有修復則 commit + push
5. 執行 `scripts/performance_analyzer.py` — 效能分析 + LINE 報告

### auto_maintain.yml（每日 03:00 台灣時間）
1. 抓取 Railway 部署日誌
2. 用 Claude API 分析錯誤並產生修復 patch
3. 執行語法測試
4. 有修復則 commit + push
5. 推送維護報告到管理員 LINE

### daily_enhance.yml（每日 04:00 台灣時間）
1. 規則式日常強化（missing imports、bare except 等）
2. 語法測試
3. 有修復則 commit + push

### weekly_enhance.yml（每週日 05:00 台灣時間）
1. 所有 API 端點健康檢查
2. 資料庫資料量審計
3. 排程器 job 數量驗證
4. 程式碼健康審計（asyncio、credit guard）
5. 推送週報到管理員 LINE

### auto_test.yml（每次 push）
1. 語法測試（backend、quant、line_webhook）
2. LINE 指令格式驗證
3. API 端點測試（需 RAILWAY_BACKEND_URL secret）

## 自動修復規則（auto_fix.py）

| 錯誤模式 | 修復動作 |
|----------|----------|
| LINE push 400 | handler.py 回覆呼叫加純文字 fallback |
| ImportError / ModuleNotFoundError | 修復已知模組路徑 |
| TWSE API 302 redirect | 更新為備用端點 URL |
| 資料庫空表 / no data | 觸發 data pipeline 重新抓取 |
| bare `except Exception:` | 補 `as e` |
| 缺少 stdlib import | 自動補 import |

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

# 5. 本機語法檢查
find backend quant line_webhook scripts -name "*.py" -exec python -m py_compile {} +
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

## 常見問題與解決方式

**Q: TWSE API 504/timeout**
A: `twse_service.py` 有三層 fallback（TWSE → TPEX → cache）。先確認 `_BASE_TWSE` 域名可連線；若持續失敗，檢查是否被封鎖，改用 `finmind_service.py` 或 `yfinance_service.py`。

**Q: LINE push 失敗（401）**
A: `LINE_CHANNEL_ACCESS_TOKEN` 過期，需在 LINE Developers Console 重新生成並更新 Railway 環境變數。

**Q: LINE push 失敗（400）**
A: 訊息格式錯誤，通常是 Flex Message 欄位不合規。`auto_fix.py` 會自動加純文字 fallback；緊急時在 handler.py 的 `_reply()` 捕捉 400 後改發純文字。

**Q: Railway 部署後 DB migration 失敗**
A: 確認 `DATABASE_URL` 指向正確的 PostgreSQL，並手動執行 `alembic upgrade head`。

**Q: APScheduler job 沒有觸發**
A: 檢查 `backend/utils/scheduler.py` 中 timezone 設定是否為 `Asia/Taipei`；確認 Railway 沒有 sleep/idle。

**Q: quant/ 模組 import 失敗**
A: 通常是相對 import 路徑問題。確認 `sys.path.insert(0, ...)` 在最上層，或改用絕對 import。

**Q: API 回傳 NaN / null 資料**
A: 檢查 `data_pipeline.py` 最近一次執行狀態，可透過 `/api/system/health` 查看模組健康度。

**Q: Google Drive 備份失敗**
A: 確認 `backup_service.py` 的 OAuth token 未過期，重新執行 `gcloud auth` 流程並更新 secret。

**Q: 自動維護 PR 建立後測試失敗**
A: 查看 GitHub Actions log，通常是 Claude 生成的 patch 有語法錯誤。手動審查 PR diff 後決定是否 merge。

**Q: LINE /agent 指令沒有回應**
A: 確認 `line_agent.yml` 的 `repository_dispatch` 事件有被正確觸發；查看 `GITHUB_TOKEN` 是否有 write 權限。
