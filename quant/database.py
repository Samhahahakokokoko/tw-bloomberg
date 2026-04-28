"""
database.py — 量化系統獨立資料庫 Schema

與 backend/models/models.py 完全獨立，不相互依賴。
使用 SQLAlchemy 2.x async 模式；同時支援 PostgreSQL（生產）和 SQLite（測試）。

資料表架構：
  stocks      — 股票基本資料（代號、名稱、產業、市場）
  prices      — 日 OHLCV 歷史價格
  features    — FeatureEngine 計算後的特徵快照（每日更新）
  predictions — AlphaModel 每日預測結果（訊號、預測報酬率、信心）
  trades      — ExecutionEngine 產生的成交紀錄

初始化方式：
  # 直接執行此檔案（建立所有資料表）
  python -m quant.database

  # 在程式中使用
  from quant.database import QuantDB
  db = QuantDB("postgresql+asyncpg://user:pass@host/dbname")
  await db.create_tables()
"""

from __future__ import annotations

import os
from datetime import datetime, date
from typing import Optional, Any

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, Float,
    Index, Integer, JSON, String, Text, UniqueConstraint,
    select, func,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, create_async_engine, async_sessionmaker,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ── Base ─────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── 資料表 ─────────────────────────────────────────────────────────────────

class Stock(Base):
    """
    股票主檔：每檔股票一行，靜態資料。
    TWSE 上市用 market='TSE'，TPEX 上櫃用 market='OTC'。
    """
    __tablename__ = "quant_stocks"

    id:          Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    code:        Mapped[str]           = mapped_column(String(10),  nullable=False, unique=True, index=True)
    name:        Mapped[str]           = mapped_column(String(50),  nullable=False)
    sector:      Mapped[Optional[str]] = mapped_column(String(50))   # 產業別
    industry:    Mapped[Optional[str]] = mapped_column(String(50))   # 細分產業
    market:      Mapped[str]           = mapped_column(String(5),   nullable=False, default="TSE")  # TSE/OTC
    is_etf:      Mapped[bool]          = mapped_column(Boolean, default=False)
    listed_date: Mapped[Optional[date]]= mapped_column(Date)
    created_at:  Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:  Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Price(Base):
    """
    日 OHLCV 價格：每股每日一行。
    adj_close 為還原權值收盤價（除權息調整後），用於技術指標計算。
    """
    __tablename__ = "quant_prices"
    __table_args__ = (
        UniqueConstraint("code", "trade_date", name="uq_price_code_date"),
        Index("ix_price_code_date", "code", "trade_date"),
    )

    id:          Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    code:        Mapped[str]            = mapped_column(String(10), nullable=False, index=True)
    trade_date:  Mapped[date]           = mapped_column(Date, nullable=False)
    open:        Mapped[float]          = mapped_column(Float)
    high:        Mapped[float]          = mapped_column(Float)
    low:         Mapped[float]          = mapped_column(Float)
    close:       Mapped[float]          = mapped_column(Float)
    adj_close:   Mapped[Optional[float]]= mapped_column(Float)   # 還原後收盤
    volume:      Mapped[Optional[int]]  = mapped_column(BigInteger)
    turnover:    Mapped[Optional[float]]= mapped_column(Float)   # 成交金額（元）
    # 外資 / 投信 / 自營商
    foreign_net: Mapped[Optional[int]]  = mapped_column(Integer)  # 外資淨買（張）
    trust_net:   Mapped[Optional[int]]  = mapped_column(Integer)  # 投信淨買
    dealer_net:  Mapped[Optional[int]]  = mapped_column(Integer)  # 自營商淨買
    created_at:  Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)


class Feature(Base):
    """
    FeatureEngine 產生的技術特徵快照。
    每日收盤後由排程任務計算並寫入；預測模型從此表讀取特徵。
    以 JSON 欄位 data 存放所有特徵值（靈活、無需加欄位）。
    """
    __tablename__ = "quant_features"
    __table_args__ = (
        UniqueConstraint("code", "feature_date", name="uq_feature_code_date"),
    )

    id:           Mapped[int]  = mapped_column(Integer, primary_key=True, autoincrement=True)
    code:         Mapped[str]  = mapped_column(String(10), nullable=False, index=True)
    feature_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 常用特徵直接存欄位（加速查詢）
    ma5:       Mapped[Optional[float]] = mapped_column(Float)
    ma20:      Mapped[Optional[float]] = mapped_column(Float)
    ma60:      Mapped[Optional[float]] = mapped_column(Float)
    rsi14:     Mapped[Optional[float]] = mapped_column(Float)
    macd_hist: Mapped[Optional[float]] = mapped_column(Float)
    boll_b:    Mapped[Optional[float]] = mapped_column(Float)
    vol_ratio: Mapped[Optional[float]] = mapped_column(Float)
    ret_5d:    Mapped[Optional[float]] = mapped_column(Float)
    # 完整特徵 JSON
    data:      Mapped[Optional[dict]]  = mapped_column(JSON)
    created_at: Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)


class Prediction(Base):
    """
    AlphaModel 每日預測結果。
    signal: 'buy' / 'sell' / 'hold'
    pred_ret: 預測 N 日後報酬率（LightGBM 輸出）
    rule_score: RuleBasedAlpha 綜合得分 0~100
    actual_ret: 實際 N 日後報酬率（事後回填，用於模型評估）
    """
    __tablename__ = "quant_predictions"
    __table_args__ = (
        UniqueConstraint("code", "predict_date", "model_version", name="uq_pred_code_date_ver"),
        Index("ix_pred_code_date", "code", "predict_date"),
    )

    id:            Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    code:          Mapped[str]            = mapped_column(String(10), nullable=False)
    predict_date:  Mapped[date]           = mapped_column(Date, nullable=False)
    model_version: Mapped[str]            = mapped_column(String(20), default="v1.0")
    signal:        Mapped[str]            = mapped_column(String(10))   # buy/sell/hold
    pred_ret:      Mapped[Optional[float]]= mapped_column(Float)        # 預測報酬率
    rule_score:    Mapped[Optional[float]]= mapped_column(Float)        # 規則型評分
    confidence:    Mapped[Optional[float]]= mapped_column(Float)        # 信心度 0~1
    reasons:       Mapped[Optional[list]] = mapped_column(JSON)         # 訊號理由清單
    # 事後回填
    actual_ret:    Mapped[Optional[float]]= mapped_column(Float)        # 實際 5 日報酬
    is_correct:    Mapped[Optional[bool]] = mapped_column(Boolean)      # 方向是否正確
    eval_date:     Mapped[Optional[date]] = mapped_column(Date)         # 評估日期
    created_at:    Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)


class Trade(Base):
    """
    ExecutionEngine 產生的成交紀錄。
    支援回測紀錄（source='backtest'）和實際交易（source='live'）。
    pnl 僅在賣出時有值，buy_order_id 可關聯對應的買入訂單。
    """
    __tablename__ = "quant_trades"
    __table_args__ = (
        Index("ix_trade_code_date", "code", "trade_date"),
        Index("ix_trade_session", "session_id"),
    )

    id:           Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id:   Mapped[Optional[str]]  = mapped_column(String(50))   # 回測 session ID
    code:         Mapped[str]            = mapped_column(String(10), nullable=False)
    trade_date:   Mapped[date]           = mapped_column(Date, nullable=False)
    side:         Mapped[str]            = mapped_column(String(4))    # buy/sell
    shares:       Mapped[int]            = mapped_column(Integer)
    price:        Mapped[float]          = mapped_column(Float)
    commission:   Mapped[float]          = mapped_column(Float, default=0.0)
    tax:          Mapped[float]          = mapped_column(Float, default=0.0)
    slippage:     Mapped[float]          = mapped_column(Float, default=0.0)
    net_amount:   Mapped[float]          = mapped_column(Float)        # 正=付出，負=收入
    pnl:          Mapped[Optional[float]]= mapped_column(Float)        # 已實現損益（賣出時）
    holding_days: Mapped[Optional[int]]  = mapped_column(Integer)      # 持有天數
    buy_order_id: Mapped[Optional[str]]  = mapped_column(String(50))   # 對應買入訂單 ID
    source:       Mapped[str]            = mapped_column(String(10), default="backtest")  # backtest/live
    note:         Mapped[Optional[str]]  = mapped_column(Text)
    created_at:   Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)


# ── QuantDB 管理器 ────────────────────────────────────────────────────────────

class QuantDB:
    """
    量化系統資料庫管理器。

    使用方式：
        db = QuantDB()   # 預設讀 QUANT_DB_URL 環境變數，fallback SQLite
        await db.create_tables()

        # 寫入價格資料
        async with db.session() as sess:
            sess.add(Price(code="2330", trade_date=date.today(), ...))
            await sess.commit()

        # 查詢最新特徵
        feats = await db.get_latest_features("2330")
    """

    DEFAULT_SQLITE = "sqlite+aiosqlite:///./quant_dev.db"

    def __init__(self, url: Optional[str] = None):
        db_url = url or os.getenv("QUANT_DB_URL") or self.DEFAULT_SQLITE
        # PostgreSQL 連線池設定
        kwargs: dict[str, Any] = {"echo": False}
        if "postgresql" in db_url:
            kwargs.update({"pool_size": 5, "max_overflow": 10, "pool_pre_ping": True})
        self.engine: AsyncEngine = create_async_engine(db_url, **kwargs)
        self.SessionLocal = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    def session(self) -> AsyncSession:
        """回傳 async session context manager"""
        return self.SessionLocal()

    async def create_tables(self) -> None:
        """建立所有 quant_ 資料表（首次部署執行一次）"""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_tables(self) -> None:
        """刪除所有 quant_ 資料表（測試用，謹慎呼叫）"""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    # ── 常用查詢 ──────────────────────────────────────────────────────────

    async def get_latest_prices(self, code: str, n: int = 250) -> list[Price]:
        """取最近 n 日價格（已排序，最新在後）"""
        async with self.session() as sess:
            result = await sess.execute(
                select(Price)
                .where(Price.code == code)
                .order_by(Price.trade_date.desc())
                .limit(n)
            )
            rows = result.scalars().all()
        return list(reversed(rows))

    async def upsert_price(self, code: str, td: date, data: dict) -> None:
        """寫入或更新單日價格（ON CONFLICT DO UPDATE 模擬）"""
        async with self.session() as sess:
            existing = await sess.execute(
                select(Price).where(Price.code == code, Price.trade_date == td)
            )
            row = existing.scalar_one_or_none()
            if row:
                for k, v in data.items():
                    setattr(row, k, v)
            else:
                sess.add(Price(code=code, trade_date=td, **data))
            await sess.commit()

    async def get_latest_features(self, code: str) -> Optional[Feature]:
        """取最新特徵快照"""
        async with self.session() as sess:
            result = await sess.execute(
                select(Feature)
                .where(Feature.code == code)
                .order_by(Feature.feature_date.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def save_prediction(
        self,
        code:    str,
        pd_date: date,
        signal:  str,
        pred_ret: Optional[float] = None,
        rule_score: Optional[float] = None,
        reasons: Optional[list] = None,
        model_version: str = "v1.0",
    ) -> None:
        """儲存模型預測結果（若同日已有紀錄則覆蓋）"""
        async with self.session() as sess:
            existing = await sess.execute(
                select(Prediction).where(
                    Prediction.code == code,
                    Prediction.predict_date == pd_date,
                    Prediction.model_version == model_version,
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                row.signal     = signal
                row.pred_ret   = pred_ret
                row.rule_score = rule_score
                row.reasons    = reasons
            else:
                sess.add(Prediction(
                    code=code,
                    predict_date=pd_date,
                    model_version=model_version,
                    signal=signal,
                    pred_ret=pred_ret,
                    rule_score=rule_score,
                    reasons=reasons,
                ))
            await sess.commit()

    async def backfill_actual_returns(self, days_back: int = 5) -> int:
        """
        回填實際報酬率（在預測 N 天後呼叫，評估模型準確率）。
        回傳成功回填的筆數。
        """
        from sqlalchemy import text
        count = 0
        async with self.session() as sess:
            # 找出尚未回填的預測，且距今已超過 days_back 天
            result = await sess.execute(
                select(Prediction).where(
                    Prediction.actual_ret.is_(None),
                    Prediction.predict_date <= func.current_date() - days_back,
                )
            )
            preds = result.scalars().all()
            for pred in preds:
                target_date = pred.predict_date
                # 查找 target + days_back 的收盤價
                close_now = await sess.execute(
                    select(Price.close)
                    .where(Price.code == pred.code)
                    .order_by(Price.trade_date.desc())
                    .limit(1)
                )
                close_now_val = close_now.scalar_one_or_none()
                close_then = await sess.execute(
                    select(Price.close)
                    .where(Price.code == pred.code, Price.trade_date == target_date)
                )
                close_then_val = close_then.scalar_one_or_none()
                if close_now_val and close_then_val:
                    actual = (close_now_val - close_then_val) / close_then_val
                    pred.actual_ret  = round(actual, 4)
                    pred.is_correct  = (pred.signal == "buy" and actual > 0) or \
                                       (pred.signal == "sell" and actual < 0)
                    pred.eval_date   = date.today()
                    count += 1
            await sess.commit()
        return count

    async def get_model_accuracy(self, model_version: str = "v1.0") -> dict:
        """計算模型準確率統計"""
        async with self.session() as sess:
            result = await sess.execute(
                select(
                    func.count(Prediction.id).label("total"),
                    func.sum(
                        func.cast(Prediction.is_correct, Integer)
                    ).label("correct"),
                    func.avg(Prediction.pred_ret).label("avg_pred"),
                    func.avg(Prediction.actual_ret).label("avg_actual"),
                ).where(
                    Prediction.model_version == model_version,
                    Prediction.is_correct.isnot(None),
                )
            )
            row = result.one()
        total   = row.total   or 0
        correct = row.correct or 0
        return {
            "model_version": model_version,
            "total":    total,
            "correct":  correct,
            "accuracy": round(correct / total, 4) if total > 0 else None,
            "avg_pred_ret":   round(row.avg_pred   or 0, 4),
            "avg_actual_ret": round(row.avg_actual or 0, 4),
        }


# ── 獨立測試 ─────────────────────────────────────────────────────────────────

async def _test():
    """建立 SQLite 測試庫並驗證所有資料表"""
    import asyncio

    db = QuantDB("sqlite+aiosqlite:///./quant_test.db")
    await db.create_tables()
    print("[QuantDB] 資料表建立完成")

    # 寫入測試股票
    async with db.session() as sess:
        stock = Stock(code="2330", name="台積電", sector="半導體", market="TSE")
        sess.add(stock)
        await sess.commit()
    print("[QuantDB] 股票資料寫入完成")

    # 寫入測試價格
    await db.upsert_price("2330", date(2024, 1, 2), {
        "open": 580.0, "high": 595.0, "low": 578.0, "close": 590.0,
        "adj_close": 590.0, "volume": 25_000_000,
    })
    print("[QuantDB] 價格資料寫入完成")

    # 查詢
    prices = await db.get_latest_prices("2330", n=5)
    print(f"[QuantDB] 查到 {len(prices)} 筆價格")

    # 儲存預測
    await db.save_prediction("2330", date(2024, 1, 2), "buy", pred_ret=0.025, rule_score=72.5)
    print("[QuantDB] 預測結果儲存完成")

    accuracy = await db.get_model_accuracy()
    print(f"[QuantDB] 模型準確率: {accuracy}")

    # 清理測試資料庫
    await db.drop_tables()
    import os
    if os.path.exists("./quant_test.db"):
        os.remove("./quant_test.db")
    print("[QuantDB] 測試完成，已清理測試資料庫")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())
