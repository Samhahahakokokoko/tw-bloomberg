"""
signal_db.py — 量化系統補強資料表

新增三個資料表：
  strategy_signals  — 每股每日策略訊號快照（score, action, confidence）
  user_settings     — 使用者偏好設定（capital, risk_level, preferred_strategy）
  alerts_log        — 訊號觸發警報記錄（stock_id, type, message, timestamp）

架構特點：
  - 與 quant/database.py 的 Base 共用（同一個 metadata），可用同一個 engine 建表
  - 提供 SignalDB 管理器：crud 操作 + 常用查詢
  - 支援 async SQLAlchemy 2.x
  - 可獨立建表（python -m quant.signal_db）
"""

from __future__ import annotations

import os
from datetime import datetime, date
from typing import Optional, Any

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, Float,
    Index, Integer, JSON, String, Text, UniqueConstraint, select, func,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, create_async_engine, async_sessionmaker,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ── 資料表 ─────────────────────────────────────────────────────────────────

class StrategySignalRecord(Base):
    """
    每股每日策略訊號快照。
    每次排程（收盤後）或手動觸發時寫入；前端查詢最新一筆。
    """
    __tablename__ = "strategy_signals"
    __table_args__ = (
        UniqueConstraint("stock_id", "signal_date", "strategy", name="uq_sig_stock_date_strat"),
        Index("ix_sig_stock_date", "stock_id", "signal_date"),
    )

    id:           Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    stock_id:     Mapped[str]            = mapped_column(String(10), nullable=False)
    name:         Mapped[str]            = mapped_column(String(50), default="")
    signal_date:  Mapped[date]           = mapped_column(Date, nullable=False)
    strategy:     Mapped[str]            = mapped_column(String(20), nullable=False)  # composite/momentum/value/chip
    regime:       Mapped[Optional[str]]  = mapped_column(String(20))                  # bull/bear/sideways
    score:        Mapped[float]          = mapped_column(Float)                        # composite 0~100
    confidence:   Mapped[float]          = mapped_column(Float)                        # 0~100
    action:       Mapped[str]            = mapped_column(String(10))                   # 強力買進/買進/...
    risk_level:   Mapped[Optional[str]]  = mapped_column(String(5))                   # 低/中/高
    target_price: Mapped[Optional[float]]= mapped_column(Float)
    stop_loss:    Mapped[Optional[float]]= mapped_column(Float)
    holding_days: Mapped[Optional[int]]  = mapped_column(Integer)
    reasons:      Mapped[Optional[list]] = mapped_column(JSON)                         # list[str]
    sub_scores:   Mapped[Optional[dict]] = mapped_column(JSON)                         # {momentum, value, chip}
    created_at:   Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)


class UserSettings(Base):
    """
    使用者偏好設定。
    LINE user_id 為識別鍵；支援多個 preferred_strategy。
    """
    __tablename__ = "user_settings"

    id:                 Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:            Mapped[str]            = mapped_column(String(50), nullable=False, unique=True, index=True)
    display_name:       Mapped[Optional[str]]  = mapped_column(String(100))
    capital:            Mapped[float]          = mapped_column(Float, default=100_000)    # 可用資金（元）
    risk_level:         Mapped[str]            = mapped_column(String(5), default="中")   # 低/中/高
    preferred_strategy: Mapped[str]            = mapped_column(String(20), default="composite")
    odd_lot_discount:   Mapped[float]          = mapped_column(Float, default=0.6)        # 手續費折扣
    watchlist:          Mapped[Optional[list]] = mapped_column(JSON)                      # ["2330","0056"]
    notification_on:    Mapped[bool]           = mapped_column(Boolean, default=True)
    created_at:         Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:         Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow,
                                                               onupdate=datetime.utcnow)


class AlertsLog(Base):
    """
    訊號觸發警報記錄。
    每次策略訊號達到閾值（如信心 > 80）或價格突破關鍵位時寫入。
    """
    __tablename__ = "alerts_log"
    __table_args__ = (
        Index("ix_alert_stock_ts", "stock_id", "triggered_at"),
    )

    id:           Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:      Mapped[Optional[str]] = mapped_column(String(50))   # 若為個人警報
    stock_id:     Mapped[str]           = mapped_column(String(10), nullable=False)
    name:         Mapped[Optional[str]] = mapped_column(String(50))
    alert_type:   Mapped[str]           = mapped_column(String(30))   # signal_high/price_break/stop_loss/...
    message:      Mapped[str]           = mapped_column(Text)
    confidence:   Mapped[Optional[float]]= mapped_column(Float)
    action:       Mapped[Optional[str]] = mapped_column(String(10))
    triggered_at: Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow, index=True)
    is_sent:      Mapped[bool]          = mapped_column(Boolean, default=False)   # 是否已推播
    sent_at:      Mapped[Optional[datetime]] = mapped_column(DateTime)


# ── SignalDB 管理器 ────────────────────────────────────────────────────────────

class SignalDB:
    """
    signal_db 資料表管理器。

    使用方式：
        db = SignalDB()   # 讀 SIGNAL_DB_URL 或 fallback SQLite
        await db.create_tables()

        await db.save_signal(signal)
        rows = await db.get_latest_signals(min_confidence=60)
        await db.log_alert(stock_id, "signal_high", "2330 信心85，強力買進")
    """

    DEFAULT_SQLITE = "sqlite+aiosqlite:///./quant_signals.db"

    def __init__(self, url: Optional[str] = None):
        db_url = url or os.getenv("SIGNAL_DB_URL") or os.getenv("DATABASE_URL") or self.DEFAULT_SQLITE
        # asyncpg 不支援 postgresql://，需換成 postgresql+asyncpg://
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif db_url.startswith("postgresql://") and "+asyncpg" not in db_url:
            db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

        kwargs: dict[str, Any] = {"echo": False}
        if "postgresql" in db_url:
            kwargs.update({"pool_size": 5, "max_overflow": 10, "pool_pre_ping": True})
        self.engine: AsyncEngine = create_async_engine(db_url, **kwargs)
        self.SessionLocal = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    def session(self) -> AsyncSession:
        return self.SessionLocal()

    async def create_tables(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_tables(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    # ── strategy_signals CRUD ─────────────────────────────────────────────

    async def save_signal(self, signal, regime: str = "unknown") -> None:
        """
        儲存 StrategySignal 到資料庫（若同日同策略已存在則更新）。
        signal: StrategySignal instance（from strategy_engine.py）
        """
        d = signal.to_dict()
        today = date.today()
        async with self.session() as sess:
            existing = await sess.execute(
                select(StrategySignalRecord).where(
                    StrategySignalRecord.stock_id   == signal.stock_id,
                    StrategySignalRecord.signal_date == today,
                    StrategySignalRecord.strategy    == signal.strategy,
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                row.score        = d["scores"]["composite"]
                row.confidence   = d["confidence"]
                row.action       = d["action"]
                row.risk_level   = d["risk_level"]
                row.target_price = d["target_price"]
                row.stop_loss    = d["stop_loss"]
                row.holding_days = d["holding_days"]
                row.reasons      = d["reasons"]
                row.sub_scores   = d["scores"]
                row.regime       = regime
            else:
                sess.add(StrategySignalRecord(
                    stock_id=signal.stock_id,
                    name=signal.name,
                    signal_date=today,
                    strategy=signal.strategy,
                    regime=regime,
                    score=d["scores"]["composite"],
                    confidence=d["confidence"],
                    action=d["action"],
                    risk_level=d["risk_level"],
                    target_price=d["target_price"],
                    stop_loss=d["stop_loss"],
                    holding_days=d["holding_days"],
                    reasons=d["reasons"],
                    sub_scores=d["scores"],
                ))
            await sess.commit()

    async def get_latest_signals(
        self,
        strategy:       str   = "composite",
        min_confidence: float = 0.0,
        action_filter:  Optional[str] = None,   # "強力買進" / "買進" / ...
        limit:          int   = 50,
    ) -> list[dict]:
        """取最新訊號（今日，按信心降序）"""
        today = date.today()
        async with self.session() as sess:
            q = select(StrategySignalRecord).where(
                StrategySignalRecord.signal_date == today,
                StrategySignalRecord.strategy    == strategy,
                StrategySignalRecord.confidence  >= min_confidence,
            )
            if action_filter:
                q = q.where(StrategySignalRecord.action == action_filter)
            q = q.order_by(StrategySignalRecord.confidence.desc()).limit(limit)
            result = await sess.execute(q)
            rows = result.scalars().all()
        return [
            {
                "stock_id":    r.stock_id,
                "name":        r.name,
                "action":      r.action,
                "confidence":  r.confidence,
                "score":       r.score,
                "risk_level":  r.risk_level,
                "target_price":r.target_price,
                "stop_loss":   r.stop_loss,
                "reasons":     r.reasons,
                "regime":      r.regime,
            }
            for r in rows
        ]

    # ── user_settings CRUD ────────────────────────────────────────────────

    async def get_or_create_user(self, user_id: str, display_name: str = "") -> dict:
        """取得或建立使用者設定"""
        async with self.session() as sess:
            result = await sess.execute(
                select(UserSettings).where(UserSettings.user_id == user_id)
            )
            row = result.scalar_one_or_none()
            if not row:
                row = UserSettings(user_id=user_id, display_name=display_name)
                sess.add(row)
                await sess.commit()
            return {
                "user_id":            row.user_id,
                "capital":            row.capital,
                "risk_level":         row.risk_level,
                "preferred_strategy": row.preferred_strategy,
                "odd_lot_discount":   row.odd_lot_discount,
                "watchlist":          row.watchlist or [],
                "notification_on":    row.notification_on,
            }

    async def update_user(self, user_id: str, **kwargs) -> None:
        """更新使用者設定（傳入關鍵字參數）"""
        allowed = {"capital", "risk_level", "preferred_strategy",
                   "odd_lot_discount", "watchlist", "notification_on", "display_name"}
        async with self.session() as sess:
            result = await sess.execute(
                select(UserSettings).where(UserSettings.user_id == user_id)
            )
            row = result.scalar_one_or_none()
            if not row:
                row = UserSettings(user_id=user_id)
                sess.add(row)
            for k, v in kwargs.items():
                if k in allowed:
                    setattr(row, k, v)
            await sess.commit()

    # ── alerts_log CRUD ───────────────────────────────────────────────────

    async def log_alert(
        self,
        stock_id:   str,
        alert_type: str,
        message:    str,
        name:       str   = "",
        user_id:    Optional[str] = None,
        confidence: Optional[float] = None,
        action:     Optional[str]   = None,
    ) -> None:
        """記錄一筆警報"""
        async with self.session() as sess:
            sess.add(AlertsLog(
                user_id=user_id,
                stock_id=stock_id,
                name=name,
                alert_type=alert_type,
                message=message,
                confidence=confidence,
                action=action,
            ))
            await sess.commit()

    async def get_unsent_alerts(self, limit: int = 50) -> list[dict]:
        """取得尚未推播的警報"""
        async with self.session() as sess:
            result = await sess.execute(
                select(AlertsLog)
                .where(AlertsLog.is_sent == False)
                .order_by(AlertsLog.triggered_at.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
        return [
            {
                "id":          r.id,
                "user_id":     r.user_id,
                "stock_id":    r.stock_id,
                "name":        r.name,
                "alert_type":  r.alert_type,
                "message":     r.message,
                "confidence":  r.confidence,
                "action":      r.action,
                "triggered_at":r.triggered_at.isoformat() if r.triggered_at else "",
            }
            for r in rows
        ]

    async def mark_alerts_sent(self, alert_ids: list[int]) -> None:
        """批次標記已推播"""
        async with self.session() as sess:
            result = await sess.execute(
                select(AlertsLog).where(AlertsLog.id.in_(alert_ids))
            )
            rows = result.scalars().all()
            for r in rows:
                r.is_sent = True
                r.sent_at = datetime.utcnow()
            await sess.commit()

    async def get_alert_stats(self, days: int = 7) -> dict:
        """近 N 日警報統計"""
        from sqlalchemy import text
        async with self.session() as sess:
            result = await sess.execute(
                select(
                    func.count(AlertsLog.id).label("total"),
                    func.sum(func.cast(AlertsLog.is_sent, Integer)).label("sent"),
                ).where(
                    AlertsLog.triggered_at >= func.current_timestamp() - func.cast(f"{days} days", Text)
                )
            )
            row = result.one()
        return {"total": row.total or 0, "sent": row.sent or 0, "days": days}


# ── 全域單例 ─────────────────────────────────────────────────────────────────

_global_signal_db: Optional[SignalDB] = None

def get_signal_db() -> SignalDB:
    global _global_signal_db
    if _global_signal_db is None:
        _global_signal_db = SignalDB()
    return _global_signal_db


# ── 獨立測試 ─────────────────────────────────────────────────────────────────

async def _test():
    import os
    db = SignalDB("sqlite+aiosqlite:///./quant_signals_test.db")
    await db.create_tables()
    print("[SignalDB] 資料表建立完成")

    # 測試 StrategySignal 儲存
    from quant.strategy_engine import StrategyEngine, MOCK_STOCKS
    engine = StrategyEngine()
    for s in MOCK_STOCKS[:3]:
        sig = engine.evaluate(s, regime="bull")
        await db.save_signal(sig, regime="bull")
    print("[SignalDB] 已儲存 3 筆訊號")

    rows = await db.get_latest_signals(min_confidence=50)
    print(f"[SignalDB] 查詢到 {len(rows)} 筆信心>=50")
    for r in rows:
        print(f"  {r['stock_id']} {r['action']} 信心={r['confidence']:.0f}")

    # 測試使用者設定
    u = await db.get_or_create_user("U_test_001", "測試用戶")
    print(f"\n[SignalDB] 使用者: {u}")
    await db.update_user("U_test_001", capital=500_000, risk_level="低")

    # 測試警報
    await db.log_alert("2330", "signal_high", "台積電信心85，強力買進", "台積電",
                       confidence=85, action="強力買進")
    alerts = await db.get_unsent_alerts()
    print(f"\n[SignalDB] 未推播警報: {len(alerts)} 筆")

    await db.drop_tables()
    if os.path.exists("./quant_signals_test.db"):
        os.remove("./quant_signals_test.db")
    print("[SignalDB] 測試完成，已清理")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())
