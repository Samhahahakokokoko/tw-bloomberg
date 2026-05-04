"""Analyst Quality Engine — 自動計算品質分數並調整 Tier"""
from __future__ import annotations

from datetime import datetime, timedelta
from loguru import logger
from sqlalchemy import select, desc


def compute_quality_score(
    win_rate: float,
    avg_return: float,
    timing_score: float,
    consistency: float,
    chase_high_rate: float,
) -> float:
    """
    品質分數公式（0-1）：
    quality = win_rate*0.35 + avg_return_norm*0.25 + timing*0.20 + consistency*0.10 + (1-chase)*0.10
    """
    avg_ret_norm = min(1.0, max(0.0, (avg_return + 0.10) / 0.20))  # 正規化 -10%~+10% → 0~1
    quality = (
        win_rate            * 0.35 +
        avg_ret_norm        * 0.25 +
        timing_score        * 0.20 +
        consistency         * 0.10 +
        (1 - chase_high_rate) * 0.10
    )
    return round(min(1.0, max(0.0, quality)), 4)


def quality_to_tier(quality: float) -> str:
    """依品質分數決定 Tier"""
    if quality >= 0.70:
        return "S"
    elif quality >= 0.55:
        return "A"
    elif quality >= 0.40:
        return "B"
    return "C"


async def calculate_timing_score(analyst_id: str) -> float:
    """計算進場時機分數（在低點推薦 = 高分）"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import AnalystCall

    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(AnalystCall)
            .where(AnalystCall.analyst_id == analyst_id)
            .where(AnalystCall.date >= cutoff)
            .where(AnalystCall.result_5d != None)
        )
        calls = r.scalars().all()

    if not calls:
        return 0.5  # 預設中性

    # 計算推薦後5日的正向比例
    good_timing = sum(1 for c in calls
                      if c.result_5d is not None and c.result_5d > 0.01
                      and c.sentiment in ("bullish", "strong_bullish"))
    total_bullish = sum(1 for c in calls
                        if c.sentiment in ("bullish", "strong_bullish"))
    if total_bullish == 0:
        return 0.5
    return round(good_timing / total_bullish, 4)


async def calculate_consistency(analyst_id: str) -> float:
    """計算觀點一致性（不頻繁改變立場）"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import AnalystViewChange

    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(AnalystViewChange)
            .where(AnalystViewChange.analyst_id == analyst_id)
            .where(AnalystViewChange.date >= cutoff)
        )
        changes = r.scalars().all()

    # 每月改變次數越少，一致性越高
    months     = 3
    changes_pm = len(changes) / months
    # 0次/月 → 1.0，1次/月 → 0.8，2次/月 → 0.6，≥3次/月 → 0.4
    consistency = max(0.2, 1.0 - changes_pm * 0.2)
    return round(consistency, 4)


async def calculate_chase_high_rate(analyst_id: str) -> float:
    """計算追高比例（在高點推薦的比例）"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import AnalystCall

    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(AnalystCall)
            .where(AnalystCall.analyst_id == analyst_id)
            .where(AnalystCall.date >= cutoff)
            .where(AnalystCall.sentiment.in_(["bullish", "strong_bullish"]))
        )
        calls = r.scalars().all()

    if not calls:
        return 0.5

    # 推薦後5日下跌 = 在高點推薦
    chased = sum(1 for c in calls
                 if c.result_5d is not None and c.result_5d < -0.02)
    return round(chased / len(calls), 4)


async def update_analyst_quality(analyst_id: str):
    """更新單一分析師的品質指標"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Analyst
    from .analyst_source_manager import TIER_WEIGHTS

    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Analyst).where(Analyst.analyst_id == analyst_id))
        a = r.scalar_one_or_none()
        if not a:
            return

    timing       = await calculate_timing_score(analyst_id)
    consistency  = await calculate_consistency(analyst_id)
    chase_rate   = await calculate_chase_high_rate(analyst_id)
    quality      = compute_quality_score(
        a.win_rate, a.avg_return, timing, consistency, chase_rate
    )
    new_tier     = quality_to_tier(quality)

    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Analyst).where(Analyst.analyst_id == analyst_id))
        a = r.scalar_one_or_none()
        if not a:
            return
        a.timing_score     = timing
        a.consistency      = consistency
        a.chase_high_rate  = chase_rate
        a.quality_score    = quality
        a.tier             = new_tier
        a.weight           = TIER_WEIGHTS[new_tier]
        a.updated_at       = datetime.utcnow()
        await db.commit()

    logger.info(f"[quality] {analyst_id}: quality={quality:.2f} tier={new_tier}")


async def run_monthly_tier_update():
    """每月1日：重新評定所有分析師 Tier"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Analyst

    async with AsyncSessionLocal() as db:
        r       = await db.execute(select(Analyst).where(Analyst.is_active == True))
        analysts = r.scalars().all()

    for a in analysts:
        try:
            await update_analyst_quality(a.analyst_id)
        except Exception as e:
            logger.warning(f"[quality] {a.analyst_id} update failed: {e}")

    logger.info(f"[quality] monthly tier update complete: {len(analysts)} analysts")


async def generate_monthly_report() -> str:
    """生成月度分析師評比報告"""
    from .analyst_source_manager import get_all_sources

    sources = await get_all_sources()
    today   = datetime.now().strftime("%Y/%m")
    lines   = [f"📊 分析師月度評比  {today}", "─" * 20]

    for tier in ["S", "A", "B", "C"]:
        tier_sources = [s for s in sources if s["tier"] == tier]
        if not tier_sources:
            continue

        if tier == "S":
            lines.append("\n⭐ S級（最高可信）：")
        elif tier == "A":
            lines.append("\n⭐ A級（穩定）：")
        elif tier == "B":
            lines.append("\n⭐ B級（參考用）：")
        else:
            lines.append("\n⚠️ C級（反向參考）：")

        for s in tier_sources:
            wr  = s["win_rate"] * 100
            ret = s["avg_return"] * 100
            lines.append(
                f"  {s['name']}：勝率{wr:.0f}%  平均{ret:+.1f}%"
                + ("  不追高✅" if s.get("quality_score", 0.5) > 0.65 else "")
            )
            if tier == "C":
                lines.append("  → 該分析師歷史勝率偏低，建議反向操作")

    return "\n".join(lines)
