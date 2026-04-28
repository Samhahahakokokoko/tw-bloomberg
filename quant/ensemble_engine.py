"""
ensemble_engine.py — 多訊號集成引擎

整合來源：
  1. RuleBasedAlpha    — 技術面規則訊號（0~100 分）
  2. AlphaModel        — LightGBM 預測報酬（BUY/HOLD/SELL）
  3. StrategyEngine    — 複合策略（動能/價值/籌碼）
  4. FactorICEngine    — 因子 IC 加權的線性合成訊號

加權模式（WeightMode）：
  EQUAL      — 等權平均（基準）
  IC_WEIGHTED — 依各來源歷史 ICIR 動態加權
  FEEDBACK   — 依 FeedbackEngine 的策略權重調整
  ADAPTIVE   — IC_WEIGHTED + FEEDBACK 混合（預設）

集成流程：
  1. 各來源輸出標準化為 score ∈ [0, 100]
  2. 依 WeightMode 計算加權平均 ensemble_score
  3. 多策略一致性加成：若 3+ 來源同向 → +5 分
  4. 最終閾值判斷：≥65 → BUY, ≤35 → SELL, 中間 → HOLD
  5. 輸出 EnsembleResult（含分項明細）

使用方式：
    engine = EnsembleEngine(mode=WeightMode.ADAPTIVE)
    result = engine.evaluate(
        alpha_out=alpha_output,        # AlphaOutput（可選）
        strategy_signal=strat_signal,  # StrategySignal（可選）
        factor_scores={"rsi14": 72},   # 原始因子分數（可選）
        factor_weights=ew_list,        # EnsembleWeight list（可選）
        regime="bull",
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── 閾值 & 常數 ───────────────────────────────────────────────────────────────

BUY_THRESHOLD   = 65.0   # ensemble_score ≥ 此值 → BUY
SELL_THRESHOLD  = 35.0   # ensemble_score ≤ 此值 → SELL
CONSISTENCY_BONUS = 5.0  # 3+ 來源同向時的額外加分

# 各來源預設基礎權重（EQUAL 模式下等權，ADAPTIVE 模式下依 IC 動態調整）
BASE_WEIGHTS: dict[str, float] = {
    "rule_alpha":  1.0,   # RuleBasedAlpha
    "lgb_alpha":   1.0,   # AlphaModel (LightGBM)
    "strategy":    1.0,   # StrategyEngine composite
    "factor_ic":   1.0,   # FactorICEngine 線性合成
}

# 各盤態下的來源權重調整
REGIME_SOURCE_BIAS: dict[str, dict[str, float]] = {
    "bull":     {"rule_alpha": 1.0, "lgb_alpha": 1.3, "strategy": 1.2, "factor_ic": 1.0},
    "bear":     {"rule_alpha": 0.8, "lgb_alpha": 1.0, "strategy": 1.3, "factor_ic": 1.2},
    "sideways": {"rule_alpha": 1.1, "lgb_alpha": 1.0, "strategy": 1.0, "factor_ic": 1.3},
    "volatile": {"rule_alpha": 0.7, "lgb_alpha": 0.8, "strategy": 1.4, "factor_ic": 1.1},
    "unknown":  {"rule_alpha": 1.0, "lgb_alpha": 1.0, "strategy": 1.0, "factor_ic": 1.0},
}


# ── 列舉 ─────────────────────────────────────────────────────────────────────

class WeightMode(str, Enum):
    EQUAL      = "equal"
    IC_WEIGHTED = "ic_weighted"
    FEEDBACK   = "feedback"
    ADAPTIVE   = "adaptive"


class EnsembleSignal(str, Enum):
    BUY  = "buy"
    HOLD = "hold"
    SELL = "sell"


# ── 資料類別 ─────────────────────────────────────────────────────────────────

@dataclass
class SourceScore:
    """單一來源的標準化分數"""
    name:    str
    score:   float    # 0~100，越高越看多
    weight:  float    # 最終使用的權重（歸一化後）
    raw:     float    # 原始輸入值（用於 debug）
    present: bool     # 是否有有效輸入（False = fallback 填補）


@dataclass
class EnsembleResult:
    """集成引擎輸出結果"""
    signal:           EnsembleSignal
    ensemble_score:   float              # 0~100
    sources:          list[SourceScore]
    consistency_bonus: float
    weight_mode:      WeightMode
    regime:           str
    reasons:          list[str]          # 主要支撐/反對理由

    def to_dict(self) -> dict:
        return {
            "signal":         self.signal.value,
            "ensemble_score": round(self.ensemble_score, 2),
            "weight_mode":    self.weight_mode.value,
            "regime":         self.regime,
            "consistency_bonus": round(self.consistency_bonus, 2),
            "sources": [
                {
                    "name":    s.name,
                    "score":   round(s.score, 2),
                    "weight":  round(s.weight, 4),
                    "present": s.present,
                }
                for s in self.sources
            ],
            "reasons": self.reasons,
        }


# ── 轉換工具 ─────────────────────────────────────────────────────────────────

def _alpha_output_to_score(alpha_out) -> float:
    """AlphaOutput → 0~100 分"""
    signal_val = alpha_out.signal.value if hasattr(alpha_out.signal, "value") else str(alpha_out.signal)
    base = float(getattr(alpha_out, "score", 50.0))

    # pred_ret 存在時，以預測報酬微調
    pred_ret = getattr(alpha_out, "pred_ret", None)
    if pred_ret is not None:
        # +5% → 加 10 分；-5% → 減 10 分（線性）
        base += float(pred_ret) * 200
    return float(np.clip(base, 0.0, 100.0))


def _lgb_signal_to_score(alpha_out) -> float:
    """LightGBM AlphaOutput（lgb_signal 欄位或 signal）→ 0~100 分"""
    pred_ret = getattr(alpha_out, "pred_ret", None)
    if pred_ret is not None:
        # 線性：+5% → 100, -5% → 0
        return float(np.clip((float(pred_ret) + 0.05) / 0.10 * 100, 0.0, 100.0))
    # fallback: 用 signal
    signal_val = alpha_out.signal.value if hasattr(alpha_out.signal, "value") else str(alpha_out.signal)
    mapping = {"buy": 75.0, "hold": 50.0, "sell": 25.0}
    return mapping.get(signal_val, 50.0)


def _strategy_signal_to_score(strategy_signal) -> float:
    """StrategySignal.confidence → 0~100 分（confidence 本身即 0~100）"""
    return float(np.clip(getattr(strategy_signal, "confidence", 50.0), 0.0, 100.0))


def _factor_scores_to_composite(
    factor_scores: dict[str, float],
    factor_weights,   # list[EnsembleWeight]
) -> float:
    """
    factor_scores: {factor_name: raw_value, ...}
    factor_weights: list[EnsembleWeight]（from FactorICEngine.to_ensemble_weights）

    各因子值先 rank 標準化到 0~100，再以 ICIR 加權合成。
    """
    if not factor_weights or not factor_scores:
        return 50.0

    weighted_sum = 0.0
    total_w = 0.0
    for ew in factor_weights:
        val = factor_scores.get(ew.factor)
        if val is None or np.isnan(float(val)):
            continue
        # 簡單 percentile 正規化：假設 factor 分布 → 映射到 0~100
        # 這裡用 sigmoid 轉換，以 0 為中心
        norm_val = 1 / (1 + np.exp(-float(val) * 0.1)) * 100
        # 負向因子反轉
        if ew.direction < 0:
            norm_val = 100.0 - norm_val
        weighted_sum += norm_val * ew.weight
        total_w += ew.weight

    if total_w < 1e-8:
        return 50.0
    return float(np.clip(weighted_sum / total_w * total_w / max(total_w, 1), 0, 100))


# ── 集成引擎主體 ──────────────────────────────────────────────────────────────

class EnsembleEngine:
    """
    多訊號集成引擎。

    使用方式（最精簡）：
        engine = EnsembleEngine()
        result = engine.evaluate(alpha_out=alpha_out)

    使用方式（完整輸入）：
        result = engine.evaluate(
            alpha_out=rule_alpha_out,
            lgb_out=lgb_alpha_out,
            strategy_signal=strat_signal,
            factor_scores={"rsi14": 62.0, "macd_hist": 0.05},
            factor_weights=ew_list,         # from FactorICEngine
            feedback_weights=fb_weights,    # from FeedbackEngine
            regime="bull",
        )
    """

    def __init__(self, mode: WeightMode = WeightMode.ADAPTIVE):
        self.mode = mode

    # ── 主評估函式 ────────────────────────────────────────────────────────

    def evaluate(
        self,
        alpha_out=None,          # RuleBasedAlpha → AlphaOutput
        lgb_out=None,            # AlphaModel → AlphaOutput（pred_ret）
        strategy_signal=None,    # StrategyEngine → StrategySignal
        factor_scores: Optional[dict[str, float]] = None,
        factor_weights=None,     # list[EnsembleWeight]
        feedback_weights: Optional[dict[str, float]] = None,
        regime: str = "unknown",
    ) -> EnsembleResult:
        """評估並集成所有訊號，回傳 EnsembleResult"""

        # ── 1. 各來源轉換為標準化分數 0~100
        raw_scores: dict[str, tuple[float, bool]] = {}   # name → (score, present)

        if alpha_out is not None:
            raw_scores["rule_alpha"] = (_alpha_output_to_score(alpha_out), True)
        else:
            raw_scores["rule_alpha"] = (50.0, False)

        if lgb_out is not None:
            raw_scores["lgb_alpha"] = (_lgb_signal_to_score(lgb_out), True)
        elif alpha_out is not None:
            # 如果只有 rule alpha，用同一個物件嘗試取 pred_ret
            pred_ret = getattr(alpha_out, "pred_ret", None)
            if pred_ret is not None:
                raw_scores["lgb_alpha"] = (_lgb_signal_to_score(alpha_out), True)
            else:
                raw_scores["lgb_alpha"] = (50.0, False)
        else:
            raw_scores["lgb_alpha"] = (50.0, False)

        if strategy_signal is not None:
            raw_scores["strategy"] = (_strategy_signal_to_score(strategy_signal), True)
        else:
            raw_scores["strategy"] = (50.0, False)

        if factor_scores and factor_weights:
            composite = _factor_scores_to_composite(factor_scores, factor_weights)
            raw_scores["factor_ic"] = (composite, True)
        else:
            raw_scores["factor_ic"] = (50.0, False)

        # ── 2. 計算各來源實際權重
        weights = self._compute_weights(raw_scores, regime, feedback_weights)

        # ── 3. 加權平均
        source_list: list[SourceScore] = []
        ensemble_score = 0.0
        total_w = sum(weights.values())

        for name, (score, present) in raw_scores.items():
            w = weights.get(name, 0.0)
            norm_w = w / total_w if total_w > 1e-8 else 1.0 / len(raw_scores)
            ensemble_score += score * norm_w
            source_list.append(SourceScore(name=name, score=score, weight=norm_w, raw=score, present=present))

        # ── 4. 一致性加成
        present_scores = [s.score for s in source_list if s.present]
        consistency_bonus = 0.0
        if len(present_scores) >= 3:
            bullish = sum(1 for s in present_scores if s >= BUY_THRESHOLD)
            bearish = sum(1 for s in present_scores if s <= SELL_THRESHOLD)
            if bullish >= 3:
                consistency_bonus = CONSISTENCY_BONUS
                ensemble_score = min(100.0, ensemble_score + consistency_bonus)
            elif bearish >= 3:
                consistency_bonus = -CONSISTENCY_BONUS
                ensemble_score = max(0.0, ensemble_score + consistency_bonus)

        ensemble_score = float(np.clip(ensemble_score, 0.0, 100.0))

        # ── 5. 訊號判斷
        if ensemble_score >= BUY_THRESHOLD:
            signal = EnsembleSignal.BUY
        elif ensemble_score <= SELL_THRESHOLD:
            signal = EnsembleSignal.SELL
        else:
            signal = EnsembleSignal.HOLD

        # ── 6. 理由彙整
        reasons = self._build_reasons(source_list, ensemble_score, consistency_bonus, regime)

        return EnsembleResult(
            signal=signal,
            ensemble_score=round(ensemble_score, 2),
            sources=source_list,
            consistency_bonus=consistency_bonus,
            weight_mode=self.mode,
            regime=regime,
            reasons=reasons,
        )

    # ── 權重計算 ──────────────────────────────────────────────────────────

    def _compute_weights(
        self,
        raw_scores: dict[str, tuple[float, bool]],
        regime: str,
        feedback_weights: Optional[dict[str, float]],
    ) -> dict[str, float]:
        """依 WeightMode 計算各來源的原始（未歸一化）權重"""

        regime_bias = REGIME_SOURCE_BIAS.get(regime, REGIME_SOURCE_BIAS["unknown"])

        if self.mode == WeightMode.EQUAL:
            return {name: 1.0 for name in raw_scores}

        if self.mode == WeightMode.FEEDBACK and feedback_weights:
            return {
                "rule_alpha": feedback_weights.get("trend", 1.0),
                "lgb_alpha":  feedback_weights.get("momentum", 1.0),
                "strategy":   feedback_weights.get("chip", 1.0),
                "factor_ic":  1.0,
            }

        if self.mode in (WeightMode.IC_WEIGHTED, WeightMode.ADAPTIVE):
            # 有效來源權重略高；缺失（present=False）的來源降低一半
            base = {name: (1.0 if present else 0.5) for name, (_, present) in raw_scores.items()}

            # 盤態偏差
            for name in base:
                base[name] *= regime_bias.get(name, 1.0)

            # ADAPTIVE 模式再疊加 feedback 權重
            if self.mode == WeightMode.ADAPTIVE and feedback_weights:
                fb_map = {
                    "rule_alpha": feedback_weights.get("trend", 1.0),
                    "lgb_alpha":  feedback_weights.get("momentum", 1.0),
                    "strategy":   feedback_weights.get("chip", 1.0),
                    "factor_ic":  1.0,
                }
                for name in base:
                    base[name] *= fb_map.get(name, 1.0)

            return base

        # fallback: equal
        return {name: 1.0 for name in raw_scores}

    # ── 理由彙整 ─────────────────────────────────────────────────────────

    def _build_reasons(
        self,
        sources: list[SourceScore],
        score: float,
        bonus: float,
        regime: str,
    ) -> list[str]:
        reasons: list[str] = []

        # 最強支撐來源
        present = [s for s in sources if s.present]
        if present:
            top = max(present, key=lambda s: abs(s.score - 50))
            direction = "看多" if top.score >= 50 else "看空"
            src_label = {
                "rule_alpha": "技術規則",
                "lgb_alpha":  "ML模型",
                "strategy":   "複合策略",
                "factor_ic":  "因子IC",
            }.get(top.name, top.name)
            reasons.append(f"{src_label}{direction}（分={top.score:.0f}）")

        # 一致性
        if abs(bonus) > 0:
            reasons.append(f"多策略{'一致看多' if bonus > 0 else '一致看空'}（+{abs(bonus):.0f}加成）")

        # 盤態說明
        regime_desc = {
            "bull": "多頭盤態加成動能權重",
            "bear": "空頭盤態加成防禦權重",
            "sideways": "震盪盤態加成均值回歸",
            "volatile": "波動盤態降低動能權重",
        }
        if regime in regime_desc:
            reasons.append(regime_desc[regime])

        return reasons[:3]

    # ── 批次評估 ──────────────────────────────────────────────────────────

    def batch_evaluate(
        self,
        inputs: list[dict],
        regime: str = "unknown",
    ) -> list[EnsembleResult]:
        """
        批次評估多組訊號。
        inputs: [{"alpha_out": ..., "strategy_signal": ..., ...}, ...]
        回傳按 ensemble_score 排序的結果。
        """
        results = []
        for inp in inputs:
            try:
                r = self.evaluate(
                    alpha_out=inp.get("alpha_out"),
                    lgb_out=inp.get("lgb_out"),
                    strategy_signal=inp.get("strategy_signal"),
                    factor_scores=inp.get("factor_scores"),
                    factor_weights=inp.get("factor_weights"),
                    feedback_weights=inp.get("feedback_weights"),
                    regime=inp.get("regime", regime),
                )
                results.append(r)
            except Exception as e:
                logger.warning(f"[Ensemble] 評估失敗: {e}")
        return sorted(results, key=lambda r: -r.ensemble_score)


# ── 全域單例 ─────────────────────────────────────────────────────────────────

_global_ensemble: Optional[EnsembleEngine] = None

def get_ensemble_engine(mode: WeightMode = WeightMode.ADAPTIVE) -> EnsembleEngine:
    global _global_ensemble
    if _global_ensemble is None:
        _global_ensemble = EnsembleEngine(mode=mode)
    return _global_ensemble


# ── 獨立測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))

    from quant.alpha_model import RuleBasedAlpha, Signal, AlphaOutput
    from quant.strategy_engine import StrategyEngine, MOCK_STOCKS

    alpha = RuleBasedAlpha()
    strat = StrategyEngine()
    engine = EnsembleEngine(mode=WeightMode.ADAPTIVE)

    print("=== Ensemble 評估（台積電）===")
    stock_data = MOCK_STOCKS[0]

    # 模擬 AlphaOutput（無 LightGBM）
    import pandas as pd
    mock_row = pd.Series({
        "ma5": 850, "ma20": 820, "ma60": 790, "ma200": 750,
        "rsi14": 58, "macd_hist": 0.8, "macd_golden": 1,
        "vol_ratio": 1.6, "obv_slope5": 100, "boll_b": 0.6,
        "close": 850,
    })
    alpha_out = alpha.evaluate(mock_row, chip_days=5)
    strat_sig = strat.evaluate(stock_data, regime="bull")

    result = engine.evaluate(
        alpha_out=alpha_out,
        strategy_signal=strat_sig,
        factor_scores={"rsi14": 58.0, "macd_hist": 0.8, "vol_ratio": 1.6},
        regime="bull",
    )

    print(f"  訊號: {result.signal.value}")
    print(f"  集成分: {result.ensemble_score:.1f}")
    print(f"  一致性加成: {result.consistency_bonus:+.1f}")
    print(f"  加權模式: {result.weight_mode.value}")
    print("  各來源分數:")
    for s in result.sources:
        mark = "✓" if s.present else "○"
        print(f"    {mark} {s.name:12s} score={s.score:5.1f}  weight={s.weight:.3f}")
    print(f"  理由: {result.reasons}")

    print("\n=== 不同盤態下的集成結果 ===")
    for regime in ["bull", "bear", "sideways", "volatile"]:
        r = engine.evaluate(alpha_out=alpha_out, strategy_signal=strat_sig, regime=regime)
        print(f"  {regime:8s}  集成分={r.ensemble_score:5.1f}  訊號={r.signal.value}")

    print("\n=== 批次評估 ===")
    inputs = [
        {"alpha_out": alpha.evaluate(mock_row, chip_days=d), "regime": "bull"}
        for d in [7, 3, 0, -3]
    ]
    for r in engine.batch_evaluate(inputs, regime="bull"):
        print(f"  {r.signal.value:5s}  score={r.ensemble_score:.1f}")
