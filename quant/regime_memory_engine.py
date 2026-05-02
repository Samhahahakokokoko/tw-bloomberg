"""
regime_memory_engine.py — 市場狀態記憶與策略自適應

記錄每種 regime 下哪些策略有效，自動學習調整權重。

預設策略權重表：
    BULL:     momentum 0.45 / breakout 0.35 / value 0.20
    BEAR:     defensive 0.50 / value 0.30 / cash 0.20
    SIDEWAYS: chip 0.40 / mean_reversion 0.35 / value 0.25
    PANIC:    mean_reversion 0.60 / defensive 0.40
    EUPHORIA: reduce_all 0.80 / cash 0.20

自動學習：
    BULL 時 momentum 勝率 > 65% → weight += 0.05
    PANIC 時 momentum 勝率 < 40% → weight -= 0.10
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── 預設策略權重 ─────────────────────────────────────────────────────────────
DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    "BULL": {
        "momentum":       0.45,
        "breakout":       0.35,
        "value":          0.20,
    },
    "BEAR": {
        "defensive":      0.50,
        "value":          0.30,
        "cash":           0.20,
    },
    "SIDEWAYS": {
        "chip":           0.40,
        "mean_reversion": 0.35,
        "value":          0.25,
    },
    "PANIC": {
        "mean_reversion": 0.60,
        "defensive":      0.40,
    },
    "EUPHORIA": {
        "reduce_all":     0.80,
        "cash":           0.20,
    },
    "RECOVERY": {
        "momentum":       0.35,
        "value":          0.35,
        "breakout":       0.30,
    },
    "UNKNOWN": {
        "momentum":       0.30,
        "value":          0.30,
        "chip":           0.25,
        "defensive":      0.15,
    },
}

# 自動學習的更新步長
LEARN_STEP_UP   = 0.05
LEARN_STEP_DOWN = 0.10
WIN_RATE_HIGH   = 0.65
WIN_RATE_LOW    = 0.40
MIN_WEIGHT      = 0.05
MAX_WEIGHT      = 0.70


@dataclass
class RegimeMemory:
    regime:       str
    strategy:     str
    win_rate:     float       # 近期勝率
    n_trades:     int         # 交易次數
    avg_return:   float       # 平均報酬率
    sharpe:       float       # Sharpe
    weight:       float       # 當前權重
    last_updated: str


@dataclass
class RegimeWeightTable:
    regime: str
    weights: dict[str, float]   # {strategy: weight}
    last_learned: str = ""

    def format_line(self) -> str:
        lines = [f"📊 {self.regime} 策略權重"]
        for strat, w in sorted(self.weights.items(), key=lambda x: -x[1]):
            bar = "█" * int(w * 20)
            lines.append(f"  {strat:<18s} {w:.0%} {bar}")
        return "\n".join(lines)

    def top_strategy(self) -> str:
        if not self.weights:
            return "momentum"
        return max(self.weights, key=self.weights.get)


class RegimeMemoryEngine:
    """
    市場狀態記憶引擎。

    使用方式：
        engine = RegimeMemoryEngine()
        await engine.record_trade(regime="BULL", strategy="momentum",
                                  pnl_pct=0.05, won=True)
        weights = await engine.get_weights("BULL")
        print(weights)
    """

    def __init__(self):
        self._weights: dict[str, dict[str, float]] = {
            r: dict(w) for r, w in DEFAULT_WEIGHTS.items()
        }
        self._loaded = False

    async def load(self) -> None:
        """從 DB 載入已學習的權重"""
        if self._loaded:
            return
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import RegimeMemoryModel
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(select(RegimeMemoryModel))).scalars().all()
            for row in rows:
                regime   = row.regime.upper()
                strategy = row.strategy
                if regime not in self._weights:
                    self._weights[regime] = {}
                self._weights[regime][strategy] = row.weight
            self._loaded = True
        except Exception as e:
            logger.debug("[RegimeMemory] load failed: %s", e)
            self._loaded = True

    async def get_weights(self, regime: str) -> RegimeWeightTable:
        """取得指定 regime 的策略權重表（標準化後）"""
        await self.load()
        regime = regime.upper()
        raw = self._weights.get(regime, self._weights.get("UNKNOWN", {}))
        total = sum(raw.values()) or 1.0
        normalized = {k: round(v / total, 4) for k, v in raw.items()}
        return RegimeWeightTable(regime=regime, weights=normalized)

    async def record_trade(
        self,
        regime:   str,
        strategy: str,
        pnl_pct:  float,
        won:      bool,
    ) -> None:
        """記錄一筆交易，觸發自動學習"""
        await self.load()
        regime = regime.upper()
        if regime not in self._weights:
            self._weights[regime] = dict(DEFAULT_WEIGHTS.get(regime, {}))

        old_weight = self._weights[regime].get(strategy, 0.25)
        new_weight = old_weight

        # 更新規則
        if won and old_weight < MAX_WEIGHT:
            new_weight = min(old_weight + LEARN_STEP_UP, MAX_WEIGHT)
        elif not won and old_weight > MIN_WEIGHT:
            new_weight = max(old_weight - LEARN_STEP_DOWN, MIN_WEIGHT)

        self._weights[regime][strategy] = round(new_weight, 4)
        await self._save_record(regime, strategy, pnl_pct, won, new_weight)

    async def learn_from_batch(
        self,
        regime:   str,
        strategy: str,
        win_rate: float,
    ) -> None:
        """批量學習：依勝率更新權重"""
        await self.load()
        regime = regime.upper()
        if regime not in self._weights:
            self._weights[regime] = dict(DEFAULT_WEIGHTS.get(regime, {}))

        old_w = self._weights[regime].get(strategy, 0.25)

        if win_rate > WIN_RATE_HIGH:
            new_w = min(old_w + LEARN_STEP_UP, MAX_WEIGHT)
            logger.info("[RegimeMemory] %s/%s win=%.0f%% → weight %.2f→%.2f",
                        regime, strategy, win_rate * 100, old_w, new_w)
        elif win_rate < WIN_RATE_LOW:
            new_w = max(old_w - LEARN_STEP_DOWN, MIN_WEIGHT)
            logger.info("[RegimeMemory] %s/%s win=%.0f%% → weight %.2f→%.2f",
                        regime, strategy, win_rate * 100, old_w, new_w)
        else:
            new_w = old_w

        self._weights[regime][strategy] = round(new_w, 4)
        await self._save_weight(regime, strategy, new_w)

    def get_weights_sync(self, regime: str) -> dict[str, float]:
        """同步取得權重（不呼叫 DB）"""
        regime = regime.upper()
        raw    = self._weights.get(regime, DEFAULT_WEIGHTS.get(regime, DEFAULT_WEIGHTS["UNKNOWN"]))
        total  = sum(raw.values()) or 1.0
        return {k: round(v / total, 4) for k, v in raw.items()}

    def top_strategy_for(self, regime: str) -> str:
        """快速取得 regime 下最高權重的策略"""
        weights = self.get_weights_sync(regime)
        return max(weights, key=weights.get) if weights else "momentum"

    def format_all_tables(self) -> str:
        """格式化所有 regime 的策略權重表"""
        lines = ["📊 Regime 策略權重記憶庫", "─" * 22]
        for regime in ["BULL", "BEAR", "SIDEWAYS", "PANIC", "EUPHORIA", "RECOVERY"]:
            weights = self.get_weights_sync(regime)
            top     = max(weights, key=weights.get) if weights else "—"
            second  = [k for k in sorted(weights, key=weights.get, reverse=True) if k != top]
            second_str = f"/{second[0]}" if second else ""
            lines.append(f"{regime:<12s} → {top}{second_str}")
        return "\n".join(lines)

    # ── 持久化 ────────────────────────────────────────────────────────────────

    async def _save_record(
        self,
        regime: str, strategy: str,
        pnl_pct: float, won: bool, weight: float,
    ) -> None:
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import RegimeMemoryModel
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:
                existing = (await db.execute(
                    select(RegimeMemoryModel).where(
                        RegimeMemoryModel.regime   == regime,
                        RegimeMemoryModel.strategy == strategy,
                    )
                )).scalar_one_or_none()

                if existing:
                    existing.n_trades  += 1
                    existing.avg_return = (
                        existing.avg_return * (existing.n_trades - 1) + pnl_pct
                    ) / existing.n_trades
                    wins               = round(existing.win_rate * (existing.n_trades - 1))
                    existing.win_rate  = (wins + int(won)) / existing.n_trades
                    existing.weight    = weight
                    existing.updated_at= datetime.utcnow()
                else:
                    db.add(RegimeMemoryModel(
                        regime=regime, strategy=strategy,
                        win_rate=float(won), n_trades=1,
                        avg_return=pnl_pct, sharpe=0.0,
                        weight=weight,
                    ))
                await db.commit()
        except Exception as e:
            logger.debug("[RegimeMemory] save_record failed: %s", e)

    async def _save_weight(self, regime: str, strategy: str, weight: float) -> None:
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import RegimeMemoryModel
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:
                existing = (await db.execute(
                    select(RegimeMemoryModel).where(
                        RegimeMemoryModel.regime   == regime,
                        RegimeMemoryModel.strategy == strategy,
                    )
                )).scalar_one_or_none()
                if existing:
                    existing.weight     = weight
                    existing.updated_at = datetime.utcnow()
                else:
                    db.add(RegimeMemoryModel(
                        regime=regime, strategy=strategy,
                        win_rate=0.5, n_trades=0,
                        avg_return=0.0, sharpe=0.0,
                        weight=weight,
                    ))
                await db.commit()
        except Exception as e:
            logger.debug("[RegimeMemory] save_weight failed: %s", e)


_engine: RegimeMemoryEngine | None = None

def get_regime_memory_engine() -> RegimeMemoryEngine:
    global _engine
    if _engine is None:
        _engine = RegimeMemoryEngine()
    return _engine


if __name__ == "__main__":
    engine = RegimeMemoryEngine()
    for regime in ["BULL", "BEAR", "SIDEWAYS", "PANIC", "EUPHORIA"]:
        table = RegimeWeightTable(
            regime=regime,
            weights=engine.get_weights_sync(regime),
        )
        print(table.format_line())
        print()
