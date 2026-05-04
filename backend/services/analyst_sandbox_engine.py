"""
analyst_sandbox_engine.py — 30 天沙盒追蹤引擎

沙盒期間：
  - 繼續用 youtube_alpha_engine 抓影片、解析觀點
  - AnalystCall 正常儲存（但 is_sandbox=True）
  - 完全不進入 decision_engine 或 consensus 計算
  - 每日計算沙盒期間的模擬績效

升級條件（全部通過）：
  - 沙盒 >= 30 天
  - total_calls >= 5
  - win_rate >= 0.50
  - avg_return >= 0.0（至少不虧）
  - 無重大飄移事件（非方向逆轉）

降級/拒絕條件（任一觸發）：
  - 連續 3 筆推薦全錯
  - 勝率 < 0.30
  - 多次沉默超過 14 天（不活躍）
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger


# ── 晉升門檻 ────────────────────────────────────────────────────────────────
SANDBOX_DAYS        = 30
MIN_CALLS           = 5
MIN_WIN_RATE        = 0.50
MIN_AVG_RETURN      = 0.0
MAX_CONSECUTIVE_FAIL = 3
FAIL_WIN_RATE_FLOOR = 0.30
SILENCE_DAYS        = 14


@dataclass
class SandboxEvaluation:
    analyst_id:   str
    analyst_name: str
    sandbox_days: int
    total_calls:  int
    win_rate:     float
    avg_return:   float
    consecutive_fail: int
    last_call_days_ago: int
    eligible_for_promotion: bool
    reject:       bool
    reject_reason: str = ""
    promotion_tier: str = "B"    # 預設晉升為 B 級
    notes:        list[str] = field(default_factory=list)

    def format_line(self) -> str:
        status = "✅ 達標可晉升" if self.eligible_for_promotion else (
                 "❌ 未達標拒絕" if self.reject else "⏳ 沙盒追蹤中")
        lines = [
            f"📊 沙盒評估：{self.analyst_name}",
            f"狀態：{status}",
            f"追蹤天數：{self.sandbox_days}/30",
            f"總推薦：{self.total_calls} 筆",
            f"勝率：{self.win_rate:.0%}  均報酬：{self.avg_return:+.1%}",
        ]
        if self.reject_reason:
            lines.append(f"拒絕原因：{self.reject_reason}")
        if self.notes:
            for n in self.notes:
                lines.append(f"  ⚠️ {n}")
        if self.eligible_for_promotion:
            lines.append(f"建議 Tier：{self.promotion_tier}")
            lines.append("輸入 /analyst promote {self.analyst_id} 確認晉升")
        return "\n".join(lines)


async def evaluate_sandbox(analyst_id: str) -> Optional[SandboxEvaluation]:
    """評估沙盒分析師是否達到晉升/拒絕條件"""
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import Analyst, AnalystSandbox, AnalystCall
        from sqlalchemy import select, func

        async with AsyncSessionLocal() as db:
            # 分析師基本資料
            r = await db.execute(
                select(Analyst).where(Analyst.analyst_id == analyst_id)
            )
            analyst = r.scalar_one_or_none()
            if not analyst:
                return None

            # 沙盒記錄
            r2 = await db.execute(
                select(AnalystSandbox).where(AnalystSandbox.analyst_id == analyst_id)
            )
            sandbox = r2.scalar_one_or_none()
            if not sandbox or sandbox.status != "active":
                return None

            # 計算沙盒天數
            try:
                start = datetime.strptime(sandbox.sandbox_start, "%Y-%m-%d")
                sandbox_days = (datetime.now() - start).days
            except Exception:
                sandbox_days = 0

            # 取得沙盒期間的 AnalystCall
            r3 = await db.execute(
                select(AnalystCall)
                .where(AnalystCall.analyst_id == analyst_id)
                .where(AnalystCall.date >= sandbox.sandbox_start)
                .order_by(AnalystCall.date.desc())
            )
            calls = r3.scalars().all()

        total_calls = len(calls)

        # 勝率：以 actual_return > 0 為勝
        scored_calls = [c for c in calls if c.actual_return is not None]
        if scored_calls:
            wins = sum(1 for c in scored_calls if (c.actual_return or 0) > 0)
            win_rate = wins / len(scored_calls)
            avg_return = sum((c.actual_return or 0) for c in scored_calls) / len(scored_calls)
        else:
            win_rate = 0.5
            avg_return = 0.0

        # 連續失敗
        consecutive_fail = 0
        for c in calls[:5]:
            if (c.actual_return or 0) < 0:
                consecutive_fail += 1
            else:
                break

        # 最後一次推薦距今天數
        last_call_days_ago = 0
        if calls:
            try:
                last_dt = datetime.strptime(calls[0].date, "%Y-%m-%d")
                last_call_days_ago = (datetime.now() - last_dt).days
            except Exception:
                pass

        # ── 判斷邏輯 ───────────────────────────────────────────────────────────
        notes: list[str] = []
        reject = False
        reject_reason = ""

        if win_rate < FAIL_WIN_RATE_FLOOR and total_calls >= 5:
            reject = True
            reject_reason = f"勝率僅 {win_rate:.0%}，低於最低門檻 {FAIL_WIN_RATE_FLOOR:.0%}"

        if consecutive_fail >= MAX_CONSECUTIVE_FAIL:
            reject = True
            reject_reason = f"連續 {consecutive_fail} 筆推薦全錯"

        if last_call_days_ago > SILENCE_DAYS and sandbox_days > 14:
            notes.append(f"已沉默 {last_call_days_ago} 天，疑似不活躍頻道")

        # 晉升條件
        eligible = (
            not reject
            and sandbox_days >= SANDBOX_DAYS
            and total_calls >= MIN_CALLS
            and win_rate >= MIN_WIN_RATE
            and avg_return >= MIN_AVG_RETURN
        )

        if sandbox_days < SANDBOX_DAYS:
            notes.append(f"沙盒期尚剩 {SANDBOX_DAYS - sandbox_days} 天")
        if total_calls < MIN_CALLS:
            notes.append(f"推薦筆數不足（{total_calls}/{MIN_CALLS}）")
        if win_rate < MIN_WIN_RATE and not reject:
            notes.append(f"勝率偏低（{win_rate:.0%}）")

        # 決定晉升 Tier
        if eligible:
            if win_rate >= 0.65 and avg_return >= 0.05:
                promotion_tier = "A"
            else:
                promotion_tier = "B"
        else:
            promotion_tier = "B"

        return SandboxEvaluation(
            analyst_id      = analyst_id,
            analyst_name    = analyst.name,
            sandbox_days    = sandbox_days,
            total_calls     = total_calls,
            win_rate        = win_rate,
            avg_return      = avg_return,
            consecutive_fail = consecutive_fail,
            last_call_days_ago = last_call_days_ago,
            eligible_for_promotion = eligible,
            reject          = reject,
            reject_reason   = reject_reason,
            promotion_tier  = promotion_tier,
            notes           = notes,
        )

    except Exception as e:
        logger.error("[sandbox] evaluate failed for %s: %s", analyst_id, e)
        return None


async def promote_analyst(analyst_id: str, new_tier: str = "") -> tuple[bool, str]:
    """將沙盒分析師晉升為正式分析師"""
    evaluation = await evaluate_sandbox(analyst_id)
    if not evaluation:
        return False, "找不到沙盒記錄"
    if not evaluation.eligible_for_promotion:
        return False, f"尚未達標：{', '.join(evaluation.notes)}"

    tier = new_tier.upper() if new_tier else evaluation.promotion_tier

    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import Analyst, AnalystSandbox
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Analyst).where(Analyst.analyst_id == analyst_id))
            analyst = r.scalar_one_or_none()
            if not analyst:
                return False, "分析師記錄不存在"

            analyst.tier        = tier
            analyst.win_rate    = evaluation.win_rate
            analyst.avg_return  = evaluation.avg_return
            analyst.total_calls = evaluation.total_calls
            analyst.notes       = analyst.notes.replace(
                f"sandbox_start={analyst.added_date}",
                f"promoted={datetime.now().strftime('%Y-%m-%d')}"
            )

            # 更新沙盒狀態
            r2 = await db.execute(
                select(AnalystSandbox).where(AnalystSandbox.analyst_id == analyst_id)
            )
            sandbox = r2.scalar_one_or_none()
            if sandbox:
                sandbox.status     = "promoted"
                sandbox.final_tier = tier
                sandbox.final_win_rate = evaluation.win_rate

            await db.commit()

        logger.info("[sandbox] promoted %s → Tier %s", evaluation.analyst_name, tier)
        return True, f"🎉 {evaluation.analyst_name} 晉升 {tier} 級正式分析師！（勝率 {evaluation.win_rate:.0%}）"

    except Exception as e:
        logger.error("[sandbox] promote failed: %s", e)
        return False, f"晉升失敗：{e}"


async def reject_sandbox_analyst(analyst_id: str, reason: str = "") -> tuple[bool, str]:
    """拒絕沙盒分析師（標記為停用）"""
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import Analyst, AnalystSandbox
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Analyst).where(Analyst.analyst_id == analyst_id))
            analyst = r.scalar_one_or_none()
            if not analyst:
                return False, "分析師不存在"

            analyst.is_active = False
            analyst.notes     = analyst.notes + f" | rejected:{reason}"

            r2 = await db.execute(
                select(AnalystSandbox).where(AnalystSandbox.analyst_id == analyst_id)
            )
            sandbox = r2.scalar_one_or_none()
            if sandbox:
                sandbox.status = "rejected"

            await db.commit()

        return True, f"已移除沙盒分析師 {analyst_id}（{reason}）"
    except Exception as e:
        return False, f"移除失敗：{e}"


async def run_daily_sandbox_evaluation():
    """每日排程：批次評估所有活躍沙盒分析師"""
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import AnalystSandbox
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(AnalystSandbox).where(AnalystSandbox.status == "active")
            )
            sandboxes = r.scalars().all()

        promoted = 0
        rejected = 0
        for sb in sandboxes:
            eval_result = await evaluate_sandbox(sb.analyst_id)
            if not eval_result:
                continue
            if eval_result.reject:
                ok, _ = await reject_sandbox_analyst(sb.analyst_id, eval_result.reject_reason)
                if ok:
                    rejected += 1
            elif eval_result.eligible_for_promotion:
                ok, _ = await promote_analyst(sb.analyst_id)
                if ok:
                    promoted += 1

        logger.info("[sandbox] daily eval: %d promoted, %d rejected, %d still active",
                    promoted, rejected, len(sandboxes) - promoted - rejected)
    except Exception as e:
        logger.error("[sandbox] daily eval failed: %s", e)


async def list_sandbox_analysts() -> list[SandboxEvaluation]:
    """列出所有活躍沙盒分析師的評估結果"""
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import AnalystSandbox
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(AnalystSandbox).where(AnalystSandbox.status == "active")
            )
            sandboxes = r.scalars().all()

        results = []
        for sb in sandboxes:
            ev = await evaluate_sandbox(sb.analyst_id)
            if ev:
                results.append(ev)
        return results
    except Exception as e:
        logger.error("[sandbox] list failed: %s", e)
        return []
