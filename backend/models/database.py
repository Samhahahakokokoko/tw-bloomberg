from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy import text
from pydantic_settings import BaseSettings
from loguru import logger


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./data/bloomberg.db"
    anthropic_api_key: str = ""
    line_channel_access_token: str = ""
    line_channel_secret: str = ""
    finmind_token: str = ""   # FinMind API token（免費版可留空）

    class Config:
        env_file = ".env"
        extra = "ignore"

    @property
    def async_database_url(self) -> str:
        """Railway 給的 postgresql:// 轉成 asyncpg 格式"""
        url = self.database_url
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url


settings = Settings()

_db_url = settings.async_database_url
_is_sqlite = "sqlite" in _db_url
engine = create_async_engine(
    _db_url,
    echo=False,
    connect_args={"check_same_thread": False} if _is_sqlite else {"timeout": 10},
    pool_pre_ping=True,
)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# ALTER TABLE 遷移 — 安全加欄位（已存在則忽略）
_SQLITE_MIGRATIONS = [
    "ALTER TABLE portfolio  ADD COLUMN user_id VARCHAR(100) NOT NULL DEFAULT ''",
    "ALTER TABLE alerts     ADD COLUMN user_id VARCHAR(100) NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS ix_portfolio_user_id  ON portfolio(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_alerts_user_id     ON alerts(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_tradelog_user_date ON trade_log(user_id, trade_date)",
    "CREATE INDEX IF NOT EXISTS ix_qh_user_hash       ON query_history(user_id, topic_hash)",
    "CREATE INDEX IF NOT EXISTS ix_watchlist_user     ON watchlist(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_perf_user_date     ON performance_records(user_id, record_date)",
    "CREATE INDEX IF NOT EXISTS ix_earnings_user      ON earnings_reminders(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_earnings_code      ON earnings_reminders(stock_code)",
    "CREATE INDEX IF NOT EXISTS ix_fin_code_yq        ON stock_financials(stock_code, year, quarter)",
    "CREATE INDEX IF NOT EXISTS ix_rev_code_ym        ON monthly_revenue(stock_code, year, month)",
    "CREATE INDEX IF NOT EXISTS ix_scores_date        ON stock_scores(score_date)",
    "CREATE INDEX IF NOT EXISTS ix_scores_total       ON stock_scores(total_score)",
    "CREATE INDEX IF NOT EXISTS ix_industry_sent_date ON industry_sentiment(analysis_date)",
    "CREATE INDEX IF NOT EXISTS ix_rec_date           ON recommendation_results(recommend_date)",
    "CREATE INDEX IF NOT EXISTS ix_rec_code           ON recommendation_results(stock_code)",
    "CREATE INDEX IF NOT EXISTS ix_broker_date_code   ON broker_activity(date, stock_code)",
    "CREATE INDEX IF NOT EXISTS ix_broker_name        ON broker_activity(broker_name)",
]

_PG_MIGRATIONS = [
    """DO $$ BEGIN
         ALTER TABLE portfolio  ADD COLUMN user_id VARCHAR(100) NOT NULL DEFAULT '';
       EXCEPTION WHEN duplicate_column THEN NULL; END $$""",
    """DO $$ BEGIN
         ALTER TABLE alerts     ADD COLUMN user_id VARCHAR(100) NOT NULL DEFAULT '';
       EXCEPTION WHEN duplicate_column THEN NULL; END $$""",
    "CREATE INDEX IF NOT EXISTS ix_portfolio_user_id  ON portfolio(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_alerts_user_id     ON alerts(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_tradelog_user_date ON trade_log(user_id, trade_date)",
    "CREATE INDEX IF NOT EXISTS ix_qh_user_hash       ON query_history(user_id, topic_hash)",
    "CREATE INDEX IF NOT EXISTS ix_watchlist_user     ON watchlist(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_perf_user_date     ON performance_records(user_id, record_date)",
    "CREATE INDEX IF NOT EXISTS ix_earnings_user      ON earnings_reminders(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_earnings_code      ON earnings_reminders(stock_code)",
    "CREATE INDEX IF NOT EXISTS ix_fin_code_yq        ON stock_financials(stock_code, year, quarter)",
    "CREATE INDEX IF NOT EXISTS ix_rev_code_ym        ON monthly_revenue(stock_code, year, month)",
    "CREATE INDEX IF NOT EXISTS ix_scores_date        ON stock_scores(score_date)",
    "CREATE INDEX IF NOT EXISTS ix_scores_total       ON stock_scores(total_score)",
    "CREATE INDEX IF NOT EXISTS ix_industry_sent_date ON industry_sentiment(analysis_date)",
    "CREATE INDEX IF NOT EXISTS ix_rec_date           ON recommendation_results(recommend_date)",
    "CREATE INDEX IF NOT EXISTS ix_rec_code           ON recommendation_results(stock_code)",
    "CREATE INDEX IF NOT EXISTS ix_broker_date_code   ON broker_activity(date, stock_code)",
    "CREATE INDEX IF NOT EXISTS ix_broker_name        ON broker_activity(broker_name)",
]


async def init_db():
    migrations = _PG_MIGRATIONS if not _is_sqlite else _SQLITE_MIGRATIONS
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for sql in migrations:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass
    logger.info(f"Database ready ({'SQLite' if _is_sqlite else 'PostgreSQL'})")
