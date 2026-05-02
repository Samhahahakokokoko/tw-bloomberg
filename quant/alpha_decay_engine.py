"""
alpha_decay_engine.py — Alpha 因子衰退追蹤

追蹤指標：
    - 滾動30日 IC 趨勢（是否持續下滑）
    - 勝率變化（近20次 vs 歷史均值）
    - Sharpe 下滑速度
    - Alpha 半衰期估算

自動狀態機：
    IC_mean < 0.05 連續10日 → DEGRADING
    IC_mean < 0    連續5日  → DEAD（自動停用）
    IC 回升 > 0.10          → RECOVERING（恢復小權重）

每週五推送 Alpha 健康週報
LINE 指令：/alpha → Alpha 健康狀態
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── 狀態機門檻 ─────────────────────────────────────────────────────────────────
IC_DEGRADING_THRESHOLD   = 0.05   # IC 均值低於此值
IC_DEGRADING_DAYS        = 10     # 連續天數 → DEGRADING
IC_DEAD_THRESHOLD        = 0.0    # IC 均值低於此值
IC_DEAD_DAYS             = 5      # 連續天數 → DEAD
IC_RECOVERY_THRESHOLD    = 0.10   # IC 回升超過此值 → RECOVERING

STATUS_ACTIVE     = "ACTIVE"
STATUS_DEGRADING  = "DEGRADING"
STATUS_RECOVERING = "RECOVERING"
STATUS_DEAD       = "DEAD"


@dataclass
class AlphaHealth:
    name:         str
    status:       str          # ACTIVE / DEGRADING / RECOVERING / DEAD
    ic_current:   float
    ic_30d_mean:  float
    ic_trend:     float        # 正=上升，負=下降（30日線性回歸斜率）
    win_rate:     float        # 近20次 vs 歷史
    win_rate_hist:float
    sharpe:       float
    half_life:    float        # 估算半衰期（天），inf = 健康
    degrading_days: int
    weight:       float        # 當前在 ensemble 的權重

    @property
    def status_icon(self) -> str:
        return {
            STATUS_ACTIVE:     "✅",
            STATUS_DEGRADING:  "⚠️",
            STATUS_RECOVERING: "🔄",
            STATUS_DEAD:       "🔴",
        }.get(self.status, "❓")

    def format_line(self) -> str:
        trend_arrow = "↑" if self.ic_trend > 0.001 else ("↓" if self.ic_trend < -0.001 else "→")
        return (
            f"{self.status_icon} {self.name}：IC={self.ic_30d_mean:.3f}{trend_arrow}"
            f"  勝率={self.win_rate:.0%}  w={self.weight:.2f}"
        )

    def to_dict(self) -> dict:
        return {
            "name":          self.name,
            "status":        self.status,
            "ic_current":    round(self.ic_current, 4),
            "ic_30d_mean":   round(self.ic_30d_mean, 4),
            "ic_trend":      round(self.ic_trend, 6),
            "win_rate":      round(self.win_rate, 4),
            "win_rate_hist": round(self.win_rate_hist, 4),
            "sharpe":        round(self.sharpe, 4),
            "half_life":     round(self.half_life, 1) if self.half_life < 9999 else None,
            "degrading_days":self.degrading_days,
            "weight":        round(self.weight, 4),
        }


class AlphaDecayEngine:
    """
    Alpha 因子衰退追蹤引擎。

    使用方式：
        engine = AlphaDecayEngine()
        engine.update_ic("momentum_alpha", ic_value=0.12)
        health = engine.get_health("momentum_alpha")
        print(health.format_line())
        report = engine.format_weekly_report()
    """

    def __init__(self):
        self._cache: dict[str, dict] = {}   # {alpha_name: state}

    # ── IC 更新入口 ───────────────────────────────────────────────────────────

    async def update_ic(self, alpha_name: str, ic_value: float) -> AlphaHealth:
        """每日呼叫，更新 IC 並判斷狀態"""
        state = await self._load_state(alpha_name)
        history: list[float] = state.get("ic_history", [])
        history.append(float(ic_value))
        if len(history) > 60:
            history = history[-60:]

        state["ic_history"]  = history
        state["ic_current"]  = ic_value
        state["updated_at"]  = datetime.now().isoformat()

        # 計算統計量
        h30 = history[-30:] if len(history) >= 30 else history
        ic_mean    = float(np.mean(h30))
        ic_trend   = self._linear_trend(h30)
        win_rate   = float(np.mean([v > 0 for v in history[-20:]])) if len(history) >= 5 else 0.5
        win_hist   = float(np.mean([v > 0 for v in history])) if history else 0.5
        sharpe     = self._ic_sharpe(h30)
        half_life  = self._estimate_half_life(history)

        # 狀態機
        prev_status    = state.get("status", STATUS_ACTIVE)
        degrading_days = state.get("degrading_days", 0)

        if ic_mean < IC_DEAD_THRESHOLD:
            degrading_days += 1
        elif ic_mean < IC_DEGRADING_THRESHOLD:
            degrading_days += 1
        else:
            degrading_days = max(0, degrading_days - 1)

        # 狀態轉換
        if prev_status == STATUS_DEAD:
            if ic_mean > IC_RECOVERY_THRESHOLD:
                new_status = STATUS_RECOVERING
                degrading_days = 0
            else:
                new_status = STATUS_DEAD
        elif degrading_days >= IC_DEAD_DAYS and ic_mean < IC_DEAD_THRESHOLD:
            new_status = STATUS_DEAD
        elif degrading_days >= IC_DEGRADING_DAYS and ic_mean < IC_DEGRADING_THRESHOLD:
            new_status = STATUS_DEGRADING
        elif prev_status in (STATUS_DEGRADING, STATUS_RECOVERING) and ic_mean > IC_RECOVERY_THRESHOLD:
            new_status = STATUS_RECOVERING
        elif prev_status == STATUS_RECOVERING and ic_mean > IC_DEGRADING_THRESHOLD and degrading_days == 0:
            new_status = STATUS_ACTIVE
        else:
            new_status = prev_status if prev_status != STATUS_DEAD else STATUS_DEGRADING

        # 權重更新
        weight = self._calc_weight(new_status, ic_mean, prev_status)

        state.update({
            "status":        new_status,
            "ic_mean":       ic_mean,
            "ic_trend":      ic_trend,
            "win_rate":      win_rate,
            "win_hist":      win_hist,
            "sharpe":        sharpe,
            "half_life":     half_life,
            "degrading_days":degrading_days,
            "weight":        weight,
        })
        await self._save_state(alpha_name, state)

        health = AlphaHealth(
            name=alpha_name, status=new_status,
            ic_current=ic_value, ic_30d_mean=ic_mean,
            ic_trend=ic_trend, win_rate=win_rate, win_rate_hist=win_hist,
            sharpe=sharpe, half_life=half_life,
            degrading_days=degrading_days, weight=weight,
        )
        await self._log_to_db(alpha_name, health)
        return health

    def update_ic_sync(self, alpha_name: str, ic_value: float, history: list[float]) -> AlphaHealth:
        """同步版本，用於測試或批量計算"""
        history = list(history) + [float(ic_value)]
        h30 = history[-30:] if len(history) >= 30 else history
        ic_mean   = float(np.mean(h30))
        ic_trend  = self._linear_trend(h30)
        win_rate  = float(np.mean([v > 0 for v in history[-20:]])) if len(history) >= 5 else 0.5
        win_hist  = float(np.mean([v > 0 for v in history])) if history else 0.5
        sharpe    = self._ic_sharpe(h30)
        half_life = self._estimate_half_life(history)
        status    = self._determine_status(ic_mean, ic_value, history)
        weight    = self._calc_weight(status, ic_mean, "ACTIVE")

        return AlphaHealth(
            name=alpha_name, status=status,
            ic_current=ic_value, ic_30d_mean=ic_mean,
            ic_trend=ic_trend, win_rate=win_rate, win_rate_hist=win_hist,
            sharpe=sharpe, half_life=half_life,
            degrading_days=0, weight=weight,
        )

    # ── 查詢 ─────────────────────────────────────────────────────────────────

    async def get_health(self, alpha_name: str) -> AlphaHealth:
        state = await self._load_state(alpha_name)
        return AlphaHealth(
            name=alpha_name,
            status=state.get("status", STATUS_ACTIVE),
            ic_current=state.get("ic_current", 0.0),
            ic_30d_mean=state.get("ic_mean", 0.0),
            ic_trend=state.get("ic_trend", 0.0),
            win_rate=state.get("win_rate", 0.5),
            win_rate_hist=state.get("win_hist", 0.5),
            sharpe=state.get("sharpe", 0.0),
            half_life=state.get("half_life", 9999),
            degrading_days=state.get("degrading_days", 0),
            weight=state.get("weight", 1.0),
        )

    async def get_all_health(self) -> list[AlphaHealth]:
        """取得所有已追蹤 Alpha 的健康狀態"""
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import AlphaRegistry
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(select(AlphaRegistry))).scalars().all()
            results = []
            for row in rows:
                history = json.loads(row.ic_history or "[]")
                h30 = history[-30:] if len(history) >= 30 else history
                ic_mean   = float(np.mean(h30)) if h30 else 0.0
                ic_trend  = self._linear_trend(h30)
                win_rate  = float(np.mean([v > 0 for v in history[-20:]])) if len(history) >= 5 else 0.5
                sharpe    = self._ic_sharpe(h30)
                half_life = self._estimate_half_life(history)
                results.append(AlphaHealth(
                    name=row.alpha_name, status=row.status,
                    ic_current=row.ic_current, ic_30d_mean=ic_mean,
                    ic_trend=ic_trend, win_rate=win_rate, win_rate_hist=win_rate,
                    sharpe=sharpe, half_life=half_life,
                    degrading_days=row.dead_days, weight=row.weight,
                ))
            results.sort(key=lambda h: -h.ic_30d_mean)
            return results
        except Exception as e:
            logger.warning("[AlphaDecay] get_all failed: %s", e)
            return self._mock_health_list()

    # ── 報告 ─────────────────────────────────────────────────────────────────

    def format_weekly_report(self, healths: list[AlphaHealth]) -> str:
        if not healths:
            return "📊 Alpha 健康週報\n\n尚無追蹤資料"
        lines = [
            "📊 Alpha 健康週報",
            f"追蹤因子：{len(healths)} 個",
            "─" * 22,
        ]
        active    = [h for h in healths if h.status == STATUS_ACTIVE]
        degrading = [h for h in healths if h.status == STATUS_DEGRADING]
        recovering= [h for h in healths if h.status == STATUS_RECOVERING]
        dead      = [h for h in healths if h.status == STATUS_DEAD]

        for h in active:
            lines.append(h.format_line())
        for h in degrading:
            lines.append(h.format_line())
        for h in recovering:
            lines.append(h.format_line())
        for h in dead:
            lines.append(h.format_line())

        summary = f"\n✅健康{len(active)}  ⚠️衰退{len(degrading)}  🔄恢復{len(recovering)}  🔴停用{len(dead)}"
        lines.append(summary)
        return "\n".join(lines)

    # ── 工具函式 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _linear_trend(series: list[float]) -> float:
        if len(series) < 5:
            return 0.0
        n = len(series)
        x = list(range(n))
        xm = sum(x) / n
        ym = sum(series) / n
        num = sum((x[i] - xm) * (series[i] - ym) for i in range(n))
        den = sum((x[i] - xm) ** 2 for i in range(n))
        return float(num / den) if den > 0 else 0.0

    @staticmethod
    def _ic_sharpe(ic_series: list[float]) -> float:
        if len(ic_series) < 3:
            return 0.0
        arr  = np.array(ic_series, dtype=float)
        std  = float(np.std(arr))
        mean = float(np.mean(arr))
        return float(mean / std * math.sqrt(252)) if std > 1e-9 else 0.0

    @staticmethod
    def _estimate_half_life(history: list[float]) -> float:
        if len(history) < 10:
            return 9999.0
        pos_count = sum(1 for v in history if v > 0)
        if pos_count == 0:
            return 0.0
        arr = np.array(history[-30:] if len(history) >= 30 else history)
        trend = AlphaDecayEngine._linear_trend(list(arr))
        if trend >= 0 or arr.mean() <= 0:
            return 9999.0
        half_life = -arr.mean() / trend if trend != 0 else 9999.0
        return max(0.0, min(float(half_life), 9999.0))

    @staticmethod
    def _determine_status(ic_mean: float, ic_current: float, history: list[float]) -> str:
        if len(history) < 5:
            return STATUS_ACTIVE
        if ic_mean < IC_DEAD_THRESHOLD:
            return STATUS_DEAD
        if ic_mean < IC_DEGRADING_THRESHOLD:
            return STATUS_DEGRADING
        if ic_mean > IC_RECOVERY_THRESHOLD:
            return STATUS_ACTIVE
        return STATUS_DEGRADING

    @staticmethod
    def _calc_weight(status: str, ic_mean: float, prev_status: str) -> float:
        if status == STATUS_DEAD:
            return 0.0
        if status == STATUS_RECOVERING:
            return 0.20
        if status == STATUS_DEGRADING:
            return max(0.0, min(0.50, ic_mean * 3))
        # ACTIVE: 基於 IC 均值正規化
        return max(0.10, min(1.50, ic_mean * 8))

    # ── 持久化（PostgreSQL / JSON fallback）──────────────────────────────────

    async def _load_state(self, alpha_name: str) -> dict:
        if alpha_name in self._cache:
            return self._cache[alpha_name]
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import AlphaRegistry
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:
                row = (await db.execute(
                    select(AlphaRegistry).where(AlphaRegistry.alpha_name == alpha_name)
                )).scalar_one_or_none()
            if row:
                state = {
                    "status":        row.status,
                    "ic_current":    row.ic_current,
                    "ic_mean":       row.ic_30d_mean,
                    "ic_history":    json.loads(row.ic_history or "[]"),
                    "degrading_days":row.dead_days,
                    "weight":        row.weight,
                }
                self._cache[alpha_name] = state
                return state
        except Exception:
            pass
        default = {
            "status": STATUS_ACTIVE, "ic_history": [],
            "ic_current": 0.0, "ic_mean": 0.0,
            "degrading_days": 0, "weight": 1.0,
        }
        self._cache[alpha_name] = default
        return default

    async def _save_state(self, alpha_name: str, state: dict) -> None:
        self._cache[alpha_name] = state
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import AlphaRegistry
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:
                row = (await db.execute(
                    select(AlphaRegistry).where(AlphaRegistry.alpha_name == alpha_name)
                )).scalar_one_or_none()
                if row:
                    row.status      = state["status"]
                    row.ic_current  = state["ic_current"]
                    row.ic_30d_mean = state["ic_mean"]
                    row.ic_history  = json.dumps(state["ic_history"][-60:])
                    row.dead_days   = state["degrading_days"]
                    row.weight      = state["weight"]
                    row.updated_at  = datetime.utcnow()
                else:
                    from backend.models.models import AlphaRegistry as AR
                    db.add(AR(
                        alpha_name=alpha_name,
                        status=state["status"],
                        ic_current=state["ic_current"],
                        ic_30d_mean=state["ic_mean"],
                        ic_history=json.dumps(state["ic_history"][-60:]),
                        dead_days=state["degrading_days"],
                        weight=state["weight"],
                    ))
                await db.commit()
        except Exception as e:
            logger.debug("[AlphaDecay] save failed: %s", e)

    async def _log_to_db(self, alpha_name: str, health: AlphaHealth) -> None:
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import AlphaDecayLog
            async with AsyncSessionLocal() as db:
                db.add(AlphaDecayLog(
                    alpha_name=alpha_name,
                    status=health.status,
                    ic_value=health.ic_current,
                    ic_30d_mean=health.ic_30d_mean,
                    ic_trend=health.ic_trend,
                    win_rate=health.win_rate,
                    sharpe=health.sharpe,
                    half_life=health.half_life if health.half_life < 9999 else None,
                    weight=health.weight,
                ))
                await db.commit()
        except Exception as e:
            logger.debug("[AlphaDecay] log failed: %s", e)

    @staticmethod
    def _mock_health_list() -> list[AlphaHealth]:
        import random
        rng = random.Random(42)
        names = ["momentum_alpha", "value_alpha", "chip_alpha",
                 "breakout_alpha", "reversal_alpha"]
        statuses = [STATUS_ACTIVE, STATUS_DEGRADING, STATUS_ACTIVE,
                    STATUS_DEAD, STATUS_RECOVERING]
        results = []
        for n, s in zip(names, statuses):
            ic = rng.uniform(-0.05, 0.25) if s != STATUS_DEAD else rng.uniform(-0.10, 0.0)
            results.append(AlphaHealth(
                name=n, status=s,
                ic_current=ic, ic_30d_mean=ic * 0.9,
                ic_trend=rng.uniform(-0.002, 0.002),
                win_rate=rng.uniform(0.4, 0.7),
                win_rate_hist=rng.uniform(0.45, 0.6),
                sharpe=rng.uniform(-0.5, 2.0),
                half_life=rng.uniform(10, 999) if s != STATUS_DEAD else 2.0,
                degrading_days=rng.randint(0, 8),
                weight=0.0 if s == STATUS_DEAD else rng.uniform(0.5, 1.5),
            ))
        return results


_engine: AlphaDecayEngine | None = None

def get_alpha_decay_engine() -> AlphaDecayEngine:
    global _engine
    if _engine is None:
        _engine = AlphaDecayEngine()
    return _engine


if __name__ == "__main__":
    engine  = AlphaDecayEngine()
    healths = engine._mock_health_list()
    print(engine.format_weekly_report(healths))
