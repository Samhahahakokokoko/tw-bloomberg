"""
self_optimizer.py — 系統自我優化引擎

每個交易日 20:00 自動執行，根據近期績效回饋調整四個維度：
  1. 評分權重（fundamental / chip / technical）
  2. Alpha 因子狀態（ACTIVE / PAUSED / DEAD）
  3. 分析師可信度權重
  4. 系統模組自動恢復

每個模組獨立 try/except，互不影響。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select, func, text

from ..models.database import AsyncSessionLocal, settings
from ..models.models import (
    RecommendationResult, ScoringWeight,
    AlphaRegistry, AlphaDecayLog,
    Analyst, AnalystCall,
    FactorWeightLog,
)


# ── 常數 ──────────────────────────────────────────────────────────────────────

_SCORE_W_MIN = 0.15   # 單一評分維度最低權重
_SCORE_W_MAX = 0.55   # 單一評分維度最高權重
_SCORE_MOMENTUM = 0.7  # 新權重混合比例：70% 舊 + 30% 新計算值
_SCORE_MIN_SAMPLES = 10  # 至少要有這麼多筆填充結果才調整

_ALPHA_PAUSE_DAYS = 3   # dead_days >= 3 → PAUSED
_ALPHA_DEAD_DAYS = 10   # dead_days >= 10 → DEAD
_ALPHA_REVIVE_DAYS = 5  # PAUSED 且 dead_days == 0 連續 5 日 → ACTIVE

_ANALYST_MIN_CALLS = 5  # 至少要有這麼多筆有結果的推薦才調整
_ANALYST_LOOKBACK = 30  # 回顧天數


# ── 1. 評分權重自動調整 ───────────────────────────────────────────────────────

async def _adjust_scoring_weights(db) -> dict[str, Any]:
    """
    讀取近 28 日已填充的推薦結果，計算各維度與成功率的相關性，
    並更新 ScoringWeight。只在變化量 > 2% 時才實際寫入。
    """
    result: dict[str, Any] = {"module": "scoring_weights", "changed": False, "detail": ""}
    try:
        since = (datetime.utcnow() - timedelta(days=28)).strftime("%Y-%m-%d")
        rows = (await db.execute(
            select(RecommendationResult)
            .where(RecommendationResult.recommend_date >= since)
            .where(RecommendationResult.is_filled_5d == True)
        )).scalars().all()

        if len(rows) < _SCORE_MIN_SAMPLES:
            result["detail"] = f"樣本不足（{len(rows)} < {_SCORE_MIN_SAMPLES}），跳過"
            return result

        # 計算每個維度的加權成功率
        # 邏輯：各維度分數 → 對 hit_target_5d 的線性相關
        wins = [r for r in rows if r.hit_target_5d]
        total = len(rows)

        def _dim_correlation(rows, attr: str) -> float:
            scores = [getattr(r, attr) or 0.0 for r in rows]
            hits = [1.0 if r.hit_target_5d else 0.0 for r in rows]
            if not scores:
                return 0.0
            mean_s = sum(scores) / len(scores)
            mean_h = sum(hits) / len(hits)
            cov = sum((s - mean_s) * (h - mean_h) for s, h in zip(scores, hits))
            std_s = (sum((s - mean_s) ** 2 for s in scores) ** 0.5) or 1e-9
            std_h = (sum((h - mean_h) ** 2 for h in hits) ** 0.5) or 1e-9
            return cov / (std_s * std_h)

        corr_f = max(0.01, _dim_correlation(rows, "fundamental_score"))
        corr_c = max(0.01, _dim_correlation(rows, "chip_score"))
        corr_t = max(0.01, _dim_correlation(rows, "technical_score"))
        total_corr = corr_f + corr_c + corr_t

        raw_f = corr_f / total_corr
        raw_c = corr_c / total_corr
        raw_t = corr_t / total_corr

        # 取上週最新權重（若無則用預設值）
        latest_w = (await db.execute(
            select(ScoringWeight).order_by(ScoringWeight.effective_date.desc()).limit(1)
        )).scalar_one_or_none()

        old_f = latest_w.fundamental_weight if latest_w else 0.35
        old_c = latest_w.chip_weight if latest_w else 0.35
        old_t = latest_w.technical_weight if latest_w else 0.30

        # Momentum blending
        new_f = _SCORE_MOMENTUM * old_f + (1 - _SCORE_MOMENTUM) * raw_f
        new_c = _SCORE_MOMENTUM * old_c + (1 - _SCORE_MOMENTUM) * raw_c
        new_t = _SCORE_MOMENTUM * old_t + (1 - _SCORE_MOMENTUM) * raw_t

        # Clamp + renormalize
        new_f = min(_SCORE_W_MAX, max(_SCORE_W_MIN, new_f))
        new_c = min(_SCORE_W_MAX, max(_SCORE_W_MIN, new_c))
        new_t = min(_SCORE_W_MAX, max(_SCORE_W_MIN, new_t))
        total_w = new_f + new_c + new_t
        new_f, new_c, new_t = new_f / total_w, new_c / total_w, new_t / total_w

        # 變化量 > 2% 才寫入
        if max(abs(new_f - old_f), abs(new_c - old_c), abs(new_t - old_t)) < 0.02:
            result["detail"] = f"變化量 < 2%，維持舊權重 F={old_f:.3f} C={old_c:.3f} T={old_t:.3f}"
            return result

        overall_win = len(wins) / total
        win_rates = {
            "fundamental": len([r for r in wins if r.fundamental_score and r.fundamental_score > 60]) / max(1, total),
            "chip":        len([r for r in wins if r.chip_score and r.chip_score > 60]) / max(1, total),
            "technical":   len([r for r in wins if r.technical_score and r.technical_score > 60]) / max(1, total),
        }

        today = datetime.utcnow().strftime("%Y-%m-%d")
        db.add(ScoringWeight(
            effective_date=today,
            fundamental_weight=round(new_f, 4),
            chip_weight=round(new_c, 4),
            technical_weight=round(new_t, 4),
            fundamental_win_rate=round(win_rates["fundamental"], 4),
            chip_win_rate=round(win_rates["chip"], 4),
            technical_win_rate=round(win_rates["technical"], 4),
            overall_win_rate=round(overall_win, 4),
            notes=f"自動調整：corr_f={corr_f:.3f} corr_c={corr_c:.3f} corr_t={corr_t:.3f}",
        ))
        await db.commit()

        result["changed"] = True
        result["detail"] = (
            f"F {old_f:.3f}→{new_f:.3f}  "
            f"C {old_c:.3f}→{new_c:.3f}  "
            f"T {old_t:.3f}→{new_t:.3f}  "
            f"整體勝率={overall_win:.1%}（n={total}）"
        )
        logger.info(f"[SelfOpt] scoring_weights updated: {result['detail']}")

    except Exception as e:
        result["detail"] = f"錯誤：{e}"
        logger.error(f"[SelfOpt] scoring_weights failed: {e}", exc_info=True)

    return result


# ── 2. Alpha 因子狀態管理 ─────────────────────────────────────────────────────

async def _manage_alpha_factors(db) -> dict[str, Any]:
    """
    根據 AlphaRegistry.dead_days 自動暫停/恢復/淘汰 Alpha 因子，
    並在 FactorWeightLog 記錄每次變動。
    """
    result: dict[str, Any] = {"module": "alpha_factors", "changed": False, "detail": ""}
    try:
        alphas = (await db.execute(select(AlphaRegistry))).scalars().all()
        changes = []

        for a in alphas:
            old_status = a.status
            old_weight = a.weight

            if a.dead_days >= _ALPHA_DEAD_DAYS and a.status != "DEAD":
                a.status = "DEAD"
                a.weight = 0.0
                changes.append(f"{a.alpha_name}: DEAD（dead_days={a.dead_days}）")

            elif a.dead_days >= _ALPHA_PAUSE_DAYS and a.status == "ACTIVE":
                a.status = "PAUSED"
                a.weight = round(max(0.02, a.weight * 0.5), 4)
                changes.append(f"{a.alpha_name}: ACTIVE→PAUSED（dead_days={a.dead_days}，weight {old_weight:.3f}→{a.weight:.3f}）")

            elif a.dead_days == 0 and a.status == "PAUSED":
                # IC 轉正，嘗試恢復
                history = json.loads(a.ic_history or "[]")
                recent_positive = sum(1 for v in history[-_ALPHA_REVIVE_DAYS:] if v > 0)
                if recent_positive >= _ALPHA_REVIVE_DAYS:
                    a.status = "ACTIVE"
                    a.weight = round(min(1.0, a.weight * 1.3), 4)
                    changes.append(f"{a.alpha_name}: PAUSED→ACTIVE（連續{recent_positive}日正IC，weight {old_weight:.3f}→{a.weight:.3f}）")

            a.updated_at = datetime.utcnow()

            if a.status != old_status or a.weight != old_weight:
                db.add(FactorWeightLog(factor_name=a.alpha_name, weight=a.weight))

        if changes:
            await db.commit()
            result["changed"] = True
            result["detail"] = "；".join(changes)
            logger.info(f"[SelfOpt] alpha_factors: {result['detail']}")
        else:
            result["detail"] = f"無變動（{len(alphas)} 個因子均在正常範圍）"

    except Exception as e:
        result["detail"] = f"錯誤：{e}"
        logger.error(f"[SelfOpt] alpha_factors failed: {e}", exc_info=True)

    return result


# ── 3. 分析師可信度權重調整 ───────────────────────────────────────────────────

async def _adjust_analyst_weights(db) -> dict[str, Any]:
    """
    讀取近 30 日有結果的 AnalystCall，更新 Analyst.weight 和 quality_score。
    只在勝率變化 > 5% 時才更新。
    """
    result: dict[str, Any] = {"module": "analyst_weights", "changed": False, "detail": ""}
    try:
        since = (datetime.utcnow() - timedelta(days=_ANALYST_LOOKBACK)).strftime("%Y-%m-%d")
        calls = (await db.execute(
            select(AnalystCall)
            .where(AnalystCall.date >= since)
            .where(AnalystCall.was_correct.isnot(None))
        )).scalars().all()

        if not calls:
            result["detail"] = "無近期結果記錄，跳過"
            return result

        # 按分析師聚合
        from collections import defaultdict
        groups: dict[str, list] = defaultdict(list)
        for c in calls:
            groups[c.analyst_id].append(c)

        analysts = {a.analyst_id: a for a in (await db.execute(select(Analyst))).scalars().all()}
        changes = []

        for aid, grp in groups.items():
            if len(grp) < _ANALYST_MIN_CALLS:
                continue
            analyst = analysts.get(aid)
            if not analyst:
                continue

            win_rate = sum(1 for c in grp if c.was_correct) / len(grp)
            avg_ret = sum(c.result_5d or 0.0 for c in grp) / len(grp)

            # quality_score = win_rate * 0.7 + max(0, avg_ret/10) * 0.3 (capped 0-1)
            new_quality = min(1.0, max(0.0, win_rate * 0.7 + max(0.0, avg_ret / 10) * 0.3))
            new_weight = round(min(1.0, max(0.2, new_quality)), 3)

            old_win = analyst.win_rate or 0.5
            if abs(win_rate - old_win) < 0.05 and abs(new_weight - analyst.weight) < 0.05:
                continue

            old_w = analyst.weight
            analyst.win_rate = round(win_rate, 4)
            analyst.avg_return = round(avg_ret, 4)
            analyst.quality_score = round(new_quality, 4)
            analyst.weight = new_weight
            analyst.updated_at = datetime.utcnow()

            changes.append(f"{analyst.name}（{aid[:8]}）勝率{old_win:.0%}→{win_rate:.0%} weight {old_w:.2f}→{new_weight:.2f}")

        if changes:
            await db.commit()
            result["changed"] = True
            result["detail"] = "；".join(changes[:5]) + (f"（等{len(changes)-5}筆）" if len(changes) > 5 else "")
            logger.info(f"[SelfOpt] analyst_weights: {result['detail']}")
        else:
            result["detail"] = f"無需調整（{len(groups)} 位分析師變化量均 < 5%）"

    except Exception as e:
        result["detail"] = f"錯誤：{e}"
        logger.error(f"[SelfOpt] analyst_weights failed: {e}", exc_info=True)

    return result


# ── 4. 系統模組自動恢復 ───────────────────────────────────────────────────────

async def _auto_recover_modules() -> dict[str, Any]:
    """
    讀取 system_monitor 狀態，若模組連續失敗且超過 30 分鐘未執行，
    記錄告警（實際重啟由 scheduler 的 misfire_grace_time 處理）。
    """
    result: dict[str, Any] = {"module": "module_recovery", "changed": False, "detail": ""}
    try:
        from .system_monitor import check_all_modules
        modules = await check_all_modules()

        now = datetime.utcnow()
        stale = []
        for m in modules:
            if m.status == "error" and m.error_count >= 3:
                last = m.last_run
                if last:
                    try:
                        last_dt = datetime.fromisoformat(str(last).replace("Z", ""))
                        if (now - last_dt).total_seconds() > 1800:
                            stale.append(f"{m.module_name}（連錯{m.error_count}次，{int((now-last_dt).total_seconds()//60)}分未執行）")
                    except Exception as e:
                        pass

        if stale:
            result["changed"] = True
            result["detail"] = "需人工確認：" + "；".join(stale)
            logger.warning(f"[SelfOpt] stale modules: {result['detail']}")
        else:
            result["detail"] = f"所有模組正常（共 {len(modules)} 個）"

    except Exception as e:
        result["detail"] = f"錯誤：{e}"
        logger.error(f"[SelfOpt] module_recovery failed: {e}", exc_info=True)

    return result


# ── 主入口 ────────────────────────────────────────────────────────────────────

async def run_self_optimizer() -> dict[str, Any]:
    """
    執行所有自我優化模組，回傳摘要並推送 LINE 報告。
    每個模組獨立 try/except，互不影響。
    """
    start = datetime.utcnow()
    logger.info("[SelfOpt] 自我優化開始")

    results = []
    async with AsyncSessionLocal() as db:
        results.append(await _adjust_scoring_weights(db))
        results.append(await _manage_alpha_factors(db))
        results.append(await _adjust_analyst_weights(db))

    results.append(await _auto_recover_modules())

    changed_count = sum(1 for r in results if r.get("changed"))
    elapsed = (datetime.utcnow() - start).total_seconds()

    summary = {
        "modules": results,
        "changed_count": changed_count,
        "elapsed_sec": round(elapsed, 1),
        "run_at": start.isoformat(),
    }

    await _push_optimizer_report(results, changed_count, elapsed)
    logger.info(f"[SelfOpt] 完成，{changed_count}/{len(results)} 個模組有變動，耗時 {elapsed:.1f}s")
    return summary


async def _push_optimizer_report(results: list, changed_count: int, elapsed: float) -> None:
    """推送自我優化摘要到 LINE 管理員"""
    admin_uid = settings.admin_line_uid if hasattr(settings, "admin_line_uid") else ""
    if not admin_uid:
        import os
        admin_uid = os.getenv("ADMIN_LINE_UID", "")
    if not admin_uid:
        return

    icons = {"scoring_weights": "⚖️", "alpha_factors": "🧬", "analyst_weights": "👤", "module_recovery": "🔧"}
    lines = [
        f"🤖 系統自我優化報告",
        f"{'─' * 22}",
        f"時間：{datetime.now().strftime('%m/%d %H:%M')}",
        f"調整項：{changed_count}/{len(results)} 個",
        f"耗時：{elapsed:.1f}s",
        "",
    ]
    for r in results:
        icon = icons.get(r["module"], "•")
        mark = "✅" if r["changed"] else "─"
        lines.append(f"{mark} {icon} {r['module']}")
        if r.get("detail"):
            lines.append(f"   {r['detail'][:80]}")

    text = "\n".join(lines)
    try:
        from .line_push import push_line_messages
        await push_line_messages(admin_uid, [{"type": "text", "text": text}], timeout=15, context="self_optimizer.report")
    except Exception as e:
        logger.warning(f"[SelfOpt] LINE report push failed: {e}")
