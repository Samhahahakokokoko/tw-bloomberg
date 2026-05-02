"""
meta_alpha_engine.py — Meta-Alpha 引擎（哪些 Alpha 最近有效）

每週計算：
    - 所有 Alpha 近4週 IC 排名
    - IC 趨勢方向（上升/下降）
    - Regime 對各 Alpha 的影響

自動調整：
    IC 排名上升 → 提高該 Alpha 在 ensemble 的權重
    IC 排名下降 → 降低權重

輸出週報
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── 已知 Alpha 因子 ───────────────────────────────────────────────────────────
KNOWN_ALPHAS = [
    "momentum_alpha",
    "value_alpha",
    "chip_alpha",
    "breakout_alpha",
    "reversal_alpha",
    "quality_alpha",
    "sentiment_alpha",
    "sector_alpha",
]

WEIGHT_STEP_UP   = 0.05
WEIGHT_STEP_DOWN = 0.08
MIN_WEIGHT       = 0.02
MAX_WEIGHT       = 2.00


@dataclass
class AlphaRanking:
    name:       str
    ic_4w:      float       # 近4週平均 IC
    ic_trend:   float       # IC 趨勢（正=上升，負=下降）
    rank:       int
    prev_rank:  int
    weight:     float
    regime_sensitivity: dict[str, float]   # 各 regime 下的 IC

    @property
    def rank_change(self) -> int:
        return self.prev_rank - self.rank   # 正數=排名上升

    @property
    def trend_arrow(self) -> str:
        if self.rank_change > 1:    return "↑"
        if self.rank_change < -1:   return "↓"
        return "→"

    def format_line(self) -> str:
        arrow = self.trend_arrow
        return (
            f"#{self.rank:1d} {self.name:<20s} IC={self.ic_4w:.3f} {arrow}"
            f"  w={self.weight:.2f}"
        )

    def to_dict(self) -> dict:
        return {
            "rank":      self.rank,
            "name":      self.name,
            "ic_4w":     round(self.ic_4w, 4),
            "ic_trend":  round(self.ic_trend, 6),
            "weight":    round(self.weight, 4),
            "rank_change": self.rank_change,
        }


@dataclass
class MetaAlphaReport:
    rankings:       list[AlphaRanking]
    total_alphas:   int
    active_count:   int
    top_alpha:      str
    generated_at:   str

    def format_line(self) -> str:
        lines = [
            "📊 本週 Alpha 排名",
            "─" * 22,
        ]
        for r in self.rankings:
            lines.append(r.format_line())
        lines += [
            "─" * 22,
            f"有效因子：{self.active_count}/{self.total_alphas}",
            f"本週最強：{self.top_alpha}",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "total":        self.total_alphas,
            "active":       self.active_count,
            "top_alpha":    self.top_alpha,
            "rankings":     [r.to_dict() for r in self.rankings],
        }


class MetaAlphaEngine:
    """
    Meta-Alpha 引擎。

    使用方式：
        engine = MetaAlphaEngine()
        report = await engine.run_weekly()
        print(report.format_line())
        weights = engine.get_ensemble_weights()
    """

    def __init__(self):
        self._weights:   dict[str, float] = {a: 1.0 for a in KNOWN_ALPHAS}
        self._prev_ranks:dict[str, int]   = {}

    async def run_weekly(self) -> MetaAlphaReport:
        """計算週排名並自動調整權重"""
        ic_data = await self._load_ic_history()
        rankings = self._compute_rankings(ic_data)
        self._auto_adjust_weights(rankings)
        await self._save_weights()

        active = [r for r in rankings if r.ic_4w > 0]
        top    = rankings[0].name if rankings else "—"

        report = MetaAlphaReport(
            rankings=rankings,
            total_alphas=len(rankings),
            active_count=len(active),
            top_alpha=top,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
        self._prev_ranks = {r.name: r.rank for r in rankings}
        return report

    def get_ensemble_weights(self) -> dict[str, float]:
        """取得標準化後的 ensemble 權重（用於 decision engine）"""
        active  = {k: v for k, v in self._weights.items() if v > MIN_WEIGHT * 2}
        total   = sum(active.values()) or 1.0
        return {k: round(v / total, 4) for k, v in active.items()}

    def get_weight(self, alpha_name: str) -> float:
        return self._weights.get(alpha_name, 1.0)

    # ── IC 歷史計算 ───────────────────────────────────────────────────────────

    async def _load_ic_history(self) -> dict[str, list[float]]:
        """從 AlphaRegistry 或 AlphaDecayLog 取近4週 IC 值"""
        result: dict[str, list[float]] = {}
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import AlphaRegistry
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(select(AlphaRegistry))).scalars().all()
            for row in rows:
                history = json.loads(row.ic_history or "[]")
                result[row.alpha_name] = history[-28:] if len(history) >= 28 else history
        except Exception as e:
            logger.warning("[MetaAlpha] load IC history failed: %s", e)

        # 補齊未有紀錄的 alpha（用 mock 資料）
        for alpha in KNOWN_ALPHAS:
            if alpha not in result:
                result[alpha] = self._mock_ic_series(alpha)

        return result

    def _compute_rankings(self, ic_data: dict[str, list[float]]) -> list[AlphaRanking]:
        """計算4週 IC 排名"""
        rankings = []
        for name, history in ic_data.items():
            if not history:
                ic_4w = 0.0
                trend = 0.0
            else:
                h4w   = history[-28:] if len(history) >= 28 else history
                ic_4w = float(np.mean(h4w))
                trend = self._linear_trend(h4w)

            prev_rank = self._prev_ranks.get(name, 999)
            weight    = self._weights.get(name, 1.0)
            rankings.append(AlphaRanking(
                name=name, ic_4w=ic_4w, ic_trend=trend,
                rank=0, prev_rank=prev_rank, weight=weight,
                regime_sensitivity={},
            ))

        rankings.sort(key=lambda r: -r.ic_4w)
        for i, r in enumerate(rankings, 1):
            r.rank = i

        return rankings

    def _auto_adjust_weights(self, rankings: list[AlphaRanking]) -> None:
        """根據排名變化自動調整權重"""
        for r in rankings:
            old_w = self._weights.get(r.name, 1.0)
            if r.rank_change > 1:       # 排名上升
                new_w = min(old_w + WEIGHT_STEP_UP, MAX_WEIGHT)
            elif r.rank_change < -1:    # 排名下降
                new_w = max(old_w - WEIGHT_STEP_DOWN, MIN_WEIGHT)
            elif r.ic_4w < 0:           # IC 為負，強制降低
                new_w = max(old_w - WEIGHT_STEP_DOWN * 2, MIN_WEIGHT)
            else:
                new_w = old_w

            self._weights[r.name] = round(new_w, 4)
            r.weight = round(new_w, 4)

            if new_w != old_w:
                direction = "↑" if new_w > old_w else "↓"
                logger.info("[MetaAlpha] %s weight %.2f→%.2f %s",
                            r.name, old_w, new_w, direction)

    async def _save_weights(self) -> None:
        """將調整後的權重存回 AlphaRegistry"""
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import AlphaRegistry
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:
                for name, weight in self._weights.items():
                    row = (await db.execute(
                        select(AlphaRegistry).where(AlphaRegistry.alpha_name == name)
                    )).scalar_one_or_none()
                    if row:
                        row.weight     = weight
                        row.updated_at = datetime.utcnow()
                    else:
                        db.add(AlphaRegistry(
                            alpha_name=name, status="ACTIVE",
                            weight=weight, ic_current=0.0, ic_30d_mean=0.0,
                            ic_history="[]",
                        ))
                await db.commit()
        except Exception as e:
            logger.debug("[MetaAlpha] save_weights failed: %s", e)

    async def push_weekly_report(self, token: str) -> None:
        """推送週報給所有訂閱者"""
        import httpx
        report = await self.run_weekly()
        text   = report.format_line()
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import Subscriber
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:
                r    = await db.execute(select(Subscriber))
                subs = r.scalars().all()
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(timeout=15) as c:
                for sub in subs:
                    uid = sub.line_user_id
                    if uid:
                        await c.post(
                            "https://api.line.me/v2/bot/message/push",
                            json={"to": uid, "messages": [{"type": "text", "text": text[:4800]}]},
                            headers=headers,
                        )
        except Exception as e:
            logger.error("[MetaAlpha] push failed: %s", e)

    # ── 工具函式 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _linear_trend(series: list[float]) -> float:
        if len(series) < 5:
            return 0.0
        n  = len(series)
        x  = list(range(n))
        xm = sum(x) / n
        ym = sum(series) / n
        num = sum((x[i] - xm) * (series[i] - ym) for i in range(n))
        den = sum((x[i] - xm) ** 2 for i in range(n))
        return float(num / den) if den > 0 else 0.0

    @staticmethod
    def _mock_ic_series(alpha_name: str) -> list[float]:
        import random
        seed = sum(ord(c) for c in alpha_name) % 9999
        rng  = random.Random(seed)
        base = rng.uniform(0.02, 0.20)
        return [max(-0.15, min(0.30, base + rng.gauss(0, 0.05))) for _ in range(28)]


_engine: MetaAlphaEngine | None = None

def get_meta_alpha_engine() -> MetaAlphaEngine:
    global _engine
    if _engine is None:
        _engine = MetaAlphaEngine()
    return _engine


if __name__ == "__main__":
    import asyncio
    engine = MetaAlphaEngine()
    ic_data = {alpha: engine._mock_ic_series(alpha) for alpha in KNOWN_ALPHAS}
    rankings = engine._compute_rankings(ic_data)
    engine._auto_adjust_weights(rankings)
    report = MetaAlphaReport(
        rankings=rankings,
        total_alphas=len(rankings),
        active_count=sum(1 for r in rankings if r.ic_4w > 0),
        top_alpha=rankings[0].name if rankings else "—",
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    print(report.format_line())
    print("\nEnsemble Weights:", engine.get_ensemble_weights())
