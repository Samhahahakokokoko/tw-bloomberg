"""Analyst Tracker — 分析師基本資料和歷史績效管理"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from loguru import logger
from sqlalchemy import select, desc

# 舊測試分析師 ID / 名稱（部署時一次性清除）
_MOCK_ANALYST_IDS   = ["tsmc_bull", "ai_server_fan", "value_investor", "chip_tracker", "macro_view"]
_MOCK_ANALYST_NAMES = ["半導體老王", "財經老師", "AI伺服器達人", "存股研究室", "籌碼觀察家", "總經視角"]

RELIABILITY_TIERS = {
    "high":    (65, "⭐⭐⭐ 高可信"),
    "medium":  (50, "⭐⭐ 中可信"),
    "low":     (35, "⭐ 低可信"),
    "reverse": (0,  "🔄 反向指標"),
}


def get_tier(win_rate: float) -> str:
    """依勝率判斷分析師評級"""
    if win_rate >= 0.65:
        return "high"
    elif win_rate >= 0.50:
        return "medium"
    elif win_rate >= 0.35:
        return "low"
    return "reverse"


def get_tier_label(win_rate: float) -> str:
    tier = get_tier(win_rate)
    return RELIABILITY_TIERS[tier][1]


async def init_default_analysts():
    """清除舊測試資料；沙盒期把現有分析師統一升為 tier=A, win_rate=0.70"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Analyst, AnalystCall
    from sqlalchemy import update, delete, or_

    async with AsyncSessionLocal() as db:
        # 清除測試用 analyst_calls（TSMC entry_price < 1000 的假資料）
        await db.execute(
            delete(AnalystCall)
            .where(AnalystCall.stock_id == "2330")
            .where(AnalystCall.entry_price < 1000)
            .where(AnalystCall.entry_price > 0)
        )
        # 清除屬於測試分析師的所有 calls
        await db.execute(
            delete(AnalystCall).where(AnalystCall.analyst_id.in_(_MOCK_ANALYST_IDS))
        )
        # 清除測試分析師本身
        await db.execute(
            delete(Analyst).where(
                or_(
                    Analyst.analyst_id.in_(_MOCK_ANALYST_IDS),
                    Analyst.name.in_(_MOCK_ANALYST_NAMES),
                )
            )
        )
        await db.commit()
        logger.info("[analyst_tracker] cleaned up mock analyst data")

        # 確保新加入的分析師（win_rate=0 且無 calls 記錄）有合理初始值
        r = await db.execute(
            select(Analyst).where(Analyst.win_rate == 0.0)
        )
        zero_rate = r.scalars().all()
        if zero_rate:
            for a in zero_rate:
                a.win_rate = 0.70
                if a.tier not in ("S", "A"):
                    a.tier = "A"
            await db.commit()
            logger.info("[analyst_tracker] set initial win_rate=0.70 for {} new analysts", len(zero_rate))

    # 補回 YouTube 真實頻道名稱
    try:
        from .analyst_onboarding import refresh_analyst_names
        updated = await refresh_analyst_names()
        if updated:
            logger.info(f"[analyst_tracker] refreshed {updated} analyst names from YouTube API")
    except Exception as e:
        logger.warning(f"[analyst_tracker] name refresh skipped: {e}")


async def get_all_analysts(active_only: bool = True) -> list[dict]:
    """取得所有分析師列表"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Analyst

    async with AsyncSessionLocal() as db:
        q = select(Analyst)
        if active_only:
            q = q.where(Analyst.is_active == True)
        q = q.order_by(desc(Analyst.reliability_score))
        r = await db.execute(q)
        analysts = r.scalars().all()

    return [
        {
            "analyst_id":       a.analyst_id,
            "name":             a.name,
            "channel_url":      a.channel_url,
            "channel_id":       a.channel_id,
            "specialty":        a.specialty,
            "total_calls":      a.total_calls,
            "win_rate":         a.win_rate,
            "avg_return":       a.avg_return,
            "reliability_score": a.reliability_score,
            "tier":             get_tier(a.win_rate),
            "tier_label":       get_tier_label(a.win_rate),
        }
        for a in analysts
    ]


async def add_analyst(analyst_id: str, name: str, channel_url: str = "",
                      specialty: str = "") -> dict:
    """新增分析師"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Analyst

    async with AsyncSessionLocal() as db:
        r    = await db.execute(select(Analyst).where(Analyst.analyst_id == analyst_id))
        exist = r.scalar_one_or_none()
        if exist:
            return {"ok": False, "error": f"分析師 {analyst_id} 已存在"}

        a = Analyst(
            analyst_id=analyst_id, name=name,
            channel_url=channel_url, specialty=specialty,
            reliability_score=50.0,
        )
        db.add(a)
        await db.commit()

    return {"ok": True, "name": name}


async def get_analyst_stats(analyst_id: str) -> dict | None:
    """取得單一分析師詳細統計"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Analyst, AnalystCall

    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Analyst).where(Analyst.analyst_id == analyst_id))
        a = r.scalar_one_or_none()
        if not a:
            return None

        r2 = await db.execute(
            select(AnalystCall)
            .where(AnalystCall.analyst_id == analyst_id)
            .order_by(desc(AnalystCall.created_at))
            .limit(10)
        )
        recent_calls = r2.scalars().all()

    return {
        "analyst_id":       a.analyst_id,
        "name":             a.name,
        "specialty":        a.specialty,
        "total_calls":      a.total_calls,
        "win_rate":         a.win_rate,
        "avg_return":       a.avg_return,
        "reliability_score": a.reliability_score,
        "tier_label":       get_tier_label(a.win_rate),
        "recent_calls":     [
            {
                "date":       c.date,
                "stock_id":   c.stock_id,
                "stock_name": c.stock_name,
                "sentiment":  c.sentiment,
                "result_5d":  c.result_5d,
                "was_correct": c.was_correct,
            }
            for c in recent_calls
        ],
    }


def format_analyst_list(analysts: list[dict]) -> str:
    if not analysts:
        return "📺 分析師追蹤清單\n\n尚無分析師資料\n輸入 /analyst add [名稱] 新增"

    lines = [f"📺 分析師追蹤清單（{len(analysts)} 位）", "─" * 18]
    for a in analysts:
        lines.append(
            f"{a['tier_label']}  {a['name']}\n"
            f"   專長：{a['specialty']}  勝率：{a['win_rate']*100:.0f}%  "
            f"推薦：{a['total_calls']}次"
        )
    return "\n".join(lines)


def format_analyst_stats(stats: dict) -> str:
    lines = [
        f"📊 {stats['name']} 績效統計",
        f"{stats['tier_label']}",
        "─" * 18,
        f"總推薦次數：{stats['total_calls']}",
        f"勝率：{stats['win_rate']*100:.1f}%",
        f"平均報酬：{stats['avg_return']*100:+.1f}%",
        f"可信度：{stats['reliability_score']:.0f}/100",
        f"專長：{stats['specialty']}",
        "",
        "最近推薦：",
    ]
    for c in stats["recent_calls"][:5]:
        icon   = "✅" if c["was_correct"] else ("❌" if c["was_correct"] is False else "⏳")
        result = f"{c['result_5d']*100:+.1f}%5日" if c["result_5d"] else "待結算"
        lines.append(f"  {icon} {c['date']} {c['stock_id']} {c['stock_name']} → {result}")
    return "\n".join(lines)
