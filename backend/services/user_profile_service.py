"""用戶畫像服務 — 風險偏好、AI 記憶、問答歷史"""
from __future__ import annotations
import hashlib
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..models.models import UserProfile, QueryHistory
from loguru import logger

# ── 風險偏好定義 ──────────────────────────────────────────────────────────────
RISK_PROFILES = {
    "conservative": {
        "label": "保守型",
        "emoji": "🛡️",
        "ai_system": (
            "用戶為【保守型】投資者，偏好高殖利率、低波動的績優股。"
            "請優先推薦：配息穩定、本益比合理、具護城河的大型股。"
            "避免：高槓桿、高波動、投機性建議。"
            "語氣：穩健、著重風險控制。"
        ),
        "strategy_pref": ["bollinger", "rsi"],
        "color": "#4488ff",
    },
    "moderate": {
        "label": "穩健型",
        "emoji": "⚖️",
        "ai_system": (
            "用戶為【穩健型】投資者，追求風險與報酬的平衡。"
            "可接受適度波動，偏好有基本面支撐的成長股與價值股。"
            "語氣：專業、平衡、兼顧機會與風險。"
        ),
        "strategy_pref": ["macd", "rsi"],
        "color": "#44cc88",
    },
    "aggressive": {
        "label": "積極型",
        "emoji": "🚀",
        "ai_system": (
            "用戶為【積極型】投資者，追求高報酬、可接受高風險。"
            "可推薦：技術突破、籌碼異動、高成長題材股。"
            "包含短線操作策略、波段交易機會。"
            "語氣：積極、著重技術面與動能。"
        ),
        "strategy_pref": ["pvd", "institutional", "macd"],
        "color": "#ff5544",
    },
}

INVESTMENT_GOALS = {
    "income":      {"label": "存股收息", "emoji": "💰"},
    "growth":      {"label": "資本成長", "emoji": "📈"},
    "speculation": {"label": "波段操作", "emoji": "⚡"},
}

INDUSTRIES = [
    "半導體", "AI/雲端", "電動車", "金融", "電信",
    "生技醫療", "傳產/水泥", "零售/電商", "航運", "營建",
]


async def get_or_create_profile(db: AsyncSession, user_id: str) -> UserProfile:
    r = await db.execute(select(UserProfile).where(UserProfile.user_id == user_id))
    profile = r.scalar_one_or_none()
    if not profile:
        profile = UserProfile(user_id=user_id)
        db.add(profile)
        await db.commit()
        await db.refresh(profile)
    return profile


async def update_risk(db: AsyncSession, user_id: str, risk: str) -> UserProfile:
    profile = await get_or_create_profile(db, user_id)
    profile.risk_tolerance = risk
    profile.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(profile)
    return profile


async def update_goal(db: AsyncSession, user_id: str, goal: str) -> UserProfile:
    profile = await get_or_create_profile(db, user_id)
    profile.investment_goal = goal
    profile.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(profile)
    return profile


async def update_industries(db: AsyncSession, user_id: str, industries: list[str]) -> UserProfile:
    profile = await get_or_create_profile(db, user_id)
    profile.preferred_industries = ",".join(industries[:6])
    profile.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(profile)
    return profile


async def build_ai_context(db: AsyncSession, user_id: str) -> str:
    """生成給 Claude 的用戶背景 system prompt 補充"""
    profile = await get_or_create_profile(db, user_id)
    risk_info   = RISK_PROFILES.get(profile.risk_tolerance, RISK_PROFILES["moderate"])
    goal_info   = INVESTMENT_GOALS.get(profile.investment_goal, INVESTMENT_GOALS["growth"])
    industries  = profile.preferred_industries or "無特定偏好"

    context = risk_info["ai_system"] + (
        f"\n投資目標：{goal_info['emoji']} {goal_info['label']}。"
        f"\n偏好產業：{industries}。"
    )

    if profile.win_rate > 0:
        context += f"\n歷史勝率：{profile.win_rate:.1f}%，交易次數：{profile.total_trades}。"

    if profile.ai_summary:
        context += f"\n用戶歷史摘要：{profile.ai_summary[:200]}"

    return context


async def save_query(db: AsyncSession, user_id: str, question: str, answer: str):
    """儲存問答歷史，同一 topic 只保留最新一筆"""
    topic_hash = hashlib.md5(question[:80].encode()).hexdigest()[:16]

    # 刪舊的同 topic
    r = await db.execute(
        select(QueryHistory).where(
            QueryHistory.user_id == user_id,
            QueryHistory.topic_hash == topic_hash,
        )
    )
    old = r.scalar_one_or_none()
    if old:
        await db.delete(old)

    entry = QueryHistory(
        user_id=user_id,
        question=question[:500],
        answer=answer[:2000],
        topic_hash=topic_hash,
    )
    db.add(entry)

    # 只保留最近 20 筆
    r2 = await db.execute(
        select(QueryHistory)
        .where(QueryHistory.user_id == user_id)
        .order_by(QueryHistory.created_at.desc())
        .offset(20)
    )
    old_entries = r2.scalars().all()
    for e in old_entries:
        await db.delete(e)

    await db.commit()


async def get_recent_queries(db: AsyncSession, user_id: str, limit: int = 5) -> list[QueryHistory]:
    r = await db.execute(
        select(QueryHistory)
        .where(QueryHistory.user_id == user_id)
        .order_by(QueryHistory.created_at.desc())
        .limit(limit)
    )
    return r.scalars().all()


async def find_similar_answer(db: AsyncSession, user_id: str, question: str) -> str | None:
    """找相同 topic 的舊答案（避免重複分析）"""
    topic_hash = hashlib.md5(question[:80].encode()).hexdigest()[:16]
    r = await db.execute(
        select(QueryHistory).where(
            QueryHistory.user_id == user_id,
            QueryHistory.topic_hash == topic_hash,
        )
    )
    old = r.scalar_one_or_none()
    if old:
        age_days = (datetime.utcnow() - old.created_at).days
        if age_days < 3:   # 3 天內的答案才重用
            return old.answer
    return None


async def update_trade_stats(db: AsyncSession, user_id: str, win_rate: float, total_trades: int):
    profile = await get_or_create_profile(db, user_id)
    profile.win_rate    = win_rate
    profile.total_trades = total_trades
    profile.updated_at  = datetime.utcnow()
    await db.commit()
