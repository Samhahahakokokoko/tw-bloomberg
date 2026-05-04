"""Analyst Topic Engine — 追蹤分析師話題專長，計算領域勝率與加成"""
from __future__ import annotations

import json
from datetime import datetime
from loguru import logger
from sqlalchemy import select, desc

# 話題正規化映射
TOPIC_MAP = {
    "AI":        "AI伺服器",
    "ai":        "AI伺服器",
    "cowos":     "AI伺服器",
    "hbm":       "AI伺服器",
    "伺服器":    "AI伺服器",
    "散熱":      "散熱",
    "電源":      "散熱",
    "半導體":    "半導體",
    "ic設計":    "半導體",
    "晶圓":      "半導體",
    "pcb":       "PCB",
    "電動車":    "電動車",
    "ev":        "電動車",
    "金融":      "金融",
    "銀行":      "金融",
    "航運":      "航運",
    "傳產":      "傳產",
    "存股":      "存股",
    "高股息":    "存股",
}

SPECIALTY_BONUS = 1.20  # 專長領域加成 20%


def normalize_topic(raw: str) -> str:
    """正規化話題名稱"""
    low = raw.lower().strip()
    return TOPIC_MAP.get(low, raw.strip())


async def record_topic_mention(analyst_id: str, topics: list[str],
                                was_correct: bool | None = None,
                                result_5d: float | None = None):
    """記錄分析師提及某話題（更新 AnalystTopicStats）"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import AnalystTopicStats

    today = datetime.now().strftime("%Y-%m-%d")
    async with AsyncSessionLocal() as db:
        for raw_topic in topics:
            topic = normalize_topic(raw_topic)
            r     = await db.execute(
                select(AnalystTopicStats)
                .where(AnalystTopicStats.analyst_id == analyst_id)
                .where(AnalystTopicStats.topic == topic)
            )
            stat = r.scalar_one_or_none()
            if stat is None:
                stat = AnalystTopicStats(analyst_id=analyst_id, topic=topic)
                db.add(stat)

            stat.mention_count += 1
            stat.last_updated   = today

            # 更新勝率（若有結果）
            if was_correct is not None:
                prev_correct = stat.win_rate * max(stat.mention_count - 1, 1)
                stat.win_rate = (prev_correct + (1 if was_correct else 0)) / stat.mention_count

            if result_5d is not None:
                prev_avg    = stat.avg_return * max(stat.mention_count - 1, 1)
                stat.avg_return = (prev_avg + result_5d) / stat.mention_count

        await db.commit()


async def get_analyst_topics(analyst_id: str) -> list[dict]:
    """取得分析師的話題統計（按提及次數排序）"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import AnalystTopicStats

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(AnalystTopicStats)
            .where(AnalystTopicStats.analyst_id == analyst_id)
            .order_by(desc(AnalystTopicStats.mention_count))
        )
        stats = r.scalars().all()

    return [
        {
            "topic":         s.topic,
            "mention_count": s.mention_count,
            "win_rate":      s.win_rate,
            "avg_return":    s.avg_return,
        }
        for s in stats
    ]


def get_specialty_bonus(analyst_specialty: str, stock_sector: str) -> float:
    """若股票族群符合分析師專長，回傳加成倍數"""
    if not analyst_specialty or not stock_sector:
        return 1.0
    specialties = [s.strip() for s in analyst_specialty.replace(",", "/").split("/")]
    for spec in specialties:
        spec_norm   = normalize_topic(spec)
        sector_norm = normalize_topic(stock_sector)
        if spec_norm == sector_norm or spec_norm in stock_sector or stock_sector in spec_norm:
            return SPECIALTY_BONUS
    return 1.0


def format_topic_profile(analyst_name: str, topics: list[dict]) -> str:
    """格式化分析師話題專長顯示"""
    if not topics:
        return f"📺 {analyst_name} 話題專長\n\n尚無足夠資料"

    lines = [f"📺 {analyst_name} 專長分析", "─" * 18]
    for t in topics[:5]:
        wr   = t["win_rate"] * 100
        icon = "🔥" if wr >= 65 else ("✅" if wr >= 50 else "⚠️")
        lines.append(
            f"{icon} {t['topic']}：提及{t['mention_count']}次  "
            f"勝率{wr:.0f}%"
        )
    if topics:
        best = max(topics, key=lambda t: t["win_rate"] * t["mention_count"])
        lines.append(f"\n→ 在「{best['topic']}」領域最可信")
    return "\n".join(lines)


async def update_topics_from_calls():
    """從 AnalystCall 批量更新話題統計"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import AnalystCall
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(AnalystCall)
            .where(AnalystCall.was_correct != None)
            .order_by(desc(AnalystCall.created_at))
            .limit(200)
        )
        calls = r.scalars().all()

    for call in calls:
        try:
            kp = json.loads(call.key_points or "[]")
            if kp:
                await record_topic_mention(
                    analyst_id=call.analyst_id,
                    topics=kp[:3],
                    was_correct=call.was_correct,
                    result_5d=call.result_5d,
                )
        except Exception:
            pass
    logger.info(f"[topic_engine] updated topics from {len(calls)} calls")
