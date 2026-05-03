from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean, UniqueConstraint, Date
from .database import Base


class Stock(Base):
    __tablename__ = "stocks"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(10), unique=True, index=True, nullable=False)
    name = Column(String(50))
    market = Column(String(10), default="TWSE")
    industry = Column(String(50))
    updated_at = Column(DateTime, default=datetime.utcnow)


class Portfolio(Base):
    __tablename__ = "portfolio"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(100), index=True, nullable=False, default="")
    stock_code = Column(String(10), index=True, nullable=False)
    stock_name = Column(String(50))
    shares = Column(Integer, nullable=False)
    cost_price = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Alert(Base):
    """
    alert_type:
      price_above / price_below  — 絕對價格
      change_pct_above / change_pct_below  — 當日漲跌幅 %
      margin_ratio_above — 融資使用率 %
    """
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)
    stock_code = Column(String(10), index=True, nullable=False)
    alert_type = Column(String(30), nullable=False)
    threshold = Column(Float, nullable=False)
    is_active = Column(Boolean, default=True)
    user_id = Column(String(100), index=True, nullable=False, default="")
    line_user_id = Column(String(100))   # 保留相容舊資料
    triggered_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)


class Subscriber(Base):
    """早報/週報 LINE 訂閱者"""
    __tablename__ = "subscribers"

    id = Column(Integer, primary_key=True, index=True)
    line_user_id = Column(String(100), unique=True, nullable=False)
    display_name = Column(String(100))
    subscribed_morning = Column(Boolean, default=True)
    subscribed_weekly = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class NewsArticle(Base):
    __tablename__ = "news_articles"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(500), nullable=False)
    content = Column(Text)
    url = Column(String(1000), unique=True)
    source = Column(String(50))
    published_at = Column(DateTime)
    sentiment = Column(String(20))
    sentiment_score = Column(Float)
    related_stocks = Column(String(200))
    created_at = Column(DateTime, default=datetime.utcnow)


class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (UniqueConstraint("stock_code", "date"),)

    id = Column(Integer, primary_key=True, index=True)
    stock_code = Column(String(10), index=True, nullable=False)
    date = Column(String(10), nullable=False)
    open_price = Column(Float)
    high_price = Column(Float)
    low_price = Column(Float)
    close_price = Column(Float)
    volume = Column(Integer)
    foreign_net = Column(Integer)          # 外資買賣超（張）
    investment_trust_net = Column(Integer) # 投信買賣超
    dealer_net = Column(Integer)           # 自營商買賣超
    created_at = Column(DateTime, default=datetime.utcnow)


class MarginData(Base):
    """融資融券每日資料"""
    __tablename__ = "margin_data"
    __table_args__ = (UniqueConstraint("stock_code", "date"),)

    id = Column(Integer, primary_key=True, index=True)
    stock_code = Column(String(10), index=True, nullable=False)
    date = Column(String(10), nullable=False)
    margin_buy = Column(Integer)
    margin_sell = Column(Integer)
    margin_balance = Column(Integer)
    short_buy = Column(Integer)
    short_sell = Column(Integer)
    short_balance = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)


class TradeLog(Base):
    """交易日誌 — 每筆買賣紀錄"""
    __tablename__ = "trade_log"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(100), index=True, nullable=False)
    trade_date = Column(String(10), nullable=False)           # YYYY-MM-DD
    stock_code = Column(String(10), nullable=False, index=True)
    stock_name = Column(String(50))
    action = Column(String(4), nullable=False)                # BUY / SELL
    price = Column(Float, nullable=False)
    shares = Column(Integer, nullable=False)
    trade_value = Column(Float)                               # price × shares
    commission = Column(Float, default=0)                     # 手續費 0.1425%
    tax = Column(Float, default=0)                            # 證交稅 0.3% (賣才有)
    net_amount = Column(Float)                                # 實收 / 實付金額
    realized_pnl = Column(Float, default=0)                  # 已實現損益 (SELL)
    avg_cost_at_trade = Column(Float)                         # 成交時的均成本
    created_at = Column(DateTime, default=datetime.utcnow)


class UserProfile(Base):
    """用戶投資風格 & AI 記憶"""
    __tablename__ = "user_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(100), unique=True, nullable=False, index=True)
    display_name = Column(String(100))
    # 風險偏好: conservative / moderate / aggressive
    risk_tolerance = Column(String(20), default="moderate")
    # 偏好產業 (逗號分隔)
    preferred_industries = Column(String(500), default="")
    # 投資目標: income / growth / speculation
    investment_goal = Column(String(20), default="growth")
    # AI 背景摘要 (Claude 自動生成)
    ai_summary = Column(Text, default="")
    # 操作習慣統計
    total_trades = Column(Integer, default=0)
    avg_hold_days = Column(Float, default=0)
    win_rate = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class QueryHistory(Base):
    """AI 問答歷史 — 避免重複分析"""
    __tablename__ = "query_history"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(100), index=True, nullable=False)
    question = Column(Text, nullable=False)
    answer = Column(Text)
    topic_hash = Column(String(64), index=True)               # 問題摘要 hash
    created_at = Column(DateTime, default=datetime.utcnow)


class Watchlist(Base):
    """自選股清單"""
    __tablename__ = "watchlist"
    __table_args__ = (UniqueConstraint("user_id", "stock_code"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(100), index=True, nullable=False, default="")
    stock_code = Column(String(10), nullable=False, index=True)
    stock_name = Column(String(50))
    target_price = Column(Float)        # 目標價
    stop_loss = Column(Float)           # 停損價
    note = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)


class PerformanceRecord(Base):
    """每日績效快照 — 用於排行榜"""
    __tablename__ = "performance_records"
    __table_args__ = (UniqueConstraint("user_id", "record_date"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(100), index=True, nullable=False)
    record_date = Column(String(10), nullable=False)   # YYYY-MM-DD
    total_mv = Column(Float, default=0)
    total_cost = Column(Float, default=0)
    total_pnl = Column(Float, default=0)
    daily_return = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class CopyTradeRelation(Base):
    """跟單關係 — follower 追蹤 leader 的持倉"""
    __tablename__ = "copy_trade_relations"
    __table_args__ = (UniqueConstraint("follower_id", "leader_id"),)

    id = Column(Integer, primary_key=True, index=True)
    follower_id = Column(String(100), index=True, nullable=False)
    leader_id = Column(String(100), index=True, nullable=False)
    is_active = Column(Boolean, default=True)
    auto_copy = Column(Boolean, default=False)   # 是否自動跟單
    created_at = Column(DateTime, default=datetime.utcnow)


class SharedPortfolio(Base):
    """公開分享的投資組合"""
    __tablename__ = "shared_portfolios"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(100), unique=True, nullable=False, index=True)
    share_code = Column(String(20), unique=True, nullable=False)
    display_name = Column(String(100), default="匿名投資人")
    description = Column(String(500))
    is_public = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class EarningsReminder(Base):
    """財報提醒 — 使用者訂閱特定股票的財報公布日提醒"""
    __tablename__ = "earnings_reminders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(100), index=True, nullable=False, default="")
    line_user_id = Column(String(100))
    stock_code = Column(String(10), nullable=False, index=True)
    stock_name = Column(String(50))
    # 財報期別，例如 "2025Q1"、"2025H1"、"2024Annual"
    period = Column(String(20))
    # 預計公布日 YYYY-MM-DD（使用者自填 or 系統估算）
    announce_date = Column(String(10))
    # 提前幾天提醒
    remind_days_before = Column(Integer, default=3)
    is_reminded = Column(Boolean, default=False)
    # 實際公布後填入
    actual_eps = Column(Float)
    expected_eps = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)


# ══════════════════════════════════════════════════════════════════════════════
# 升級架構 v2 — FinMind 數據 + 多維度選股引擎
# ══════════════════════════════════════════════════════════════════════════════

class StockFinancials(Base):
    """季度財務報表 — 來源 FinMind TaiwanFinancialStatements"""
    __tablename__ = "stock_financials"
    __table_args__ = (UniqueConstraint("stock_code", "year", "quarter"),)

    id = Column(Integer, primary_key=True, index=True)
    stock_code     = Column(String(10), index=True, nullable=False)
    stock_name     = Column(String(50))
    year           = Column(Integer, nullable=False)
    quarter        = Column(Integer, nullable=False)        # 1-4
    revenue        = Column(Float)                          # 營收（千元）
    gross_profit   = Column(Float)                          # 毛利
    operating_income = Column(Float)                        # 營業利益
    net_income     = Column(Float)                          # 淨利
    eps            = Column(Float)                          # 每股盈餘
    gross_margin   = Column(Float)                          # 毛利率 %
    operating_margin = Column(Float)                        # 營益率 %
    net_margin     = Column(Float)                          # 淨利率 %
    is_anomaly     = Column(Boolean, default=False)         # 異常資料標記
    updated_at     = Column(DateTime, default=datetime.utcnow)


class MonthlyRevenue(Base):
    """月營收 — 來源 FinMind TaiwanStockMonthRevenue"""
    __tablename__ = "monthly_revenue"
    __table_args__ = (UniqueConstraint("stock_code", "year", "month"),)

    id = Column(Integer, primary_key=True, index=True)
    stock_code     = Column(String(10), index=True, nullable=False)
    stock_name     = Column(String(50))
    year           = Column(Integer, nullable=False)
    month          = Column(Integer, nullable=False)
    revenue        = Column(Float)                          # 當月營收（千元）
    revenue_mom    = Column(Float)                          # 月增率 %
    revenue_yoy    = Column(Float)                          # 年增率 %
    cum_revenue    = Column(Float)                          # 累計營收
    cum_revenue_yoy = Column(Float)                         # 累計年增率 %
    updated_at     = Column(DateTime, default=datetime.utcnow)


class StockScore(Base):
    """三維度評分快照 — Agent B 每日計算"""
    __tablename__ = "stock_scores"
    __table_args__ = (UniqueConstraint("stock_code", "score_date"),)

    id = Column(Integer, primary_key=True, index=True)
    stock_code        = Column(String(10), index=True, nullable=False)
    stock_name        = Column(String(50))
    score_date        = Column(String(10), nullable=False)   # YYYY-MM-DD
    # 三維度評分（各 0-100）
    fundamental_score = Column(Float, default=0)
    chip_score        = Column(Float, default=0)
    technical_score   = Column(Float, default=0)
    total_score       = Column(Float, default=0)             # 加權總分
    # 明細指標（供前端雷達圖）
    revenue_yoy       = Column(Float)                        # 最新月營收 YoY %
    gross_margin      = Column(Float)                        # 最新毛利率 %
    three_margins_up  = Column(Boolean, default=False)       # 三率齊升
    eps_growth_qtrs   = Column(Integer, default=0)           # 連續 EPS 成長季數
    foreign_consec_buy = Column(Integer, default=0)          # 外資連續買超日
    trust_consec_buy  = Column(Integer, default=0)           # 投信連續買超日
    ma_aligned        = Column(Boolean, default=False)       # 均線多頭排列
    kd_golden_cross   = Column(Boolean, default=False)       # KD 黃金交叉
    vol_breakout      = Column(Boolean, default=False)       # 量能突破
    bb_breakout       = Column(Boolean, default=False)       # 布林上軌突破
    # AI 推薦
    confidence        = Column(Float, default=0)             # 信心指數 0-100
    ai_reason         = Column(Text)                         # AI 推薦理由
    updated_at        = Column(DateTime, default=datetime.utcnow)


class IndustrySentiment(Base):
    """產業情緒分析快照"""
    __tablename__ = "industry_sentiment"
    __table_args__ = (UniqueConstraint("industry", "analysis_date"),)

    id = Column(Integer, primary_key=True, index=True)
    industry       = Column(String(50), index=True, nullable=False)
    analysis_date  = Column(String(10), nullable=False)
    bullish_score  = Column(Float, default=50)              # 偏多分數 0-100
    bearish_score  = Column(Float, default=50)              # 偏空分數 0-100
    net_sentiment  = Column(Float, default=0)               # bullish - bearish
    key_stocks     = Column(String(500))                    # 影響股票（逗號分隔）
    bullish_factors = Column(Text)                          # 利多因素
    bearish_factors = Column(Text)                          # 利空因素
    ai_summary     = Column(Text)
    news_count     = Column(Integer, default=0)
    updated_at     = Column(DateTime, default=datetime.utcnow)


# ══════════════════════════════════════════════════════════════════════════════
# 進階功能 v3 — RL 回饋 + 分點追蹤 + 投組最佳化
# ══════════════════════════════════════════════════════════════════════════════

class RecommendationResult(Base):
    """推薦結果追蹤 — 記錄每次 AI 推薦，並回填後續股價驗證準確率"""
    __tablename__ = "recommendation_results"
    __table_args__ = (UniqueConstraint("stock_code", "recommend_date"),)

    id = Column(Integer, primary_key=True, index=True)
    stock_code         = Column(String(10), index=True, nullable=False)
    stock_name         = Column(String(50))
    recommend_date     = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    recommend_price    = Column(Float)                                    # 推薦當日收盤
    fundamental_score  = Column(Float, default=0)
    chip_score         = Column(Float, default=0)
    technical_score    = Column(Float, default=0)
    total_score        = Column(Float, default=0)
    confidence         = Column(Float, default=0)
    ai_reason          = Column(Text)
    # 後驗結果（每日回填）
    price_5d           = Column(Float)     # 5 交易日後收盤
    price_10d          = Column(Float)     # 10 交易日後收盤
    return_5d          = Column(Float)     # 5 日報酬率 %
    return_10d         = Column(Float)     # 10 日報酬率 %
    hit_target_5d      = Column(Boolean)   # 5 日漲幅 > 3%
    hit_target_10d     = Column(Boolean)   # 10 日漲幅 > 3%
    is_filled_5d       = Column(Boolean, default=False)
    is_filled_10d      = Column(Boolean, default=False)
    created_at         = Column(DateTime, default=datetime.utcnow)


class ScoringWeight(Base):
    """動態評分權重 — 每週根據推薦準確率自動調整"""
    __tablename__ = "scoring_weights"

    id = Column(Integer, primary_key=True, index=True)
    effective_date      = Column(String(10), unique=True, nullable=False, index=True)
    fundamental_weight  = Column(Float, default=0.35)
    chip_weight         = Column(Float, default=0.35)
    technical_weight    = Column(Float, default=0.30)
    # 上週各維度推薦成功率（回填完成後計算）
    fundamental_win_rate = Column(Float)
    chip_win_rate        = Column(Float)
    technical_win_rate   = Column(Float)
    overall_win_rate     = Column(Float)
    notes               = Column(String(500))
    created_at          = Column(DateTime, default=datetime.utcnow)


class BrokerActivity(Base):
    """券商分點交易快取 — 來源 FinMind BrokerTradingDetail"""
    __tablename__ = "broker_activity"
    __table_args__ = (UniqueConstraint("date", "stock_code", "broker_id"),)

    id = Column(Integer, primary_key=True, index=True)
    date          = Column(String(10), nullable=False, index=True)
    stock_code    = Column(String(10), nullable=False, index=True)
    stock_name    = Column(String(50))
    broker_id     = Column(String(20), nullable=False)
    broker_name   = Column(String(100), index=True)
    buy_shares    = Column(Integer, default=0)    # 買進張數
    sell_shares   = Column(Integer, default=0)    # 賣出張數
    net_shares    = Column(Integer, default=0)    # 淨買超張數
    buy_price     = Column(Float)                 # 均買價
    sell_price    = Column(Float)                 # 均賣價
    created_at    = Column(DateTime, default=datetime.utcnow)


# ══════════════════════════════════════════════════════════════════════════════
# v4 — 回測 Feedback + Feature Engineering Schema
# ══════════════════════════════════════════════════════════════════════════════

class BacktestSession(Base):
    """回測 Session 摘要"""
    __tablename__ = "backtest_sessions"

    id = Column(Integer, primary_key=True, index=True)
    session_id         = Column(String(50), unique=True, nullable=False, index=True)
    stock_code         = Column(String(10), index=True)
    strategy           = Column(String(30), index=True)
    start_date         = Column(String(10))
    end_date           = Column(String(10))
    initial_capital    = Column(Float)
    final_capital      = Column(Float)
    total_return       = Column(Float)
    annualized_return  = Column(Float)
    max_drawdown       = Column(Float)
    sharpe_ratio       = Column(Float)
    win_rate           = Column(Float)
    total_trades       = Column(Integer, default=0)
    total_commission   = Column(Float, default=0)
    total_tax          = Column(Float, default=0)
    total_slippage     = Column(Float, default=0)
    cost_impact        = Column(Float, default=0)   # 成本對報酬的影響%
    market_regime      = Column(String(20))          # bull/bear/sideways
    created_at         = Column(DateTime, default=datetime.utcnow)


class BacktestTradeRecord(Base):
    """回測個別交易記錄（含成本明細）"""
    __tablename__ = "backtest_trade_records"

    id = Column(Integer, primary_key=True, index=True)
    session_id   = Column(String(50), index=True, nullable=False)
    stock_code   = Column(String(10), index=True)
    strategy     = Column(String(30))
    entry_date   = Column(String(10))
    exit_date    = Column(String(10))
    entry_price  = Column(Float)
    exit_price   = Column(Float)
    shares       = Column(Integer)
    gross_return = Column(Float)   # 未扣成本損益
    net_return   = Column(Float)   # 扣成本後損益
    commission   = Column(Float, default=0)
    tax          = Column(Float, default=0)
    slippage     = Column(Float, default=0)
    holding_days = Column(Integer, default=0)
    is_winner    = Column(Boolean, default=False)
    created_at   = Column(DateTime, default=datetime.utcnow)


class FeatureRecord(Base):
    """技術/基本/籌碼 Feature 值快照（用於機器學習 / 回測特徵工程）"""
    __tablename__ = "features"
    __table_args__ = (UniqueConstraint("date", "stock_id", "feature_name"),)

    id           = Column(Integer, primary_key=True, index=True)
    date         = Column(String(10), nullable=False, index=True)
    stock_id     = Column(String(10), nullable=False, index=True)
    feature_name = Column(String(50), nullable=False, index=True)
    value        = Column(Float)
    created_at   = Column(DateTime, default=datetime.utcnow)


class PredictionRecord(Base):
    """AI 預測結果（每日對每檔股票的預測報酬和信心評分）"""
    __tablename__ = "predictions"
    __table_args__ = (UniqueConstraint("date", "stock_id"),)

    id               = Column(Integer, primary_key=True, index=True)
    date             = Column(String(10), nullable=False, index=True)
    stock_id         = Column(String(10), nullable=False, index=True)
    stock_name       = Column(String(50))
    predicted_return = Column(Float)     # 預測 5 日報酬率 %
    score            = Column(Float)     # 綜合評分 0-100
    model_version    = Column(String(20), default="v1")
    fundamental_score = Column(Float)
    chip_score       = Column(Float)
    technical_score  = Column(Float)
    created_at       = Column(DateTime, default=datetime.utcnow)


class FeatureWeight(Base):
    """Feature 權重 — 由 feedback_engine 自動調整"""
    __tablename__ = "feature_weights"

    id                 = Column(Integer, primary_key=True, index=True)
    fundamental_weight = Column(Float, default=0.35)
    chip_weight        = Column(Float, default=0.35)
    technical_weight   = Column(Float, default=0.30)
    notes              = Column(String(500))
    updated_at         = Column(DateTime, default=datetime.utcnow)


class StrategySetting(Base):
    """用戶策略設定：每個用戶可獨立開關策略 + 調整權重"""
    __tablename__ = "strategy_settings"
    __table_args__ = (UniqueConstraint("user_id", "strategy"),)

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(String(100), index=True, nullable=False)
    strategy   = Column(String(30), nullable=False)   # momentum/value/chip/breakout/defensive
    enabled    = Column(Boolean, default=True)
    weight     = Column(Float, default=1.0)
    preset     = Column(String(20), default="balanced")  # conservative/balanced/aggressive
    updated_at = Column(DateTime, default=datetime.utcnow)


class PipelineLog(Base):
    """新聞 Pipeline 執行日誌 — 每步驟記錄成功/失敗"""
    __tablename__ = "pipeline_log"

    id         = Column(Integer, primary_key=True, index=True)
    run_id     = Column(String(50), index=True)
    step       = Column(String(30))    # scrape/summarize/sentiment/image/push/done
    status     = Column(String(10))    # ok/fail/skip
    detail     = Column(Text)
    articles   = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class AlphaRegistry(Base):
    """Alpha 因子狀態登記 — ACTIVE/PAUSED/DEAD + IC 追蹤"""
    __tablename__ = "alpha_registry"

    id          = Column(Integer, primary_key=True, index=True)
    alpha_name  = Column(String(50), unique=True, nullable=False, index=True)
    status      = Column(String(10), default="ACTIVE")  # ACTIVE/PAUSED/DEAD
    ic_current  = Column(Float, default=0.0)
    ic_30d_mean = Column(Float, default=0.0)
    ic_history  = Column(Text, default="[]")   # JSON: last 30 days IC values
    weight      = Column(Float, default=1.0)
    dead_days   = Column(Integer, default=0)   # consecutive days IC < 0
    notes       = Column(String(200))
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow)


class CallbackLog(Base):
    """LINE Bot Callback 錯誤日誌"""
    __tablename__ = "callback_log"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(String(100), index=True)
    action     = Column(String(100))
    params     = Column(Text)
    error      = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── Quant Alpha Pipeline 擴充資料表 ───────────────────────────────────────────

class ConvictionLog(Base):
    """信心強度計算紀錄"""
    __tablename__ = "conviction_log"

    id                 = Column(Integer, primary_key=True, index=True)
    ticker             = Column(String(10), index=True, nullable=False)
    name               = Column(String(50))
    conviction         = Column(Float, nullable=False)
    position_size      = Column(Float)
    layer              = Column(String(20))   # core / medium / no_trade
    signal_strength    = Column(Float)
    factor_consensus   = Column(Float)
    regime_alignment   = Column(Float)
    research_quality   = Column(Float)
    note               = Column(String(200))
    created_at         = Column(DateTime, default=datetime.utcnow)


class AlphaDecayLog(Base):
    """Alpha 因子 IC 日誌（每日記錄）"""
    __tablename__ = "alpha_decay_log"

    id          = Column(Integer, primary_key=True, index=True)
    alpha_name  = Column(String(50), index=True, nullable=False)
    status      = Column(String(15))   # ACTIVE / DEGRADING / RECOVERING / DEAD
    ic_value    = Column(Float)
    ic_30d_mean = Column(Float)
    ic_trend    = Column(Float)
    win_rate    = Column(Float)
    sharpe      = Column(Float)
    half_life   = Column(Float)
    weight      = Column(Float)
    created_at  = Column(DateTime, default=datetime.utcnow)


class SectorRotationLog(Base):
    """族群輪動強度快照（每日）"""
    __tablename__ = "sector_rotation_log"

    id                  = Column(Integer, primary_key=True, index=True)
    sector_name         = Column(String(20), index=True, nullable=False)
    composite_score     = Column(Float)
    avg_return_5d       = Column(Float)
    foreign_flow        = Column(Float)
    volume_change       = Column(Float)
    chip_concentration  = Column(Float)
    rank                = Column(Integer)
    trend               = Column(String(5))   # ↑ / ↓ / →
    created_at          = Column(DateTime, default=datetime.utcnow)


class CapitalFlowLog(Base):
    """資金流向日誌（每日快照）"""
    __tablename__ = "capital_flow_log"

    id                   = Column(Integer, primary_key=True, index=True)
    top_inflow_sector    = Column(String(20))
    top_outflow_sector   = Column(String(20))
    foreign_futures_net  = Column(Float)
    futures_bull_bear    = Column(String(10))   # bull / bear / neutral
    chip_concentration   = Column(Float)
    margin_change        = Column(Float)
    short_change         = Column(Float)
    sector_flows_json    = Column(Text)          # JSON 族群流向分數
    main_flow_days       = Column(Integer)       # 正=持續流入，負=持續流出
    rotation_warning     = Column(Boolean, default=False)
    created_at           = Column(DateTime, default=datetime.utcnow)


class TradeJournal(Base):
    """AI 交易日誌：每筆買賣附帶 AI 生成原因與風險備註"""
    __tablename__ = "trade_journal"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(String(100), index=True, nullable=False)
    date         = Column(Date, nullable=False)
    stock_id     = Column(String(10), nullable=False, index=True)
    stock_name   = Column(String(50), default="")
    action       = Column(String(10), nullable=False)   # buy / sell
    price        = Column(Float, nullable=False)
    shares       = Column(Integer, nullable=False)
    reason       = Column(Text, default="")             # AI 自動生成進場原因
    risk_notes   = Column(Text, default="")             # AI 風險提示
    stop_loss    = Column(Float, nullable=True)
    target_price = Column(Float, nullable=True)
    outcome      = Column(String(20), default="holding")  # holding/profit/loss
    pnl          = Column(Float, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)


class RegimeMemoryModel(Base):
    """Regime 記憶：各市場狀態下策略績效"""
    __tablename__ = "regime_memory"

    id          = Column(Integer, primary_key=True, index=True)
    regime      = Column(String(20), index=True, nullable=False)   # BULL/BEAR/...
    strategy    = Column(String(30), nullable=False)
    win_rate    = Column(Float, default=0.5)
    n_trades    = Column(Integer, default=0)
    avg_return  = Column(Float, default=0.0)
    sharpe      = Column(Float, default=0.0)
    weight      = Column(Float, default=1.0)
    updated_at  = Column(DateTime, default=datetime.utcnow)


class SmartMoneyLog(Base):
    """聰明錢追蹤記錄"""
    __tablename__ = "smart_money_log"
    id          = Column(Integer, primary_key=True, index=True)
    date        = Column(String(10), nullable=False, index=True)
    stock_id    = Column(String(10), nullable=False, index=True)
    stock_name  = Column(String(50), default="")
    broker_name = Column(String(50), default="")
    signal_type = Column(String(30), default="")   # consecutive_buy / multi_broker / etf_add
    shares      = Column(Integer, default=0)
    days        = Column(Integer, default=1)        # 連續天數
    note        = Column(Text, default="")
    created_at  = Column(DateTime, default=datetime.utcnow)


class InsiderFlowLog(Base):
    """董監持股異動記錄"""
    __tablename__ = "insider_flow_log"
    id           = Column(Integer, primary_key=True, index=True)
    date         = Column(String(10), nullable=False)
    stock_id     = Column(String(10), nullable=False, index=True)
    stock_name   = Column(String(50), default="")
    insider_name = Column(String(100), default="")
    role         = Column(String(50), default="")   # 董事長 / 獨立董事 / 大股東
    action       = Column(String(10), default="")   # buy / sell
    shares       = Column(Integer, default=0)
    note         = Column(Text, default="")
    created_at   = Column(DateTime, default=datetime.utcnow)


class PublicPortfolio(Base):
    """公開投組設定"""
    __tablename__ = "public_portfolio"
    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(String(100), unique=True, nullable=False, index=True)
    display_name = Column(String(50), default="")
    is_public   = Column(Boolean, default=False)
    style_tag   = Column(String(50), default="")    # 散熱主力 / 半導體 / 存股
    weekly_return = Column(Float, default=0.0)
    total_return  = Column(Float, default=0.0)
    updated_at  = Column(DateTime, default=datetime.utcnow)


class StrategyMarketplace(Base):
    """策略市集"""
    __tablename__ = "strategy_marketplace"
    id           = Column(Integer, primary_key=True, index=True)
    owner_id     = Column(String(100), nullable=False, index=True)
    name         = Column(String(100), nullable=False)
    description  = Column(Text, default="")
    screen_type  = Column(String(30), default="momentum")
    return_3m    = Column(Float, default=0.0)
    win_rate     = Column(Float, default=0.5)
    max_drawdown = Column(Float, default=0.0)
    subscribers  = Column(Integer, default=0)
    is_active    = Column(Boolean, default=True)
    created_at   = Column(DateTime, default=datetime.utcnow)


class DailyResearchLog(Base):
    """每日自動研究報告"""
    __tablename__ = "daily_research_log"
    id           = Column(Integer, primary_key=True, index=True)
    date         = Column(String(10), nullable=False, index=True)
    opportunities = Column(Text, default="[]")      # JSON list of stock picks
    summary      = Column(Text, default="")
    market_state = Column(String(20), default="unknown")
    created_at   = Column(DateTime, default=datetime.utcnow)


class AgentDecisionLog(Base):
    """AI 基金經理決策記錄"""
    __tablename__ = "agent_decision_log"
    id           = Column(Integer, primary_key=True, index=True)
    date         = Column(String(10), nullable=False, index=True)
    user_id      = Column(String(100), default="system", index=True)
    decisions    = Column(Text, default="[]")       # JSON list of decisions
    health_score = Column(Integer, default=75)
    market_state = Column(String(20), default="unknown")
    main_risk    = Column(Text, default="")
    created_at   = Column(DateTime, default=datetime.utcnow)


class UserSubscription(Base):
    """用戶訂閱方案"""
    __tablename__ = "user_subscriptions"
    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(String(100), unique=True, nullable=False, index=True)
    plan         = Column(String(20), default="free")   # free / standard / pro
    expires_at   = Column(DateTime, nullable=True)
    auto_trade   = Column(Boolean, default=False)
    auto_threshold = Column(Float, default=0.95)
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow)


class ReferralCode(Base):
    """用戶推薦碼"""
    __tablename__ = "referral_codes"
    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(String(100), nullable=False, index=True)
    code         = Column(String(20), unique=True, nullable=False, index=True)
    referrals    = Column(Integer, default=0)
    bonus_months = Column(Integer, default=0)
    created_at   = Column(DateTime, default=datetime.utcnow)


class TradingOrder(Base):
    """Fugle 下單記錄"""
    __tablename__ = "trading_orders"
    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(String(100), nullable=False, index=True)
    stock_id     = Column(String(10), nullable=False)
    stock_name   = Column(String(50), default="")
    action       = Column(String(10), nullable=False)   # buy / sell
    order_type   = Column(String(10), default="limit")  # limit / market
    price        = Column(Float, nullable=True)
    shares       = Column(Integer, nullable=False)
    status       = Column(String(20), default="pending")  # pending/confirmed/executed/cancelled
    fugle_order_id = Column(String(50), nullable=True)
    confidence   = Column(Float, default=0.0)
    auto_executed = Column(Boolean, default=False)
    created_at   = Column(DateTime, default=datetime.utcnow)


class SystemHealthLog(Base):
    """系統健康狀態記錄"""
    __tablename__ = "system_health_log"
    id           = Column(Integer, primary_key=True, index=True)
    module       = Column(String(50), nullable=False, index=True)
    status       = Column(String(20), default="ok")    # ok / warning / error
    message      = Column(Text, default="")
    last_run     = Column(DateTime, nullable=True)
    error_count  = Column(Integer, default=0)
    created_at   = Column(DateTime, default=datetime.utcnow)


class UsageLog(Base):
    """用戶使用行為記錄"""
    __tablename__ = "usage_log"
    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(String(100), nullable=False, index=True)
    action       = Column(String(50), nullable=False, index=True)
    params       = Column(String(200), default="")
    response_ms  = Column(Integer, default=0)
    created_at   = Column(DateTime, default=datetime.utcnow)
