from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean, UniqueConstraint
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
