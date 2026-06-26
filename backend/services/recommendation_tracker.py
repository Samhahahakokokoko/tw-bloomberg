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

# 成功定義（新）：股票 5 日報酬 > 大盤（0050）5 日報酬（超額報酬為正）
# 舊定義：絕對報酬 > SUCCESS_THRESHOLD_ABS = 3.0%
SUCCESS_THRESHOLD_ABS = 3.0   # 保留舊閾值供歷史對比展示
BENCHMARK_CODE        = "0050"   # 大盤基準
WEIGHT_STEP           = 0.05  # 每次調整 5%
MIN_WEIGHT            = 0.10  # 單一維度最低 10%
MAX_WEIGHT            = 0.60  # 單一維度最高 60%


# ── 存入推薦記錄 ──────────────────────────────────────────────────────────────

async def save_recommendations(top_stocks: list[dict], today: str, scoring_version: str = "v2"):
    """
    Agent C 推薦後呼叫，將高分股票存入 recommendation_results。
    避免重複插入（UNIQUE on stock_code + recommend_date）。
    scoring_version: 'v1'=舊邏輯, 'v2'=2026-06-20 後翻轉BB/MA/Chip邏輯
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
            rec_price = s.get("recommend_price") or s.get("price")

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
                scoring_version   = scoring_version,
            ))

        await db.commit()
    logger.info(f"[Tracker] 存入 {len(top_stocks)} 筆推薦記錄 ({today}) [ver={scoring_version}]")


# ── 歷史收盤價查詢（Yahoo Finance 點位資料）──────────────────────────────────

async def _get_historical_close(code: str, target_date: date) -> float | None:
    """
    從 Yahoo Finance 取 target_date 當天（或之後最近的）收盤價。
    這才是正確的「T+5 報酬」依據，不使用即時報價。
    """
    import httpx
    from datetime import datetime as _dt
    try:
        start_ts = int(_dt.combine(target_date, _dt.min.time()).timestamp())
        end_ts   = int(_dt.combine(target_date + timedelta(days=14), _dt.min.time()).timestamp())
        for suffix in (".TW", ".TWO"):
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suffix}"
            params = {"period1": start_ts, "period2": end_ts, "interval": "1d"}
            async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as c:
                r = await c.get(url, params=params)
            data = r.json()
            result_list = data.get("chart", {}).get("result") or []
            if not result_list:
                continue
            res = result_list[0]
            timestamps = res.get("timestamp", [])
            closes_raw = res.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            for ts, cl in zip(timestamps, closes_raw):
                if cl and _dt.fromtimestamp(ts).date() >= target_date:
                    return float(cl)
    except Exception as e:
        logger.debug(f"[Tracker] historical close {code} {target_date}: {e}")
    return None


async def _get_benchmark_return_5d(rec_date: date) -> float | None:
    """取大盤 0050 在同一 T+5 視窗的報酬率（超額報酬基準）"""
    t5 = _subtract_trading_days(rec_date + timedelta(days=1), -5)  # T+5 trading day
    try:
        import httpx
        start_ts = int(datetime.combine(rec_date, datetime.min.time()).timestamp())
        end_ts   = int(datetime.combine(rec_date + timedelta(days=14), datetime.min.time()).timestamp())
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/0050.TW"
        params = {"period1": start_ts, "period2": end_ts, "interval": "1d"}
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(url, params=params)
        data = r.json()
        res = (data.get("chart", {}).get("result") or [None])[0]
        if not res:
            return None
        timestamps = res.get("timestamp", [])
        closes_raw = res.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        prices = [(datetime.fromtimestamp(ts).date(), cl)
                  for ts, cl in zip(timestamps, closes_raw) if cl]
        if len(prices) < 2:
            return None
        entry = prices[0][1]
        # Find price at T+5
        rec_date_plus = _subtract_trading_days(rec_date, -5)
        exit_price = None
        for d, cl in prices:
            if d >= rec_date_plus:
                exit_price = cl
                break
        if exit_price and entry:
            return round((exit_price - entry) / entry * 100, 2)
    except Exception as e:
        logger.debug(f"[Tracker] 0050 benchmark {rec_date}: {e}")
    return None


# ── 回填後續股價（使用真實歷史收盤，非即時報價）────────────────────────────────

async def backfill_prices():
    """每日 15:30 回填 5/10 交易日後的真實歷史收盤價"""
    today = date.today()

    async with AsyncSessionLocal() as db:
        cutoff_5d  = _subtract_trading_days(today, 5)
        cutoff_10d = _subtract_trading_days(today, 10)

        r5 = await db.execute(
            select(RecommendationResult).where(
                and_(
                    RecommendationResult.is_filled_5d == False,
                    RecommendationResult.recommend_date <= cutoff_5d.isoformat(),
                )
            )
        )
        recs_5d = r5.scalars().all()

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
            rec_date = date.fromisoformat(rec.recommend_date)
            t5_date  = _subtract_trading_days(rec_date, -5)  # T+5 target date
            price    = await _get_historical_close(rec.stock_code, t5_date)
            mkt_ret  = await _get_benchmark_return_5d(rec_date)

            if price and rec.recommend_price:
                ret       = (price - rec.recommend_price) / rec.recommend_price * 100
                # 新：超額報酬 > 0 才算成功；若取不到大盤則 fallback 到 > 0%
                hit_rel   = (ret > mkt_ret) if mkt_ret is not None else (ret > 0)
                async with AsyncSessionLocal() as db:
                    r = await db.execute(
                        select(RecommendationResult).where(RecommendationResult.id == rec.id)
                    )
                    obj = r.scalar_one_or_none()
                    if obj:
                        obj.price_5d      = price
                        obj.return_5d     = round(ret, 2)
                        obj.hit_target_5d = hit_rel   # 超額報酬 > 0
                        obj.is_filled_5d  = True
                        await db.commit()
                        filled_5 += 1
            await asyncio.sleep(1.5)
        except Exception as e:
            logger.error(f"[Tracker] 5d fill error {rec.stock_code}: {e}")

    for rec in recs_10d:
        try:
            rec_date = date.fromisoformat(rec.recommend_date)
            t10_date = _subtract_trading_days(rec_date, -10)
            price    = await _get_historical_close(rec.stock_code, t10_date)

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
                        obj.hit_target_10d = ret > 0
                        obj.is_filled_10d  = True
                        await db.commit()
                        filled_10 += 1
            await asyncio.sleep(1.5)
        except Exception as e:
            logger.error(f"[Tracker] 10d fill error {rec.stock_code}: {e}")

    logger.info(f"[Tracker] 回填完成：5d={filled_5} 10d={filled_10}")


# ── 準確率統計 ────────────────────────────────────────────────────────────────

async def get_accuracy_stats(days: int = 30) -> dict:
    """取得近 days 日的推薦準確率統計（含多閾值對比）"""
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    async with AsyncSessionLocal() as db:
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

    total   = len(recs)
    returns = [r.return_5d for r in recs if r.return_5d is not None]
    avg_ret = sum(returns) / len(returns) if returns else 0

    # 多閾值勝率對比（讓使用者看清楚門檻對數字的影響）
    thresholds = {
        "超額報酬>0（跑贏0050）": sum(1 for r in recs if r.hit_target_5d),   # 新標準
        "正報酬（>0%）":          sum(1 for r in recs if (r.return_5d or 0) > 0),
        "報酬>1%":                sum(1 for r in recs if (r.return_5d or 0) >= 1),
        "報酬>3%（舊標準）":      sum(1 for r in recs if (r.return_5d or 0) >= SUCCESS_THRESHOLD_ABS),
    }

    # 主要勝率：新標準（超額報酬 > 0）
    hits_5d  = thresholds["超額報酬>0（跑贏0050）"]
    win_rate = hits_5d / total * 100 if total else 0

    sorted_recs = sorted(recs, key=lambda x: x.return_5d or 0)
    worst = sorted_recs[:3]
    best  = sorted_recs[-3:]

    by_date: dict[str, list] = {}
    for r in recs:
        by_date.setdefault(r.recommend_date, []).append(r.return_5d or 0)

    daily_win_rates = [
        {
            "date":     d,
            "win_rate": round(sum(1 for x in rets if x > 0) / len(rets) * 100, 1),
            "avg_ret":  round(sum(rets) / len(rets), 2),
            "count":    len(rets),
        }
        for d, rets in sorted(by_date.items())
    ]

    # 版本分組統計（v1 舊邏輯 / v2 翻轉BB+MA+Chip / v3 基本面優先）
    version_stats: dict[str, dict] = {}
    for ver in ("v1", "v2", "v3"):
        ver_recs = [r for r in recs if (getattr(r, "scoring_version", None) or "v1") == ver]
        if ver_recs:
            ver_rets = [r.return_5d for r in ver_recs if r.return_5d is not None]
            ver_hits = sum(1 for r in ver_recs if r.hit_target_5d)
            version_stats[ver] = {
                "count":    len(ver_recs),
                "win_rate": round(ver_hits / len(ver_recs) * 100, 1),
                "avg_ret":  round(sum(ver_rets) / len(ver_rets), 2) if ver_rets else 0,
            }

    return {
        "total":      total,
        "win_rate":   round(win_rate, 1),
        "avg_return": round(avg_ret, 2),
        "hits_5d":    hits_5d,
        "threshold":  "超額報酬>0（跑贏0050）",
        "threshold_comparison": {
            k: {"hits": v, "rate": round(v / total * 100, 1)}
            for k, v in thresholds.items()
        },
        "version_stats":   version_stats,
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

    # 需要 300+ 筆才有統計意義；少於此數時自動調整是雜訊而非訊號
    if len(recs) < 300:
        logger.info(f"[Weights] 樣本不足（{len(recs)}/300），跳過調整")
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
    """
    n > 0：往過去扣除 n 個交易日（取 cutoff 日）。
    n < 0：往未來推進 abs(n) 個交易日（取 T+N 目標日）。
    n = 0：原日。
    """
    result = d
    step   = timedelta(days=-1 if n > 0 else 1)
    count  = 0
    target = abs(n)
    while count < target:
        result += step
        if result.weekday() < 5:
            count += 1
    return result
