"""
dynamic_weight_engine.py — 動態因子加權引擎

三種加權模式：
  EQUAL    — 所有因子等權（基準）
  IC       — 依 FactorICEngine 的 ICIR 歸一化加權
  ADAPTIVE — 根據 MarketRegime 自動切換：
               bull     → IC weighting（動能因子偏重）
               bear     → 防禦性等權（降低全倉）
               sideways → IC weighting + mean_reversion bias
               volatile → equal（降低所有信號強度）

可每日更新：update_weights(feat_df, regime) → {factor: weight}
可直接 plug 到 EnsembleEngine：ensemble.weights = engine.weights
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── 常數 ─────────────────────────────────────────────────────────────────────

DEFAULT_EQUAL_WEIGHT = 1.0

# 各盤態下的策略群組調整係數（ADAPTIVE 模式）
_REGIME_FACTOR_BIAS: dict[str, dict[str, float]] = {
    "bull": {
        "momentum":    1.5,    # 多頭放大動能因子
        "breakout":    1.3,
        "value":       0.7,    # 壓縮防禦型
        "defensive":   0.5,
        "chip":        1.2,
    },
    "bear": {
        "momentum":    0.5,
        "breakout":    0.4,
        "value":       1.5,    # 空頭防禦優先
        "defensive":   2.0,
        "chip":        0.8,
    },
    "sideways": {
        "momentum":    0.8,
        "breakout":    0.8,
        "value":       1.2,
        "defensive":   1.0,
        "chip":        1.3,    # 盤整偏籌碼
        "mean_reversion": 1.5,
    },
    "volatile": {
        "momentum":    0.6,
        "breakout":    0.5,
        "value":       1.3,
        "defensive":   1.8,
        "chip":        0.7,
    },
    "unknown": {},  # 不調整
}

# 各技術因子屬於哪個策略群組
_FACTOR_GROUP: dict[str, str] = {
    "ret_1d":    "momentum",
    "ret_5d":    "momentum",
    "ret_10d":   "momentum",
    "ret_20d":   "momentum",
    "macd_hist": "momentum",
    "excess_ret":"momentum",
    "boll_b":    "breakout",
    "boll_width":"breakout",
    "k":         "breakout",
    "d":         "breakout",
    "vol_ratio": "breakout",
    "rsi14":     "mean_reversion",
    "obv_slope5":"chip",
    "ma5":       "momentum",
    "ma20":      "momentum",
    "ma60":      "value",
    "atr14":     "defensive",
    "body_ratio":"momentum",
    "hl_ratio":  "breakout",
}


# ── 列舉 ─────────────────────────────────────────────────────────────────────

class WeightMode(str, Enum):
    EQUAL    = "equal"
    IC       = "ic"
    ADAPTIVE = "adaptive"


@dataclass
class WeightResult:
    """加權計算輸出"""
    weights:   dict[str, float]    # {factor: weight}，已歸一化
    mode:      WeightMode
    regime:    str
    n_factors: int
    top5:      list[dict]          # 前5大權重因子

    def to_dict(self) -> dict:
        return {
            "mode":      self.mode.value,
            "regime":    self.regime,
            "n_factors": self.n_factors,
            "weights":   self.weights,
            "top5":      self.top5,
        }


# ── 動態加權引擎 ──────────────────────────────────────────────────────────────

class DynamicWeightEngine:
    """
    動態因子加權引擎。

    使用方式（每日更新）：
        from quant.factor_ic_engine import FactorICEngine
        ic_engine = FactorICEngine(feat_df)
        dw_engine = DynamicWeightEngine(mode=WeightMode.ADAPTIVE)

        # 每日更新
        result = dw_engine.update(feat_df, regime="bull")
        weights = result.weights   # {factor: weight}

    Plug 到 EnsembleEngine：
        from quant.ensemble_engine import get_ensemble_engine
        ensemble = get_ensemble_engine()
        ensemble.set_factor_weights(weights)
    """

    def __init__(
        self,
        mode: WeightMode = WeightMode.ADAPTIVE,
        factors: Optional[list[str]] = None,
        forward_days: int = 5,
    ):
        self.mode         = mode
        self.forward_days = forward_days
        self.factors      = factors  # None = use DEFAULT_FACTORS
        self._weights:    dict[str, float] = {}
        self._last_regime: str = "unknown"

    @property
    def weights(self) -> dict[str, float]:
        return dict(self._weights)

    # ── 主更新函式 ───────────────────────────────────────────────────────────

    def update(
        self,
        feat_df,                      # pd.DataFrame from FeatureEngine
        regime: str = "unknown",
        ic_weights: Optional[dict[str, float]] = None,
    ) -> WeightResult:
        """
        根據最新特徵資料和盤態更新因子權重。

        feat_df   : FeatureEngine.compute_all() 輸出
        regime    : 當前盤態字串（bull/bear/sideways/volatile/unknown）
        ic_weights: 預計算好的 IC 權重字典（可選，若不傳則實時計算）

        回傳 WeightResult，並更新 self._weights。
        """
        from quant.factor_ic_engine import FactorICEngine, DEFAULT_FACTORS

        factors = self.factors or DEFAULT_FACTORS
        self._last_regime = regime

        # ── EQUAL 模式 ────────────────────────────────────────────────────
        if self.mode == WeightMode.EQUAL:
            raw = {f: DEFAULT_EQUAL_WEIGHT for f in factors}
            weights = _normalize(raw)
            self._weights = weights
            return _make_result(weights, WeightMode.EQUAL, regime)

        # ── IC 模式：計算或使用傳入的 IC 權重 ───────────────────────────
        if ic_weights is None:
            try:
                ic_engine = FactorICEngine(feat_df, forward_days=self.forward_days)
                ic_weights = ic_engine.get_factor_weights(factors)
            except Exception as e:
                logger.warning("[DW] IC 計算失敗，降級為 equal: %s", e)
                raw = {f: DEFAULT_EQUAL_WEIGHT for f in factors}
                self._weights = _normalize(raw)
                return _make_result(self._weights, WeightMode.EQUAL, regime)

        if self.mode == WeightMode.IC:
            self._weights = ic_weights
            return _make_result(ic_weights, WeightMode.IC, regime)

        # ── ADAPTIVE 模式：IC 權重 × 盤態偏差係數 ──────────────────────
        bias = _REGIME_FACTOR_BIAS.get(regime, {})
        raw: dict[str, float] = {}
        for f, w in ic_weights.items():
            group = _FACTOR_GROUP.get(f, "")
            b     = bias.get(group, 1.0)
            raw[f] = w * b

        weights = _normalize(raw)
        self._weights = weights

        logger.info("[DW] regime=%s mode=adaptive n=%d bias_groups=%s",
                    regime, len(weights), list(set(g for g in bias.keys() if bias[g] != 1.0)))

        return _make_result(weights, WeightMode.ADAPTIVE, regime)

    # ── Regime 切換建議 ──────────────────────────────────────────────────────

    def get_regime_strategy_hint(self, regime: str) -> dict:
        """根據盤態回傳策略配置建議（供 AlphaPortfolioEngine 使用）"""
        hints = {
            "bull": {
                "active_strategies": ["momentum", "breakout"],
                "weight_multiplier": 1.0,
                "position_scale":    1.0,
                "note":              "多頭：動能 + 突破策略雙倍權重",
            },
            "bear": {
                "active_strategies": ["defensive", "value"],
                "weight_multiplier": 0.7,
                "position_scale":    0.5,
                "note":              "空頭：防禦 + 價值，整體倉位降至 50%",
            },
            "sideways": {
                "active_strategies": ["chip", "mean_reversion"],
                "weight_multiplier": 0.85,
                "position_scale":    0.75,
                "note":              "盤整：籌碼 + 均值回歸",
            },
            "volatile": {
                "active_strategies": ["defensive"],
                "weight_multiplier": 0.6,
                "position_scale":    0.4,
                "note":              "高波動：防禦優先，大幅降倉",
            },
        }
        return hints.get(regime, {
            "active_strategies": ["value", "defensive"],
            "weight_multiplier": 0.7,
            "position_scale":    0.5,
            "note":              "未知盤態：保守操作",
        })

    # ── 序列化 ───────────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "mode":    self.mode.value,
            "regime":  self._last_regime,
            "weights": self._weights,
        }


# ── 工具函式 ─────────────────────────────────────────────────────────────────

def _normalize(d: dict[str, float]) -> dict[str, float]:
    """歸一化：總和 = 1.0，僅對 > 0 的項目"""
    total = sum(v for v in d.values() if v > 0)
    if total <= 0:
        n = max(len(d), 1)
        return {k: round(1.0 / n, 6) for k in d}
    return {k: round(v / total, 6) if v > 0 else 0.0 for k, v in d.items()}


def _make_result(weights: dict[str, float], mode: WeightMode, regime: str) -> WeightResult:
    top5 = sorted(
        [{"factor": k, "weight": v} for k, v in weights.items() if v > 0],
        key=lambda x: -x["weight"],
    )[:5]
    return WeightResult(
        weights=weights,
        mode=mode,
        regime=regime,
        n_factors=sum(1 for v in weights.values() if v > 0),
        top5=top5,
    )


# ── 全域單例 ─────────────────────────────────────────────────────────────────

_global_dw: Optional[DynamicWeightEngine] = None

def get_dynamic_weight_engine(mode: WeightMode = WeightMode.ADAPTIVE) -> DynamicWeightEngine:
    global _global_dw
    if _global_dw is None:
        _global_dw = DynamicWeightEngine(mode=mode)
    return _global_dw
