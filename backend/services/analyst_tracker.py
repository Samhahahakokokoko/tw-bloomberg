"""Analyst Tracker — 分析師基本資料和歷史績效管理"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from loguru import logger
from sqlalchemy import select, desc

# ── 預設追蹤名單 ───────────────────────────────────────────────────────────────
DEFAULT_ANALYSTS = [
    {
        "analyst_id":  "tsmc_bull",
        "name":        "半導體老王",
        "channel_url": "https://www.youtube.com/@example1",
        "channel_id":  "",
        "specialty":   "半導體,IC設計",
        "reliability_score": 70.0,
    },
    {
        "analyst_id":  "ai_server_fan",
        "name":        "AI伺服器達人",
        "channel_url": "https://www.youtube.com/@example2",
        "channel_id":  "",
        "specialty":   "AI Server,散熱",
        "reliability_score": 65.0,
    },
    {
        "analyst_id":  "value_investor",
        "name":        "存股研究室",
        "channel_url": "https://www.youtube.com/@example3",
        "channel_id":  "",
        "specialty":   "存股,高股息",
        "reliability_score": 72.0,
    },
    {
        "analyst_id":  "chip_tracker",
        "name":        "籌碼觀察家",
        "channel_url": "https://www.youtube.com/@example4",
        "channel_id":  "",
        "specialty":   "籌碼,法人",
        "reliability_score": 68.0,
    },
    {
        "analyst_id":  "macro_view",
        "name":        "總經視角",
        "channel_url": "https://www.youtube.com/@example5",
        "channel_id":  "",
        "specialty":   "總經,ETF",
        "reliability_score": 60.0,
    },
]

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
    """初始化預設分析師清單（若表為空）；沙盒期統一升為 tier=A, win_rate=0.70"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Analyst
    from sqlalchemy import update

    async with AsyncSessionLocal() as db:
        r     = await db.execute(select(Analyst).limit(1))
        exist = r.scalar_one_or_none()
        if not exist:
            for a in DEFAULT_ANALYSTS:
                db.add(Analyst(**a, total_calls=0, win_rate=0.70, avg_return=0.0, tier="A"))
            await db.commit()
            logger.info(f"[analyst_tracker] initialized {len(DEFAULT_ANALYSTS)} analysts tier=A")
        else:
            # 沙盒期：把所有非 S 的分析師升為 A tier + win_rate=0.70（確保高可信）
            await db.execute(
                update(Analyst)
                .where(Analyst.tier.notin_(["S"]))
                .values(tier="A", win_rate=0.70)
            )
            await db.commit()
            logger.info("[analyst_tracker] sandbox upgrade: all analysts → tier=A win_rate=0.70")


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
