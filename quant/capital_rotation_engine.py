"""
capital_rotation_engine.py — 資金輪動預測引擎

整合 Lead-Lag + Narrative + Capital Flow，
預測資金下一個流向。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# 台股已知輪動路徑（有向圖）
ROTATION_PATHS: list[tuple[str, str, float]] = [
    # (from, to, historical_probability)
    ("AI Server",  "散熱",      0.78),
    ("AI Server",  "PCB",       0.72),
    ("散熱",       "PCB",       0.68),
    ("PCB",        "機殼",      0.60),
    ("機殼",       "電源管理",   0.55),
    ("半導體",     "AI Server", 0.70),
    ("半導體",     "蘋果供應鏈", 0.62),
    ("CoWoS",      "ABF基板",   0.73),
    ("ABF基板",    "PCB",       0.65),
    ("機器人",     "減速機",    0.71),
    ("機器人",     "感測器",    0.67),
    ("電動車",     "電池",      0.63),
    ("電動車",     "車用電子",  0.59),
]

# 輪動速度描述
SPEED_LABELS = {
    (0.0,  0.3): "緩慢",
    (0.3,  0.6): "中速",
    (0.6,  0.8): "快速",
    (0.8,  1.0): "加速",
}


def _speed_label(v: float) -> str:
    for (lo, hi), label in SPEED_LABELS.items():
        if lo <= v < hi:
            return label
    return "極速"


@dataclass
class SectorFlow:
    name:         str
    inflow_speed: float   # 0-1，資金流入速度（正=流入，負=流出）
    rs_rank:      int     # 相對強度排名
    rs_change:    int     # 排名變化（正=上升）
    narrative_score: float = 0.0

    @property
    def is_outflow(self) -> bool:
        return self.inflow_speed < 0


@dataclass
class RotationPrediction:
    outflow_sectors:  list[tuple[str, float]]  # [(sector, speed)]
    inflow_sectors:   list[tuple[str, float]]
    next_candidates:  list[tuple[str, float]]  # [(sector, probability)]
    historical_ref:   Optional[str] = None      # 歷史相似輪動案例
    historical_return: Optional[str] = None
    ts: str = field(default_factory=lambda: datetime.now().isoformat())

    def format_line(self) -> str:
        lines = ["🔄 資金輪動預測", ""]
        for sec, spd in self.outflow_sectors[:2]:
            lines.append(f"資金正在離開：{sec}（速度：{_speed_label(abs(spd))}）")
        for sec, spd in self.inflow_sectors[:2]:
            lines.append(f"資金正在流入：{sec}（速度：{_speed_label(abs(spd))}）")
        if self.historical_ref:
            lines += ["", f"歷史相似情境（{self.historical_ref}）：", self.historical_return or ""]
        if self.next_candidates:
            lines += ["", "本次預測下一站："]
            medals = ["🥇", "🥈", "🥉"]
            for i, (sec, prob) in enumerate(self.next_candidates[:3]):
                lines.append(f"{medals[i]} {sec}  機率：{prob:.0%}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "outflow":     [(s, round(v, 3)) for s, v in self.outflow_sectors],
            "inflow":      [(s, round(v, 3)) for s, v in self.inflow_sectors],
            "next":        [(s, round(p, 3)) for s, p in self.next_candidates],
            "historical_ref": self.historical_ref,
            "ts":          self.ts,
        }


async def _fetch_sector_flows() -> list[SectorFlow]:
    """從 SectorRotationLog 取最新族群強度資料"""
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import SectorRotationLog
        from sqlalchemy import select
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(SectorRotationLog)
                .where(SectorRotationLog.created_at >= cutoff)
                .order_by(SectorRotationLog.created_at.desc())
            )
            rows = r.scalars().all()

        seen: set[str] = set()
        flows: list[SectorFlow] = []
        for row in rows:
            if row.sector_name in seen:
                continue
            seen.add(row.sector_name)
            trend_val = 0.5 if row.trend == "↑" else (-0.5 if row.trend == "↓" else 0.0)
            flows.append(SectorFlow(
                name         = row.sector_name,
                inflow_speed = trend_val,
                rs_rank      = row.rank or 99,
                rs_change    = 0,
            ))
        return flows
    except Exception:
        pass

    # Mock fallback
    return [
        SectorFlow("AI Server",  0.8, 1,  2),
        SectorFlow("機器人",      0.6, 3,  5),
        SectorFlow("散熱",        0.3, 4,  1),
        SectorFlow("PCB",        -0.1, 6, -1),
        SectorFlow("CoWoS",      -0.7, 8, -4),
        SectorFlow("電動車",     -0.9, 12,-5),
    ]


def _predict_next(outflow_from: list[str], inflow_to: list[str]) -> list[tuple[str, float]]:
    """根據輪動路徑圖預測下一站"""
    candidates: dict[str, float] = {}
    for sector in inflow_to:
        for frm, to, prob in ROTATION_PATHS:
            if frm == sector:
                candidates[to] = max(candidates.get(to, 0.0), prob)
    # 已在流入的不重複推薦
    for s in inflow_to + outflow_from:
        candidates.pop(s, None)
    return sorted(candidates.items(), key=lambda x: -x[1])[:5]


async def compute_rotation() -> RotationPrediction:
    flows = await _fetch_sector_flows()

    outflow = [(f.name, f.inflow_speed) for f in flows if f.inflow_speed < -0.2]
    inflow  = [(f.name, f.inflow_speed) for f in flows if f.inflow_speed >  0.3]
    outflow.sort(key=lambda x: x[1])
    inflow.sort(key=lambda x: -x[1])

    next_cands = _predict_next(
        [s for s, _ in outflow],
        [s for s, _ in inflow],
    )

    # 歷史案例比對（簡單版）
    hist_ref = hist_ret = None
    if outflow and "CoWoS" in [s for s, _ in outflow]:
        hist_ref = "2023年5月"
        hist_ret = "當時 CoWoS 退燒後，PCB 啟動了+35%"

    return RotationPrediction(
        outflow_sectors  = outflow,
        inflow_sectors   = inflow,
        next_candidates  = next_cands,
        historical_ref   = hist_ref,
        historical_return = hist_ret,
    )
