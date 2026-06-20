# tw-bloomberg — 台股 AI 量化交易系統

LINE Bot + FastAPI 量化交易輔助平台，整合台灣股市資料、多因子評分引擎與自動推播，部署於 Railway。

---

## 系統架構

| 層級 | 技術 | 備注 |
|------|------|------|
| LINE Bot | LINE Messaging API v3 | Webhook + Push |
| API 後端 | FastAPI + Uvicorn | `backend/main.py` |
| 資料庫 | PostgreSQL（Railway）/ SQLite（本機） | SQLAlchemy async |
| 量化引擎 | 自研多因子模型 | `quant/` 58 個模組 |
| 排程 | APScheduler | `backend/utils/scheduler.py` 25+ jobs |
| 部署 | Railway（Docker） | `railway.toml`, `Dockerfile` |

### 外部 API 依賴

| 來源 | 用途 |
|------|------|
| TWSE openapi.twse.com.tw | 上市股票日線、法人買賣 |
| TPEX (TPEx) | 上櫃股票資料 fallback |
| Yahoo Finance query1.finance.yahoo.com | K 線歷史資料（回填用） |
| FinMind finmindtrade.com | 財報、月營收、籌碼 |
| LINE Messaging API | Bot 推播 |
| Anthropic Claude API | AI 分析與決策 |
| YouTube Data API | 分析師頻道影片抓取 |
| Google Drive API | PostgreSQL 備份 |

---

## 完整指令清單

### 報價與分析
| 指令 | 功能 |
|------|------|
| `2330` / `/quote 2330` | 即時報價 |
| `/ai 2330` | AI 個股分析（三維度評分） |
| `/pe 2330` | 本益比估值 |
| `/valuation 2330` | 估值分析 |
| `/inst 2330` | 法人買賣明細 |
| `/chip 2330` | 籌碼分析 |
| `/margin 2330` | 融資融券狀況 |
| `/compare 2330 2454` | 雙股比較 |
| `/check 2330` | 股票健康度 |
| `/why 2330` | AI 判斷依據明細 + 反事實情境 |
| `/why sentiment` | 大盤情緒分數計算過程 |

### 投資組合
| 指令 | 功能 |
|------|------|
| `/p` / `/portfolio` | 庫存總覽 |
| `/buy 2330 10 900` | 買入（10張，900元） |
| `/sell 2330 10 950` | 賣出 |
| `/stops 2330 850 1000` | 設定停損/停利 |
| `/performance` | 累計績效 |
| `/history` | 交易記錄 |
| `/rebalance` | 再平衡建議 |
| `/optimize` | 投組最佳化 |
| `/var` | 風險值（Value at Risk） |
| `/correlation` | 持倉相關性矩陣 |

### 選股與策略
| 指令 | 功能 |
|------|------|
| `/screener top` | 今日高分股票 |
| `/r` / `/recommend` | AI 推薦選股 |
| `/strategy` | 策略選股（動能/價值/籌碼/突破） |
| `/backtest 2330` | 個股回測 |
| `/smart` / `/smartscreen` | 智慧選股（RSI+MACD+量能） |
| `/rs` | 相對強度排行 |
| `/breadth` | 市場廣度快照 |
| `/movers` | 今日強弱股 |
| `/theme` | 主題選股 |
| `/accuracy` | AI 推薦準確率（含 v1/v2 版本對比） |

### 早報與週報
| 指令 | 功能 |
|------|------|
| `/morning` / `/早報` | 今日早報 |
| `/weekly` | 週報 |
| `/timeline 2330` | 個股事件時間軸 |

### 自選股
| 指令 | 功能 |
|------|------|
| `/watch 2330` | 加入自選股 |
| `/unwatch 2330` | 移除自選股 |
| `/watchlist` | 自選股列表（依漲跌幅排序） |
| `/watch add 2330 sl=850 tp=1000` | 加入含停損停利 |

### 分析師追蹤
| 指令 | 功能 |
|------|------|
| `/analyst` | 今日分析師共識 |
| `/analyst 2330` | 特定股票分析師觀點 |
| `/consensus` | YouTube 分析師共識（7 頻道） |
| `/debate` | AI 多空辯論 |

### 風控
| 指令 | 功能 |
|------|------|
| `/risk` | 投組風險報告 |
| `/euphoria` | 過熱偵測 |
| `/stress 2330` | 壓力測試 |
| `/blackswan` | 黑天鵝預警 |
| `/footprint 2330` | 主力足跡追蹤 |

### 市場情報
| 指令 | 功能 |
|------|------|
| `/sentiment` | 大盤情緒指數 |
| `/vix` | VIX 恐慌指數 |
| `/pcr` | 選擇權 Put/Call Ratio |
| `/global` | 全球市場概況 |
| `/adr 2330` | ADR 溢價分析 |
| `/rotation` | 產業輪動訊號 |
| `/sector` | 產業熱力圖 |
| `/news` | 最新財經新聞 |
| `/buzz 2330` | 個股新聞熱度 |

### 投資日記
| 指令 | 功能 |
|------|------|
| `/journal` | 查看日記列表（含本月統計） |
| `/journal add 買入 2330 10張 900元 原因` | 新增記錄 |
| `/journal analysis` | AI 交易品質分析 |
| `/journal del <id>` | 刪除記錄 |

### 系統管理
| 指令 | 功能 |
|------|------|
| `/notify list` | 查看所有推播任務狀態 |
| `/notify on/off <job_id>` | 開啟/關閉特定推播 |
| `/quiet on [小時]` | 暫停所有推播 |
| `/quiet off` | 恢復推播 |
| `/ailearn` | AI 自學記錄查詢 |
| `/sysstatus` | 系統健康儀表板 |

---

## 自動推播時間表（精簡版）

| 時間 | 內容 |
|------|------|
| 08:10 週一至五 | 盤前簡報 |
| 08:30 週一至五 | 早報 + 盤前選股 |
| 08:45 週一至五 | 精簡晨報 + 自選股晨報 |
| 09:00–13:30 每 3–5 分鐘 | 警報掃描 |
| 12:00 週一至五 | 上午警報彙整 |
| 13:00 + 15:30 | 法人明細 + 下午警報 |
| 15:00 週一至五 | 收盤總結 |
| 15:30 週一至五 | 推薦結果回填 |
| 18:00–19:30 | Alpha Pipeline（多步驟選股） |
| 19:30 週一至五 | 每日建議 + AI 決策 |
| 20:00 週一至五 | 分析師共識報告 |

---

## 進行中實驗：v1/v2 選股邏輯對比

**實驗開始日期：** 2026-06-20

**v1 舊邏輯（推薦日 < 2026-06-20）**：
- 布林上軌突破 → +20 分（追漲型，反預測力）
- MA5>MA20>MA60 完全排列 → +30 分（已漲很久才符合）
- 外資連買 5+ 天 → +40 分（擁擠入場）

**v2 新邏輯（推薦日 >= 2026-06-20）**：
- 布林下軌超賣 → +20 分；上軌突破 → −5 分警示
- MA5 剛翻越 MA20（黃金交叉）→ +30 分；完全排列 → +15 分
- 外資連買 1-2 天（早期訊號）→ +40 分；5+ 天 → +10 分

**查看對比：** `/accuracy` → 「邏輯版本對比」區塊

**預計有效數據：** 2026-07-15 後（~75 筆 v2 記錄）

---

## 已知限制

| 功能 | 限制說明 |
|------|---------|
| 自動權重調整 | 已暫停，需 300+ 筆回填記錄才啟動（目前 ~234 筆） |
| `/trade` 時間戳 | 儲存為 UTC，顯示時間與台灣時區差 8 小時 |
| `/notify` 設定 | 儲存於本地檔案，容器重啟後可能需重新設定 |
| FinMind 資料 | 需要 token 才能抓取籌碼歷史；未設定時部分指令無資料 |
| YouTube 分析 | 需要 YOUTUBE_API_KEY；無設定時分析師功能受限 |
| 回填準確率 | 需推薦後 5 個交易日才能計算，新推薦需等待 |
| `/ailearn` 記錄 | 只有 techrating 服務有寫入；其他服務尚未接入 |

---

## 環境變數

| 變數名稱 | 用途 | 必要 |
|----------|------|------|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Bot 推播 token | ✅ |
| `LINE_CHANNEL_SECRET` | LINE Webhook 驗簽密鑰 | ✅ |
| `ANTHROPIC_API_KEY` | Claude AI API | ✅ |
| `DATABASE_URL` | PostgreSQL 連線字串（Railway 自動注入） | ✅ |
| `ADMIN_LINE_UID` | 接收維護報告與警報的 LINE UID | ✅ |
| `RAILWAY_BACKEND_URL` | 部署後的 HTTPS 位址（健康檢查用） | 建議 |
| `RAILWAY_TOKEN` | Railway CLI/API 認證 | CI 用 |
| `RAILWAY_PROJECT_ID` | Railway 專案 ID | CI 用 |
| `RAILWAY_SERVICE_ID` | Railway 服務 ID | CI 用 |
| `FINMIND_TOKEN` | FinMind 資料 API | 籌碼功能 |
| `YOUTUBE_API_KEY` | YouTube Data API | 分析師功能 |
| `ADMIN_API_TOKEN` | 後端管理 API 認證 | 管理用 |

---

## 自動化維護

| GitHub Action | 時間 | 說明 |
|---------------|------|------|
| `self_improve.yml` | 每日 02:00 | 健康檢查 + 規則式修復 |
| `auto_maintain.yml` | 每日 03:00 | Claude API 分析日誌 + 生成 patch |
| `daily_enhance.yml` | 每日 04:00 | 規則式日常強化 |
| `weekly_enhance.yml` | 週日 05:00 | 深度週報 + 端點審計 |
| `auto_test.yml` | 每次 push | 語法檢查 + API 端點測試 |

---

## 快速健康確認

```bash
# 系統健康
curl -s $RAILWAY_BACKEND_URL/api/system/health | python -m json.tool

# 台積電報價
curl -s $RAILWAY_BACKEND_URL/api/quote/2330

# 語法全檢
find backend quant line_webhook scripts -name "*.py" -exec python -m py_compile {} +
```
