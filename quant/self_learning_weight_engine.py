"""
self_learning_weight_engine.py — 自動調整因子權重

每週計算各因子近期 IC 和預測準確率，
自動調升有效因子、降權失效因子、暫停連續失效因子。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ── 調整規則 ──────────────────────────────────────────────────────────────────
BOOST_THRESHOLD  = 0.10    # 準確率高於均值 +10% → weight × 1.2
DECAY_THRESHOLD  = 0.10    # 準確率低於均值 -10% → weight × 0.8
DISABLE_THRESHOLD = 0.40   # 連續 2 週 < 40% → weight = 0
MIN_WEIGHT       = 0.05
MAX_WEIGHT       = 3.0

# ── 因子初始權重 ─────────────────────────────────────────────────────────────
DEFAULT_WEIGHTS: dict[str, float] = {
    "momentum_20d":     0.28,
    "chip_flow":        0.25,
    "breakout_vol":     0.20,
    "eps_momentum":     0.12,
    "sector_rotation":  0.15,
    "foreign_net":      0.08,
    "value_mean_rev":   0.06,
    "earnings_surp":    0.10,
    "analyst_consensus": 0.18,
    "rs_rank":          0.14,
}


@dataclass
class FactorPerformance:
    name:          str
    current_weight: float
    ic_4w:         float    # 近4週 IC
    accuracy_4w:   float    # 近4週預測準確率
    ic_hist_mean:  float    # 歷史 IC 均值
    acc_hist_mean: float    # 歷史準確率均值
    weeks_below_threshold: int = 0   # 連續低於門檻週數
    new_weight:    float = 0.0
    adjustment:    str = "HOLD"      # BOOST / DECAY / DISABLE / HOLD

    def compute_adjustment(self) -> "FactorPerformance":
        acc_delta = self.accuracy_4w - self.acc_hist_mean
        if self.weeks_below_threshold >= 2 or self.accuracy_4w < DISABLE_THRESHOLD:
            self.new_weight = 0.0
            self.adjustment = "DISABLE"
        elif acc_delta > BOOST_THRESHOLD:
            self.new_weight = min(MAX_WEIGHT, self.current_weight * 1.2)
            self.adjustment = "BOOST"
        elif acc_delta < -DECAY_THRESHOLD:
            self.new_weight = max(MIN_WEIGHT, self.current_weight * 0.8)
            self.adjustment = "DECAY"
        else:
            self.new_weight = self.current_weight
            self.adjustment = "HOLD"
        return self

    @property
    def change_pct(self) -> float:
        if self.current_weight <= 0:
            return 0.0
        return (self.new_weight - self.current_weight) / self.current_weight

    def format_row(self) -> str:
        icons = {"BOOST": "📈", "DECAY": "📉", "DISABLE": "⏸️", "HOLD": "➡️"}
        icon = icons.get(self.adjustment, "➡️")
        chg  = f"{self.change_pct:+.0%}" if self.adjustment != "HOLD" else ""
        return f"{icon} {self.name:<20} {self.current_weight:.2f}→{self.new_weight:.2f} {chg}  IC={self.ic_4w:.3f}"


@dataclass
class WeightUpdateReport:
    factor_performances: list[FactorPerformance]
    top3_effective:      list[str]
    disabled_factors:    list[str]
    boosted_factors:     list[str]
    decayed_factors:     list[str]
    ts: str = field(default_factory=lambda: datetime.now().isoformat())

    def format_line(self) -> str:
        lines = [
            "⚙️ 系統自動調權週報",
            f"更新時間：{self.ts[:16]}",
            "",
            "本週調整：",
        ]
        for fp in self.factor_performances:
            if fp.adjustment != "HOLD":
                lines.append(f"  {fp.format_row()}")

        if not any(fp.adjustment != "HOLD" for fp in self.factor_performances):
            lines.append("  （本週無顯著調整）")

        if self.top3_effective:
            lines += ["", "當前有效因子 Top3："]
            medals = ["1.", "2.", "3."]
            for i, name in enumerate(self.top3_effective[:3]):
                fp = next((f for f in self.factor_performances if f.name == name), None)
                ic = f"（IC={fp.ic_4w:.2f}）" if fp else ""
                lines.append(f"  {medals[i]} {name}{ic}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "factors":    [{"name": f.name, "old": f.current_weight,
                            "new": f.new_weight, "adj": f.adjustment}
                           for f in self.factor_performances],
            "top3":       self.top3_effective,
            "disabled":   self.disabled_factors,
            "ts":         self.ts,
        }

    def new_weights_dict(self) -> dict[str, float]:
        return {f.name: f.new_weight for f in self.factor_performances}


async def _load_current_weights() -> dict[str, float]:
    """從 DB 載入目前權重，若無則用預設值"""
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import FactorWeightLog
        from sqlalchemy import select
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(FactorWeightLog)
                .order_by(FactorWeightLog.created_at.desc())
                .limit(len(DEFAULT_WEIGHTS))
            )
            rows = r.scalars().all()
        if rows:
            return {row.factor_name: row.weight for row in rows}
    except Exception:
        pass
    return dict(DEFAULT_WEIGHTS)


async def _fetch_factor_ic(factor_name: str, weeks: int = 4) -> tuple[float, float]:
    """
    從 AlphaDecayLog 取近 N 週的 IC 和歷史均值。
    回傳 (ic_4w, ic_hist_mean)
    """
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import AlphaDecayLog
        from sqlalchemy import select, func
        cutoff = (datetime.now() - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(func.avg(AlphaDecayLog.ic_value))
                .where(AlphaDecayLog.alpha_name == factor_name)
                .where(AlphaDecayLog.created_at >= cutoff)
            )
            ic_4w = float(r.scalar() or 0)
            r2 = await db.execute(
                select(func.avg(AlphaDecayLog.ic_value))
                .where(AlphaDecayLog.alpha_name == factor_name)
            )
            ic_hist = float(r2.scalar() or 0)
        return ic_4w, ic_hist
    except Exception:
        return 0.0, 0.0


async def compute_weight_update() -> WeightUpdateReport:
    """主函數：計算所有因子的近期表現並產生調整方案"""
    current_weights = await _load_current_weights()

    performances: list[FactorPerformance] = []
    for factor, base_w in current_weights.items():
        ic_4w, ic_hist = await _fetch_factor_ic(factor)
        acc_4w   = max(0.0, min(1.0, ic_4w + 0.5))  # IC → 準確率近似
        acc_hist = max(0.0, min(1.0, ic_hist + 0.5))

        fp = FactorPerformance(
            name           = factor,
            current_weight = base_w,
            ic_4w          = ic_4w,
            accuracy_4w    = acc_4w,
            ic_hist_mean   = ic_hist,
            acc_hist_mean  = acc_hist,
        ).compute_adjustment()
        performances.append(fp)

    performances.sort(key=lambda f: -f.ic_4w)

    top3     = [f.name for f in performances[:3]]
    disabled = [f.name for f in performances if f.adjustment == "DISABLE"]
    boosted  = [f.name for f in performances if f.adjustment == "BOOST"]
    decayed  = [f.name for f in performances if f.adjustment == "DECAY"]

    report = WeightUpdateReport(
        factor_performances = performances,
        top3_effective      = top3,
        disabled_factors    = disabled,
        boosted_factors     = boosted,
        decayed_factors     = decayed,
    )

    # 儲存新權重到 DB
    await _save_weights(report.new_weights_dict())
    logger.info("[SelfLearn] updated %d factors: +%d -%d pause=%d",
                len(performances), len(boosted), len(decayed), len(disabled))
    return report


async def _save_weights(weights: dict[str, float]):
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import FactorWeightLog
        async with AsyncSessionLocal() as db:
            for name, w in weights.items():
                db.add(FactorWeightLog(factor_name=name, weight=w))
            await db.commit()
    except Exception as e:
        logger.warning("[SelfLearn] save weights failed: %s", e)


async def get_current_weights() -> dict[str, float]:
    return await _load_current_weights()
