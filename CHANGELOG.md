# CHANGELOG

## v2 — 2026-06-20：選股邏輯翻轉（indicator_engine.py）

### 動機
舊邏輯的三個技術/籌碼訊號均為「追漲型」，對 5 日報酬的預測力為負相關（r ≈ −0.05）。
根據歷史推薦資料分析（234 筆，勝率 25.2%），系統性地在股票已經漲多之後才給高分。

### 改動 1：翻轉布林通道訊號
**檔案：** `backend/services/indicator_engine.py`

- 舊：上軌突破（追高）→ +20 分
- 新：下軌超賣（回升機會）→ +20 分；上軌突破（追高警示）→ −5 分
- BB 位置評分方向完全翻轉：越低位置分數越高

### 改動 2：均線評分改為偵測「剛翻揚」
**檔案：** `backend/services/indicator_engine.py`

- 舊：MA5 > MA20 > MA60 完全多頭排列 → +30 分（已漲很久才符合）
- 新：MA5 剛穿越 MA20（前一根 MA5 ≤ MA20，現在 MA5 > MA20）→ +30 分（早期訊號）
       完全多頭排列（漲幅已大）→ +15 分；MA5 > MA20 → +10 分

### 改動 3：翻轉外資連買計分
**檔案：** `backend/services/indicator_engine.py`

- 舊：連買 5+ 天 → +40 分（最高，但此時消息已充分反映）
- 新：連買 1-2 天 → +40 分（早期佈局訊號）；3-4 天 → +20 分；5+ 天 → +10 分（擁擠）

### 改動 4：暫停自動權重調整
**檔案：** `backend/services/recommendation_tracker.py`

- 舊：樣本 ≥ 5 筆就調整（統計意義接近零）
- 新：需 ≥ 300 筆才啟動調整（基於大數定律，每天 ~10 筆約需 30 天積累）

### 版本追蹤
**檔案：** `backend/models/models.py`, `backend/models/database.py`,
         `backend/services/recommendation_tracker.py`, `backend/services/ai_decision_agent.py`

- `recommendation_results` 表新增 `scoring_version` 欄位（VARCHAR(10)，預設 `v1`）
- 舊推薦記錄標記為 `v1`；2026-06-20 後新推薦標記為 `v2`
- `/accuracy` 指令新增「邏輯版本對比」區塊

### 觀察期建議
- 每日推薦約 5-15 檔，需 3-4 週（約 75-300 筆）才能在統計上比較 v1 vs v2
- 建議在 2026-07-20 後執行首次版本對比分析

---

## v1 — 2026-06-15 以前：舊邏輯（已棄用）

初始評分體系：
- 布林上軌突破 +20 分
- MA5>MA20>MA60 完全排列 +30 分
- 外資連買 5+ 天 +40 分
- 自動權重調整：樣本 ≥ 5 筆即觸發
