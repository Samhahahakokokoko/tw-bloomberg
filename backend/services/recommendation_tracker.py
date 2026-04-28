"""強化學習回饋系統

流程：
  1. Agent C 推薦時呼叫 save_recommendations() 存入 DB
  2. 每日 15:30 執行 backfill_prices() 回填 5/10 日後股價
  3. 每週一執行 adjust_weights() 根據準確率微調評分權重
  4. /accuracy 指令查看統計
"""
from __future__ import annotations
import asyncio
from datetime import date, timedelta, datetime
from loguru import logger
from sqlalchemy import select, and_, func

from ..models.database import AsyncSessionLocal
from ..models.models import RecommendationResult, ScoringWeight, StockScore
from .twse_service import fetch_realtime_quote

# 成功定義：5 日報酬率 > SUCCESS_THRESHOLD
SUCCESS_THRESHOLD = 3.0   # %
WEIGHT_STEP       = 0.05  # 每次調整 5%
MIN_WEIGHT        = 0.10  # 單一維度最低 10%
MAX_WEIGHT        = 0.60  # 單一維度最高 60%


# ── 存入推薦記錄 ──────────────────────────────────────────────────────────────

async def save_recommendations(top_stocks: list[dict], today: str):
    """
    Agent C 推薦後呼叫，將高分股票存入 recommendation_results。
    避免重複插入（UNIQUE on stock_code + recommend_date）。
    """
    if not top_stocks:
        return

    async with AsyncSessionLocal() as db:
        for s in top_stocks[:20]:  # 最多存 20 檔
            existing = await db.execute(
                select(RecommendationResult).where(
                    RecommendationResult.stock_code  == s["stock_code"],
                    RecommendationResult.recommend_date == today,
                )
            )
            if existing.scalar_one_or_none():
                continue  # 今日已存

            # 取當日股價
            rec_price = None
            try:
                q = await fetch_realtime_quote(s["stock_code"])
                rec_price = q.get("price")
            except Exception:
                pass

            db.add(RecommendationResult(
                stock_code        = s["stock_code"],
                stock_name        = s.get("stock_name", ""),
                recommend_date    = today,
                recommend_price   = rec_price,
                fundamental_score = s.get("fundamental_score", 0),
                chip_score        = s.get("chip_score", 0),
                technical_score   = s.get("technical_score", 0),
                total_score       = s.get("total_score", 0),
                confidence        = s.get("confidence", 0),
                ai_reason         = s.get("ai_reason", ""),
            ))

        await db.commit()
    logger.info(f"[Tracker] 存入 {len(top_stocks)} 筆推薦記錄 ({today})")


# ── 回填後續股價 ──────────────────────────────────────────────────────────────

async def backfill_prices():
    """每日 15:30 回填 5/10 交易日後的股價"""
    today = date.today()

    async with AsyncSessionLocal() as db:
        # 需要回填 5d 的記錄：推薦日 ≤ 今天 - 5 個交易日
        cutoff_5d  = _subtract_trading_days(today, 5)
        cutoff_10d = _subtract_trading_days(today, 10)

        # 未填 5 日的
        r5 = await db.execute(
            select(RecommendationResult).where(
                and_(
                    RecommendationResult.is_filled_5d == False,
                    RecommendationResult.recommend_date <= cutoff_5d.isoformat(),
                )
            )
        )
        recs_5d = r5.scalars().all()

        # 未填 10 日的
        r10 = await db.execute(
            select(RecommendationResult).where(
                and_(
                    RecommendationResult.is_filled_10d == False,
                    RecommendationResult.recommend_date <= cutoff_10d.isoformat(),
                )
            )
        )
        recs_10d = r10.scalars().all()

    filled_5 = filled_10 = 0

    for rec in recs_5d:
        try:
            q = await fetch_realtime_quote(rec.stock_code)
            price = q.get("price")
            if price and rec.recommend_price:
                ret = (price - rec.recommend_price) / rec.recommend_price * 100
                async with AsyncSessionLocal() as db:
                    r = await db.execute(
                        select(RecommendationResult).where(RecommendationResult.id == rec.id)
                    )
                    obj = r.scalar_one_or_none()
                    if obj:
                        obj.price_5d      = price
                        obj.return_5d     = round(ret, 2)
                        obj.hit_target_5d = ret >= SUCCESS_THRESHOLD
                        obj.is_filled_5d  = True
                        await db.commit()
                        filled_5 += 1
            await asyncio.sleep(1.5)
        except Exception as e:
            logger.error(f"[Tracker] 5d fill error {rec.stock_code}: {e}")

    for rec in recs_10d:
        try:
            q = await fetch_realtime_quote(rec.stock_code)
            price = q.get("price")
            if price and rec.recommend_price:
                ret = (price - rec.recommend_price) / rec.recommend_price * 100
                async with AsyncSessionLocal() as db:
                    r = await db.execute(
                        select(RecommendationResult).where(RecommendationResult.id == rec.id)
                    )
                    obj = r.scalar_one_or_none()
                    if obj:
                        obj.price_10d      = price
                        obj.return_10d     = round(ret, 2)
                        obj.hit_target_10d = ret >= SUCCESS_THRESHOLD
                        obj.is_filled_10d  = True
                        await db.commit()
                        filled_10 += 1
            await asyncio.sleep(1.5)
        except Exception as e:
            logger.error(f"[Tracker] 10d fill error {rec.stock_code}: {e}")

    logger.info(f"[Tracker] 回填完成：5d={filled_5} 10d={filled_10}")


# ── 準確率統計 ────────────────────────────────────────────────────────────────

async def get_accuracy_stats(days: int = 30) -> dict:
    """取得近 days 日的推薦準確率統計"""
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    async with AsyncSessionLocal() as db:
        # 已回填 5d 的記錄
        r = await db.execute(
            select(RecommendationResult).where(
                and_(
                    RecommendationResult.is_filled_5d == True,
                    RecommendationResult.recommend_date >= cutoff,
                )
            ).order_by(RecommendationResult.recommend_date.desc())
        )
        recs = r.scalars().all()

    if not recs:
        return {"message": f"近 {days} 日尚無回填完成的推薦記錄", "total": 0}

    total    = len(recs)
    hits_5d  = sum(1 for r in recs if r.hit_target_5d)
    returns  = [r.return_5d for r in recs if r.return_5d is not None]
    avg_ret  = sum(returns) / len(returns) if returns else 0
    win_rate = hits_5d / total * 100 if total else 0

    # 最佳/最差
    sorted_recs = sorted(recs, key=lambda x: x.return_5d or 0)
    worst = sorted_recs[:3]
    best  = sorted_recs[-3:]

    # 依推薦日分組計算每日勝率（用於折線圖）
    by_date: dict[str, list] = {}
    for r in recs:
        by_date.setdefault(r.recommend_date, []).append(r.return_5d or 0)

    daily_win_rates = [
        {
            "date":     d,
            "win_rate": round(sum(1 for x in rets if x >= SUCCESS_THRESHOLD) / len(rets) * 100, 1),
            "avg_ret":  round(sum(rets) / len(rets), 2),
            "count":    len(rets),
        }
        for d, rets in sorted(by_date.items())
    ]

    return {
        "total":      total,
        "win_rate":   round(win_rate, 1),
        "avg_return": round(avg_ret, 2),
        "hits_5d":    hits_5d,
        "threshold":  SUCCESS_THRESHOLD,
        "daily_win_rates": daily_win_rates,
        "best_picks":  [_rec_to_dict(r) for r in reversed(best)],
        "worst_picks": [_rec_to_dict(r) for r in worst],
    }


# ── 自動調整評分權重 ──────────────────────────────────────────────────────────

async def adjust_weights():
    """
    每週一根據上週推薦結果調整三維度權重：
    - 某維度高分股的勝率 > 70% → 該維度 +5%
    - 勝率 < 40% → -5%
    其餘不變，並重新正規化使三者之和 = 1
    """
    today = date.today().isoformat()

    # 取上週的回填記錄（7-14 天前）
    end   = (date.today() - timedelta(days=7)).isoformat()
    start = (date.today() - timedelta(days=14)).isoformat()

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(RecommendationResult).where(
                and_(
                    RecommendationResult.is_filled_5d == True,
                    RecommendationResult.recommend_date.between(start, end),
                )
            )
        )
        recs = r.scalars().all()

    if len(recs) < 5:
        logger.info("[Weights] 樣本不足，跳過調整")
        return

    # 取當前權重
    current = await _get_current_weights()
    fw, cw, tw = current

    # 計算各維度主導的股票勝率
    def win_rate_for_top_dim(dim: str) -> float:
        # 取該維度得分最高的前 1/3 記錄
        sorted_recs = sorted(recs, key=lambda r: getattr(r, dim, 0), reverse=True)
        top = sorted_recs[: max(1, len(sorted_recs) // 3)]
        hits = sum(1 for r in top if r.hit_target_5d)
        return hits / len(top) * 100 if top else 50

    f_wr = win_rate_for_top_dim("fundamental_score")
    c_wr = win_rate_for_top_dim("chip_score")
    t_wr = win_rate_for_top_dim("technical_score")

    def adjust(w, wr):
        if wr > 70:   return min(MAX_WEIGHT, w + WEIGHT_STEP)
        elif wr < 40: return max(MIN_WEIGHT, w - WEIGHT_STEP)
        return w

    fw_new = adjust(fw, f_wr)
    cw_new = adjust(cw, c_wr)
    tw_new = adjust(tw, t_wr)

    # 正規化
    total = fw_new + cw_new + tw_new
    fw_new = round(fw_new / total, 4)
    cw_new = round(cw_new / total, 4)
    tw_new = round(1 - fw_new - cw_new, 4)

    overall_wr = sum(1 for r in recs if r.hit_target_5d) / len(recs) * 100

    async with AsyncSessionLocal() as db:
        db.add(ScoringWeight(
            effective_date       = today,
            fundamental_weight   = fw_new,
            chip_weight          = cw_new,
            technical_weight     = tw_new,
            fundamental_win_rate = round(f_wr, 1),
            chip_win_rate        = round(c_wr, 1),
            technical_win_rate   = round(t_wr, 1),
            overall_win_rate     = round(overall_wr, 1),
            notes = (
                f"基:{fw:.2f}→{fw_new:.2f}({f_wr:.0f}%) "
                f"籌:{cw:.2f}→{cw_new:.2f}({c_wr:.0f}%) "
                f"技:{tw:.2f}→{tw_new:.2f}({t_wr:.0f}%)"
            )
        ))
        await db.commit()

    logger.info(f"[Weights] 更新權重 F={fw_new} C={cw_new} T={tw_new}，整體勝率={overall_wr:.0f}%")


async def _get_current_weights() -> tuple[float, float, float]:
    """取最新一筆有效權重"""
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(ScoringWeight).order_by(ScoringWeight.effective_date.desc()).limit(1)
        )
        latest = r.scalar_one_or_none()
    if latest:
        return latest.fundamental_weight, latest.chip_weight, latest.technical_weight
    return 0.35, 0.35, 0.30


async def get_current_weights() -> dict:
    fw, cw, tw = await _get_current_weights()
    return {"fundamental": fw, "chip": cw, "technical": tw}


async def get_weight_history(limit: int = 20) -> list[dict]:
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(ScoringWeight).order_by(ScoringWeight.effective_date.desc()).limit(limit)
        )
        rows = r.scalars().all()
    return [
        {
            "date":              w.effective_date,
            "fundamental_weight": w.fundamental_weight,
            "chip_weight":        w.chip_weight,
            "technical_weight":   w.technical_weight,
            "fundamental_win_rate": w.fundamental_win_rate,
            "chip_win_rate":      w.chip_win_rate,
            "technical_win_rate": w.technical_win_rate,
            "overall_win_rate":   w.overall_win_rate,
            "notes":              w.notes,
        }
        for w in reversed(rows)
    ]


def _rec_to_dict(r: RecommendationResult) -> dict:
    return {
        "stock_code":        r.stock_code,
        "stock_name":        r.stock_name or "",
        "recommend_date":    r.recommend_date,
        "recommend_price":   r.recommend_price,
        "total_score":       r.total_score,
        "confidence":        r.confidence,
        "ai_reason":         r.ai_reason or "",
        "price_5d":          r.price_5d,
        "return_5d":         r.return_5d,
        "hit_target_5d":     r.hit_target_5d,
        "price_10d":         r.price_10d,
        "return_10d":        r.return_10d,
        "hit_target_10d":    r.hit_target_10d,
    }


def _subtract_trading_days(d: date, n: int) -> date:
    """簡單估算扣除 N 個交易日（週末不算，不含假日）"""
    result = d
    count  = 0
    while count < n:
        result -= timedelta(days=1)
        if result.weekday() < 5:  # 週一到週五
            count += 1
    return result
