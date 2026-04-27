"""多維度選股引擎

支援：
  1. 結構化篩選（preset 或自訂條件）
  2. 自然語言篩選（由 nl_query_parser 解析後傳入）
  3. 從 stock_scores 快取快速查詢（毫秒級回應）
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from loguru import logger
from sqlalchemy import select, and_

from ..models.database import AsyncSessionLocal
from ..models.models import StockScore


# ── 篩選條件資料結構 ──────────────────────────────────────────────────────────

@dataclass
class ScreenerFilter:
    # 基本面
    revenue_yoy_min:       float | None = None   # 月營收 YoY 最低 %
    gross_margin_min:      float | None = None   # 毛利率最低 %
    three_margins_up:      bool  | None = None   # 是否三率齊升
    eps_growth_qtrs_min:   int   | None = None   # 連續 EPS 成長最少季數

    # 籌碼面
    foreign_consec_buy_min: int  | None = None   # 外資連續買超最少天
    trust_consec_buy_min:   int  | None = None   # 投信連續買超最少天
    dual_signal:            bool | None = None   # 外資+投信雙強
    foreign_net_5d_min:     int  | None = None   # 近 5 日外資淨買量（張）

    # 技術面
    ma_aligned:    bool | None = None   # 均線多頭排列
    kd_golden_cross: bool | None = None  # KD 黃金交叉
    vol_breakout:  bool | None = None   # 量能突破
    bb_breakout:   bool | None = None   # 布林上軌突破

    # 評分門檻
    fundamental_score_min: float | None = None
    chip_score_min:        float | None = None
    technical_score_min:   float | None = None
    total_score_min:       float | None = None

    # 排序
    sort_by:   str = "total_score"    # total_score / confidence / fundamental_score
    limit:     int = 20


# ── 預設篩選組合 ──────────────────────────────────────────────────────────────

PRESETS: dict[str, ScreenerFilter] = {
    "strong_fundamental": ScreenerFilter(
        revenue_yoy_min=20, gross_margin_min=30, three_margins_up=True,
        eps_growth_qtrs_min=2, total_score_min=60,
        sort_by="fundamental_score",
    ),
    "institutional_favorite": ScreenerFilter(
        foreign_consec_buy_min=3, dual_signal=True,
        chip_score_min=65, sort_by="chip_score",
    ),
    "technical_breakout": ScreenerFilter(
        ma_aligned=True, vol_breakout=True,
        technical_score_min=65, sort_by="technical_score",
    ),
    "golden_triangle": ScreenerFilter(
        # 三維度都好
        fundamental_score_min=55,
        chip_score_min=55,
        technical_score_min=55,
        total_score_min=60,
        sort_by="total_score",
    ),
    "high_conviction": ScreenerFilter(
        three_margins_up=True, foreign_consec_buy_min=3,
        ma_aligned=True, kd_golden_cross=True,
        total_score_min=65,
        sort_by="total_score",
    ),
}


# ── 核心篩選邏輯 ──────────────────────────────────────────────────────────────

async def run_screener(
    filters: ScreenerFilter,
    score_date: str = "",
) -> list[dict]:
    """
    從 stock_scores 表快速篩選。
    預設查最新一天的評分快照。
    """
    if not score_date:
        score_date = date.today().strftime("%Y-%m-%d")

    conditions = [StockScore.score_date == score_date]

    # 基本面
    if filters.revenue_yoy_min is not None:
        conditions.append(StockScore.revenue_yoy >= filters.revenue_yoy_min)
    if filters.gross_margin_min is not None:
        conditions.append(StockScore.gross_margin >= filters.gross_margin_min)
    if filters.three_margins_up is True:
        conditions.append(StockScore.three_margins_up == True)
    if filters.eps_growth_qtrs_min is not None:
        conditions.append(StockScore.eps_growth_qtrs >= filters.eps_growth_qtrs_min)

    # 籌碼面
    if filters.foreign_consec_buy_min is not None:
        conditions.append(StockScore.foreign_consec_buy >= filters.foreign_consec_buy_min)
    if filters.trust_consec_buy_min is not None:
        conditions.append(StockScore.trust_consec_buy >= filters.trust_consec_buy_min)
    if filters.dual_signal is True:
        conditions.append(StockScore.foreign_consec_buy >= 1)
        conditions.append(StockScore.trust_consec_buy >= 1)

    # 技術面
    if filters.ma_aligned is True:
        conditions.append(StockScore.ma_aligned == True)
    if filters.kd_golden_cross is True:
        conditions.append(StockScore.kd_golden_cross == True)
    if filters.vol_breakout is True:
        conditions.append(StockScore.vol_breakout == True)
    if filters.bb_breakout is True:
        conditions.append(StockScore.bb_breakout == True)

    # 評分門檻
    if filters.fundamental_score_min is not None:
        conditions.append(StockScore.fundamental_score >= filters.fundamental_score_min)
    if filters.chip_score_min is not None:
        conditions.append(StockScore.chip_score >= filters.chip_score_min)
    if filters.technical_score_min is not None:
        conditions.append(StockScore.technical_score >= filters.technical_score_min)
    if filters.total_score_min is not None:
        conditions.append(StockScore.total_score >= filters.total_score_min)

    # 排序欄位對應
    sort_col = {
        "total_score":        StockScore.total_score,
        "confidence":         StockScore.confidence,
        "fundamental_score":  StockScore.fundamental_score,
        "chip_score":         StockScore.chip_score,
        "technical_score":    StockScore.technical_score,
    }.get(filters.sort_by, StockScore.total_score)

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(StockScore)
            .where(and_(*conditions))
            .order_by(sort_col.desc())
            .limit(filters.limit)
        )
        rows = r.scalars().all()

    # 若今日無資料，往前找最近一天
    if not rows:
        async with AsyncSessionLocal() as db:
            r2 = await db.execute(
                select(StockScore.score_date)
                .order_by(StockScore.score_date.desc())
                .limit(1)
            )
            latest_date = r2.scalar()

        if latest_date and latest_date != score_date:
            logger.info(f"[Screener] {score_date} 無資料，改用 {latest_date}")
            return await run_screener(filters, score_date=latest_date)
        return []

    return [_row_to_dict(r) for r in rows]


def _row_to_dict(r: StockScore) -> dict:
    return {
        "stock_code":         r.stock_code,
        "stock_name":         r.stock_name or "",
        "score_date":         r.score_date,
        "total_score":        r.total_score,
        "fundamental_score":  r.fundamental_score,
        "chip_score":         r.chip_score,
        "technical_score":    r.technical_score,
        "confidence":         r.confidence,
        "ai_reason":          r.ai_reason or "",
        # 明細指標
        "revenue_yoy":        r.revenue_yoy,
        "gross_margin":       r.gross_margin,
        "three_margins_up":   r.three_margins_up,
        "eps_growth_qtrs":    r.eps_growth_qtrs,
        "foreign_consec_buy": r.foreign_consec_buy,
        "trust_consec_buy":   r.trust_consec_buy,
        "ma_aligned":         r.ma_aligned,
        "kd_golden_cross":    r.kd_golden_cross,
        "vol_breakout":       r.vol_breakout,
        "bb_breakout":        r.bb_breakout,
    }


async def get_top_scores(limit: int = 20, score_date: str = "") -> list[dict]:
    """取得最新評分排行（總分前 N）"""
    return await run_screener(
        ScreenerFilter(total_score_min=0, limit=limit, sort_by="total_score"),
        score_date=score_date,
    )


async def get_stock_score(stock_code: str, score_date: str = "") -> dict | None:
    """取得單一股票的最新評分"""
    if not score_date:
        score_date = date.today().strftime("%Y-%m-%d")
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(StockScore)
            .where(StockScore.stock_code == stock_code)
            .order_by(StockScore.score_date.desc())
            .limit(1)
        )
        row = r.scalar_one_or_none()
    return _row_to_dict(row) if row else None
