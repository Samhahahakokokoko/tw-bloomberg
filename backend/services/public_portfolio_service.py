"""Public Portfolio & Strategy Marketplace"""
from __future__ import annotations

from datetime import datetime
from loguru import logger
from sqlalchemy import select, desc


# ── 公開投組 ──────────────────────────────────────────────────────────────────

async def set_public(uid: str, is_public: bool, display_name: str = "") -> str:
    """設定用戶投組是否公開"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import PublicPortfolio

    async with AsyncSessionLocal() as db:
        r    = await db.execute(select(PublicPortfolio).where(PublicPortfolio.user_id == uid))
        rec  = r.scalar_one_or_none()
        if rec is None:
            rec = PublicPortfolio(user_id=uid, display_name=display_name or uid[:8])
            db.add(rec)
        rec.is_public  = is_public
        rec.updated_at = datetime.utcnow()
        if display_name:
            rec.display_name = display_name
        await db.commit()

    return "公開" if is_public else "私人"


async def get_top_portfolios(limit: int = 10) -> list[dict]:
    """取得本週績效最佳的公開投組"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import PublicPortfolio

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(PublicPortfolio)
            .where(PublicPortfolio.is_public == True)
            .order_by(desc(PublicPortfolio.weekly_return))
            .limit(limit)
        )
        recs = r.scalars().all()

    result = []
    for i, rec in enumerate(recs, 1):
        try:
            from .portfolio_service import get_holdings
            holdings = await get_holdings(rec.user_id)
            sectors  = list({h.get("sector", "其他") for h in holdings if h.get("sector")})[:3]
            style    = "、".join(sectors[:2]) if sectors else "多元配置"
        except Exception:
            style = rec.style_tag or "未知"

        result.append({
            "rank":    i,
            "uid":     rec.user_id,
            "name":    rec.display_name or f"用戶{i}",
            "weekly":  rec.weekly_return,
            "total":   rec.total_return,
            "style":   style,
        })
    return result


def format_top_portfolios(items: list[dict]) -> str:
    if not items:
        return "🏆 績效排行榜\n\n目前沒有公開投組\n輸入 /public on 公開你的投組"

    medals = ["🥇", "🥈", "🥉"] + ["🎖️"] * 20
    lines  = ["🏆 本週投組績效排行", "─" * 18]
    for item in items[:8]:
        sign = "+" if item["weekly"] >= 0 else ""
        lines.append(
            f"{medals[item['rank']-1]} {item['name']}：{sign}{item['weekly']:.1f}%"
            f"（{item['style']}）"
        )
    lines += ["", "[查看冠軍持股] /copy @用戶名稱複製策略"]
    return "\n".join(lines)


async def update_weekly_returns():
    """更新所有公開投組的本週報酬（每週五執行）"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import PublicPortfolio

    async with AsyncSessionLocal() as db:
        r    = await db.execute(select(PublicPortfolio).where(PublicPortfolio.is_public == True))
        recs = r.scalars().all()
        for rec in recs:
            try:
                from .portfolio_service import get_holdings
                holdings   = await get_holdings(rec.user_id)
                total_val  = sum(h.get("market_value", 0) or 0 for h in holdings)
                total_cost = sum(h.get("cost", 0) or 0 for h in holdings)
                if total_cost > 0:
                    rec.total_return  = (total_val - total_cost) / total_cost * 100
                    rec.weekly_return = rec.total_return * 0.15   # 近似週報酬
                rec.updated_at = datetime.utcnow()
            except Exception:
                pass
        await db.commit()


# ── 策略市集 ──────────────────────────────────────────────────────────────────

async def publish_strategy(uid: str, name: str, screen_type: str = "momentum") -> dict:
    """發布用戶策略到市集"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import StrategyMarketplace
    from .report_screener import run_screener

    # 計算策略近3個月績效（模擬）
    rows       = run_screener(screen_type)
    avg_score  = sum(r.confidence for r in rows[:20]) / max(len(rows[:20]), 1)
    ret_3m     = (avg_score - 50) / 50 * 0.20    # 模擬報酬
    win_rate   = 0.55 + (avg_score - 50) / 500
    drawdown   = abs(ret_3m) * 0.4

    async with AsyncSessionLocal() as db:
        strat = StrategyMarketplace(
            owner_id=uid, name=name,
            screen_type=screen_type,
            return_3m=round(ret_3m, 4),
            win_rate=round(win_rate, 3),
            max_drawdown=round(drawdown, 4),
        )
        db.add(strat)
        await db.commit()
        await db.refresh(strat)

    return {
        "id": strat.id, "name": name,
        "return_3m": ret_3m, "win_rate": win_rate,
    }


async def get_strategy_list(limit: int = 10) -> list[dict]:
    """取得策略市集列表"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import StrategyMarketplace

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(StrategyMarketplace)
            .where(StrategyMarketplace.is_active == True)
            .order_by(desc(StrategyMarketplace.return_3m))
            .limit(limit)
        )
        strats = r.scalars().all()

    return [
        {
            "id":      s.id,
            "name":    s.name,
            "return_3m": s.return_3m,
            "win_rate":  s.win_rate,
            "drawdown":  s.max_drawdown,
            "subs":      s.subscribers,
        }
        for s in strats
    ]


def format_strategy_list(items: list[dict]) -> str:
    if not items:
        return "📈 策略市集\n\n目前沒有公開策略\n輸入 /strategy publish [名稱] 上架你的策略"

    medals = ["🥇", "🥈", "🥉"] + ["📊"] * 20
    lines  = ["📈 策略排行榜", "─" * 18]
    for i, s in enumerate(items[:5]):
        ret  = s["return_3m"] * 100
        sign = "+" if ret >= 0 else ""
        lines.append(
            f"{medals[i]} {s['name']}：{sign}{ret:.1f}%  "
            f"勝率{s['win_rate']*100:.0f}%  "
            f"回撤{s['drawdown']*100:.1f}%"
        )
    lines += ["", "[訂閱] /strategy subscribe [ID]  [上架] /strategy publish [名稱]"]
    return "\n".join(lines)
