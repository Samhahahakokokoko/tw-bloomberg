"""Analyst Consensus Engine — 計算多分析師共識強度並整合進 Alpha 系統"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from loguru import logger
from sqlalchemy import select, and_, func

SENTIMENT_SCORE = {
    "strong_bullish": +2.0,
    "bullish":        +1.0,
    "neutral":         0.0,
    "bearish":        -1.0,
    "strong_bearish": -2.0,
}


@dataclass
class ConsensusResult:
    stock_id:        str
    stock_name:      str
    consensus_score: float      # 0-100
    bullish_count:   int
    bearish_count:   int
    total_analysts:  int
    high_cred_count: int
    key_thesis:      list[str]
    is_divergent:    bool = False

    @property
    def strength_icons(self) -> str:
        if self.consensus_score >= 80:
            return "🔥🔥🔥"
        elif self.consensus_score >= 55:
            return "🔥🔥"
        elif self.consensus_score >= 30:
            return "🔥"
        return "─"

    def to_line_text(self) -> str:
        lines = [
            f"{self.strength_icons} {self.stock_id} {self.stock_name}",
            f"共識強度：{self.consensus_score:.0f}/100",
            f"提及：{self.total_analysts}位分析師（{self.high_cred_count}位高可信）",
        ]
        if self.key_thesis:
            lines.append(f"論點：{'、'.join(self.key_thesis[:2])}")
        if self.is_divergent:
            lines.append(f"⚠️ 高分歧：看多{self.bullish_count}位 vs 看空{self.bearish_count}位")
        return "\n".join(lines)


async def calculate_daily_consensus(days: int = 7) -> list[ConsensusResult]:
    """計算過去 N 日的分析師共識（每日 17:00 執行）"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import AnalystCall, Analyst
    from .twse_service import fetch_realtime_quote

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    results: list[ConsensusResult] = []

    async with AsyncSessionLocal() as db:
        # 取得所有分析師的可信度
        r = await db.execute(select(Analyst).where(Analyst.is_active == True))
        analysts    = r.scalars().all()
        cred_map    = {a.analyst_id: a.reliability_score for a in analysts}
        win_map     = {a.analyst_id: a.win_rate for a in analysts}

        # 取得近 N 日所有推薦
        r2 = await db.execute(
            select(AnalystCall).where(AnalystCall.date >= cutoff)
        )
        calls = r2.scalars().all()

    # 按股票聚合
    stock_map: dict[str, list] = {}
    for c in calls:
        if c.stock_id not in stock_map:
            stock_map[c.stock_id] = []
        stock_map[c.stock_id].append(c)

    for stock_id, stock_calls in stock_map.items():
        total        = len(stock_calls)
        bullish      = sum(1 for c in stock_calls if c.sentiment in ("bullish", "strong_bullish"))
        bearish      = sum(1 for c in stock_calls if c.sentiment in ("bearish", "strong_bearish"))
        high_cred    = sum(1 for c in stock_calls if cred_map.get(c.analyst_id, 50) >= 65)

        # 加權共識分數
        weighted_sum = 0.0
        weight_total = 0.0
        for c in stock_calls:
            cred     = cred_map.get(c.analyst_id, 50) / 100
            sent_val = SENTIMENT_SCORE.get(c.sentiment, 0)
            # 反向指標：勝率 < 35% 則反轉信號
            if win_map.get(c.analyst_id, 0.5) < 0.35:
                sent_val = -sent_val
            weighted_sum += cred * sent_val
            weight_total += cred

        if weight_total == 0:
            continue

        raw_score = weighted_sum / weight_total   # -2 to +2
        # 正規化到 0-100
        consensus = (raw_score + 2) / 4 * 100

        # 高分歧檢測
        is_divergent = bullish > 0 and bearish > 0 and abs(bullish - bearish) <= 1

        # 整合論點
        all_points: list[str] = []
        for c in stock_calls:
            try:
                pts = json.loads(c.key_points or "[]")
                all_points.extend(pts)
            except Exception:
                pass
        unique_thesis = list(dict.fromkeys(all_points))[:3]

        # 取股票名稱
        stock_name = stock_calls[0].stock_name if stock_calls[0].stock_name else stock_id

        results.append(ConsensusResult(
            stock_id        = stock_id,
            stock_name      = stock_name,
            consensus_score = round(consensus, 1),
            bullish_count   = bullish,
            bearish_count   = bearish,
            total_analysts  = total,
            high_cred_count = high_cred,
            key_thesis      = unique_thesis,
            is_divergent    = is_divergent,
        ))

    results.sort(key=lambda r: -r.consensus_score)
    return results


async def save_consensus_to_db(results: list[ConsensusResult]):
    """儲存每日共識分數到資料庫"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import AnalystConsensusDaily

    today = datetime.now().strftime("%Y-%m-%d")
    async with AsyncSessionLocal() as db:
        for res in results:
            r = await db.execute(
                select(AnalystConsensusDaily)
                .where(and_(
                    AnalystConsensusDaily.date == today,
                    AnalystConsensusDaily.stock_id == res.stock_id,
                ))
            )
            rec = r.scalar_one_or_none()
            if rec is None:
                rec = AnalystConsensusDaily(date=today, stock_id=res.stock_id)
                db.add(rec)
            rec.stock_name      = res.stock_name
            rec.consensus_score = res.consensus_score
            rec.bullish_count   = res.bullish_count
            rec.bearish_count   = res.bearish_count
            rec.total_analysts  = res.total_analysts
            rec.high_cred_count = res.high_cred_count
            rec.key_thesis      = "、".join(res.key_thesis)
            rec.is_divergent    = res.is_divergent
        await db.commit()
    logger.info(f"[consensus] saved {len(results)} consensus records")


async def get_stock_consensus(stock_id: str) -> ConsensusResult | None:
    """查詢特定股票的最新共識"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import AnalystConsensusDaily
    from sqlalchemy import desc

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(AnalystConsensusDaily)
            .where(AnalystConsensusDaily.stock_id == stock_id)
            .order_by(desc(AnalystConsensusDaily.date))
            .limit(1)
        )
        rec = r.scalar_one_or_none()

    if not rec:
        return None
    return ConsensusResult(
        stock_id        = rec.stock_id,
        stock_name      = rec.stock_name,
        consensus_score = rec.consensus_score,
        bullish_count   = rec.bullish_count,
        bearish_count   = rec.bearish_count,
        total_analysts  = rec.total_analysts,
        high_cred_count = rec.high_cred_count,
        key_thesis      = rec.key_thesis.split("、") if rec.key_thesis else [],
        is_divergent    = rec.is_divergent,
    )


def get_consensus_boost(consensus: ConsensusResult | None) -> float:
    """計算信心指數調整值（供 decision_engine Step 11 使用）"""
    if consensus is None:
        return 0.0
    if consensus.is_divergent:
        return -5.0
    if consensus.consensus_score >= 75 and consensus.high_cred_count >= 3:
        return +5.0
    elif consensus.consensus_score >= 60 and consensus.high_cred_count >= 2:
        return +3.0
    elif consensus.consensus_score >= 80:
        return +2.0
    return 0.0


async def run_daily_consensus():
    """每日 17:00 計算並儲存共識"""
    try:
        results = await calculate_daily_consensus(days=7)
        await save_consensus_to_db(results)
        logger.info(f"[consensus] daily run complete: {len(results)} stocks")
    except Exception as e:
        logger.error(f"[consensus] daily run failed: {e}")
