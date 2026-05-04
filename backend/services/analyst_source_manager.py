"""Analyst Source Manager — 白名單模式分析師池管理"""
from __future__ import annotations

from datetime import datetime
from loguru import logger
from sqlalchemy import select, desc

TIER_CONFIG = {
    "S": {"label": "⭐S級 高可信",   "weight_base": 1.5,  "threshold": 0.65},
    "A": {"label": "⭐A級 穩定",     "weight_base": 1.0,  "threshold": 0.55},
    "B": {"label": "⭐B級 參考用",   "weight_base": 0.5,  "threshold": 0.45},
    "C": {"label": "⚠️C級 反向指標", "weight_base": -0.3, "threshold": 0.0},
}

TIER_WEIGHTS = {"S": 1.5, "A": 1.0, "B": 0.5, "C": -0.3}

TOPIC_ALIASES = {
    "ai":      "AI伺服器",
    "ai伺服器": "AI伺服器",
    "散熱":    "散熱",
    "半導體":  "半導體",
    "pcb":     "PCB",
    "金融":    "金融",
    "航運":    "航運",
    "傳產":    "傳產",
    "電動車":  "電動車",
    "存股":    "存股",
}


def normalize_specialty(raw: str) -> str:
    """正規化專長欄位"""
    parts = [p.strip() for p in raw.replace(",", "/").split("/") if p.strip()]
    normalized = []
    for p in parts:
        normalized.append(TOPIC_ALIASES.get(p.lower(), p))
    return ",".join(normalized[:3])


async def add_analyst(name: str, channel_id: str = "", specialty: str = "",
                      tier: str = "A", style: str = "", notes: str = "",
                      channel_url: str = "") -> dict:
    """新增分析師到追蹤清單"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Analyst

    analyst_id = channel_id or name.replace(" ", "_").lower()[:30]
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Analyst).where(
            (Analyst.analyst_id == analyst_id) | (Analyst.name == name)
        ))
        if r.scalar_one_or_none():
            return {"ok": False, "error": f"「{name}」已在追蹤清單中"}

        a = Analyst(
            analyst_id  = analyst_id,
            name        = name,
            channel_id  = channel_id,
            channel_url = channel_url,
            specialty   = normalize_specialty(specialty),
            tier        = tier.upper() if tier.upper() in TIER_CONFIG else "A",
            style       = style,
            notes       = notes,
            weight      = TIER_WEIGHTS.get(tier.upper(), 1.0),
            added_date  = datetime.now().strftime("%Y-%m-%d"),
        )
        db.add(a)
        await db.commit()
    logger.info(f"[source_manager] added analyst: {name} tier={tier}")
    return {"ok": True, "name": name, "tier": tier}


async def remove_analyst(name: str) -> dict:
    """移除分析師"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Analyst
    from sqlalchemy import delete

    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Analyst).where(
            (Analyst.name == name) | (Analyst.analyst_id == name)
        ))
        a = r.scalar_one_or_none()
        if not a:
            return {"ok": False, "error": f"找不到「{name}」"}
        await db.execute(delete(Analyst).where(Analyst.id == a.id))
        await db.commit()
    return {"ok": True, "name": name}


async def set_enabled(name: str, enabled: bool) -> dict:
    """啟用/停用分析師"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Analyst

    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Analyst).where(
            (Analyst.name == name) | (Analyst.analyst_id == name)
        ))
        a = r.scalar_one_or_none()
        if not a:
            return {"ok": False, "error": f"找不到「{name}」"}
        a.enabled    = enabled
        a.updated_at = datetime.utcnow()
        await db.commit()
    return {"ok": True, "name": name, "enabled": enabled}


async def set_tier(name: str, tier: str) -> dict:
    """手動調整分析師評級"""
    tier = tier.upper()
    if tier not in TIER_CONFIG:
        return {"ok": False, "error": f"無效評級，請使用 S/A/B/C"}

    from ..models.database import AsyncSessionLocal
    from ..models.models import Analyst

    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Analyst).where(
            (Analyst.name == name) | (Analyst.analyst_id == name)
        ))
        a = r.scalar_one_or_none()
        if not a:
            return {"ok": False, "error": f"找不到「{name}」"}
        a.tier       = tier
        a.weight     = TIER_WEIGHTS[tier]
        a.updated_at = datetime.utcnow()
        await db.commit()
    return {"ok": True, "name": name, "tier": tier, "label": TIER_CONFIG[tier]["label"]}


async def get_all_sources(enabled_only: bool = False) -> list[dict]:
    """取得所有分析師來源"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import Analyst

    async with AsyncSessionLocal() as db:
        q = select(Analyst).where(Analyst.is_active == True)
        if enabled_only:
            q = q.where(Analyst.enabled == True)
        q = q.order_by(Analyst.tier, desc(Analyst.quality_score))
        r = await db.execute(q)
        analysts = r.scalars().all()

    return [_analyst_to_dict(a) for a in analysts]


def _analyst_to_dict(a) -> dict:
    return {
        "analyst_id":  a.analyst_id,
        "name":        a.name,
        "channel_id":  a.channel_id,
        "channel_url": a.channel_url,
        "specialty":   a.specialty,
        "tier":        a.tier,
        "tier_label":  TIER_CONFIG.get(a.tier, TIER_CONFIG["A"])["label"],
        "style":       a.style,
        "weight":      a.weight,
        "enabled":     a.enabled,
        "total_calls": a.total_calls,
        "win_rate":    a.win_rate,
        "avg_return":  a.avg_return,
        "quality_score": a.quality_score,
        "notes":       a.notes,
        "added_date":  a.added_date,
    }


def format_source_list(sources: list[dict]) -> str:
    if not sources:
        return (
            "📺 分析師追蹤清單\n\n"
            "尚未新增任何分析師\n\n"
            "新增方式：\n"
            "/analyst add [名稱] [channel_id] [專長]\n"
            "例：/analyst add 財經雪倫 UCxxxxx AI,散熱"
        )
    lines = [f"📺 分析師追蹤清單（{len(sources)} 位）", "─" * 20]
    for tier in ["S", "A", "B", "C"]:
        tier_sources = [s for s in sources if s["tier"] == tier]
        if not tier_sources:
            continue
        lines.append(f"\n{TIER_CONFIG[tier]['label']}：")
        for s in tier_sources:
            status = "✅" if s["enabled"] else "⏸"
            wr     = f"勝率{s['win_rate']*100:.0f}%" if s["total_calls"] > 0 else "新追蹤"
            lines.append(f"  {status} {s['name']}  {s['specialty'][:15]}  {wr}")
    return "\n".join(lines)


def format_source_stats(a: dict) -> str:
    tier_lbl = TIER_CONFIG.get(a["tier"], TIER_CONFIG["A"])["label"]
    lines = [
        f"📊 {a['name']} 績效統計",
        f"{tier_lbl}  {a['specialty']}",
        "─" * 20,
        f"總推薦次數：{a['total_calls']}",
        f"勝率：{a['win_rate']*100:.1f}%",
        f"平均報酬：{a['avg_return']*100:+.1f}%",
        f"品質分數：{a['quality_score']*100:.0f}/100",
    ]
    if a["channel_url"]:
        lines.append(f"頻道：{a['channel_url'][:50]}")
    if a["notes"]:
        lines.append(f"備註：{a['notes']}")
    return "\n".join(lines)
