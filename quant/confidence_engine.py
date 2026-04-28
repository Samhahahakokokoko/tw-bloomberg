"""
confidence_engine.py — 信心指數計算引擎

信心指數（0~100）整合三個來源：
  1. 回測歷史表現（30%）：同策略、同盤態下的歷史夏普值、勝率
  2. 模型預測分數（40%）：LightGBM / RuleBasedAlpha 的預測強度
  3. 當前訊號強度（30%）：技術 + 籌碼即時訊號的複合分

設計原則：
  - 各來源皆有 fallback（缺資料時用其他來源補齊）
  - 支援時間衰退（最近 30 日回測權重較高）
  - 多策略一致性加成（三策略皆看多 → 額外 +5 信心）
  - 輸出包含信心分項，方便前端視覺化
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class ConfidenceBreakdown:
    """信心指數詳細拆解"""
    total:           float        # 0~100，最終信心指數
    backtest_score:  float        # 回測歷史分 0~100
    model_score:     float        # 模型預測分 0~100
    signal_score:    float        # 即時訊號分 0~100
    consistency_bonus: float      # 多策略一致性加分
    data_quality:    float        # 資料完整度 0~1
    level:           str          # 高/中/低

    def to_dict(self) -> dict:
        return {
            "total":            round(self.total, 1),
            "level":            self.level,
            "breakdown": {
                "backtest":    round(self.backtest_score, 1),
                "model":       round(self.model_score, 1),
                "signal":      round(self.signal_score, 1),
                "consistency": round(self.consistency_bonus, 1),
            },
            "data_quality": round(self.data_quality, 2),
        }


class ConfidenceEngine:
    """
    信心指數計算器。

    使用方式：
        ce = ConfidenceEngine()
        breakdown = ce.calc(
            signal_score=75.0,
            backtest_records=[{"sharpe": 1.2, "win_rate": 0.58}, ...],
            pred_ret=0.03,
            strategy_scores={"momentum": 70, "value": 40, "chip": 65},
        )
        print(breakdown.total)   # e.g. 72.5
    """

    # 各來源權重
    W_BACKTEST = 0.30
    W_MODEL    = 0.40
    W_SIGNAL   = 0.30

    # ── 信心等級閾值
    LEVELS = [(70, "高"), (50, "中"), (0, "低")]

    def calc(
        self,
        signal_score:       float,
        backtest_records:   Optional[list[dict]] = None,
        pred_ret:           Optional[float]      = None,
        pred_score:         Optional[float]      = None,
        strategy_scores:    Optional[dict]       = None,
    ) -> ConfidenceBreakdown:
        """
        計算信心指數。

        signal_score       : 即時技術+籌碼複合評分（0~100），來自 StrategyEngine
        backtest_records   : 歷史回測記錄 list，每筆 dict 含:
                               sharpe, win_rate, total_return, days_ago（可選）
        pred_ret           : LightGBM 預測 5 日報酬率（e.g. 0.03 = +3%）
        pred_score         : 模型直接輸出的 0~100 分（可替代 pred_ret）
        strategy_scores    : {"momentum": 70, "value": 40, "chip": 65}
                             用於計算一致性加分
        """
        data_available = 0

        # ── 1. 回測歷史分 ────────────────────────────────────────────────
        if backtest_records:
            bt_score = self._calc_backtest_score(backtest_records)
            data_available += 1
        else:
            bt_score = signal_score   # fallback：用訊號強度估計
            logger.debug("[Confidence] 無回測記錄，以訊號分替代回測分")

        # ── 2. 模型預測分 ────────────────────────────────────────────────
        if pred_score is not None:
            model_sc = float(pred_score)
            data_available += 1
        elif pred_ret is not None:
            # pred_ret: -10% → 0, 0% → 50, +5% → 100（線性插值）
            model_sc = min(100, max(0, (float(pred_ret) / 0.10 + 0.5) * 100))
            data_available += 1
        else:
            model_sc = signal_score   # fallback
            logger.debug("[Confidence] 無模型預測，以訊號分替代")

        # ── 3. 即時訊號分 ────────────────────────────────────────────────
        signal_sc = float(signal_score)
        data_available += 1

        # ── 4. 多策略一致性加分（最多 +8）───────────────────────────────
        consistency_bonus = self._calc_consistency(strategy_scores)

        # ── 加權合計
        total = (
            bt_score  * self.W_BACKTEST +
            model_sc  * self.W_MODEL    +
            signal_sc * self.W_SIGNAL
        ) + consistency_bonus

        total = min(100, max(0, total))
        data_quality = data_available / 3.0

        # ── 信心等級
        level = "低"
        for threshold, lv in self.LEVELS:
            if total >= threshold:
                level = lv
                break

        return ConfidenceBreakdown(
            total=round(total, 1),
            backtest_score=round(bt_score, 1),
            model_score=round(model_sc, 1),
            signal_score=round(signal_sc, 1),
            consistency_bonus=round(consistency_bonus, 1),
            data_quality=round(data_quality, 2),
            level=level,
        )

    # ── 私有方法 ─────────────────────────────────────────────────────────

    def _calc_backtest_score(self, records: list[dict]) -> float:
        """
        回測歷史分：
          - 夏普值（>1.5→100, 1.0→75, 0.5→50, 0.0→25, <0→0）加權平均
          - 勝率（>60%→100, 50%→50, <40%→0）
          - 時間衰退：days_ago=0→w=1.0, days_ago=30→w=0.5

        綜合 = sharpe_score * 0.6 + win_rate_score * 0.4
        """
        if not records:
            return 50.0

        sharpe_scores, win_scores, weights = [], [], []
        for r in records:
            sharpe = float(r.get("sharpe", r.get("sharpe_ratio", 0)))
            win    = float(r.get("win_rate", 0.5))
            days   = float(r.get("days_ago", 0))

            # 時間衰退：每 15 天衰減一半
            w = 2 ** (-days / 15.0)

            ss = min(100, max(0, (sharpe + 0.5) / 2.0 * 100))
            ws = min(100, max(0, (win - 0.30) / 0.40 * 100))

            sharpe_scores.append(ss)
            win_scores.append(ws)
            weights.append(w)

        total_w = sum(weights) or 1
        avg_sharpe = sum(s * w for s, w in zip(sharpe_scores, weights)) / total_w
        avg_win    = sum(s * w for s, w in zip(win_scores,    weights)) / total_w

        return avg_sharpe * 0.60 + avg_win * 0.40

    def _calc_consistency(self, strategy_scores: Optional[dict]) -> float:
        """
        多策略一致性加分：
          三策略皆 > 60 → +8
          兩策略 > 60   → +4
          一策略 > 60   → +0
          全部 < 40     → -5（分歧警示）
        """
        if not strategy_scores:
            return 0.0
        high = sum(1 for v in strategy_scores.values() if float(v) >= 60)
        low  = sum(1 for v in strategy_scores.values() if float(v) < 40)
        if high == len(strategy_scores):
            return 8.0
        elif high >= 2:
            return 4.0
        elif low == len(strategy_scores):
            return -5.0
        return 0.0

    # ── 批次計算 ─────────────────────────────────────────────────────────

    def batch_calc(self, items: list[dict]) -> list[ConfidenceBreakdown]:
        """
        批次計算多股信心。
        items 每筆為傳入 calc() 的關鍵字參數 dict。
        """
        return [self.calc(**item) for item in items]

    # ── 融合 StrategySignal 的快捷方式 ───────────────────────────────────

    def from_strategy_signal(
        self,
        signal,                              # StrategySignal instance
        backtest_records: Optional[list[dict]] = None,
        pred_ret:         Optional[float]      = None,
    ) -> ConfidenceBreakdown:
        """直接傳入 StrategySignal，自動拆解各分項"""
        strategy_scores = {
            "momentum": signal.momentum_score,
            "value":    signal.value_score,
            "chip":     signal.chip_score,
        }
        return self.calc(
            signal_score=signal.composite_score,
            backtest_records=backtest_records,
            pred_ret=pred_ret,
            strategy_scores=strategy_scores,
        )


# ── 獨立測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ce = ConfidenceEngine()

    print("=== 完整資料信心計算（2330）===")
    bd = ce.calc(
        signal_score=74.5,
        backtest_records=[
            {"sharpe": 1.2, "win_rate": 0.58, "days_ago": 0},
            {"sharpe": 0.9, "win_rate": 0.52, "days_ago": 15},
            {"sharpe": 1.4, "win_rate": 0.62, "days_ago": 30},
        ],
        pred_ret=0.04,
        strategy_scores={"momentum": 82, "value": 38, "chip": 72},
    )
    print(f"  信心: {bd.total}  等級: {bd.level}")
    print(f"  回測分: {bd.backtest_score}  模型分: {bd.model_score}  訊號分: {bd.signal_score}")
    print(f"  一致性加分: {bd.consistency_bonus}  資料完整度: {bd.data_quality}")

    print("\n=== 無回測資料 fallback（0056）===")
    bd2 = ce.calc(
        signal_score=48.5,
        pred_ret=0.01,
        strategy_scores={"momentum": 20, "value": 72, "chip": 30},
    )
    print(f"  信心: {bd2.total}  等級: {bd2.level}")

    print("\n=== 三策略完全一致（2454）===")
    bd3 = ce.calc(
        signal_score=85.0,
        backtest_records=[{"sharpe": 1.5, "win_rate": 0.65, "days_ago": 3}],
        pred_ret=0.06,
        strategy_scores={"momentum": 90, "value": 65, "chip": 80},
    )
    print(f"  信心: {bd3.total}  等級: {bd3.level}（+{bd3.consistency_bonus} 一致性加分）")

    print("\n=== StrategySignal 快捷方式 ===")
    from quant.strategy_engine import StrategyEngine, MOCK_STOCKS
    engine = StrategyEngine()
    sig = engine.evaluate(MOCK_STOCKS[0], regime="bull")
    bd4 = ce.from_strategy_signal(sig, pred_ret=0.04)
    print(f"  {sig.stock_id} 信心={bd4.total}  等級={bd4.level}")
    print(f"  {bd4.to_dict()}")
